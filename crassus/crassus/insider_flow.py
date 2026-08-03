"""SEC EDGAR Form 4 filing-activity observation.

Mirrors the ingestion-and-scoring shape of `sentiment.py` and
`trump_sentiment.py`: a fixed set of sources is polled on a cadence slower
than the underlying signal moves, an aggregate is computed in-process, and
the resulting snapshot is what a strategy reads. The source here is SEC
EDGAR's free, public, unauthenticated JSON submissions API --
`https://data.sec.gov/submissions/CIK##########.json` -- which lists a
filer's recent filings (form type, filing date, accession number) for every
company that files with the SEC, no app registration or API key required.

Scope limitation, read this before using the resulting count for anything:
this module counts recent Form 4 *filings* per company -- it does not parse
individual filing documents, so it has no idea whether a given Form 4
reflects a purchase or a sale, how many shares, at what price, or under what
transaction code (open-market buy, option exercise, tax-withholding sale,
10b5-1 plan, gift, etc). That information exists, but only inside each
filing's own XML (`ownership_transaction.xml` referenced from the filing
index), which is a heavier per-document fetch-and-parse than a v1 batching
across a basket of tickers every cycle should take on. A v2 worth building
later would fetch each new accession's XML and read the actual `transactionCode`
(P = open-market purchase, S = sale, ...) and `transactionShares` /
`transactionPricePerShare` fields to get a real, signed, dollar-weighted
insider-buying signal instead of a bare activity count.

Because of that limitation, `total_recent_form4_count` here can only ever be
an ACTIVITY signal -- "more insiders at these companies filed something
ownership-related recently than usual" -- not a directional one. It says
nothing about whether that activity was bullish or bearish. The strategy
module consuming this (`strategies/insider_form4.py`) is deliberately built
around that constraint: elevated activity is used only as a one-sided lean
that *confirms* a direction already visible in the price itself, never as a
standalone buy/sell call.

SEC EDGAR's fair-access policy requires every request carry a descriptive
User-Agent identifying the requester and a contact method (see
https://www.sec.gov/os/webmaster-faq#developers) -- unlike Reddit's or
trumpstruth.org's User-Agent, which are just a courtesy, EDGAR will reject
or throttle requests that send a generic/blank one. `config.INSIDER_FEED_USER_AGENT`
is read for this, with a descriptive fallback so a request is never sent
with an empty or generic identity even if the operator hasn't set the env
var. Courtesy caching (`min_interval_s`, default 600s) exists for the same
reason `SnapshotReader` and `RedditSentimentReader` cache: EDGAR's own
guidance asks bulk/automated consumers not to exceed roughly 10 requests per
second, and a small basket of tickers polled every strategy cycle would
otherwise refetch identical filing lists far faster than they actually
change (Form 4s are typically filed within two business days of a
transaction, not intraday).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import clock
from .config import INSIDER_FEED_USER_AGENT

# A small basket of QQQ mega-cap components, hardcoded for v1 -- the point is
# a diversified read on "is insider filing activity elevated across the
# Nasdaq-100's biggest names," not a complete or configurable universe.
# CIKs are the SEC's own 10-digit zero-padded filer identifiers.
DEFAULT_BASKET: tuple[tuple[str, str], ...] = (
    ("AAPL", "0000320193"),
    ("MSFT", "0000789019"),
    ("NVDA", "0001045810"),
    ("AMZN", "0001018724"),
    ("GOOGL", "0001652044"),
)

DEFAULT_LOOKBACK_HOURS = 48.0

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_FALLBACK_USER_AGENT = (
    # SEC EDGAR requires a descriptive User-Agent with contact info per its
    # fair-access policy (e.g. "CompanyName contact@example.com") -- this is
    # only reached if INSIDER_FEED_USER_AGENT is unset, so it identifies the
    # project generically rather than sending nothing.
    "crassus-insider-form4-strategy/1.0 (contact: unset, see config.py)"
)


class InsiderFetchError(RuntimeError):
    """A company's EDGAR submissions listing could not be fetched or parsed."""


