"""Market observation: the durable R2 snapshot and on-demand live quotes.

Two different things with two different cadences, deliberately kept apart:

  * `SnapshotReader` reads the durable board (`intraday/latest.json`) that the
    collector republishes about every 60 seconds. It is the strategy's view of
    the chain -- strikes, greeks, volume, VolDelta, underlying.
  * `QuoteReader` hits `GET /api/live-quotes` only when a strategy has already
    decided it cares about a specific contract. Execution quotes must be
    fresh (the Worker rejects anything older than ~15s), so this is pulled at
    decision time, not polled.

No direct DXLink connection -- explicitly deferred by the MOO-24 contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import requests

from . import clock
from .config import SNAPSHOT_URL

MAX_QUOTE_SYMBOLS = 100  # server-enforced ceiling on /api/live-quotes
EXECUTION_QUOTE_MAX_AGE_S = 15.0  # server-enforced staleness cutoff


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float | None
    ask: float | None
    quote_ts: str | None
    server_ts: str | None

    @property
    def age_seconds(self) -> float | None:
        """Quote age measured against the server's own clock.

        Compared server-side rather than to local time so a skewed laptop
        clock cannot make a stale quote look fresh (or vice versa).
        """
        if not self.quote_ts or not self.server_ts:
            return None
        return (
            datetime.fromisoformat(self.server_ts) - datetime.fromisoformat(self.quote_ts)
        ).total_seconds()

    @property
    def is_executable(self) -> bool:
        """Whether this quote is fresh enough that the Worker would accept it.

        A pre-check only. The server re-fetches its own quote at execution
        time; this just avoids spending rate-limit budget on a certain 409.
        """
        age = self.age_seconds
        return (
            self.bid is not None
            and self.ask is not None
            and age is not None
            and age <= EXECUTION_QUOTE_MAX_AGE_S
        )


@dataclass(frozen=True)
class UnderlyingMarket:
    """VWAP/RVOL for the underlying, as published in `underlying_market` on
    the snapshot payload (see collector.py's `_compute_underlying_market` and
    `docs/plans/2026-07-vwap-rvol.md`). One canonical record consumed
    identically by the UI and by strategies -- see
    `crassus/crassus/vwap_rvol.py` for how a strategy evaluates it.

    `rvol_status` is `"no_data"` (no baseline bucket yet) | `"insufficient_history"`
    (bucket exists, fewer than the collector's configured minimum days of
    samples) | `"ok"` -- same three-state shape as `momentum.MomentumSignal.status`,
    same rationale: separate "nothing" from "not enough yet" from "trustworthy."

    `freshness` is `"live"` | `"stale"` as written by the collector (it can
    only ever observe one of those two while it's running); a `"closed"`
    market some strategy or UI code wants to render off leftover data from a
    prior session is a read-time inference the consumer makes for itself
    (e.g. from `ctx.session_phase`), not a value the collector can predict.
    """

    symbol: str | None
    spot: float | None
    spot_ts: str | None
    vwap: float | None
    vwap_ts: str | None
    vwap_session_date: str | None
    price_vs_vwap_abs: float | None
    price_vs_vwap_pct: float | None
    session_volume: int | None
    session_volume_ts: str | None
    rvol_status: str
    rvol_multiple: float | None
    rvol_bucket_label: str | None
    rvol_baseline_volume: float | None
    rvol_baseline_days_used: int | None
    rvol_baseline_lookback_days: int | None
    source: str | None
    freshness: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "UnderlyingMarket | None":
        if not payload:
            return None
        rvol = payload.get("rvol") or {}
        return cls(
            symbol=payload.get("symbol"),
            spot=payload.get("spot"),
            spot_ts=payload.get("spot_ts"),
            vwap=payload.get("vwap"),
            vwap_ts=payload.get("vwap_ts"),
            vwap_session_date=payload.get("vwap_session_date"),
            price_vs_vwap_abs=payload.get("price_vs_vwap_abs"),
            price_vs_vwap_pct=payload.get("price_vs_vwap_pct"),
            session_volume=payload.get("session_volume"),
            session_volume_ts=payload.get("session_volume_ts"),
            rvol_status=rvol.get("status", "no_data"),
            rvol_multiple=rvol.get("multiple"),
            rvol_bucket_label=rvol.get("bucket_label"),
            rvol_baseline_volume=rvol.get("baseline_volume"),
            rvol_baseline_days_used=rvol.get("baseline_days_used"),
            rvol_baseline_lookback_days=rvol.get("baseline_lookback_days"),
            source=payload.get("source"),
            freshness=payload.get("freshness", "stale"),
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """One immutable read of the durable option board."""

    url: str
    fetched_at: str
    timestamp: str
    snapshot_time: str
    expiration: str
    underlying_price: float
    rows: list[dict[str, Any]]
    sha256: str
    underlying_market: UnderlyingMarket | None = None

    @classmethod
    def from_payload(cls, url: str, payload: dict[str, Any], raw: bytes) -> "MarketSnapshot":
        return cls(
            url=url,
            fetched_at=clock.iso_utc(),
            timestamp=payload.get("timestamp", ""),
            snapshot_time=payload.get("snapshot_time", ""),
            expiration=payload.get("expiration", ""),
            underlying_price=float(payload["underlying_price"]),
            rows=payload.get("rows", []),
            sha256=hashlib.sha256(raw).hexdigest(),
            underlying_market=UnderlyingMarket.from_payload(payload.get("underlying_market")),
        )

    @property
    def provenance(self) -> str:
        """What goes in the audit record's market_snapshot_url_or_hash field."""
        return f"{self.url}#sha256:{self.sha256}"

    def quoted(self, option_type: str | None = None) -> list[dict[str, Any]]:
        """Rows carrying a two-sided quote, optionally filtered to call/put."""
        rows = [r for r in self.rows if r.get("Bid") and r.get("Ask")]
        if option_type:
            rows = [r for r in rows if r.get("Type") == option_type]
        return rows

    def atm(self, option_type: str) -> dict[str, Any] | None:
        """Strike closest to the underlying, among rows that have a quote."""
        candidates = self.quoted(option_type)
        if not candidates:
            return None
        return min(candidates, key=lambda r: abs(r["Strike"] - self.underlying_price))

    def by_symbol(self, symbol: str) -> dict[str, Any] | None:
        return next((r for r in self.rows if r.get("OptionSymbol") == symbol), None)


class SnapshotReader:
    """Polls the durable snapshot, respecting its native ~60s cadence.

    Re-fetching faster than the collector republishes just burns bandwidth to
    receive the same bytes, so reads inside the interval return the cached
    snapshot unless explicitly forced.
    """

    def __init__(self, url: str = SNAPSHOT_URL, min_interval_s: float = 60.0, timeout_s: float = 15.0):
        self.url = url
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._cached: MarketSnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> MarketSnapshot:
        import time as _time

        age = _time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        resp = requests.get(self.url, timeout=self.timeout_s)
        resp.raise_for_status()
        snapshot = MarketSnapshot.from_payload(self.url, json.loads(resp.content), resp.content)
        self._cached, self._cached_at = snapshot, _time.monotonic()
        return snapshot


class QuoteRateLimited(Exception):
    """Quote-side 429s exhausted their retries against the shared budget.

    Kept distinct from a bare `requests.HTTPError` so the runner can
    classify this as `rate_limited` rather than letting it surface as a
    generic, unclassified strategy exception.
    """

    def __init__(self, message: str, retries: int):
        super().__init__(message)
        self.retries = retries


class QuoteReader:
    """Fetches ephemeral quotes. Needs no session -- this endpoint is public.

    Shares the same process-wide `RateLimiter` as account traffic. The limit
    that binds is per-IP (see `client.RateLimiter`), so a quote poll and an
    account mutation draw from one budget, not two -- six bot accounts
    plus their quote reads must not be able to collectively trip a 429 that
    each individually stayed under.
    """

    def __init__(
        self,
        base_url: str,
        rate_limiter: Any = None,
        timeout_s: float = 10.0,
        max_attempts: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        # Set by the most recent call to `quotes()`; the runner reads this to
        # fold retry history into the decision ledger's audit record instead
        # of dropping it once the call finally succeeds.
        self.last_retry_note: str | None = None

    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        symbols = list(dict.fromkeys(symbols))  # de-dupe, preserve order
        if not symbols:
            return {}
        if len(symbols) > MAX_QUOTE_SYMBOLS:
            raise ValueError(
                f"/api/live-quotes accepts at most {MAX_QUOTE_SYMBOLS} symbols, got {len(symbols)}"
            )

        self.last_retry_note = None
        retries = 0
        for attempt in range(1, self.max_attempts + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()

            resp = requests.get(
                f"{self.base_url}/api/live-quotes",
                params={"symbols": ",".join(symbols)},
                timeout=self.timeout_s,
            )

            if resp.status_code == 429:
                retries += 1
                retry_after = float(resp.headers.get("Retry-After", 5))
                if self.rate_limiter is not None:
                    # Honored globally: the next `acquire()` from *any*
                    # account or quote read will wait out this pause too.
                    self.rate_limiter.penalize(retry_after)
                self.last_retry_note = (
                    f"Quote request rate-limited ({retries} "
                    f"retr{'y' if retries == 1 else 'ies'}); Retry-After="
                    f"{retry_after}s honored globally."
                )
                if attempt < self.max_attempts:
                    continue
                raise QuoteRateLimited(self.last_retry_note, retries)

            resp.raise_for_status()
            payload = resp.json()
            server_ts = payload.get("server_ts")
            return {
                q["symbol"]: Quote(
                    symbol=q["symbol"],
                    bid=q.get("bid"),
                    ask=q.get("ask"),
                    quote_ts=q.get("quote_ts"),
                    server_ts=server_ts,
                )
                for q in payload.get("quotes", [])
            }
