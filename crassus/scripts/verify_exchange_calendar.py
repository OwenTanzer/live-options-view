#!/usr/bin/env python3
"""Prove exchange_calendar.session_close catches NYSE early closes beyond
the hand-rolled fallback's two hard-coded categories (day after
Thanksgiving, Christmas Eve) -- specifically the case flagged in PR review:
the hand-rolled rules had no way to know NYSE closes early on 2028-07-03
(the trading day before July 4th, a Tuesday that year), so a mandatory EOD
flatten computed against an unconditional 16:00 would open its window 3
hours late on that half day.

    python scripts/verify_exchange_calendar.py
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus import exchange_calendar as ec  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    print("1. The case flagged in review: 2028-07-03 early close")
    close = ec.session_close(date(2028, 7, 3))
    check(
        "2028-07-03 resolves to exactly NYSE's real published early close (13:00 ET), not an unconditional 16:00",
        close == time(13, 0),
        str(close),
    )

    print("\n2. Regressions: the old hand-rolled fallback's own two categories still resolve correctly via the real calendar")
    check("day after Thanksgiving 2026 (2026-11-27) is an early close", ec.session_close(date(2026, 11, 27)) == time(13, 0))
    check("Christmas Eve 2026 (2026-12-24) is an early close", ec.session_close(date(2026, 12, 24)) == time(13, 0))
    check("an ordinary trading day (2026-08-10) is a normal 16:00 close", ec.session_close(date(2026, 8, 10)) == time(16, 0))

    print("\n3. Holidays")
    check("2026-12-25 (Christmas) is a holiday", ec.is_holiday(date(2026, 12, 25)))
    check("2026-08-10 (ordinary Monday) is not a holiday", not ec.is_holiday(date(2026, 8, 10)))

    print("\n4. Fail-safe when pandas_market_calendars is unavailable: session_close is conservative, not silently wrong")
    ec._nyse_schedule_cache[2099] = None  # force the failure path without needing to uninstall the package
    check(
        "an ordinary day returns EARLY_CLOSE (conservative), not the old fallback rules' 16:00",
        ec.session_close(date(2099, 8, 10)) == ec.EARLY_CLOSE,
        "flagged in review: falling back to the known-incomplete hand-rolled rules risked silently "
        "restoring an incorrect 16:00 on an unmodeled half day -- the safe failure direction for a "
        "mandatory flatten is to open the window too early, not too late",
    )
    check(
        "a real early-close day also returns EARLY_CLOSE when the calendar is unavailable",
        ec.session_close(date(2099, 12, 24)) == ec.EARLY_CLOSE,
    )
    observed_christmas_2099 = ec._observed(date(2099, 12, 25))
    check(
        "is_holiday still uses its own hand-rolled fallback (not close-safety-critical)",
        ec.is_holiday(observed_christmas_2099),
        str(observed_christmas_2099),
    )
    del ec._nyse_schedule_cache[2099]

    print(f"\n{'=' * 66}\n{passed} passed, {failed} failed\n{'=' * 66}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
