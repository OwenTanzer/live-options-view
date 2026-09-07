#!/usr/bin/env python3
"""Prove the anomaly scanner fix: z-scoring runs on per-tick returns (not
raw price levels, which flag an ordinary trend as anomalous forever), and a
per-symbol cooldown suppresses re-flagging the same ongoing move on every
scan interval.

    python scripts/verify_anomaly.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from correlate.anomaly import _zscore_of_latest_return, run_anomaly_scanner  # noqa: E402
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
    print("\n1. _zscore_of_latest_return: a steady, gently-trending series scores near zero")
    # Each tick up by the same small, constant return -- looks anomalous
    # under a raw-price-level z-score (every level is far from the old
    # mean), but every *return* is identical, so a return-based z-score
    # should sit near zero.
    prices = [100.0 * (1.0005 ** i) for i in range(30)]
    z = _zscore_of_latest_return(prices)
    check("steady trend scores near zero on returns", z is not None and abs(z) < 0.5, z)


def scenario_genuine_jump_is_flagged() -> None:
    print("\n2. _zscore_of_latest_return: a sudden jump after a quiet series scores high")
    prices = [100.0 + (0.01 if i % 2 == 0 else -0.01) for i in range(29)]  # noisy but flat
    prices.append(prices[-1] * 1.03)  # a real 3% jump on the last tick
    z = _zscore_of_latest_return(prices)
    check("a genuine jump scores well above threshold", z is not None and abs(z) >= settings.ANOMALY_ZSCORE_THRESHOLD, z)


class FakeStorage:
    def __init__(self):
        self.unexplained = []

    def recent_headline_texts(self, symbol, since_ts):
        return []  # no headlines -- keep every flagged move "unexplained"

    def insert_unexplained_move(self, *, symbol, pct_move, zscore, volume_ratio):
        row_id = len(self.unexplained) + 1
        self.unexplained.append({"id": row_id, "symbol": symbol})
        return row_id


async def scenario_cooldown_suppresses_repeat_flags() -> None:
    print("\n3. run_anomaly_scanner: a cooldown stops the same symbol re-flagging every scan")
    storage = FakeStorage()
    tracker = PriceTracker()

    now = time.time()
    # Quiet baseline, then a jump that persists (still anomalous relative to
    # the *old* baseline on the next scan too, if cooldown didn't exist).
    for i in range(20):
        tracker.on_trade("QQQ", 100.0, 10, now - 200 + i)
    tracker.on_trade("QQQ", 103.0, 10, now)

    original_interval = settings.ANOMALY_CHECK_INTERVAL_SECS
    original_cooldown = settings.ANOMALY_COOLDOWN_SECS
    settings.ANOMALY_CHECK_INTERVAL_SECS = 0.05
    settings.ANOMALY_COOLDOWN_SECS = 10.0
    try:
        task = asyncio.create_task(run_anomaly_scanner(storage, tracker, ("QQQ",)))
        await asyncio.sleep(0.3)  # several scan intervals' worth
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        settings.ANOMALY_CHECK_INTERVAL_SECS = original_interval
        settings.ANOMALY_COOLDOWN_SECS = original_cooldown

    check("flagged at most once across several scans within the cooldown",
          len(storage.unexplained) <= 1, len(storage.unexplained))


def main() -> int:
    scenario_steady_trend_is_not_flagged()
    scenario_genuine_jump_is_flagged()
    asyncio.run(scenario_cooldown_suppresses_repeat_flags())

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
