"""In-memory rolling price/volume state per symbol, fed by the Alpaca trade
stream. This is the shared source of truth both the pin engine (headline ->
did price actually move) and the anomaly scanner (price moved -> was there
news) read from.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Trade:
    price: float
    size: float
    ts: float


class SymbolState:
    """Ring buffer of recent trades for one symbol. `maxlen` bounds memory;
    it's a generous window (a few hours at typical print rates), not a full
    tick archive -- this bot pins short-term reactions, not history."""

    def __init__(self, maxlen: int = 20_000):
        self.trades: deque[Trade] = deque(maxlen=maxlen)

    def add(self, price: float, size: float, ts: float) -> None:
        self.trades.append(Trade(price, size, ts))

    def last_price(self) -> float | None:
        return self.trades[-1].price if self.trades else None

    def price_at_or_before(self, ts: float) -> float | None:
        """Most recent trade price at or before `ts` -- used to get the
        "before" price for a pin window without needing exact-timestamp
        alignment."""
        candidate = None
        for trade in self.trades:
            if trade.ts <= ts:
                candidate = trade.price
            else:
                break
        return candidate

    def trades_since(self, since_ts: float) -> list[Trade]:
        return [t for t in self.trades if t.ts >= since_ts]

    def volume_since(self, since_ts: float) -> float:
        return sum(t.size for t in self.trades_since(since_ts))

    def baseline_volume_rate(self, window_secs: float, before_ts: float) -> float | None:
        """Average volume-per-second over the window ending at `before_ts`
        -- the "normal" rate an anomaly check compares a recent burst
        against. None if there's not enough history yet."""
        window_start = before_ts - window_secs
        vol = sum(t.size for t in self.trades if window_start <= t.ts < before_ts)
        span = min(window_secs, before_ts - (self.trades[0].ts if self.trades else before_ts))
        if span <= 0:
            return None
        return vol / span

    def price_series(self, window_secs: float, before_ts: float) -> list[float]:
        window_start = before_ts - window_secs
        return [t.price for t in self.trades if window_start <= t.ts < before_ts]


class PriceTracker:
    def __init__(self):
        self._symbols: dict[str, SymbolState] = {}

    def on_trade(self, symbol: str, price: float, size: float, ts: float) -> None:
        self._symbols.setdefault(symbol, SymbolState()).add(price, size, ts)

    def state(self, symbol: str) -> SymbolState | None:
        return self._symbols.get(symbol)

    def symbols(self) -> list[str]:
        return list(self._symbols.keys())
