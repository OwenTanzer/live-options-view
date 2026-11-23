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
      --out /tmp/d4_inventory.json [--check-keys]

v2 adds the predeclared session denominator (every NYSE session in the
ledger window, zero-trade days included, with coverage only), the trip
strata, snapshot identifiability and, with --check-keys, a read-only HEAD
of one recorded snapshot key per session on the public collector bucket.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
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


ET = ZoneInfo("America/New_York")
LEDGER_START = "2026-08-31"
NYSE_HOLIDAYS = {date(2026, 9, 7)}             # the only NYSE holiday in the window
PUBLIC_BUCKET = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/"
#: Predeclared Tier 2 session eligibility (proposal): coverage only, never outcomes.
MIN_RTH_CYCLES, MAX_GAP_S, MAX_ERROR_SHARE = 75, 630, 0.20


def sessions_between(first, last):
    d, out = first, []
    while d <= last:
        if d.weekday() < 5 and d not in NYSE_HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--underlying", required=True)
    ap.add_argument("--ledger", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--check-keys", action="store_true",
                    help="HEAD one recorded snapshot key per session (read-only, public bucket)")
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
    rth = collections.defaultdict(list)
    holding = holding_inputs = 0
    snap_id = {"lineage_key": 0, "snapshot_timestamp_only": 0, "neither": 0}
    for f in a.ledger:
        for line in open(f, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            m = (r.get("decision") or {}).get("metadata") or {}
            t = ts(r["timestamp_utc"])
            cycles.append(t)
            skew_ok += m.get("imbalance_ratio") is not None
            lin = r.get("market_snapshot_lineage") or {}
            key = lin.get("snapshot_key") if lin.get("status") == "valid" else None
            snap_id["lineage_key" if key else "snapshot_timestamp_only"
                    if r.get("market_snapshot_timestamp") else "neither"] += 1
            if "Already holding" in (r.get("reason") or ""):
                holding += 1
                holding_inputs += all(m.get(k) is not None for k in
                                      ("imbalance_ratio", "session_start_imbalance", "band_strikes"))
            te = t.astimezone(ET)
            if te.weekday() < 5 and time(9, 30) <= te.time() <= time(16, 0):
                rth[te.date()].append((te, r.get("outcome_class"), key))
    cycles.sort()
    by_day = collections.defaultdict(list)
    for c in cycles:
        by_day[c.date().isoformat()].append(c)
    cyc_gaps = [(b - a_).total_seconds() for v in by_day.values() for a_, b in zip(v, v[1:])]

    # -- underlying 5-minute inputs ---------------------------------------------
    u_sessions = sorted(under)
    u_samples = [len(v) if isinstance(v, list) else len(v.get("samples", [])) for v in under.values()]

    trade_days_compact = {d.replace("-", "") for d in days}

    # -- predeclared session denominator (coverage only) ------------------------
    window = sessions_between(date.fromisoformat(LEDGER_START), date.fromisoformat(days[-1]))
    trips_per_day = collections.Counter(t["day_et"] for t in trips)
    session_rows, key_checks = [], {"checked": 0, "ok": 0, "failed": []}
    for d in window:
        v = sorted(rth.get(d, []))
        row = {"session": d.isoformat(), "rth_cycles": len(v)}
        if v:
            o, c = datetime.combine(d, time(9, 30), ET), datetime.combine(d, time(16, 0), ET)
            stamps = [x[0] for x in v]
            row["max_gap_s"] = round(max((b - a_).total_seconds() for a_, b in zip([o] + stamps, stamps + [c])))
            row["error_share"] = round(sum(x[1] in ("transport_error", "runner_error") for x in v) / len(v), 3)
            key = next((x[2] for x in v if x[2]), None)
            if a.check_keys and key:
                key_checks["checked"] += 1
                try:
                    req = urllib.request.Request(PUBLIC_BUCKET + key, method="HEAD",
                                                 headers={"User-Agent": "d4-inventory"})
                    key_checks["ok"] += urllib.request.urlopen(req, timeout=15).status == 200
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    key_checks["failed"].append([d.isoformat(), type(exc).__name__])
        reasons = []
        if row["rth_cycles"] < MIN_RTH_CYCLES:
            reasons.append(f"rth_cycles<{MIN_RTH_CYCLES}")
        if row.get("max_gap_s") is not None and row["max_gap_s"] > MAX_GAP_S:
            reasons.append(f"max_gap>{MAX_GAP_S}s")
        if row.get("error_share") is not None and row["error_share"] > MAX_ERROR_SHARE:
            reasons.append(f"error_share>{MAX_ERROR_SHARE}")
        row["eligible"] = not reasons
        row["exclusion"] = reasons
        row["has_actual_trips"] = trips_per_day.get(d.isoformat(), 0) > 0
        session_rows.append(row)
    eligible = {r["session"] for r in session_rows if r["eligible"]}

    def stratum(t):
        return ("pre_fix" if t["pre_fix"] == "True" else "post_fix",
                "ledger" if t["day_et"] >= LEDGER_START else "pre_ledger",
                t["exit_by"] or "unattributed")
    strata = collections.Counter("/".join(stratum(t)) for t in trips)
    paired = [t for t in trips if t["day_et"] in eligible]
    out = {
        "schema": "d4_inventory.v2",
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
        "trip_strata": dict(strata),
        "populations": {
            "descriptive_all_trips": len(trips),
            "ledger_covered_trips": sum(t["day_et"] >= LEDGER_START for t in trips),
            "paired_primary_trips_on_eligible_sessions": len(paired),
            "paired_primary_trade_sessions": len({t["day_et"] for t in paired}),
            "ledger_trips_outside_eligible_sessions": sorted(
                t["day_et"] for t in trips if t["day_et"] >= LEDGER_START and t["day_et"] not in eligible),
        },
        "session_denominator": {
            "window": [window[0].isoformat(), window[-1].isoformat()],
            "nyse_sessions": len(window), "eligible_sessions": len(eligible),
            "eligible_trade_sessions": sum(r["eligible"] and r["has_actual_trips"] for r in session_rows),
            "eligible_zero_trade_sessions": sum(r["eligible"] and not r["has_actual_trips"] for r in session_rows),
            "criteria": {"min_rth_cycles": MIN_RTH_CYCLES, "max_gap_s": MAX_GAP_S,
                         "max_error_share": MAX_ERROR_SHARE},
            "excluded": [[r["session"], r["exclusion"]] for r in session_rows if not r["eligible"]],
            "sessions": session_rows,
        },
        "snapshot_identification": snap_id,
        "holding_cycles": {"records": holding, "with_strategy_inputs": holding_inputs},
        "snapshot_key_check": key_checks if a.check_keys else "not run (use --check-keys)",
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
