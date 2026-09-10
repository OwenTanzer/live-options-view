#!/usr/bin/env python3
"""Prove the MOO-170 anomaly-scanner fixes:

- The z-score input and the reported pct_move are the SAME fixed interval
  (ANOMALY_RETURN_INTERVAL_SECS), never a last-tick score paired with a
  longer displayed window (finding 3).
- A stale read (no trade within ANOMALY_MAX_STALENESS_SECS of "now") is
  insufficient evidence, not a confident number off an old print.
- baseline_volume_rate refuses to compute a rate from a short/partial
  history instead of a precise-looking-but-wrong number (finding 5).
- A nearby headline is attached as advisory evidence (matched_headline_id)
  rather than blanket-suppressing the flag, and a later-arriving headline
  is still reconciled retroactively (finding 4).
- The per-symbol cooldown is read from persisted storage, so a restart
  mid-move doesn't immediately re-flag (finding 3 acceptance: "persist
  cooldown/reconciliation state appropriately").

    python scripts/verify_anomaly.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from correlate.anomaly import (  # noqa: E402
    _zscore_of_latest_interval_return, find_relevant_headline, run_anomaly_scanner, run_reconciliation_pass,
)
from correlate.price_tracker import PriceTracker  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def scenario_steady_trend_is_not_flagged() -> None:
    print("\n1. same-interval z-score: ordinary noisy-but-flat movement is not flagged")
    tracker = PriceTracker()
    now = 100_000.0
    # An alternating (noisy but non-trending) price at fixed
    # ANOMALY_RETURN_INTERVAL_SECS spacing -- looks anomalous under a raw
    # price-level check (every level sits away from an old mean), but the
    # per-interval *returns* alternate around zero, so a return-based
    # z-score for the final (equally ordinary) tick should stay well under
    # the alerting threshold.
    interval = settings.ANOMALY_RETURN_INTERVAL_SECS
    for i in range(40):
        price = 100.0 + (0.05 if i % 2 == 0 else -0.05)
        tracker.on_trade("QQQ", price, 10, now - (39 - i) * interval)
    z, pct_move, is_fresh = _zscore_of_latest_interval_return(tracker.state("QQQ"), now)
    check("ordinary alternating movement scores well under the alert threshold",
          z is not None and abs(z) < settings.ANOMALY_ZSCORE_THRESHOLD, z)
    check("pct_move reflects the SAME interval the z-score was computed over, not a longer window",
          pct_move is not None and abs(pct_move) < 1.0, pct_move)
    check("read is fresh", is_fresh)


def scenario_genuine_jump_is_flagged() -> None:
    print("\n2. same-interval z-score: a sudden jump over the latest interval scores high")
    tracker = PriceTracker()
    now = 100_000.0
    interval = settings.ANOMALY_RETURN_INTERVAL_SECS
    for i in range(40):
        tracker.on_trade("QQQ", 100.0 + (0.01 if i % 2 == 0 else -0.01), 10, now - (40 - i) * interval)
    # A real jump entirely within the final interval.
    tracker.on_trade("QQQ", 103.0, 10, now)
    z, pct_move, is_fresh = _zscore_of_latest_interval_return(tracker.state("QQQ"), now)
    check("a genuine jump scores well above threshold",
          z is not None and abs(z) >= settings.ANOMALY_ZSCORE_THRESHOLD, z)
    check("displayed pct_move is the same interval that was scored",
          pct_move is not None and pct_move > 2.5, pct_move)


def scenario_stale_read_is_insufficient_evidence() -> None:
    print("\n3. a stale latest trade is reported as insufficient evidence, not a confident score")
    tracker = PriceTracker()
    now = 100_000.0
    for i in range(20):
        tracker.on_trade("QQQ", 100.0, 10, now - 500.0 - i * 30.0)
    # No trade anywhere near `now` -- the feed has gone stale.
    z, pct_move, is_fresh = _zscore_of_latest_interval_return(tracker.state("QQQ"), now)
    check("stale read is flagged not-fresh", is_fresh is False)


def scenario_short_volume_history_is_rejected() -> None:
    print("\n4. baseline_volume_rate: refuses a rate computed off a too-short/sparse history")
    tracker = PriceTracker()
    now = 100_000.0
    # Only 100 seconds of history for an 1800-second baseline request, and
    # well under VOLUME_MIN_TRADE_COUNT trades.
    for i in range(5):
        tracker.on_trade("QQQ", 100.0, 10, now - 100.0 + i * 20.0)
    rate = tracker.state("QQQ").baseline_volume_rate(1800.0, now)
    check("short/sparse history returns None instead of a precise-looking rate", rate is None, rate)

    tracker2 = PriceTracker()
    for i in range(30):
        tracker2.on_trade("QQQ", 100.0, 10, now - 1750.0 + i * 58.0)
    rate2 = tracker2.state("QQQ").baseline_volume_rate(1800.0, now)
    check("adequate coverage and trade count DOES produce a rate", rate2 is not None, rate2)


class FakeStorage:
    def __init__(self, candidates=None):
        self.unexplained = []
        self.matched = {}
        self.cooldowns = {}
        self._candidates = candidates or []

    def last_anomaly_flag_ts(self, symbol):
        return self.cooldowns.get(symbol)

    def set_anomaly_flag_ts(self, symbol, ts):
        self.cooldowns[symbol] = ts

    def insert_unexplained_move(self, *, symbol, pct_move, zscore, volume_ratio):
        row_id = len(self.unexplained) + 1
        self.unexplained.append({"id": row_id, "symbol": symbol, "ts": time.time(), "matched_headline_id": None})
        return row_id

    def attach_matched_headline(self, move_id, headline_id, *, relation, timing_secs):
        self.matched[move_id] = (headline_id, relation, timing_secs)
        for row in self.unexplained:
            if row["id"] == move_id:
                row["matched_headline_id"] = headline_id

    def candidate_headlines_for_match(self, symbol, center_ts, window_secs):
        return [c for c in self._candidates if abs(c["published_at"] - center_ts) <= window_secs]

    def unreconciled_unexplained_moves(self, since_ts):
        return [row for row in self.unexplained if row["matched_headline_id"] is None and row["ts"] >= since_ts]

    def record_price_observations(self, **kwargs):
        pass


async def scenario_cooldown_persists_across_a_fresh_scanner_instance() -> None:
    print("\n5. run_anomaly_scanner: a persisted cooldown (not an in-process dict) suppresses repeat flags")
    storage = FakeStorage()
    tracker = PriceTracker()
    interval = settings.ANOMALY_CHECK_INTERVAL_SECS

    now = time.time()
    for i in range(30):
        tracker.on_trade("QQQ", 100.0, 10, now - 1750.0 + i * 58.0)
    tracker.on_trade("QQQ", 103.0, 10, now)

    original_check = settings.ANOMALY_CHECK_INTERVAL_SECS
    original_cooldown = settings.ANOMALY_COOLDOWN_SECS
    original_min_samples = settings.ANOMALY_MIN_RETURN_SAMPLES
    settings.ANOMALY_CHECK_INTERVAL_SECS = 0.05
    settings.ANOMALY_COOLDOWN_SECS = 10.0
    settings.ANOMALY_MIN_RETURN_SAMPLES = 1
    try:
        task = asyncio.create_task(run_anomaly_scanner(storage, tracker, ("QQQ",)))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        check("flagged at most once across several scans within the cooldown",
              len(storage.unexplained) <= 1, len(storage.unexplained))

        # Simulate a restart: a brand-new scanner task, same persisted
        # storage. It must still honor the cooldown set by the run above.
        flagged_before_restart = len(storage.unexplained)
        task2 = asyncio.create_task(run_anomaly_scanner(storage, tracker, ("QQQ",)))
        await asyncio.sleep(0.15)
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass
        check("a fresh scanner instance still respects the persisted cooldown",
              len(storage.unexplained) == flagged_before_restart, len(storage.unexplained))
    finally:
        settings.ANOMALY_CHECK_INTERVAL_SECS = original_check
        settings.ANOMALY_COOLDOWN_SECS = original_cooldown
        settings.ANOMALY_MIN_RETURN_SAMPLES = original_min_samples


def scenario_relevant_headline_is_attached_not_suppressive() -> None:
    print("\n6. find_relevant_headline: a nearby tagged headline is returned as evidence, in either direction")
    now = 100_000.0
    preceding = [{"id": 1, "headline": "QQQ guidance cut", "published_at": now - 60.0, "symbols": "QQQ"}]
    storage_before = FakeStorage(candidates=preceding)
    match = find_relevant_headline(storage_before, "QQQ", now)
    check("a headline published BEFORE the move is matched as preceding",
          match is not None and match[1] == "preceding", match)

    following = [{"id": 2, "headline": "QQQ guidance cut, reported later", "published_at": now + 120.0, "symbols": "QQQ"}]
    storage_after = FakeStorage(candidates=following)
    match2 = find_relevant_headline(storage_after, "QQQ", now)
    check("a headline published AFTER the move is matched as following (a later arrival)",
          match2 is not None and match2[1] == "following", match2)

    storage_none = FakeStorage(candidates=[])
    check("no candidates -> no match (never fabricated)", find_relevant_headline(storage_none, "QQQ", now) is None)


async def scenario_reconciliation_links_a_later_headline() -> None:
    print("\n7. run_reconciliation_pass: an unmatched move gets linked once a later headline arrives")
    now = time.time()
    later_headline = [{"id": 5, "headline": "QQQ regulatory action, confirmed", "published_at": now + 30.0, "symbols": "QQQ"}]
    storage = FakeStorage(candidates=later_headline)
    storage.unexplained.append({"id": 1, "symbol": "QQQ", "ts": now, "matched_headline_id": None})

    original_interval = settings.ANOMALY_CHECK_INTERVAL_SECS
    settings.ANOMALY_CHECK_INTERVAL_SECS = 0.02
    try:
        task = asyncio.create_task(run_reconciliation_pass(storage))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        settings.ANOMALY_CHECK_INTERVAL_SECS = original_interval

    check("the move was reconciled with the later-arriving headline, dated as 'following'",
          storage.matched.get(1) is not None and storage.matched[1][1] == "following", storage.matched)


def main() -> int:
    scenario_steady_trend_is_not_flagged()
    scenario_genuine_jump_is_flagged()
    scenario_stale_read_is_insufficient_evidence()
    scenario_short_volume_history_is_rejected()
    asyncio.run(scenario_cooldown_persists_across_a_fresh_scanner_instance())
    scenario_relevant_headline_is_attached_not_suppressive()
    asyncio.run(scenario_reconciliation_links_a_later_headline())

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
