"""Reddit listing acquisition and unchanged VADER aggregation.

One reader is shared by the Reddit strategy and its Phelps wrappers. Successes
and failures expire after the same retrieval window; scoring/thresholds remain
per account. Production acquisition runs in a disposable, deadline-controlled
process (reddit_acquisition.py). Injected transports support offline fixtures.
Denial, rate limits, malformed payloads and challenge pages fail closed. Only
transport/server failures may use the ordinary, non-masking browser fallback.
No source availability or post-age policy is implied by a recent fetch time.
"""

from __future__ import annotations

import time
import logging
import math
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import clock
from .config import REDDIT_USER_AGENT
from .observability import event

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

# Keep the former single navigation timeout as the TOTAL acquisition budget,
# including HTTP, launch, navigation, extraction and graceful cleanup. The
# deployed cadence is 300 seconds; do not multiply this budget by accounts.
_ACQUISITION_TIMEOUT_S = _BROWSER_NAV_TIMEOUT_S
log = logging.getLogger("crassus")


class RedditFetchError(RuntimeError):
    """A subreddit listing could not be fetched by any available method."""


class RedditSourceDenied(RedditFetchError):
    """Access denial: never retry using a different transport."""


class RedditParseError(RedditFetchError):
    """An untrusted payload must never be scored."""


class RedditStaleData(RedditFetchError):
    """A cached source response exceeds the existing retrieval window."""


class RedditRateLimited(RedditFetchError):
    """Reddit requested a shared cooldown; no alternate-transport retry."""

    def __init__(self, message: str, *, retry_after_s: float):
        super().__init__(message)
        self.retry_after_s = retry_after_s


def _retry_after(headers: Any) -> float:
    """Use a valid provider delay, otherwise retain the conservative fallback."""
    raw = None
    try:
        raw = headers.get("Retry-After", headers.get("retry-after"))
        value = float(raw)
        if math.isfinite(value) and value >= 0:
            return value
    except (AttributeError, TypeError, ValueError):
        if isinstance(raw, str):
            try:
                until = parsedate_to_datetime(raw)
                if until.tzinfo is not None:
                    delay = (until - datetime.now(timezone.utc)).total_seconds()
                    if math.isfinite(delay) and delay >= 0:
                        return delay
            except (TypeError, ValueError, OverflowError):
                pass
    return _DEFAULT_RATE_LIMIT_COOLDOWN_S


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


def _http_expiry(headers: Any, subreddit: str, max_age_s: float) -> float:
    """Carry source-response age into the cache; downloading is not renewal."""
    age = 0.0
    if isinstance(headers, dict) or hasattr(headers, "items"):
        try:
            age_header = headers.get("Age", headers.get("age"))
            date_header = headers.get("Date", headers.get("date"))
            if isinstance(age_header, str):
                age = float(age_header)
                if not math.isfinite(age) or age < 0:
                    raise ValueError()
            if isinstance(date_header, str):
                source_time = parsedate_to_datetime(date_header)
                if source_time.tzinfo is None:
                    raise ValueError()
                age = max(age, (datetime.now(timezone.utc) - source_time).total_seconds())
        except (TypeError, ValueError, OverflowError) as exc:
            raise RedditParseError(f"parsing_failure: r/{subreddit}: invalid freshness header") from exc
    if age >= max_age_s and age > 0:
        raise RedditStaleData(f"stale_data: r/{subreddit}: HTTP age exceeds retrieval window")
    return time.monotonic() + max(0, max_age_s - age)


