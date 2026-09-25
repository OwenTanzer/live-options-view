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

Scoring delay never shifts the observation window: resolution sleeps only
until the original endpoint and uses prices/volume inside that window.
Restart loses the in-memory price history, so unfinished pins are marked
incomplete and excluded from accuracy statistics, never reconstructed from
unrelated post-restart ticks.

MOO-170 finding 2: alongside the post-window return, a pin also records a
pre-headline baseline return (PIN_BASELINE_LOOKBACK_SECS ending at
window_start) and classifies the pin as one of "already_moving_before"
(price was already moving into the headline), "subsequent_move" (moved only
after), "no_qualifying_move", or "insufficient_evidence". This is an
observation, not a causal claim -- a headline associated with a move is not
proof it caused that move. The trade samples spanning the window are also
persisted (price_observations) so the classification can be replayed later
without depending on the in-memory ring buffer, which evicts.
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

    async def open_pin(self, headline_id: int, symbol: str, *, window_start: float, shadow: bool = False) -> None:
        """Called when a headline scores above the impact threshold for a
        watched symbol (or, with shadow=True, above SHADOW_SCORE_FLOOR only
        -- a non-posting observation kept purely for evaluation coverage).
        window_start is the headline's own ingest time, so the pin window
        actually covers the reaction to the headline instead of starting
        PIN_PRE_SECONDS late."""
        state = self._tracker.state(symbol)
        trade = state.trade_at_or_before(window_start) if state else None
        if trade is None or (window_start - trade.ts) > settings.PIN_PRE_SECONDS:
            log.info("no recent-enough price data for %s, skipping pin", symbol)
            return

        pin_id = self._storage.create_pin(
            headline_id=headline_id, symbol=symbol,
            window_start=window_start, price_before=trade.price, shadow=shadow,
        )
        asyncio.create_task(self._resolve_pin(pin_id, symbol, trade.price, window_start))

    async def _resolve_pin(self, pin_id: int, symbol: str, price_before: float, window_start: float) -> None:
        target = window_start + settings.PIN_POST_SECONDS
        remaining = target - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

        state = self._tracker.state(symbol)
        after = state.trade_at_or_before(target) if state else None
        before = state.trade_at_or_before(window_start) if state else None
        if (after is None or before is None or target - after.ts > settings.PIN_PRE_SECONDS):
            self._storage.mark_pin_incomplete(pin_id, "missing_window_price_history")
            return
        price_after = after.price
        pct_move = (price_after - price_before) / price_before * 100.0 if price_before else 0.0

        elapsed = max(1.0, target - window_start)
        baseline_rate = state.baseline_volume_rate(1800.0, window_start)
        if baseline_rate is None:
            self._storage.mark_pin_incomplete(pin_id, "missing_baseline_volume")
            return
        window_rate = sum(t.size for t in state.trades_since(window_start) if t.ts <= target) / elapsed
        volume_ratio = window_rate / baseline_rate if baseline_rate else 0.0

        confirmed = (
            abs(pct_move) >= settings.PIN_MOVE_THRESHOLD_PCT
            and volume_ratio >= settings.PIN_VOLUME_RATIO_THRESHOLD
        )

        # Pre-headline baseline: was price already moving into the headline,
        # independent of whether the post-window move confirms? An
        # observation, not a causal claim either way.
        pre_move, pre_fresh = state.return_over_interval(settings.PIN_BASELINE_LOOKBACK_SECS, window_start)
        already_moving = (
            pre_fresh and pre_move is not None and abs(pre_move) >= settings.PIN_MOVE_THRESHOLD_PCT
        )
        if already_moving:
            classification = "already_moving_before"
        elif confirmed:
            classification = "subsequent_move"
        else:
            classification = "no_qualifying_move"

        self._storage.resolve_pin(
            pin_id, price_after=price_after, pct_move=pct_move,
            volume_ratio=volume_ratio, confirmed=confirmed, window_end=target,
            classification=classification,
        )

        window_trades = [t for t in state.trades_since(window_start) if t.ts <= target]
        samples, cum = [], 0.0
        for t in window_trades:
            cum += t.size
            samples.append((t.ts, t.price, cum))
        self._storage.record_price_observations(event_type="pin", event_id=pin_id, symbol=symbol, samples=samples)

        # Approximate no-news control: the same-length window, shifted back
        # CONTROL_LOOKBACK_SECS (same time-of-day). Skipped -- left NULL,
        # never zero-filled -- if a headline was published near that
        # shifted window, since that would no longer be a no-news
        # comparison. See docs/moo170_evaluation_protocol.md.
        control_start = window_start - settings.CONTROL_LOOKBACK_SECS
        nearby_headlines = self._storage.candidate_headlines_for_match(
            symbol, control_start, settings.ANOMALY_HEADLINE_MATCH_WINDOW_SECS,
        )
        if not nearby_headlines:
            control_pct, control_fresh = state.return_over_interval(elapsed, control_start + elapsed)
            self._storage.set_pin_control(pin_id, control_pct if control_fresh else None)

        log.info(
            "pin %s resolved: %s moved %.2f%% (vol ratio %.1fx) confirmed=%s classification=%s",
            pin_id, symbol, pct_move, volume_ratio, confirmed, classification,
        )

    def recover_open_pins(self) -> int:
        """Close unfinished observations as incomplete after losing tick history.

        Called once at startup, before ingest begins. Neither expired nor
        still-open windows can be evaluated faithfully across that gap.
        """
        rows = self._storage.open_pins()
        for row in rows:
            self._storage.mark_pin_incomplete(row["id"], "restart_lost_price_history")
        if rows:
            log.info("marked %d prior-run pin(s) incomplete: price history lost", len(rows))
        return len(rows)
