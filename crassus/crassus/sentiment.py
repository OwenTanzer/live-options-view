"""Reddit sentiment observation.

Mirrors the ingestion-and-scoring shape of
[nama1arpit/reddit-streaming-pipeline](https://github.com/nama1arpit/reddit-streaming-pipeline):
recent posts are pulled from a fixed set of subreddits, VADER scores each one,
and the aggregate (there: a per-minute Cassandra rollup fed to Grafana; here:
an in-memory mean) is the signal a strategy reads.

Deliberately not a port of that project's Kafka -> Spark -> Cassandra ->
Grafana stack. Crassus is one lightweight Python process per account reading
one number every few minutes; standing up a Kubernetes cluster to answer "is
r/wallstreetbets bullish on QQQ right now" would be infrastructure in search
of a justification. `aggregate()` below is the part of that project actually
worth reusing -- the scoring method -- reimplemented as a plain function so it
can be unit-tested without a broker or a database.

Ingestion has two layers, tried in order per subreddit:

1. `_fetch_listing_json()` -- a plain scrape of Reddit's public per-subreddit
   JSON listing (`https://www.reddit.com/r/<name>/new.json`). Unauthenticated
   and public -- the same page a logged-out browser gets, just requested as
   `.json` -- so there is no app to register at reddit.com/prefs/apps and no
   client id/secret to provision or rotate. Cheap and fast when it works.

2. `_fetch_listing_browser()` -- a real headless Chromium tab (Playwright)
   that loads the rendered `/r/<name>/new/` page and reads posts out of the
   `<shreddit-post>` DOM elements Reddit's frontend renders them into. This
   exists because Reddit can (and, observed in practice, does) front the
   `.json` endpoint with a same-origin JS proof-of-work challenge ("Please
   wait for verification") that a plain HTTP client cannot solve -- a real
   browser engine executes that challenge script itself, transparently, the
   same way it would for a human visitor. This is strictly slower (a browser
   launch and a real page load per subreddit) and is only invoked when layer
   1 fails, per subreddit, so a working `.json` endpoint on a given deployment
   never pays the browser-launch cost.

Either way the tradeoff is what an unauthenticated, non-API surface implies:
no SLA, and Reddit is free to reshape or block either path without notice.
Both ingestion functions return the same shape (`{"title", "selftext"}` per
post) so `RedditSentimentReader._collect_texts` doesn't need to know which
layer actually served a given subreddit.

A 429 from the JSON layer is handled as neither of the above: it means
Reddit itself is asking for backoff, so `RedditSentimentReader._fetch_listing`
starts a shared cooldown (`_rate_limited_until`, from the response's
`Retry-After` when present) across every subreddit and every future cycle
until it expires, rather than falling through to the browser -- which hits
the same backend and would only compound the limit, not work around it the
way it correctly does for a JS-challenge page. See `RedditRateLimited`.

The browser fallback also recovers from its own failure mode: a headless
Chromium process that crashes or disconnects mid-run (not merely a page
that fails to load) would otherwise stay cached as a permanently-dead
context, failing identically every subsequent cycle until the whole bot
process restarted. `RedditSentimentReader._get_browser_context` detects
this via `browser.is_connected()` and allows exactly one relaunch-and-retry.

vaderSentiment and playwright are imported lazily (inside functions, not at
module scope) so importing this module -- and therefore `crassus.strategies`
-- never fails for an account not configured to use the sentiment strategy,
and the JSON-only path never requires playwright to be installed at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import clock
from .config import REDDIT_USER_AGENT

DEFAULT_SUBREDDITS: tuple[str, ...] = ("wallstreetbets", "stocks", "options", "investing")
DEFAULT_KEYWORDS: tuple[str, ...] = ("qqq", "nasdaq-100", "nasdaq 100", "nasdaq100")
DEFAULT_POST_LIMIT = 50

# Reddit's listing endpoints return at most 100 items per request; anything
# beyond that needs the `after` pagination cursor.
_MAX_PAGE_SIZE = 100
_LISTING_URL = "https://www.reddit.com/r/{subreddit}/new.json"
_LISTING_PAGE_URL = "https://www.reddit.com/r/{subreddit}/new/"
_FALLBACK_USER_AGENT = "crassus-reddit-sentiment-scraper/1.0"

# Safety valves for the browser fallback: how long to wait for the first
# posts to render, how many scroll-and-wait rounds to run trying to
# accumulate `post_limit` posts, and how many consecutive rounds with no new
# posts before giving up on a subreddit that's thinner than the requested
# limit (e.g. a quiet subreddit with only 20 posts in its `new` feed).
_BROWSER_NAV_TIMEOUT_S = 20.0
_BROWSER_MAX_SCROLL_ROUNDS = 8
_BROWSER_STALL_LIMIT = 3

# Fallback cooldown when Reddit 429s without a Retry-After header (or with
# one that doesn't parse as a number) -- conservative rather than immediate.
_DEFAULT_RATE_LIMIT_COOLDOWN_S = 60.0

# A default (CDP-automated) headless Chromium is trivially distinguishable
# from a real browser -- `navigator.webdriver` is `true` and the
# `AutomationControlled` blink feature changes other fingerprintable
# behavior -- and Reddit's JS challenge stalls on exactly that signal rather
# than solving and redirecting through, observed as an indefinite `<html><body
# class="theme-beta">` shell with no `<shreddit-post>` ever appearing. Faking
# a browser identity via the User-Agent header alone (as
# `_default_session_factory` does for the plain HTTP path) does nothing here
# -- the challenge JS inspects the live `navigator` object, not request
# headers. Masking `navigator.webdriver` and a realistic UA/viewport/locale
# is the minimum that was verified to get a real `<shreddit-post>` render.
_BROWSER_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"


class RedditFetchError(RuntimeError):
    """A subreddit listing could not be fetched by any available method."""


class RedditRateLimited(RedditFetchError):
    """Reddit returned 429 for the JSON listing.

    Deliberately a distinct type from a generic `RedditFetchError`: a 429
    means Reddit itself is asking for backoff, which the browser fallback
    cannot honor by retrying through a different transport -- it hits the
    same backend and would only compound the rate limit rather than working
    around it the way the fallback correctly does for a JS-challenge page.
    Carries `retry_after_s` (from the `Retry-After` header when present and
    numeric, else `_DEFAULT_RATE_LIMIT_COOLDOWN_S`) so the reader can start a
    shared cooldown instead of retrying immediately.
    """

    def __init__(self, message: str, *, retry_after_s: float):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class SentimentSnapshot:
    """One aggregation pass over whatever matched at fetch time."""

    fetched_at: str
    symbol: str
    subreddits: tuple[str, ...]
    sample_size: int
    mean_compound: float | None
    bullish_count: int
    bearish_count: int
    neutral_count: int
    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def bullish_share(self) -> float | None:
        return (self.bullish_count / self.sample_size) if self.sample_size else None

    @property
    def bearish_share(self) -> float | None:
        return (self.bearish_count / self.sample_size) if self.sample_size else None


def _default_session_factory() -> Any:
    import requests  # noqa: PLC0415 -- optional dependency, only needed if this runs

    session = requests.Session()
    # A distinctive, honest User-Agent is a courtesy to Reddit, not an auth
    # requirement -- unlike the old PRAW path, an unset REDDIT_USER_AGENT
    # still works, it just identifies itself generically.
    session.headers["User-Agent"] = REDDIT_USER_AGENT or _FALLBACK_USER_AGENT
    return session


def _default_analyzer_factory() -> Any:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # noqa: PLC0415

    return SentimentIntensityAnalyzer()


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in keywords)


def _fetch_listing_json(
    session: Any,
    subreddit: str,
    *,
    limit: int,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Pull up to `limit` posts from a subreddit's public `new.json` listing.

    Paginates with `after` since Reddit caps a single request at 100 items.
    Raises RedditFetchError on any HTTP error, timeout, or malformed
    response -- including the HTML JS-challenge page Reddit sometimes
    substitutes for the JSON body, which fails the `response.json()` parse
    below -- rather than letting the underlying exception escape. The
    caller (`_fetch_listing`) treats this uniformly as "layer 1 failed, try
    the browser" regardless of which of these it was.
    """
    posts: list[dict[str, Any]] = []
    after: str | None = None
    url = _LISTING_URL.format(subreddit=subreddit)

    while len(posts) < limit:
        page_size = min(_MAX_PAGE_SIZE, limit - len(posts))
        params: dict[str, Any] = {"limit": page_size, "raw_json": 1}
        if after:
            params["after"] = after

        try:
            response = session.get(url, params=params, timeout=timeout_s)
        except Exception as exc:  # requests.RequestException and friends
            raise RedditFetchError(f"r/{subreddit}: request failed: {exc}") from exc

        if response.status_code == 429:
            retry_after_header = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after_header) if retry_after_header is not None else None
            except ValueError:
                retry_after_s = None
            if retry_after_s is None:
                retry_after_s = _DEFAULT_RATE_LIMIT_COOLDOWN_S
            raise RedditRateLimited(
                f"r/{subreddit}: rate limited (429), cooling down {retry_after_s:.0f}s",
                retry_after_s=retry_after_s,
            )
        if response.status_code != 200:
            raise RedditFetchError(f"r/{subreddit}: HTTP {response.status_code}")

        try:
            payload = response.json()
            children = payload["data"]["children"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RedditFetchError(f"r/{subreddit}: unexpected listing payload: {exc}") from exc

        if not children:
            break
        posts.extend(child["data"] for child in children)
        after = payload["data"].get("after")
        if not after:
            break

    return posts[:limit]


def _default_browser_factory() -> Any:
    """Launch one headless Chromium instance and one browser context, reused
    across every subreddit and every poll cycle for the life of the process
    -- launching fresh per subreddit would multiply the (already-expensive,
    only reached on JSON failure) fallback cost by the subreddit count for
    no benefit, since nothing about the challenge or the page requires a
    fresh profile each time.

    The context (not the bare browser) is what callers get pages from -- it
    carries the UA/viewport/locale and the `navigator.webdriver`-masking
    init script described above `_BROWSER_LAUNCH_ARGS`, none of which a page
    created directly from `browser.new_page()` would inherit.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 -- optional dependency

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_LAUNCH_ARGS)
        context = browser.new_context(
            user_agent=_BROWSER_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        context.add_init_script(_BROWSER_INIT_SCRIPT)
    except Exception:
        playwright.stop()
        raise
    return playwright, browser, context


def _fetch_listing_browser(
    context: Any,
    subreddit: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Load the rendered `/new/` page in a real browser tab and read posts
    out of the `<shreddit-post>` custom elements Reddit's frontend renders
    them into.

    A real Chromium engine executes Reddit's same-origin JS challenge itself
    -- the same thing a human visitor's browser does -- which is the entire
    reason this exists as a fallback: `_fetch_listing_json` cannot execute
    JS and has no other way through that challenge. `post-title` and
    `permalink` are plain attributes on the element; self-post body text
    lives in a `[slot="text-body"]` child and is absent for link posts,
    matching how `_fetch_listing_json` already treats a missing `selftext`
    (empty string, not an error).

    Reddit's feed lazy-loads more posts as you scroll, so this scrolls and
    waits in rounds until either `limit` posts have been seen or
    `_BROWSER_STALL_LIMIT` consecutive rounds produce no new post ids --
    the latter guards against looping forever on a subreddit whose `new`
    feed genuinely has fewer than `limit` posts.

    Every Playwright call in here -- including `context.new_page()` and the
    `finally` cleanup, not just navigation/scrolling -- is wrapped so a raw
    Playwright exception can never escape this function. That matters beyond
    tidiness: `_fetch_listing`'s crash-recovery path only triggers on
    `RedditFetchError`, so a `new_page()` call that raises because the
    browser disconnected between `_is_browser_alive()` and this call (or a
    `page.close()` that raises because Chromium already crashed) would
    otherwise bypass that `except RedditFetchError` entirely -- the relaunch
    logic would simply never run for exactly the crash shapes it exists to
    handle. Cleanup failures are swallowed rather than normalized and raised,
    since `page.close()` failing after Chromium already crashed carries no
    information the navigation/scroll error above it doesn't already have,
    and letting it propagate would silently replace that original, more
    informative error instead of just being logged as noise.
    """
    url = _LISTING_PAGE_URL.format(subreddit=subreddit)
    try:
        page = context.new_page()
    except Exception as exc:
        raise RedditFetchError(f"r/{subreddit}: opening a browser page failed: {exc}") from exc

    try:
        try:
            page.goto(url, timeout=_BROWSER_NAV_TIMEOUT_S * 1000)
            page.wait_for_selector("shreddit-post", timeout=_BROWSER_NAV_TIMEOUT_S * 1000)
        except Exception as exc:
            raise RedditFetchError(f"r/{subreddit}: browser navigation failed: {exc}") from exc

        posts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        stalled_rounds = 0

        while len(posts) < limit and stalled_rounds < _BROWSER_STALL_LIMIT:
            try:
                elements = page.query_selector_all("shreddit-post")
            except Exception as exc:
                raise RedditFetchError(f"r/{subreddit}: reading post elements failed: {exc}") from exc

            before = len(seen_ids)
            for element in elements:
                if len(posts) >= limit:
                    break
                post_id = element.get_attribute("id") or element.get_attribute("permalink")
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                title = element.get_attribute("post-title") or ""
                body_element = element.query_selector('[slot="text-body"]')
                body = body_element.inner_text() if body_element else ""
                posts.append({"title": title, "selftext": body})

            if len(posts) >= limit:
                break
            stalled_rounds = stalled_rounds + 1 if len(seen_ids) == before else 0
            if stalled_rounds >= _BROWSER_STALL_LIMIT:
                break
            try:
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(1000)
            except Exception as exc:
                raise RedditFetchError(f"r/{subreddit}: scrolling feed failed: {exc}") from exc

        return posts
    finally:
        try:
            page.close()
        except Exception:
            pass


def aggregate(
    texts: Iterable[str],
    analyzer: Any,
    *,
    symbol: str,
    subreddits: tuple[str, ...],
) -> SentimentSnapshot:
    """Pure aggregation step: no network, no scraping. This is what gets tested.

    Per-item VADER `compound` score in [-1, 1], same threshold shape the
    reference pipeline's `stream_processor.py` uses to bucket a comment,
    just not persisted to a database -- the mean over the batch is the
    signal, taken fresh on every call rather than a rolling window.
    """
    items: list[dict[str, Any]] = []
    total = 0.0
    bullish = bearish = neutral = 0

    for text in texts:
        compound = float(analyzer.polarity_scores(text)["compound"])
        total += compound
        if compound >= 0.05:
            bullish += 1
        elif compound <= -0.05:
            bearish += 1
        else:
            neutral += 1
        items.append({"text": text[:280], "compound": compound})

    n = len(items)
    return SentimentSnapshot(
        fetched_at=clock.iso_utc(),
        symbol=symbol,
        subreddits=subreddits,
        sample_size=n,
        mean_compound=(total / n) if n else None,
        bullish_count=bullish,
        bearish_count=bearish,
        neutral_count=neutral,
        items=tuple(items),
    )


class RedditSentimentReader:
    """Scrapes a fixed subreddit set for symbol-relevant chatter and scores it.

    Cached on the same "don't re-fetch faster than the signal moves"
    principle as `market.SnapshotReader`: the unauthenticated listing endpoint
    has a much tighter shared rate limit than an OAuth client would, and
    sentiment doesn't meaningfully shift inside a few minutes anyway.

    `symbol`/`subreddits`/`keywords` are all constructor arguments, not
    hardcoded -- this reader is not QQQ-specific; `reddit_sentiment_qqq` is
    just the one strategy currently instantiating it with QQQ's defaults.
    """

    def __init__(
        self,
        *,
        symbol: str = "QQQ",
        subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
        keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
        post_limit: int = DEFAULT_POST_LIMIT,
        min_interval_s: float = 300.0,
        session_factory: Callable[[], Any] = _default_session_factory,
        analyzer_factory: Callable[[], Any] = _default_analyzer_factory,
        browser_factory: Callable[[], Any] = _default_browser_factory,
    ):
        self.symbol = symbol
        self.subreddits = subreddits
        self.keywords = keywords
        self.post_limit = post_limit
        self.min_interval_s = min_interval_s
        self._session_factory = session_factory
        self._analyzer_factory = analyzer_factory
        self._browser_factory = browser_factory
        self._session: Any = None
        self._analyzer: Any = None
        self._playwright: Any = None
        self._browser: Any = None
        self._browser_context: Any = None
        self._browser_unavailable = False
        self._rate_limited_until: float = 0.0
        self._cached: SentimentSnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> SentimentSnapshot:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        if self._session is None:
            self._session = self._session_factory()
        if self._analyzer is None:
            self._analyzer = self._analyzer_factory()

        snapshot = aggregate(
            self._collect_texts(),
            self._analyzer,
            symbol=self.symbol,
            subreddits=self.subreddits,
        )
        self._cached, self._cached_at = snapshot, time.monotonic()
        return snapshot

    def _is_browser_alive(self) -> bool:
        if self._browser is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:
            return False

    def _close_browser(self) -> None:
        """Tear down whatever's cached (best-effort, ignoring errors from an
        already-dead process) and clear the cache so the next
        `_get_browser_context` call relaunches from scratch. Does not touch
        `_browser_unavailable` -- a crash after a previously successful
        launch is a transient recovery case, not the same as playwright
        never being installed at all."""
        for obj, method in (
            (self._browser_context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._playwright = None
        self._browser = None
        self._browser_context = None

    def _get_browser_context(self) -> Any:
        """Lazily launch the shared headless browser context on first use,
        once per reader for the life of the process, and transparently
        relaunch it once if a previously-working browser has since
        crashed or disconnected.

        `_browser_unavailable` remembers a *launch* failure (e.g. playwright
        not installed, or `playwright install chromium` never run) so a
        repeatedly-failing JSON layer doesn't retry the launch every cycle --
        it fails the same way every time and the error is already surfaced to
        the strategy as a no_trade reason. A crash of an already-running
        browser is different: without the `is_connected()` check below, a
        dead context would stay cached forever and every subsequent fallback
        would fail identically until the whole bot process restarted.
        """
        if self._browser_unavailable:
            raise RedditFetchError(
                "browser fallback unavailable (see prior error); install with "
                "`pip install playwright && playwright install chromium`"
            )
        if self._browser_context is not None and not self._is_browser_alive():
            self._close_browser()
        if self._browser_context is None:
            try:
                self._playwright, self._browser, self._browser_context = self._browser_factory()
            except Exception as exc:
                self._browser_unavailable = True
                raise RedditFetchError(f"browser fallback failed to launch: {exc}") from exc
        return self._browser_context

    def _fetch_listing(self, subreddit: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now < self._rate_limited_until:
            raise RedditFetchError(
                f"r/{subreddit}: skipping fetch, cooling down after a prior "
                f"Reddit 429 for another {self._rate_limited_until - now:.0f}s"
            )

        try:
            return _fetch_listing_json(self._session, subreddit, limit=self.post_limit)
        except RedditRateLimited as rate_limited:
            # A 429 is Reddit explicitly asking for backoff -- start a
            # cooldown shared across every subreddit and every future cycle
            # until it expires, and decline outright rather than falling
            # through to the browser fallback, which would hit the same
            # backend and risk compounding the limit instead of working
            # around it (unlike the JS-challenge case the fallback exists for).
            self._rate_limited_until = time.monotonic() + rate_limited.retry_after_s
            raise RedditFetchError(str(rate_limited)) from rate_limited
        except RedditFetchError as json_error:
            try:
                context = self._get_browser_context()
                return _fetch_listing_browser(context, subreddit, limit=self.post_limit)
            except RedditFetchError as browser_error:
                if self._is_browser_alive():
                    raise RedditFetchError(
                        f"r/{subreddit}: JSON listing failed ({json_error}); "
                        f"browser fallback also failed ({browser_error})"
                    ) from browser_error
                # The browser process itself died mid-fetch (crash,
                # disconnect, OOM-killed headless Chromium, etc.) rather than
                # the page merely failing to load -- drop the dead context
                # and allow exactly one relaunch-and-retry instead of caching
                # a context that would fail identically every subsequent
                # cycle until the process restarts.
                self._close_browser()
                try:
                    context = self._get_browser_context()
                    return _fetch_listing_browser(context, subreddit, limit=self.post_limit)
                except RedditFetchError as retry_error:
                    raise RedditFetchError(
                        f"r/{subreddit}: JSON listing failed ({json_error}); browser "
                        f"fallback crashed and relaunch also failed ({retry_error})"
                    ) from retry_error

    def _collect_texts(self) -> Iterable[str]:
        for name in self.subreddits:
            posts = self._fetch_listing(name)
            for post in posts:
                haystack = f"{post.get('title', '')} {post.get('selftext', '')}"
                if _matches_keywords(haystack, self.keywords):
                    yield haystack
