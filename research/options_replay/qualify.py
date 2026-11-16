#!/usr/bin/env python3
"""Choose the Replay day pool: sessions with parity-consistent spot and option coverage.

Two stages, both read-only against R2:

1. Listing only: a QQQ session (NYSE calendar, weekdays, not a holiday) whose
   snapshots cover >= --min-coverage of the regular session's minute slots
   with no gap longer than --max-gap seconds.
2. Sampled bodies: ~--samples snapshots spread across the session are
   downloaded and each one's recorded UnderlyingPrice is checked against the
   put-call-parity-implied underlying of its own quotes (archive.assess). A
   day qualifies when >= --min-synced of the sampled snapshots are usable.

The full day pack is re-checked snapshot by snapshot when a session loads it
(archive.pack_qualifies), so a day that slips through sampling is still
refused at play time.

    python research/options_replay/qualify.py            # writes .cache/qualified_days.json
    python research/options_replay/qualify.py --refresh  # re-list the bucket first
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive as A  # noqa: E402


def qualify(arc: A.Archive, refresh: bool, samples: int, min_coverage: float,
            max_gap: int, min_synced: float) -> dict:
    index = arc.snapshot_index(refresh=refresh)
    days = []
    for yyyymmdd, keys in index.items():
        day = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        rec = {"day": day.isoformat(), "snapshots_listed": len(keys)}
        days.append(rec)
        if day.weekday() >= 5 or A.is_holiday(day):
            rec["verdict"], rec["reason"] = "excluded", "not a session"
            continue
        times = [A.parse_snapshot_key(k)[1] for k in keys]
        cov = A.coverage(times, day)
        rec["coverage"] = cov
        if cov["ratio"] < min_coverage or cov["max_gap_s"] > max_gap:
            rec["verdict"] = "excluded"
            rec["reason"] = f"listing coverage {cov['ratio']:.0%}, max gap {cov['max_gap_s']}s"
            continue
        open_s, close_s = A.session_bounds(day)
        in_session = [k for k, t in zip(keys, times) if open_s <= t < close_s]
        step = max(1, len(in_session) // samples)
        picked = in_session[::step][:samples]
        bodies = arc.get_many(picked)
        snaps = []
        for key, body in zip(picked, bodies):
            snaps.append({"t": A.parse_snapshot_key(key)[1], "key": key,
                          **A.parse_snapshot(body, day.isoformat())})
        A.assess(snaps)
        ok = sum(1 for s in snaps if s["ok"])
        diffs = [round(abs(s["parity_spot"] - s["spot"]), 4) for s in snaps
                 if s["parity_spot"] is not None and s["spot"] is not None]
        rec["sampled"] = len(snaps)
        rec["sampled_synced"] = ok
        rec["parity_abs_diff_max"] = max(diffs) if diffs else None
        rec["parity_abs_diff_median"] = sorted(diffs)[len(diffs) // 2] if diffs else None
        reasons: dict[str, int] = {}
        for s in snaps:
            if not s["ok"]:
                reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        rec["sample_failures"] = reasons
        if not snaps or ok / len(snaps) < min_synced:
            rec["verdict"] = "excluded"
            rec["reason"] = f"parity-consistent in {ok}/{len(snaps)} sampled snapshots"
        else:
            rec["verdict"] = "qualified"
    qualified = [d["day"] for d in days if d["verdict"] == "qualified"]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rules": {"min_coverage": min_coverage, "max_gap_s": max_gap, "samples": samples,
                  "min_synced": min_synced, "parity_tol_usd": A.PARITY_TOL_USD,
                  "max_quote_age_s": A.MAX_QUOTE_AGE_S},
        "qualified": qualified,
        "days": days,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--refresh", action="store_true", help="re-list intraday/ instead of using the cached listing")
    ap.add_argument("--samples", type=int, default=25)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--max-gap", type=int, default=600)
    ap.add_argument("--min-synced", type=float, default=0.9)
    args = ap.parse_args()
    arc = A.Archive(args.cache)
    result = qualify(arc, args.refresh, args.samples, args.min_coverage, args.max_gap, args.min_synced)
    out = Path(args.cache) / "qualified_days.json"
    out.write_text(json.dumps(result, indent=1))
    n = len(result["days"])
    print(f"{len(result['qualified'])}/{n} listed days qualified -> {out}")
    for d in result["days"]:
        if d["verdict"] != "qualified":
            print(f"  excluded {d['day']}: {d.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