def _fetch_listing_json(
    session: Any,
    subreddit: str,
    *,
    limit: int,
    timeout_s: float = 10.0,
    max_age_s: float = 300.0,
    freshness_callback: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """Pull public listings; denial/invalid payloads never trigger fallback."""
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
            raise RedditFetchError(f"source_unavailable: r/{subreddit}: request failed ({type(exc).__name__})") from exc

        if response.status_code == 429:
            retry_after_s = _retry_after(response.headers)
            raise RedditRateLimited(
                f"r/{subreddit}: rate limited (429), cooling down {retry_after_s:.0f}s",
                retry_after_s=retry_after_s,
            )
        if response.status_code in (401, 403):
            raise RedditSourceDenied(f"source_denied: r/{subreddit}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise RedditFetchError(f"r/{subreddit}: HTTP {response.status_code}")

        expiry = _http_expiry(getattr(response, "headers", {}), subreddit, max_age_s)
        if freshness_callback is not None:
            freshness_callback(expiry)

        try:
            payload = response.json()
            children = payload["data"]["children"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RedditParseError(f"parsing_failure: r/{subreddit}: unexpected listing payload") from exc

        if not isinstance(children, list):
            raise RedditParseError(f"parsing_failure: r/{subreddit}: children must be a list")
        if not children:
            break
        try:
            for child in children:
                post = child["data"]
                if not isinstance(post, dict) or not isinstance(post.get("title"), str):
                    raise ValueError("post title must be text")
                if not isinstance(post.get("selftext", ""), str):
                    raise ValueError("post body must be text")
                posts.append(post)
        except (KeyError, TypeError, ValueError) as exc:
            raise RedditParseError(f"parsing_failure: r/{subreddit}: invalid post shape") from exc
        next_after = payload["data"].get("after")
        if next_after is not None and (not isinstance(next_after, str) or next_after == after):
            raise RedditParseError(f"parsing_failure: r/{subreddit}: invalid pagination cursor")
        after = next_after
        if not after:
            break

    return posts[:limit]


def _release_browser(context: Any, browser: Any, playwright: Any) -> None:
    """Attempt every teardown even if a close is interrupted or already failed."""
    interrupted = None
    for obj, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
        if obj is None:
            continue
        try:
            getattr(obj, method)()
        except Exception:
            pass  # Includes driver/target already closed.
        except BaseException as exc:
            interrupted = exc
    if interrupted is not None:
        raise interrupted


def _default_browser_factory() -> Any:
    """Ordinary headless browser; no challenge bypass or identity masking."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 -- optional dependency

    playwright = sync_playwright().start()
    browser = context = None
    try:
        browser = playwright.chromium.launch(headless=True, timeout=_BROWSER_NAV_TIMEOUT_S * 1000)
        context = browser.new_context(user_agent=REDDIT_USER_AGENT or _FALLBACK_USER_AGENT)
    except BaseException:
        _release_browser(context, browser, playwright)
        raise
    return playwright, browser, context


def _fetch_listing_browser(
    context: Any,
    subreddit: str,
    *,
    limit: int,
    max_age_s: float = 300.0,
    freshness_callback: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """Read ordinary rendered post elements after a transport/server failure.

    One failed read aborts the acquisition. The caller's disposable process
    bounds all browser operations and teardown together, including calls
    without Playwright timeout arguments. This function also caps scrolls.
    """
    url = _LISTING_PAGE_URL.format(subreddit=subreddit)
    try:
        page = context.new_page()
    except Exception as exc:
        raise RedditFetchError(f"r/{subreddit}: opening a browser page failed: {type(exc).__name__}") from exc

    try:
        try:
            response = page.goto(url, timeout=_BROWSER_NAV_TIMEOUT_S * 1000)
            status = getattr(response, "status", None)
            if status in (401, 403):
                raise RedditSourceDenied(f"source_denied: r/{subreddit}: browser HTTP {status}")
            if status == 429:
                raise RedditRateLimited("source_rate_limited: browser HTTP 429",
                                        retry_after_s=_retry_after(response.all_headers()))
            if isinstance(status, int) and status >= 400:
                raise RedditFetchError(f"source_unavailable: browser HTTP {status}")
            headers = response.all_headers() if response is not None else {}
            expiry = _http_expiry(headers, subreddit, max_age_s)
            if freshness_callback is not None:
                freshness_callback(expiry)
            page.wait_for_selector("shreddit-post", timeout=_BROWSER_NAV_TIMEOUT_S * 1000)
        except RedditFetchError:
            raise
        except Exception as exc:
            kind = "browser_timeout" if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError" else "browser_failure"
            raise RedditFetchError(f"{kind}: r/{subreddit}: navigation failed") from exc

        posts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        stalled_rounds = 0

        for _ in range(_BROWSER_MAX_SCROLL_ROUNDS):
            if len(posts) >= limit or stalled_rounds >= _BROWSER_STALL_LIMIT:
                break
            try:
                elements = page.query_selector_all("shreddit-post")
            except Exception as exc:
                raise RedditFetchError(f"r/{subreddit}: reading post elements failed: {type(exc).__name__}") from exc

            before = len(seen_ids)
            for element in elements:
                if len(posts) >= limit:
                    break
                try:
                    post_id = element.get_attribute("id") or element.get_attribute("permalink")
                    if not post_id or post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)
                    title = element.get_attribute("post-title")
                    if not isinstance(title, str) or not title:
                        raise RedditParseError(f"parsing_failure: r/{subreddit}: missing post-title")
                    body_element = element.query_selector('[slot="text-body"]')
                    body = body_element.inner_text() if body_element else ""
                    posts.append({"title": title, "selftext": body})
                except RedditParseError:
                    raise
                except Exception as exc:
                    raise RedditFetchError(
                        f"parsing_failure: r/{subreddit}: reading post content failed: {type(exc).__name__}"
                    ) from exc

            if len(posts) >= limit:
                break
            stalled_rounds = stalled_rounds + 1 if len(seen_ids) == before else 0
            if stalled_rounds >= _BROWSER_STALL_LIMIT:
                break
            try:
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(1000)
            except Exception as exc:
                raise RedditFetchError(f"r/{subreddit}: scrolling feed failed: {type(exc).__name__}") from exc

        if not posts:
            raise RedditParseError(f"parsing_failure: r/{subreddit}: no readable post elements")
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
        self._source_expires = float("inf")
        self._failure: str | None = None
        self._failure_until = 0.0
        self._read_lock = threading.Lock()
        self._phase_callback: Callable[[str], None] = lambda phase: None
        self._rate_limit_callback: Callable[[float], None] = lambda until: None
        self._isolated = session_factory is _default_session_factory and browser_factory is _default_browser_factory

    def read(self, force: bool = False) -> SentimentSnapshot:
        with self._read_lock:
            now = time.monotonic()
            # force may refresh success, but cannot bypass a failed-source cooldown.
            if self._failure is not None and now < self._failure_until:
                raise RedditFetchError(self._failure)
            if self._cached is not None and not force and now - self._cached_at < self.min_interval_s and now < self._source_expires:
                return self._cached
            self._cached = None  # Never serve a previous success after a failed refresh.
            self._browser_unavailable = False
            self._source_expires = now + self.min_interval_s
            try:
                if self._isolated:
                    from .reddit_acquisition import acquire
                    texts = acquire(self)
                else:
                    if self._session is None:
                        self._session = self._session_factory()
                    try:
                        texts = list(self._collect_texts())
                    finally:
                        self._close_browser()
                if self._analyzer is None:
                    self._analyzer = self._analyzer_factory()
                if self.min_interval_s > 0 and time.monotonic() >= self._source_expires:
                    raise RedditStaleData("stale_data: response expired during acquisition")
                snapshot = aggregate(texts, self._analyzer, symbol=self.symbol,
                                     subreddits=self.subreddits)
                if self.min_interval_s > 0 and time.monotonic() >= self._source_expires:
                    raise RedditStaleData("stale_data: response expired during scoring")
            except Exception as exc:
                # Store text, not the exception/traceback (which retains resources).
                self._failure = str(exc) if isinstance(exc, RedditFetchError) else f"source_unavailable: {type(exc).__name__}"
                self._failure_until = max(time.monotonic() + self.min_interval_s, self._rate_limited_until)
                event(log, "reddit_acquisition_failed", reason=self._failure,
                      duration_seconds=time.monotonic() - now,
                      retry_in_seconds=max(0, self._failure_until - time.monotonic()))
                raise
            except BaseException:
                self._failure = "source_cancelled"
                self._failure_until = time.monotonic() + self.min_interval_s
                raise
            self._failure = None
            # Age starts at acquisition start, not at completion/scoring time.
            self._cached, self._cached_at = snapshot, now
            event(log, "reddit_acquisition_completed", sample_size=snapshot.sample_size,
                  duration_seconds=time.monotonic() - now)
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
        `_get_browser_context` call starts from scratch. The shared failure
        window controls when another acquisition may begin."""
        resources = self._browser_context, self._browser, self._playwright
        self._playwright = None
        self._browser = None
        self._browser_context = None
        _release_browser(*resources)

    def _get_browser_context(self) -> Any:
        """One stack per acquisition; retry failed launches only after expiry."""
        if self._browser_unavailable:
            raise RedditFetchError(
                "browser fallback unavailable for this read (see prior error); install with "
                "`pip install playwright && playwright install chromium`"
            )
        if self._browser_context is not None and not self._is_browser_alive():
            self._close_browser()
        if self._browser_context is None:
            try:
                self._playwright, self._browser, self._browser_context = self._browser_factory()
            except Exception as exc:
                self._browser_unavailable = True
                raise RedditFetchError(f"browser fallback failed to launch: {type(exc).__name__}") from exc
        return self._browser_context

    def _observe_expiry(self, expiry: float) -> None:
        self._source_expires = min(self._source_expires, expiry)

    def _fetch_listing(self, subreddit: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now < self._rate_limited_until:
            raise RedditFetchError(
                f"r/{subreddit}: skipping fetch, cooling down after a prior "
                f"Reddit 429 for another {self._rate_limited_until - now:.0f}s"
            )

        self._phase_callback("http")
        try:
            return _fetch_listing_json(self._session, subreddit, limit=self.post_limit, max_age_s=self.min_interval_s, freshness_callback=self._observe_expiry)
        except RedditRateLimited as rate_limited:
            self._rate_limited_until = time.monotonic() + rate_limited.retry_after_s
            self._rate_limit_callback(self._rate_limited_until)
            raise
        except (RedditSourceDenied, RedditParseError, RedditStaleData):
            raise  # Never route denied/challenged/malformed data around to a browser.
        except RedditFetchError as json_error:
            self._phase_callback("browser")
            try:
                context = self._get_browser_context()
                return _fetch_listing_browser(context, subreddit, limit=self.post_limit,
                                              max_age_s=self.min_interval_s, freshness_callback=self._observe_expiry)
            except RedditRateLimited as rate_limited:
                self._rate_limited_until = time.monotonic() + rate_limited.retry_after_s
                self._rate_limit_callback(self._rate_limited_until)
                raise
            except RedditFetchError as browser_error:
                # No relaunch within a failed acquisition. Recovery occurs when
                # the shared window expires, not once for each waiting account.
                raise RedditFetchError(
                    f"{json_error}; {browser_error}"
                ) from browser_error

    def _collect_texts(self) -> Iterable[str]:
        for name in self.subreddits:
            posts = self._fetch_listing(name)
            for post in posts:
                haystack = f"{post.get('title', '')} {post.get('selftext', '')}"
                if _matches_keywords(haystack, self.keywords):
                    yield haystack
