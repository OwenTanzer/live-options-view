#!/usr/bin/env python3
"""Count reserved S3c held-out sessions without looking at them.

Answers one question: how many collector sessions dated on or after the held-out start
(s3c_protocol.json split.held_out.from, 2026-11-16) exist per symbol, against the 10 required
before `evaluate_s3c.py --set held_out` may run.

It reads R2 *listings only* (snapshot file names and their timestamps). It never downloads a
snapshot body, so no quote, price, spread or parity value from a reserved session is seen, and
nothing here can inform a model choice. It does not write to s3c_attempts.jsonl and does not use
any of the six held-out evaluations.

The gate applied is the listing stage of qualify.py with its default thresholds (a weekday,
non-holiday session whose snapshots quote >= 90% of the regular session with no gap > 600 s).
The full Replay gates (sampled parity check, per-grid-point age/frozen/parity gates) run later, at
evaluation time, so "listing-eligible" is an upper bound: a day counted here can still fail them
(QQQ development: 97 of 116 listed days qualified).

    python research/options_replay/calibration/coverage_s3c.py
    python research/options_replay/calibration/coverage_s3c.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import archive as A  # noqa: E402

PROT = json.loads((HERE / "s3c_protocol.json").read_text())
HELD_FROM = date.fromisoformat(PROT["split"]["held_out"]["from"])
MIN_SESSIONS = PROT["split"]["held_out"]["min_sessions_per_symbol"]
PREFIX = {"QQQ": "intraday/{d}/", "SPY": "intraday/spy/{d}/"}


def snapshot_times(keys: list[str]) -> list[int]:
    out = []
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        if name.startswith("snapshot_") and name.endswith(".csv"):
            stamp = name[len("snapshot_"):][:6]
            if stamp.isdigit():
                out.append(int(stamp[:2]) * 3600 + int(stamp[2:4]) * 60 + int(stamp[4:6]))
    return sorted(out)


def classify(day: date, times: list[int], today: date, now_s: int,
             min_coverage: float = 0.9, max_gap: int = 600) -> tuple[str, str]:
    """(status, reason) for one calendar day, from snapshot timestamps only."""
    if day.weekday() >= 5 or A.is_holiday(day):
        return "not_session", "weekend or holiday"
    _, close_s = A.session_bounds(day)
    if day == today and now_s < close_s + 900:
        return "pending", "session not finished yet"
    if not times:
        return "missing", "no snapshots listed"
    cov = A.coverage(times, day)
    if cov["ratio"] < min_coverage or cov["max_gap_s"] > max_gap:
        return "excluded", f"listing coverage {cov['ratio']:.0%}, max gap {cov['max_gap_s']}s"
    return "eligible", f"listing coverage {cov['ratio']:.0%}, max gap {cov['max_gap_s']}s"


def count(arc: A.Archive, today: date, now_s: int) -> dict:
    report = {"held_out_from": HELD_FROM.isoformat(), "required_per_symbol": MIN_SESSIONS,
              "checked_at": datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"),
              "gate": "listing stage only (upper bound); full Replay gates run at evaluation",
              "symbols": {}}
    for symbol, prefix in PREFIX.items():
        days = []
        d = HELD_FROM
        while d <= today:
            status, reason = "not_session", "weekend or holiday"
            if d.weekday() < 5 and not A.is_holiday(d):
                keys = arc.list_keys(prefix.format(d=d.strftime("%Y%m%d")), refresh=True)
                status, reason = classify(d, snapshot_times(keys), today, now_s)
            if status != "not_session":
                days.append({"day": d.isoformat(), "status": status, "reason": reason})
            d += timedelta(days=1)
        eligible = [x["day"] for x in days if x["status"] == "eligible"]
        report["symbols"][symbol] = {"eligible": len(eligible), "remaining": max(0, MIN_SESSIONS - len(eligible)),
                                     "ready": len(eligible) >= MIN_SESSIONS,
                                     "first_ten": eligible[:MIN_SESSIONS], "days": days}
    report["ready"] = all(s["ready"] for s in report["symbols"].values())
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args()
    now = datetime.now(ZoneInfo("America/New_York"))
    report = count(A.Archive(args.cache), now.date(), now.hour * 3600 + now.minute * 60 + now.second)
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    print(f"S3c held-out coverage (sessions from {report['held_out_from']}; {MIN_SESSIONS} per symbol required)")
    print(f"gate: {report['gate']}")
    for symbol, s in report["symbols"].items():
        print(f"  {symbol}: {s['eligible']}/{MIN_SESSIONS} listing-eligible, {s['remaining']} to go")
        for x in s["days"]:
            if x["status"] != "eligible":
                print(f"    {x['day']}: {x['status']} ({x['reason']})")
    print("READY: run the held-out evaluation" if report["ready"] else "not ready: do not run --set held_out yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
