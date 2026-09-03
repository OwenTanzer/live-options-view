#!/usr/bin/env python3
"""Prove the pin-window fix: `open_pin`'s "before" price is anchored at the
headline's own ingest time (not PIN_PRE_SECONDS after it), staleness gets
rejected, `_resolve_pin` sleeps only the remaining window (not a blind full
sleep), and `recover_open_pins` reschedules pins left open by a prior run.

Hermetic: no real websockets, no real time.sleep -- PIN_POST_SECONDS is
patched small and asyncio.sleep actually runs (fast) rather than being
mocked out, so the "sleeps only what's left" behavior is exercised for
real, not just asserted about.

    python scripts/verify_pin_engine.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from correlate.pin_engine import PinEngine  # noqa: E402
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


class FakeStorage:
    """In-memory stand-in for db.storage.Storage -- same call shape, no
    SQLite file."""

    def __init__(self):
        self.pins: dict[int, dict] = {}
        self._next_id = 1

    def create_pin(self, *, headline_id, symbol, window_start, price_before):
        pin_id = self._next_id
        self._next_id += 1
        self.pins[pin_id] = {
            "id": pin_id, "headline_id": headline_id, "symbol": symbol,
            "window_start": window_start, "price_before": price_before,
            "window_end": None, "price_after": None, "pct_move": None,
            "volume_ratio": None, "confirmed": 0,
        }
        return pin_id

    def resolve_pin(self, pin_id, *, price_after, pct_move, volume_ratio, confirmed):
        row = self.pins[pin_id]
        row.update(window_end=time.time(), price_after=price_after,
                    pct_move=pct_move, volume_ratio=volume_ratio, confirmed=int(confirmed))

    def open_pins(self):
        return [row for row in self.pins.values() if row["window_end"] is None]


async def scenario_before_price_is_ingest_time_anchored() -> None:
    print("\n1. open_pin: price_before is the trade at/before ingest time, not PIN_PRE_SECONDS later")
    storage = FakeStorage()
    tracker = PriceTracker()
    engine = PinEngine(storage, tracker)

    now = time.time()
    tracker.on_trade("QQQ", 500.0, 100, now - 5.0)  # the "true" pre-headline price
    original_post = settings.PIN_POST_SECONDS
    settings.PIN_POST_SECONDS = 0.05
    try:
        await engine.open_pin(headline_id=1, symbol="QQQ")
        await asyncio.sleep(0.15)  # let the resolve task run
    finally:
        settings.PIN_POST_SECONDS = original_post

    pin = next(iter(storage.pins.values()))
    check("price_before is the pre-headline print (500.0), not a later one",
          pin["price_before"] == 500.0, pin["price_before"])
    check("window_start is ~ the headline's own ingest time, not PRE_SECONDS later",
          abs(pin["window_start"] - now) < 1.0, pin["window_start"] - now)


async def scenario_stale_price_skips_the_pin() -> None:
    print("\n2. open_pin: no trade within PIN_PRE_SECONDS of ingest time -> pin is skipped")
    storage = FakeStorage()
    tracker = PriceTracker()
    engine = PinEngine(storage, tracker)

    now = time.time()
    original_pre = settings.PIN_PRE_SECONDS
    settings.PIN_PRE_SECONDS = 5.0
    tracker.on_trade("QQQ", 500.0, 100, now - 60.0)  # far older than PIN_PRE_SECONDS
    try:
        await engine.open_pin(headline_id=1, symbol="QQQ")
    finally:
        settings.PIN_PRE_SECONDS = original_pre
    check("no pin was created for a stale price anchor", len(storage.pins) == 0, len(storage.pins))


async def scenario_resolve_sleeps_only_remaining_time() -> None:
    print("\n3. _resolve_pin: sleeps only the time remaining to window_start + PIN_POST_SECONDS")
    storage = FakeStorage()
    tracker = PriceTracker()
    engine = PinEngine(storage, tracker)

    window_start = time.time() - 0.4  # pretend the window opened 0.4s ago
    # A quiet baseline (small, steady volume) well before the window, so
    # baseline_volume_rate has something to compare the burst against.
    for i in range(20):
        tracker.on_trade("QQQ", 499.0, 10, window_start - 1700.0 + i * 80.0)
    tracker.on_trade("QQQ", 500.0, 100, window_start)
    tracker.on_trade("QQQ", 510.0, 5000, time.time())

    original_post = settings.PIN_POST_SECONDS
    settings.PIN_POST_SECONDS = 0.5  # so only ~0.1s should remain
    started = time.time()
    try:
        await engine._resolve_pin(pin_id=storage.create_pin(
            headline_id=1, symbol="QQQ", window_start=window_start, price_before=500.0,
        ), symbol="QQQ", price_before=500.0, window_start=window_start)
    finally:
        settings.PIN_POST_SECONDS = original_post
    elapsed = time.time() - started
    check("resolved in well under a full PIN_POST_SECONDS sleep", elapsed < 0.4, f"{elapsed:.3f}s")

    row = next(iter(storage.pins.values()))
    check("pin was resolved", row["window_end"] is not None, row)
    check("confirmed (moved >= threshold on volume)", row["confirmed"] == 1, row)


async def scenario_recovery_reschedules_open_pins() -> None:
    print("\n4. recover_open_pins: reschedules a pin left open by a prior run")
    storage = FakeStorage()
    tracker = PriceTracker()
    engine = PinEngine(storage, tracker)

    window_start = time.time() - 10.0  # already well past a short PIN_POST_SECONDS
    storage.create_pin(headline_id=1, symbol="QQQ", window_start=window_start, price_before=500.0)
    tracker.on_trade("QQQ", 500.0, 100, window_start)
    tracker.on_trade("QQQ", 512.0, 400, time.time())

    original_post = settings.PIN_POST_SECONDS
    settings.PIN_POST_SECONDS = 1.0  # window already elapsed -> should resolve ~immediately
    try:
        n = engine.recover_open_pins()
        check("recovered exactly one open pin", n == 1, n)
        await asyncio.sleep(0.1)
    finally:
        settings.PIN_POST_SECONDS = original_post

    row = next(iter(storage.pins.values()))
    check("recovered pin resolved without waiting a fresh full window",
          row["window_end"] is not None, row)


def main() -> int:
    for scenario in (
        scenario_before_price_is_ingest_time_anchored,
        scenario_stale_price_skips_the_pin,
        scenario_resolve_sleeps_only_remaining_time,
        scenario_recovery_reschedules_open_pins,
    ):
        asyncio.run(scenario())

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
