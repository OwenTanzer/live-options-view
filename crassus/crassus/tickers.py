"""Reads for the public, unauthenticated intraday tickers board.

`stat_arb_qqq_smh` needs a price for SMH -- a ticker that never appears in
the option-chain snapshot (`market.py`), which only ever covers QQQ's own
chain and underlying. This is a second, independent source published by the
same collector into the same R2 bucket (see `config.TICKERS_URL`): a flat
`{"prices": {"SYMBOL": {"price": ...}, ...}}` board republished on roughly
the same ~60s cadence as the option snapshot.

Kept as a separate, network-aware module from the strategy's decision logic
for the same reason `trump_sentiment.py` is split from `trump_whisperer.py`:
the fetch/parse step is the part that needs a fake session or monkeypatching
to test, so it's isolated here and the strategy only ever depends on
`TickerBoardReader.read()` returning a `TickerBoard` or raising
`TickerFetchError`.

`TickerBoardReader` caches on the same "don't re-fetch faster than the
source republishes" principle as `market.SnapshotReader` and
`trump_sentiment.TrumpSentimentReader` -- one board, one cache, shared by
every account running a strategy that reads it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import clock
from .config import TICKERS_URL


class TickerFetchError(RuntimeError):
    """The tickers board could not be fetched or parsed."""


@dataclass(frozen=True)
class TickerBoard:
    """One read of the tickers board."""

    fetched_at: str
    timestamp: str
    snapshot_time: str
    feed_stale: bool
    prices: dict[str, float | None] = field(default_factory=dict)

    def price(self, symbol: str) -> float | None:
        """The last-published price for `symbol`, or `None` if the ticker is
        absent from the board or was published with a null/missing price.

        Deliberately does not distinguish "ticker never existed on the
        board" from "ticker exists but its current reading is null" --
        both mean the same thing to a caller: there is no usable price for
        this cycle.
        """
        return self.prices.get(symbol)


def _parse_board(payload: dict[str, Any], fetched_at: str) -> TickerBoard:
    raw_prices = payload.get("prices")
    if not isinstance(raw_prices, dict):
        raise TickerFetchError("tickers board payload has no 'prices' object")

    prices: dict[str, float | None] = {}
    for symbol, entry in raw_prices.items():
        price = entry.get("price") if isinstance(entry, dict) else None
        prices[symbol] = float(price) if isinstance(price, (int, float)) else None

    return TickerBoard(
        fetched_at=fetched_at,
        timestamp=payload.get("timestamp", ""),
        snapshot_time=payload.get("snapshot_time", ""),
        feed_stale=bool(payload.get("feed_stale", False)),
        prices=prices,
    )


class TickerBoardReader:
    """Polls the tickers board, respecting its native ~60s cadence.

    `session` defaults to the `requests` module itself (it exposes `.get`
    at module scope, same call shape as a `requests.Session`), and is
    injectable so tests can hand in a fake with a `.get()` that returns a
    canned response -- no real network access needed to exercise this class
    or anything built on it.
    """

    def __init__(
        self,
        url: str = TICKERS_URL,
        min_interval_s: float = 60.0,
        timeout_s: float = 15.0,
        session: Any = None,
    ):
        self.url = url
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._session = session
        self._cached: TickerBoard | None = None
        self._cached_at: float = 0.0

    def _get_session(self) -> Any:
        if self._session is None:
            import requests  # noqa: PLC0415 -- optional dependency, only needed if this runs

            self._session = requests
        return self._session

    def read(self, force: bool = False) -> TickerBoard:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        session = self._get_session()
        try:
            response = session.get(self.url, timeout=self.timeout_s)
        except Exception as exc:  # requests.RequestException and friends
            raise TickerFetchError(f"request to {self.url} failed: {exc}") from exc

        status_code = getattr(response, "status_code", 200)
        if status_code != 200:
            raise TickerFetchError(f"{self.url}: HTTP {status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            raise TickerFetchError(f"malformed JSON from {self.url}: {exc}") from exc

        board = _parse_board(payload, clock.iso_utc())
        self._cached, self._cached_at = board, time.monotonic()
        return board
