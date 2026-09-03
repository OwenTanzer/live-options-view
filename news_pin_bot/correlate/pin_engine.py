"""Headline -> price-move correlation ("pinning").

A high-impact headline opens a pin window [window_start, window_start +
PIN_POST_SECONDS], where window_start is the headline's own ingest time --
NOT a time PIN_PRE_SECONDS after it. `price_before` is read from history
already in the tracker (via `price_at_or_before`), so the window actually
starts at the headline, catching the immediate reaction instead of missing
it. PIN_PRE_SECONDS bounds how stale that "before" print is allowed to be:
if the most recent trade at/before window_start is older than that, there's
no reliable anchor yet (e.g. right after startup) and the pin is skipped.

After the window closes, we check whether price actually moved beyond
threshold on above-normal volume; if so the pin is "confirmed" and posted,
tying the headline to the move it (plausibly) caused. An unconfirmed pin is
still logged -- that's the data that lets accuracy_stats() eventually show
which sources/keywords actually predict moves and which don't.

`_resolve_pin` sleeps only the *remaining* time to `window_start +
PIN_POST_SECONDS` rather than a blind full-length sleep, so the same method
serves both a freshly-opened pin and one recovered from storage after a
restart (see `recover_open_pins`), where part of the window may have
already elapsed while the process was down.
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
        watched symbol. window_start is the headline's own ingest time, so
        the pin window actually covers the reaction to the headline instead
        of starting PIN_PRE_SECONDS late."""
        window_start = time.time()
        state = self._tracker.state(symbol)
        trade = state.trade_at_or_before(window_start) if state else None
        if trade is None or (window_start - trade.ts) > settings.PIN_PRE_SECONDS:
            log.info("no recent-enough price data for %s, skipping pin", symbol)
            return

        pin_id = self._storage.create_pin(
            headline_id=headline_id, symbol=symbol,
            window_start=window_start, price_before=trade.price,
        )
        asyncio.create_task(self._resolve_pin(pin_id, symbol, trade.price, window_start))

    async def _resolve_pin(self, pin_id: int, symbol: str, price_before: float, window_start: float) -> None:
        target = window_start + settings.PIN_POST_SECONDS
        remaining = target - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

        state = self._tracker.state(symbol)
        if state is None:
            return
        price_after = state.last_price() or price_before
        pct_move = abs(price_after - price_before) / price_before * 100.0 if price_before else 0.0

        elapsed = max(1.0, time.time() - window_start)
        baseline_rate = state.baseline_volume_rate(1800.0, window_start) or 0.0
        window_rate = state.volume_since(window_start) / elapsed
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

    def recover_open_pins(self) -> int:
        """Reschedules every pin left with window_end IS NULL from a prior
        run -- otherwise a restart silently loses every in-flight pin
        (Storage.open_pins() existed but nothing ever called it). Each
        recovered pin resolves after only its remaining time, or immediately
        if the window already elapsed while the process was down."""
        rows = self._storage.open_pins()
        for row in rows:
            asyncio.create_task(
                self._resolve_pin(row["id"], row["symbol"], row["price_before"], row["window_start"])
            )
        if rows:
            log.info("recovered %d open pin(s) from a prior run", len(rows))
        return len(rows)