@dataclass(frozen=True)
class CompanyFilingCount:
    """One company's recent Form 4 count within the lookback window."""

    ticker: str
    cik: str
    recent_form4_count: int


@dataclass(frozen=True)
class InsiderActivitySnapshot:
    """One aggregation pass across the basket at fetch time."""

    fetched_at: str
    lookback_hours: float
    basket: tuple[CompanyFilingCount, ...]

    @property
    def total_recent_form4_count(self) -> int:
        return sum(c.recent_form4_count for c in self.basket)


def _default_session_factory() -> Any:
    import requests  # noqa: PLC0415 -- optional dependency, only needed if this runs

    session = requests.Session()
    session.headers["User-Agent"] = INSIDER_FEED_USER_AGENT or _FALLBACK_USER_AGENT
    return session


def _parse_filing_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def count_recent_form4(
    payload: dict[str, Any],
    *,
    now: datetime,
    lookback_hours: float,
) -> int:
    """Pure counting step: no network. This is what gets tested.

    `payload` is the parsed JSON body of a `submissions/CIK##########.json`
    response. The recent-filings table lives under
    `payload["filings"]["recent"]`, with parallel arrays (`form`,
    `filingDate`, ...) rather than a list of per-filing objects -- that's
    EDGAR's own shape, not something this module chose.
    """
    recent = ((payload or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    cutoff = now - timedelta(hours=lookback_hours)

    count = 0
    for form, date_str in zip(forms, dates):
        if form != "4":
            continue
        filed_at = _parse_filing_date(date_str)
        if filed_at is None or filed_at < cutoff:
            continue
        count += 1
    return count


class InsiderFlowReader:
    """Polls EDGAR submissions listings for a fixed basket of companies.

    Cached on the same "don't re-fetch faster than the signal moves"
    principle as `market.SnapshotReader` and `RedditSentimentReader`: Form 4s
    trickle in over business days, not seconds, so polling every strategy
    cycle would only burn EDGAR's rate-limit budget for identical results.
    """

    def __init__(
        self,
        *,
        basket: tuple[tuple[str, str], ...] = DEFAULT_BASKET,
        lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
        min_interval_s: float = 600.0,
        timeout_s: float = 10.0,
        session_factory: Callable[[], Any] = _default_session_factory,
    ):
        self.basket = basket
        self.lookback_hours = lookback_hours
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._session_factory = session_factory
        self._session: Any = None
        self._cached: InsiderActivitySnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> InsiderActivitySnapshot:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        if self._session is None:
            self._session = self._session_factory()

        now = datetime.now(timezone.utc)
        counts: list[CompanyFilingCount] = []
        for ticker, cik in self.basket:
            payload = self._fetch_submissions(cik)
            n = count_recent_form4(payload, now=now, lookback_hours=self.lookback_hours)
            counts.append(CompanyFilingCount(ticker=ticker, cik=cik, recent_form4_count=n))

        snapshot = InsiderActivitySnapshot(
            fetched_at=clock.iso_utc(),
            lookback_hours=self.lookback_hours,
            basket=tuple(counts),
        )
        self._cached, self._cached_at = snapshot, time.monotonic()
        return snapshot

    def _fetch_submissions(self, cik: str) -> dict[str, Any]:
        url = _SUBMISSIONS_URL.format(cik=cik)
        try:
            response = self._session.get(url, timeout=self.timeout_s)
        except Exception as exc:  # requests.RequestException and friends
            raise InsiderFetchError(f"CIK {cik}: request failed: {exc}") from exc

        if response.status_code != 200:
            raise InsiderFetchError(f"CIK {cik}: HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise InsiderFetchError(f"CIK {cik}: unexpected non-JSON payload: {exc}") from exc
