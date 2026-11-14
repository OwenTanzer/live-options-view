#!/usr/bin/env python3
"""D4 historical-feasibility inventory (read-only; computes NO arm outcome).

Measures what the assembled private inputs can and cannot support for the D4
comparison: trip/session coverage, held-contract quote cadence and spread
inside each actual holding window, ledger cycle and skew-reading cadence, and
underlying-bar coverage. It never evaluates an exit rule and prints no P&L.

    python research/oa236_protection/d4_inventory.py \\
      --trips crassus-ledger/chej-export/analysis/out/round_trips.csv \\
      --quotes crassus-ledger/premium-stop/quote_paths.json \\
      --underlying crassus-ledger/premium-stop/underlying_paths.json \\
      --ledger crassus-ledger/chej-export/oiskew_decisions_2026-08-31_to_09-02.jsonl \\
      --ledger crassus-ledger/chej-export/oiskew_decisions_railway_2026-09-03_to_11-09.jsonl \\
      --out /tmp/d4_inventory.json
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


def ts(s):
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def q(values, p):
    v = sorted(values)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def summary(values, digits=3):
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": round(min(v), digits), "median": round(median(v), digits),
            "p90": round(q(v, 0.9), digits), "max": round(max(v), digits)}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--underlying", required=True)
    ap.add_argument("--ledger", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    trips = list(csv.DictReader(open(a.trips, encoding="utf-8")))
    quotes = json.loads(Path(a.quotes).read_text())
    under = json.loads(Path(a.underlying).read_text())

    # -- trips -----------------------------------------------------------------
    days = sorted({t["day_et"] for t in trips})
    per_day = collections.Counter(t["day_et"] for t in trips)
    holds = [float(t["hold_min"]) for t in trips if t["hold_min"]]

    # -- held-contract quotes inside each actual holding window ----------------
    gaps, ask_ok, bid_ok, rows_in_hold, spread_pct, absent = [], 0, 0, 0, [], 0
    trips_with_gap_over_90 = 0
    for t in trips:
        entry, exit_ = ts(t["entry_utc"]), ts(t["exit_utc"])
        path = [p for p in quotes[t["n"]]["path"] if entry <= ts(p["t"]) <= exit_]
        times = [ts(p["t"]) for p in path if not p.get("absent")]
        g = [(b - a_).total_seconds() for a_, b in zip(times, times[1:])]
        gaps += g
        trips_with_gap_over_90 += any(x > 90 for x in g)
        for p in path:
            if p.get("absent"):
                absent += 1
                continue
            rows_in_hold += 1
            bid_ok += p.get("bid") not in (None, 0)
            ask_ok += p.get("ask") not in (None, 0)
            if p.get("bid") and p.get("ask"):
                mid = (p["bid"] + p["ask"]) / 2
                spread_pct.append((p["ask"] - p["bid"]) / mid * 100)

    # -- ledger cycles and skew readings ----------------------------------------
    cycles, skew_ok = [], 0
    for f in a.ledger:
        for line in open(f, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            m = (r.get("decision") or {}).get("metadata") or {}
            cycles.append(ts(r["timestamp_utc"]))
            skew_ok += m.get("imbalance_ratio") is not None
    cycles.sort()
    by_day = collections.defaultdict(list)
    for c in cycles:
        by_day[c.date().isoformat()].append(c)
    cyc_gaps = [(b - a_).total_seconds() for v in by_day.values() for a_, b in zip(v, v[1:])]

    # -- underlying 5-minute inputs ---------------------------------------------
    u_sessions = sorted(under)
    u_samples = [len(v) if isinstance(v, list) else len(v.get("samples", [])) for v in under.values()]

    trade_days_compact = {d.replace("-", "") for d in days}
    out = {
        "schema": "d4_inventory.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs_sha256": {"trips": sha(a.trips), "quotes": sha(a.quotes), "underlying": sha(a.underlying),
                          **{f"ledger_{i}": sha(f) for i, f in enumerate(a.ledger)}},
        "trips": {
            "round_trips": len(trips), "first_day": days[0], "last_day": days[-1],
            "sessions_with_trades": len(days), "max_trips_per_session": max(per_day.values()),
            "by_side": dict(collections.Counter(t["kind"] for t in trips)),
            "exit_by": dict(collections.Counter(t["exit_by"] or "unattributed" for t in trips)),
            "pre_settlement_fix": sum(t["pre_fix"] == "True" for t in trips),
            "hold_minutes": summary(holds, 1),
        },
        "held_quotes_inside_actual_hold": {
            "snapshot_rows": rows_in_hold, "absent_rows": absent,
            "bid_present_share": round(bid_ok / rows_in_hold, 4) if rows_in_hold else None,
            "ask_present_share": round(ask_ok / rows_in_hold, 4) if rows_in_hold else None,
            "gap_between_snapshots_s": summary(gaps, 1),
            "trips_with_a_gap_over_90s": trips_with_gap_over_90,
            "spread_pct_of_mid": summary(spread_pct, 2),
            "window_note": "paths cover entry-3min .. actual exit+3min only; nothing after the actual exit",
        },
        "ledger": {
            "records": len(cycles), "first": cycles[0].isoformat(), "last": cycles[-1].isoformat(),
            "days": len(by_day), "skew_reading_present_share": round(skew_ok / len(cycles), 4),
            "cycle_gap_s_within_day": summary(cyc_gaps, 1),
            "coverage_note": "no ledger before 2026-08-31; earlier cycles must be synthetic",
        },
        "underlying_spot_samples": {
            "sessions": len(u_sessions), "first": u_sessions[0], "last": u_sessions[-1],
            "samples_per_session": summary(u_samples, 0),
            "trade_sessions_covered": len(trade_days_compact & set(u_sessions)),
            "trade_sessions_missing": sorted(trade_days_compact - set(u_sessions)),
            "coverage_note": "built for trip days only; zero-trade sessions not assembled",
        },
    }
    Path(a.out).write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
