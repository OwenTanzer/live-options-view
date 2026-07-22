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


class QuoteReader:
    """Fetches ephemeral quotes. Needs no session -- this endpoint is public."""

    def __init__(self, base_url: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        symbols = list(dict.fromkeys(symbols))  # de-dupe, preserve order
        if not symbols:
            return {}
        if len(symbols) > MAX_QUOTE_SYMBOLS:
            raise ValueError(
                f"/api/live-quotes accepts at most {MAX_QUOTE_SYMBOLS} symbols, got {len(symbols)}"
            )

        resp = requests.get(
            f"{self.base_url}/api/live-quotes",
            params={"symbols": ",".join(symbols)},
            timeout=self.timeout_s,
        )
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
