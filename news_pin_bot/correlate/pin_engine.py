"""Headline -> price-move correlation ("pinning").

A high-impact headline opens a pin window [-PIN_PRE_SECONDS, +PIN_POST_SECONDS]
around its ingest time. After the window closes, we check whether price
actually moved beyond threshold on above-normal volume; if so the pin is
"confirmed" and posted, tying the headline to the move it (plausibly)
caused. An unconfirmed pin is still logged -- that's the data that lets
accuracy_stats() eventually show which sources/keywords actually predict
moves and which don't.
"""
from __future__ import annotations

import asyncio
import logging
import time

from config import settings
from correlate.price_tracker import PriceTracker
from db.storage import Storage

log = logging.getLogger("correlate.pin_engine")


class PinEngine:
    def __init__(self, storage: Storage, tracker: PriceTracker):
        self._storage = storage
        self._tracker = tracker

    async def open_pin(self, headline_id: int, symbol: str) -> None:
        """Called when a headline scores above the impact threshold for a
        watched symbol. Waits for the pre-window so a "before" price exists
        even for a headline ingested at the very start of a trading burst,
        then schedules the resolve after the post-window."""
        await asyncio.sleep(settings.PIN_PRE_SECONDS)
        state = self._tracker.state(symbol)
        price_before = state.last_price() if state else None
        if price_before is None:
            log.info("no price data yet for %s, skipping pin", symbol)
            return

        window_start = time.time()
        pin_id = self._storage.create_pin(
            headline_id=headline_id, symbol=symbol,
            window_start=window_start, price_before=price_before,
        )
        asyncio.create_task(self._resolve_pin(pin_id, symbol, price_before, window_start))

    async def _resolve_pin(self, pin_id: int, symbol: str, price_before: float, window_start: float) -> None:
        await asyncio.sleep(settings.PIN_POST_SECONDS)
        state = self._tracker.state(symbol)
        if state is None:
            return
        price_after = state.last_price() or price_before
        pct_move = abs(price_after - price_before) / price_before * 100.0 if price_before else 0.0

        baseline_rate = state.baseline_volume_rate(1800.0, window_start) or 0.0
        window_rate = state.volume_since(window_start) / max(1.0, settings.PIN_POST_SECONDS)
        volume_ratio = (window_rate / baseline_rate) if baseline_rate else 0.0

        confirmed = (
            pct_move >= settings.PIN_MOVE_THRESHOLD_PCT
            and volume_ratio >= settings.PIN_VOLUME_RATIO_THRESHOLD
        )
        self._storage.resolve_pin(
            pin_id, price_after=price_after, pct_move=pct_move,
            volume_ratio=volume_ratio, confirmed=confirmed,
        )
        log.info(
            "pin %s resolved: %s moved %.2f%% (vol ratio %.1fx) confirmed=%s",
            pin_id, symbol, pct_move, volume_ratio, confirmed,
        )
