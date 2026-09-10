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

from config import settings


@dataclass
class Trade:
    price: float
    size: float
    ts: float  # exchange/provider trade time -- the clock all windows key off
    received_at: float = 0.0  # local receipt time, kept only for latency/backlog visibility


class SymbolState:
    """Ring buffer of recent trades for one symbol. `maxlen` bounds memory;
    it's a generous window (a few hours at typical print rates), not a full
    tick archive -- this bot pins short-term reactions, not history."""

    def __init__(self, maxlen: int = 20_000):
        self.trades: deque[Trade] = deque(maxlen=maxlen)

    def add(self, price: float, size: float, ts: float, received_at: float | None = None) -> None:
        self.trades.append(Trade(price, size, ts, received_at if received_at is not None else ts))

    def last_price(self) -> float | None:
        return self.trades[-1].price if self.trades else None

    def price_at_or_before(self, ts: float) -> float | None:
        """Most recent trade price at or before `ts` -- used to get the
        "before" price for a pin window without needing exact-timestamp
        alignment."""
        trade = self.trade_at_or_before(ts)
        return trade.price if trade else None

    def trade_at_or_before(self, ts: float) -> Trade | None:
        """Most recent Trade at or before `ts`, or None if every known trade
        is after `ts` (or there's no history at all). Scanned from the newest
        end since the common caller wants a price "just now" or a few
        seconds/minutes back, not one from hours ago."""
        for trade in reversed(self.trades):
            if trade.ts <= ts:
                return trade
        return None

    def trades_since(self, since_ts: float) -> list[Trade]:
        return [t for t in self.trades if t.ts >= since_ts]

    def volume_since(self, since_ts: float) -> float:
        return sum(t.size for t in self.trades_since(since_ts))

    def baseline_volume_rate(self, window_secs: float, before_ts: float) -> float | None:
        """Average volume-per-second over the window ending at `before_ts`
        -- the "normal" rate an anomaly check compares a recent burst
        against. None if there isn't enough *coverage* of that window yet:
        a short buffer/recent warm-up would otherwise still produce a
        precise-looking rate computed off a much shorter span than
        requested (MOO-170 finding 5) -- both a minimum fraction of the
        window and a minimum trade count are required."""
        window_start = before_ts - window_secs
        trades_in_window = [t for t in self.trades if window_start <= t.ts < before_ts]
        span = min(window_secs, before_ts - (self.trades[0].ts if self.trades else before_ts))
        if span < window_secs * settings.VOLUME_MIN_COVERAGE_FRACTION:
            return None
        if len(trades_in_window) < settings.VOLUME_MIN_TRADE_COUNT:
            return None
        vol = sum(t.size for t in trades_in_window)
        return vol / span

    def price_series(self, window_secs: float, before_ts: float) -> list[float]:
        window_start = before_ts - window_secs
        return [t.price for t in self.trades if window_start <= t.ts < before_ts]

    def return_over_interval(self, interval_secs: float, now: float) -> tuple[float | None, bool]:
        """Percent return over a fixed [now - interval_secs, now] window,
        using the same trade-anchoring logic as everywhere else
        (`trade_at_or_before`), plus a freshness check on the "now" end.

        Returns (pct_return, is_fresh). pct_return is None if either
        endpoint has no trade to anchor to. is_fresh is False if the most
        recent trade at/before `now` is older than
        ANOMALY_MAX_STALENESS_SECS -- callers should treat a stale read as
        insufficient evidence rather than a confident number off an old
        print. This is the single interval used both to z-score "is this
        unusual" and to report the displayed move, so the two can never
        silently refer to different spans (MOO-170 finding 3)."""
        latest = self.trade_at_or_before(now)
        earliest = self.trade_at_or_before(now - interval_secs)
        if latest is None or earliest is None or not earliest.price:
            return None, False
        pct_return = (latest.price - earliest.price) / earliest.price * 100.0
        is_fresh = (now - latest.ts) <= settings.ANOMALY_MAX_STALENESS_SECS
        return pct_return, is_fresh

    def interval_return_series(self, interval_secs: float, lookback_secs: float, now: float) -> list[float]:
        """A rolling series of fixed-`interval_secs` returns, one per trade
        timestamp inside [now - lookback_secs, now], each computed against
        the trade `interval_secs` earlier. This is the distribution a
        single latest interval-return is z-scored against -- built from the
        same fixed interval as the score itself, not per-tick deltas."""
        window_start = now - lookback_secs
        sample_times = [t.ts for t in self.trades if window_start <= t.ts <= now]
        out = []
        for ts in sample_times:
            pct_return, _ = self.return_over_interval(interval_secs, ts)
            if pct_return is not None:
                out.append(pct_return)
        return out


class PriceTracker:
    def __init__(self):
        self._symbols: dict[str, SymbolState] = {}

    def on_trade(self, symbol: str, price: float, size: float, ts: float, received_at: float | None = None) -> None:
        self._symbols.setdefault(symbol, SymbolState()).add(price, size, ts, received_at)

    def state(self, symbol: str) -> SymbolState | None:
        return self._symbols.get(symbol)

    def symbols(self) -> list[str]:
        return list(self._symbols.keys())
