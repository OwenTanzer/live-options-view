"""Unexplained-move scanner: the reverse of pinning.

Runs a periodic sweep over every watched symbol's recent price action. If a
symbol's fixed-interval return (ANOMALY_RETURN_INTERVAL_SECS, e.g. the last
60 seconds) is unusual relative to its own recent distribution of
same-interval returns, it's flagged as an unexplained move -- catching the
harder half of this problem: leaks, dark-pool activity, or a story that
hasn't been published yet.

MOO-170 finding 3 fix: the z-score input and the reported pct_move are now
the *same* fixed interval (via SymbolState.return_over_interval), computed
fresh each scan. Previously the detector scored the latest single-tick
return while reporting movement over the entire 30-minute baseline window --
a 15-second scan could miss an intervening jump the detector never saw.

MOO-170 finding 4 fix: a nearby headline no longer blanket-suppresses a
flag. The move is always logged; a relevance-matched headline (tagged
symbol, or an explicit word-boundary keyword match for untagged wires) is
attached as advisory evidence via matched_headline_id, in either direction
(published before OR after the move -- a later arrival is reconciled by a
periodic pass over `unreconciled_unexplained_moves`). "No matching headline
observed" describes monitored-source coverage, not proof of no explanation.

A per-symbol cooldown (ANOMALY_COOLDOWN_SECS), persisted in storage rather
than an in-process dict, suppresses re-flagging the same ongoing move on
every scan interval -- including across a restart.
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time

from config import settings
from correlate.price_tracker import PriceTracker
from db.storage import Storage

log = logging.getLogger("correlate.anomaly")


def _zscore_of_latest_interval_return(tracker_state, now: float) -> tuple[float | None, float | None, bool]:
    """Returns (zscore, pct_move, is_fresh) using ONE fixed interval for
    both the score and the displayed move -- see module docstring. zscore is
    None if there isn't enough return-sample history yet (warm-up) or the
    baseline has no spread; pct_move is None only if the interval itself
    couldn't be computed at all."""
    pct_move, is_fresh = tracker_state.return_over_interval(settings.ANOMALY_RETURN_INTERVAL_SECS, now)
    if pct_move is None:
        return None, None, False
    history = tracker_state.interval_return_series(
        settings.ANOMALY_RETURN_INTERVAL_SECS,
        settings.ANOMALY_BASELINE_WINDOW_MIN * 60.0,
        now - settings.ANOMALY_RETURN_INTERVAL_SECS,
    )
    if len(history) < settings.ANOMALY_MIN_RETURN_SAMPLES:
        return None, pct_move, is_fresh
    mean = statistics.mean(history)
    stdev = statistics.pstdev(history)
    if stdev == 0:
        return None, pct_move, is_fresh
    return (pct_move - mean) / stdev, pct_move, is_fresh


def find_relevant_headline(storage: Storage, symbol: str, anomaly_ts: float) -> tuple[int, str, float] | None:
    """Looks for a headline plausibly explaining a flagged move, within
    ANOMALY_HEADLINE_MATCH_WINDOW_SECS on either side of `anomaly_ts`.
    Candidates are already ticker-tagged (via candidate_headlines_for_match,
    which filters on the stored `symbols` column) -- untagged/general-wire
    relevance is handled separately by main.py's word-boundary keyword
    match before a headline is even stored against this symbol. Prefers the
    closest-in-time candidate. Returns (headline_id, relation, timing_secs)
    or None. Coverage limit: only headlines from monitored sources/wires are
    ever considered -- absence of a match here says nothing about leaks or
    unpublished information."""
    candidates = storage.candidate_headlines_for_match(
        symbol, anomaly_ts, settings.ANOMALY_HEADLINE_MATCH_WINDOW_SECS,
    )
    if not candidates:
        return None
    best = min(candidates, key=lambda row: abs(row["published_at"] - anomaly_ts))
    timing_secs = best["published_at"] - anomaly_ts
    relation = "preceding" if timing_secs <= 0 else "following"
    return best["id"], relation, timing_secs


async def run_anomaly_scanner(storage: Storage, tracker: PriceTracker, watchlist: tuple[str, ...]) -> None:
    while True:
        await asyncio.sleep(settings.ANOMALY_CHECK_INTERVAL_SECS)
        now = time.time()
        for symbol in watchlist:
            state = tracker.state(symbol)
            if state is None:
                continue

            last_flag = storage.last_anomaly_flag_ts(symbol)
            if last_flag is not None and (now - last_flag) < settings.ANOMALY_COOLDOWN_SECS:
                continue

            zscore, pct_move, is_fresh = _zscore_of_latest_interval_return(state, now)
            if pct_move is None or not is_fresh:
                continue  # no fixed-interval read available, or it's stale -- insufficient evidence, not "no move"
            if zscore is None or abs(zscore) < settings.ANOMALY_ZSCORE_THRESHOLD:
                continue

            baseline_rate = state.baseline_volume_rate(1800.0, now)
            recent_rate = state.volume_since(now - settings.ANOMALY_CHECK_INTERVAL_SECS) / settings.ANOMALY_CHECK_INTERVAL_SECS
            if baseline_rate is None:
                volume_ratio = 0.0  # insufficient IEX volume history to baseline against; move still logged
            else:
                volume_ratio = recent_rate / baseline_rate if baseline_rate else 0.0

            row_id = storage.insert_unexplained_move(
                symbol=symbol, pct_move=pct_move, zscore=zscore, volume_ratio=volume_ratio,
            )
            storage.set_anomaly_flag_ts(symbol, now)

            match = find_relevant_headline(storage, symbol, now)
            if match is not None:
                headline_id, relation, timing_secs = match
                storage.attach_matched_headline(row_id, headline_id, relation=relation, timing_secs=timing_secs)

            window_trades = state.trades_since(now - settings.ANOMALY_RETURN_INTERVAL_SECS)
            samples, cum = [], 0.0
            for t in window_trades:
                cum += t.size
                samples.append((t.ts, t.price, cum))
            storage.record_price_observations(event_type="anomaly", event_id=row_id, symbol=symbol, samples=samples)

            log.info(
                "unexplained move #%s: %s z=%.2f move(%ss)=%.2f%% vol_ratio=%.1fx matched=%s",
                row_id, symbol, zscore, settings.ANOMALY_RETURN_INTERVAL_SECS, pct_move, volume_ratio,
                match[0] if match else None,
            )


async def run_reconciliation_pass(storage: Storage) -> None:
    """Periodically re-checks unmatched unexplained moves against
    newly-ingested headlines, so a headline that arrives *after* the move
    still gets linked -- explicitly dated as a retrospective link via
    matched_relation='following' and matched_timing_secs, per the issue's
    acceptance criterion that later-news links are never presented as if
    known at decision time."""
    while True:
        await asyncio.sleep(settings.ANOMALY_CHECK_INTERVAL_SECS * 4)
        now = time.time()
        for move in storage.unreconciled_unexplained_moves(now - settings.ANOMALY_RECONCILE_WINDOW_SECS):
            match = find_relevant_headline(storage, move["symbol"], move["ts"])
            if match is not None:
                headline_id, relation, timing_secs = match
                storage.attach_matched_headline(move["id"], headline_id, relation=relation, timing_secs=timing_secs)
                log.info("reconciled unexplained move #%s with headline #%s (%s, %+.0fs)",
                          move["id"], headline_id, relation, timing_secs)
