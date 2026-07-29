"""Trump Truth Social sentiment observation for `trump_whisperer_qqq`.

Prior art considered:

- [maxbbraun/trump2cash](https://github.com/maxbbraun/trump2cash) --
  watches Trump's tweets via the Twitter Streaming API, uses Google Cloud
  Natural Language for per-company entity detection + sentiment, and buys
  the mentioned company on positive sentiment / shorts it on negative,
  closing at market close. The entity-resolution step (which company did he
  mean) doesn't apply here -- this strategy only ever trades QQQ, the one
  instrument this whole platform trades -- but the shape (score each post,
  buy on positive / short-equivalent on negative) is exactly what
  `aggregate()` below reuses. It's also 2010s-Twitter-specific and
  unmaintained since Trump moved off Twitter; not runnable as-is regardless.

- [TheNeuroDeveloper/TrumpTruthsMarketAnalysis](https://github.com/TheNeuroDeveloper/TrumpTruthsMarketAnalysis) --
  polls `https://trumpstruth.org/feed` (an unauthenticated RSS mirror of
  Trump's actual Truth Social posts) every 30s with `feedparser`, then sends
  each post through an LLM for structured market-impact analysis across
  five asset classes. The ingestion source (`trumpstruth.org/feed`, no
  auth, no app to register) is the part reused here -- verified live and
  working. The LLM-based analysis step is not: crassus is one lightweight
  process per account reading one number every few minutes (same reasoning
  as `crassus.sentiment`'s Reddit ingestion), and a per-post LLM call for
  every poll cycle is a different cost/complexity tier than a VADER
  `compound` score. Parsing is stdlib `xml.etree.ElementTree` rather than
  adding a `feedparser` dependency for a small, fixed RSS 2.0 shape.

Deliberately not a port of either. `aggregate()` is this project's
equivalent of `crassus.sentiment.aggregate()` -- pure, VADER-scored,
testable without a database -- kept as a separate, small, independently
testable function rather than sharing the Reddit module's, so this
strategy's ingestion and scoring can be verified and evolve on its own.

Unlike Reddit's `new` listing (already just recent posts), the RSS feed is
an append-only archive going back years, so freshness matters here in a way
it didn't there: `aggregate()` only scores posts published within
`max_post_age_minutes` of the read, so a quiet stretch with no fresh posts
reads as "no signal" rather than re-scoring the same old archived posts on
every cycle.

`aggregate()` also does novelty filtering: Truth Social supports verbatim
reposts ("ReTruths"), and a real statement is sometimes followed minutes
later by a near-identical restatement. Real news-analytics vendors
(RavenPack's "novelty" score is the industry term for this) exist
specifically to stop a trading system from treating a rehash of a story
already priced in as fresh, independent signal. Counting a repost as a
second data point would silently double the sentiment of whatever was
being restated -- not a new opinion, the same one twice. `_is_duplicate`
does this with a similarity ratio over normalized text
(`difflib.SequenceMatcher`, stdlib, not a new dependency for what's a small
per-cycle comparison set) rather than exact-match only, since a restatement
is rarely character-for-character identical to the original.

vaderSentiment is imported lazily (inside functions, not at module scope)
so importing this module -- and therefore `crassus.strategies` -- never
fails for an account not configured to use this strategy.
"""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable
from xml.etree import ElementTree

from . import clock
from .config import TRUMP_FEED_USER_AGENT

DEFAULT_FEED_URL = "https://trumpstruth.org/feed"
DEFAULT_POST_LIMIT = 100
DEFAULT_MAX_POST_AGE_MINUTES = 180.0

# How similar two posts' normalized text must be (0-1, difflib's ratio()) to
# treat the later one as a repost/rehash of the earlier one rather than
# independent signal. 1.0 would only catch verbatim duplicates; this is
# deliberately looser to also catch a near-identical restatement, but not so
# loose that two posts that are merely on the same topic get conflated.
_DUPLICATE_SIMILARITY_THRESHOLD = 0.9

_FALLBACK_USER_AGENT = "crassus-trump-sentiment-reader/1.0"


class TrumpFetchError(RuntimeError):
    """The Truth Social feed could not be fetched or parsed."""


