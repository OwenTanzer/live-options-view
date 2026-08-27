"""Unexplained-move scanner: the reverse of pinning.

Runs a periodic sweep over every watched symbol's recent price action. If a
symbol has moved further/faster than its own recent baseline (z-score over
a short lookback vs its own trailing volatility) *and* no headline for that
symbol has been ingested recently, it's flagged as "moved, no news yet" --
catching the harder half of this problem: leaks, dark-pool activity, or a
story that hasn't been published yet. If a matching headline shows up
later, the caller can reconcile it against this log.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time

from config import settings
from correlate.price_tracker import PriceTracker
from db.storage import Storage

log = logging.getLogger("correlate.anomaly")


def _zscore_of_latest_move(prices: list[float]) -> float | None:
    """z-score of the most recent price relative to the mean/stdev of the
    window preceding it -- a simple, symbol-relative measure of "is this
    move unusual for this stock right now" rather than a fixed % threshold
    that would flag a normally-choppy small-cap constantly."""
    if len(prices) < 10:
        return None
    *history, latest = prices
    mean = statistics.mean(history)
    stdev = statistics.pstdev(history)
    if stdev == 0:
        return None
    return (latest - mean) / stdev


async def run_anomaly_scanner(storage: Storage, tracker: PriceTracker, watchlist: tuple[str, ...]) -> None:
    while True:
        await asyncio.sleep(settings.ANOMALY_CHECK_INTERVAL_SECS)
        now = time.time()
        for symbol in watchlist:
            state = tracker.state(symbol)
            if state is None:
                continue
            prices = state.price_series(settings.ANOMALY_BASELINE_WINDOW_MIN * 60.0, now)
            zscore = _zscore_of_latest_move(prices)
            if zscore is None or abs(zscore) < settings.ANOMALY_ZSCORE_THRESHOLD:
                continue

            recent_headlines = storage.recent_headline_texts(symbol, now - 300.0)
            if recent_headlines:
                continue  # a headline already explains this, not "unexplained"

            price_before, price_after = prices[0], prices[-1]
            pct_move = (price_after - price_before) / price_before * 100.0 if price_before else 0.0
            baseline_rate = state.baseline_volume_rate(1800.0, now) or 0.0
            recent_rate = state.volume_since(now - settings.ANOMALY_CHECK_INTERVAL_SECS) / settings.ANOMALY_CHECK_INTERVAL_SECS
            volume_ratio = (recent_rate / baseline_rate) if baseline_rate else 0.0

            row_id = storage.insert_unexplained_move(
                symbol=symbol, pct_move=pct_move, zscore=zscore, volume_ratio=volume_ratio,
            )
            log.info(
                "unexplained move #%s: %s z=%.2f move=%.2f%% vol_ratio=%.1fx, no recent headline",
                row_id, symbol, zscore, pct_move, volume_ratio,
            )
