"""Reddit sentiment observation.

Mirrors the ingestion-and-scoring shape of
[nama1arpit/reddit-streaming-pipeline](https://github.com/nama1arpit/reddit-streaming-pipeline):
PRAW pulls recent posts from a fixed set of subreddits, VADER scores each one,
and the aggregate (there: a per-minute Cassandra rollup fed to Grafana; here:
an in-memory mean) is the signal a strategy reads.

Deliberately not a port of that project's Kafka -> Spark -> Cassandra ->
Grafana stack. Crassus is one lightweight Python process per account reading
one number every few minutes; standing up a Kubernetes cluster to answer "is
r/wallstreetbets bullish on QQQ right now" would be infrastructure in search
of a justification. `aggregate()` below is the part of that project actually
worth reusing -- the scoring method -- reimplemented as a plain function so it
can be unit-tested without a broker or a database.

praw and vaderSentiment are imported lazily (inside functions, not at module
scope) so importing this module -- and therefore `crassus.strategies` --
never fails for an account not configured to use the sentiment strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import clock
from .config import REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT

DEFAULT_SUBREDDITS: tuple[str, ...] = ("wallstreetbets", "stocks", "options", "investing")
DEFAULT_KEYWORDS: tuple[str, ...] = ("qqq", "nasdaq-100", "nasdaq 100", "nasdaq100")
DEFAULT_POST_LIMIT = 50


class RedditCredentialsMissing(RuntimeError):
    """REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT unset."""


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


def _default_client_factory() -> Any:
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT):
        raise RedditCredentialsMissing(
            "REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT must "
            "all be set (see crassus/README.md) to poll Reddit sentiment."
        )
    import praw  # noqa: PLC0415 -- optional dependency, only needed if this runs

    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )


def _default_analyzer_factory() -> Any:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # noqa: PLC0415

    return SentimentIntensityAnalyzer()


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in keywords)


def aggregate(
    texts: Iterable[str],
    analyzer: Any,
    *,
    symbol: str,
    subreddits: tuple[str, ...],
) -> SentimentSnapshot:
    """Pure aggregation step: no network, no praw. This is what gets tested.

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
    """Polls a fixed subreddit set for symbol-relevant chatter and scores it.

    Cached on the same "don't re-fetch faster than the signal moves"
    principle as `market.SnapshotReader`: Reddit's OAuth rate limit is shared
    across every account's cycle, and sentiment doesn't meaningfully shift
    inside a few minutes anyway.
    """

    def __init__(
        self,
        *,
        symbol: str = "QQQ",
        subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
        keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
        post_limit: int = DEFAULT_POST_LIMIT,
        min_interval_s: float = 300.0,
        client_factory: Callable[[], Any] = _default_client_factory,
        analyzer_factory: Callable[[], Any] = _default_analyzer_factory,
    ):
        self.symbol = symbol
        self.subreddits = subreddits
        self.keywords = keywords
        self.post_limit = post_limit
        self.min_interval_s = min_interval_s
        self._client_factory = client_factory
        self._analyzer_factory = analyzer_factory
        self._client: Any = None
        self._analyzer: Any = None
        self._cached: SentimentSnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> SentimentSnapshot:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        if self._client is None:
            self._client = self._client_factory()
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

    def _collect_texts(self) -> Iterable[str]:
        for name in self.subreddits:
            subreddit = self._client.subreddit(name)
            for submission in subreddit.new(limit=self.post_limit):
                haystack = f"{submission.title} {getattr(submission, 'selftext', '')}"
                if _matches_keywords(haystack, self.keywords):
                    yield haystack