@dataclass(frozen=True)
class TrumpSentimentSnapshot:
    """One aggregation pass over whatever fresh posts matched at fetch time."""

    fetched_at: str
    sample_size: int
    mean_compound: float | None
    bullish_count: int
    bearish_count: int
    neutral_count: int
    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    latest_post_at: str | None = None
    duplicate_count: int = 0

    @property
    def bullish_share(self) -> float | None:
        return (self.bullish_count / self.sample_size) if self.sample_size else None

    @property
    def bearish_share(self) -> float | None:
        return (self.bearish_count / self.sample_size) if self.sample_size else None


def _default_session_factory() -> Any:
    import requests  # noqa: PLC0415 -- optional dependency, only needed if this runs

    session = requests.Session()
    session.headers["User-Agent"] = TRUMP_FEED_USER_AGENT or _FALLBACK_USER_AGENT
    return session


def _default_analyzer_factory() -> Any:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # noqa: PLC0415

    return SentimentIntensityAnalyzer()


def _parse_feed(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Parse `trumpstruth.org/feed`'s RSS 2.0 body into plain dicts.

    Deliberately narrow: this feed's shape is `<rss><channel><item>` with
    `title`/`link`/`pubDate` per item, not general-purpose RSS/Atom
    handling. `pubDate` uses the standard RFC 822 format
    (`email.utils.parsedate_to_datetime` handles it directly); a missing or
    unparseable date is kept as `published_at=None` rather than raising --
    such a post simply can't pass the freshness filter in `aggregate()`, the
    same "can't be scored, not a fetch failure" treatment
    `_fetch_listing_json` gives a missing `selftext` in the Reddit module.
    """
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise TrumpFetchError(f"malformed feed XML: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        raise TrumpFetchError("unexpected feed shape: no <channel> element")

    posts: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate")
        published_at: datetime | None
        try:
            published_at = parsedate_to_datetime(pub_date_raw) if pub_date_raw else None
        except (TypeError, ValueError):
            published_at = None
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        posts.append({"text": title, "link": link, "published_at": published_at})
    return posts


def _normalize_for_dedup(text: str) -> str:
    return " ".join(text.lower().split())


def _is_duplicate(text: str, seen_normalized: list[str]) -> bool:
    """Has `text` already been counted via an earlier (fresher -- see
    `aggregate`'s iteration order) post this cycle?

    A post with no real content (empty title -- e.g. an image/media-only
    Truth) never matches, deliberately: several such posts in one window
    would otherwise all normalize to the same empty string and get flagged
    as reposts of each other, which isn't a meaningful novelty judgment --
    there's no statement there to have been restated. They're harmless
    either way since VADER scores empty text as neutral (0.0).
    """
    normalized = _normalize_for_dedup(text)
    if not normalized:
        return False
    for other in seen_normalized:
        if normalized == other:
            return True
        if difflib.SequenceMatcher(None, normalized, other).ratio() >= _DUPLICATE_SIMILARITY_THRESHOLD:
            return True
    return False


def _fetch_posts(session: Any, *, feed_url: str, limit: int, timeout_s: float = 10.0) -> list[dict[str, Any]]:
    """Fetch and parse up to `limit` most recent posts from the feed.

    No pagination: unlike Reddit's listing, `trumpstruth.org/feed` returns
    its most recent ~100 items in one response with no cursor, which is
    already more than `aggregate()`'s freshness filter would keep in any
    normal polling interval.
    """
    try:
        response = session.get(feed_url, timeout=timeout_s)
    except Exception as exc:  # requests.RequestException and friends
        raise TrumpFetchError(f"request to {feed_url} failed: {exc}") from exc

    if response.status_code != 200:
        raise TrumpFetchError(f"{feed_url}: HTTP {response.status_code}")

    posts = _parse_feed(response.content)
    return posts[:limit]


def aggregate(
    posts: Iterable[dict[str, Any]],
    analyzer: Any,
    *,
    now: datetime,
    max_post_age_minutes: float,
) -> TrumpSentimentSnapshot:
    """Pure aggregation step: no network, no feed parsing. This is what gets
    tested.

    Only posts with a known `published_at` within `max_post_age_minutes` of
    `now` are scored -- see the module docstring for why freshness matters
    here in a way it didn't for Reddit's already-recent `new` listing.
    `now` is a parameter rather than `clock.now_utc()` called internally so
    this stays a pure function of its inputs, exactly like
    `crassus.sentiment.aggregate`.

    Posts are expected in the feed's natural newest-first order (what
    `_fetch_posts` returns): a repost is therefore compared against
    already-accepted *fresher* posts and dropped in favor of them, on the
    theory that the newest instance of a restated claim is the one worth
    keeping if only one can count, not an arbitrarily earlier one. This
    ordering isn't validated here -- passing posts in a different order
    would just change which of a duplicate pair survives, not silently
    break anything.
    """
    items: list[dict[str, Any]] = []
    seen_normalized: list[str] = []
    total = 0.0
    bullish = bearish = neutral = 0
    duplicates = 0
    latest_post_at: datetime | None = None
    max_age = max_post_age_minutes * 60.0

    for post in posts:
        published_at = post.get("published_at")
        if published_at is None:
            continue
        age_s = (now - published_at).total_seconds()
        if age_s < 0 or age_s > max_age:
            continue

        text = post.get("text", "")
        if _is_duplicate(text, seen_normalized):
            duplicates += 1
            continue
        normalized = _normalize_for_dedup(text)
        if normalized:
            seen_normalized.append(normalized)

        if latest_post_at is None or published_at > latest_post_at:
            latest_post_at = published_at

        compound = float(analyzer.polarity_scores(text)["compound"])
        total += compound
        if compound >= 0.05:
            bullish += 1
        elif compound <= -0.05:
            bearish += 1
        else:
            neutral += 1
        items.append({
            "text": text[:280],
            "compound": compound,
            "link": post.get("link"),
            "published_at": clock.iso_utc(published_at),
        })

    n = len(items)
    return TrumpSentimentSnapshot(
        fetched_at=clock.iso_utc(now),
        sample_size=n,
        mean_compound=(total / n) if n else None,
        bullish_count=bullish,
        bearish_count=bearish,
        neutral_count=neutral,
        items=tuple(items),
        latest_post_at=clock.iso_utc(latest_post_at) if latest_post_at else None,
        duplicate_count=duplicates,
    )


class TrumpSentimentReader:
    """Polls `trumpstruth.org/feed` for fresh posts and scores them.

    Cached on the same "don't re-fetch faster than the signal moves"
    principle as `crassus.sentiment.RedditSentimentReader`: there is exactly
    one Trump, so there is exactly one feed to poll regardless of how many
    accounts run this strategy, and the same module-level singleton pattern
    (`crassus.strategies.trump_whisperer._reader`) applies for the same
    reason it does there.
    """

    def __init__(
        self,
        *,
        feed_url: str = DEFAULT_FEED_URL,
        post_limit: int = DEFAULT_POST_LIMIT,
        max_post_age_minutes: float = DEFAULT_MAX_POST_AGE_MINUTES,
        min_interval_s: float = 180.0,
        session_factory: Callable[[], Any] = _default_session_factory,
        analyzer_factory: Callable[[], Any] = _default_analyzer_factory,
    ):
        self.feed_url = feed_url
        self.post_limit = post_limit
        self.max_post_age_minutes = max_post_age_minutes
        self.min_interval_s = min_interval_s
        self._session_factory = session_factory
        self._analyzer_factory = analyzer_factory
        self._session: Any = None
        self._analyzer: Any = None
        self._cached: TrumpSentimentSnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> TrumpSentimentSnapshot:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        if self._session is None:
            self._session = self._session_factory()
        if self._analyzer is None:
            self._analyzer = self._analyzer_factory()

        posts = _fetch_posts(self._session, feed_url=self.feed_url, limit=self.post_limit)
        snapshot = aggregate(
            posts,
            self._analyzer,
            now=clock.now_utc(),
            max_post_age_minutes=self.max_post_age_minutes,
        )
        self._cached, self._cached_at = snapshot, time.monotonic()
        return snapshot
