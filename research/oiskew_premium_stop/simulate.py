#!/usr/bin/env python3
"""Offline replay of the 35% entry-premium stop over OI Skew's actual paper trips.

Skew + Direction step 2. Holds entries FIXED (each actual round trip, with its
own fill price as basis, including every re-entry) and asks, per trip: would a
full-position exit at the first eligible executable bid <= 65% of that basis
have fired before the actual exit, at what price, and with what overshoot?
Portfolio effects (what the bot would have done after a stop, re-entry after
cooldown) are step 4's comparison, not this replay.

The stop decision itself is made by the production function
`crassus.exits.evaluate` with `ExitPolicy(stop_loss_pct=-0.35)` -- the
simulator does not re-implement the rule.

Model (every knob explicit, printed into the output):

* Cycles: the OI Skew account's actual runner decision timestamps from the
  private ledger (5-minute cadence); for trips before the ledger began
  (2026-08-12..08-31) synthetic 300 s cycles from the entry fill.
* Eligible cycles: strictly after the entry fill and strictly before the
  actual exit decision. At the actual exit cycle the runner's order is EOD
  flatten -> exits -> strategy, so a flatten keeps its exit; a stop and a
  signal sell on the same cycle close the same full position at the same quote.
* Observed quote: the most recent collector chain snapshot (~64 s cadence) at
  or before the cycle, if no older than --max-quote-age (default 90 s). The
  snapshot carries no per-row quote time, so this is a PROXY for the runner's
  15 s live-quote rule. Older, absent or bid-less -> the exit is pending and
  retried next cycle (missing-quote handling).
* Fill: the decision bid (latency 0), or with --latency-snapshots N the bid
  N snapshots later (latency), minus --slippage-ticks x $0.01 (floored at
  $0.01). Overshoot = threshold price - fill.
* Fees are reported separately (--fee-per-contract, per exit). The stop does
  not change the number of exits per trip, so it adds no fees in this replay.

    python research/oiskew_premium_stop/simulate.py \\
      --trips round_trips.csv --quotes quote_paths.json \\
      --ledger decisions_a.jsonl --ledger decisions_b.jsonl --out out/stop35
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics as st
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

from crassus import exits  # noqa: E402
from crassus.client import Book  # noqa: E402
from crassus.market import Quote  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402

CONTRACT = 100  # premium dollars per contract per 1.00 of price


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_cycles(paths, alias="OI Skew"):
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("account_alias") == alias and r.get("timestamp_utc"):
                    out.append(ts(r["timestamp_utc"]))
    return sorted(set(out))


def quote_at(path, t, max_age_s):
    """Most recent present snapshot at or before t, if fresh enough."""
    times = [p["_t"] for p in path]
    i = bisect_right(times, t) - 1
    if i < 0:
        return None, "no_snapshot_yet", None
    p = path[i]
    age = (t - p["_t"]).total_seconds()
    if p.get("absent") or p.get("bid") in (None, 0):
        return None, "missing_liquidation_quote", i
    if age > max_age_s:
        return None, "stale_quote", i
    return p, None, i


def decide(entry_price, symbol, snap):
    """The production exit layer, fed this position's own one-lot book."""
    q = Quote(symbol=symbol, bid=snap["bid"], ask=snap["ask"],
              quote_ts=snap["_t"].replace(tzinfo=None).isoformat(),
              server_ts=snap["_t"].replace(tzinfo=None).isoformat())
    book = Book([{"sym": symbol, "side": "buy", "qty": 1, "price": entry_price,
                  "ts": "2026-01-01T00:00:00Z"}])
    ctx = StrategyContext(snapshot=None, account_state={"username": "sim"}, book=book,
                          now_et=snap["_t"], session_phase="open",
                          quotes=lambda syms: {symbol: q}, params={}, account_alias="sim")
    return exits.evaluate(ctx, exits.ExitPolicy(stop_loss_pct=-0.35), strategy_id="oi_skew_qqq")


def simulate(trips, quotes, cycles, *, max_age, latency_snaps, slip_ticks, fee):
    rows = []
    for t in trips:
        n = t["n"]
        path = [dict(p, _t=ts(p["t"])) for p in quotes[n]["path"]]
        entry, exit_ = ts(t["entry_utc"]), ts(t["exit_utc"])
        basis = float(t["entry_price"])
        level = exits.threshold_price(basis, -0.35, 1)
        trip_cycles = [c for c in cycles if entry < c < exit_ - timedelta(seconds=1)]
        source = "ledger"
        if not trip_cycles:
            if entry < ts("2026-08-31T20:48:00Z"):
                trip_cycles, k = [], 1
                while entry + timedelta(seconds=300 * k) < exit_ - timedelta(seconds=1):
                    trip_cycles.append(entry + timedelta(seconds=300 * k)); k += 1
                source = "synthetic_300s"
            else:
                source = "ledger_no_cycle_inside_hold"
        pending = []
        fired = None
        for c in trip_cycles:
            snap, why, idx = quote_at(path, c, max_age)
            if snap is None:
                pending.append({"cycle": c.isoformat(), "reason": why})
                continue
            d = decide(basis, t["symbol"], snap)
            if d is not None and d.action == "sell":
                fired = (c, snap, idx, d)
                break
        actual_exit = float(t["exit_price"])
        rec = {"n": n, "day": t["day_et"], "symbol": t["symbol"], "kind": t["kind"],
               "entry_utc": t["entry_utc"], "basis": basis, "stop_level": level,
               "actual_exit_utc": t["exit_utc"], "actual_exit_price": actual_exit,
               "actual_exit_by": t["exit_by"] or "unattributed", "actual_return_pct": float(t["return_pct"]),
               "cycles_evaluated": len(trip_cycles), "cycle_source": source,
               "pending_cycles": len(pending), "pending_reasons": sorted({p["reason"] for p in pending}),
               "stop_fired": fired is not None}
        if fired:
            c, snap, idx, d = fired
            fill_snap = snap
            if latency_snaps:
                later = [p for p in path[idx + 1:] if not p.get("absent") and p.get("bid")]
                fill_snap = later[latency_snaps - 1] if len(later) >= latency_snaps else snap
            fill = max(0.01, round(fill_snap["bid"] - 0.01 * slip_ticks, 2))
            rec.update({
                "stop_cycle_utc": c.isoformat(), "decision_bid": snap["bid"],
                "decision_quote_age_s": round((c - snap["_t"]).total_seconds(), 1),
                "fill_price": fill, "fill_quote_utc": fill_snap["_t"].isoformat(),
                "overshoot": round(level - fill, 4),
                "overshoot_pct_of_basis": round((level - fill) / basis * 100, 2),
                "stop_return_pct": round((fill - basis) / basis * 100, 2),
                "pnl_change_usd": round((fill - actual_exit) * CONTRACT, 2),
                "reason": d.reason,
            })
        if not fired:
            # Precedence check at the actual exit cycle: if the level was also
            # breached there, stop and signal/flatten close the same full
            # position on the same quote -- same outcome, different attribution.
            snap, why, _ = quote_at(path, exit_, max_age)
            rec["level_breached_at_actual_exit"] = (
                None if snap is None else exits._threshold_reached(basis, snap["bid"], 1, -0.35))
        rec["fees_usd_separate"] = round(fee * 1, 2)  # one exit either way
        rows.append(rec)
    return rows


def summarize(rows):
    fired = [r for r in rows if r["stop_fired"]]
    s = {
        "trips": len(rows), "stop_fired": len(fired),
        "actual_total_pnl_usd": round(sum((r["actual_exit_price"] - r["basis"]) * CONTRACT for r in rows), 2),
        "pnl_change_usd_total": round(sum(r["pnl_change_usd"] for r in fired), 2),
        "trips_with_pending_cycles": sum(1 for r in rows if r["pending_cycles"]),
        "pending_cycles_total": sum(r["pending_cycles"] for r in rows),
    }
    if fired:
        s.update({
            "helped": sum(1 for r in fired if r["pnl_change_usd"] > 0),
            "hurt": sum(1 for r in fired if r["pnl_change_usd"] < 0),
            "unchanged": sum(1 for r in fired if r["pnl_change_usd"] == 0),
            "winners_cut_short": [r["n"] for r in fired if r["actual_return_pct"] > 0],
            "overshoot_pct_of_basis": {"median": st.median(r["overshoot_pct_of_basis"] for r in fired),
                                       "max": max(r["overshoot_pct_of_basis"] for r in fired)},
            "stop_return_pct": {"median": st.median(r["stop_return_pct"] for r in fired),
                                "min": min(r["stop_return_pct"] for r in fired)},
            "actual_return_pct_of_stopped": {"median": st.median(r["actual_return_pct"] for r in fired),
                                             "min": min(r["actual_return_pct"] for r in fired)},
        })
    if fired:
        changes = sorted((r["pnl_change_usd"] for r in fired), reverse=True)
        s["pnl_change_without_top3_saves_usd"] = round(sum(changes[3:]), 2)
        s["pnl_change_top3_saves_usd"] = round(sum(changes[:3]), 2)
    losers = [r for r in rows if r["actual_return_pct"] <= -35 and not r["stop_fired"]]
    s["losers_le_35pct_not_fired"] = {
        "count": len(losers),
        "level_breached_only_at_actual_exit_cycle": sum(1 for r in losers if r.get("level_breached_at_actual_exit")),
        "level_not_breached_on_snapshot_at_exit": [r["n"] for r in losers if r.get("level_breached_at_actual_exit") is False],
        "no_quote_at_exit": [r["n"] for r in losers if r.get("level_breached_at_actual_exit") is None],
    }
    top = sorted(rows, key=lambda r: r["actual_return_pct"], reverse=True)[:5]
    s["largest_winners"] = [{"n": r["n"], "actual_return_pct": r["actual_return_pct"],
                             "stop_fired": r["stop_fired"]} for r in top]
    s["actual_exit_by_of_stopped"] = {k: sum(1 for r in fired if r["actual_exit_by"] == k)
                                      for k in sorted({r["actual_exit_by"] for r in fired})}
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trips", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--ledger", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-quote-age", type=float, default=90.0)
    ap.add_argument("--fee-per-contract", type=float, default=0.0)
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        raise SystemExit(f"{out} exists; outputs are never overwritten")
    trips = list(csv.DictReader(open(a.trips, encoding="utf-8")))
    quotes = json.loads(Path(a.quotes).read_text())
    cycles = load_cycles(a.ledger)
    scenarios = {}
    for name, lat, slip in (("base_decision_bid", 0, 0), ("slip_1tick", 0, 1),
                            ("latency_1snapshot", 1, 0), ("latency_1snapshot_slip_1tick", 1, 1)):
        rows = simulate(trips, quotes, cycles, max_age=a.max_quote_age, latency_snaps=lat,
                        slip_ticks=slip, fee=a.fee_per_contract)
        scenarios[name] = {"params": {"latency_snapshots": lat, "slippage_ticks": slip,
                                      "max_quote_age_s": a.max_quote_age,
                                      "fee_per_contract_separate": a.fee_per_contract},
                           "summary": summarize(rows), "rows": rows}
    result = {
        "schema": "oiskew_premium_stop_replay.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {"stop_loss_pct": -0.35, "exit": "full position", "basis": "own entry fill incl. re-entry",
                   "decision_function": "crassus.exits.evaluate", "exits_version": exits.STRATEGY_VERSION},
        "inputs_sha256": {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                          for p in [a.trips, a.quotes, *a.ledger]},
        "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (Path(__file__), ROOT / "crassus/crassus/exits.py")},
        "scenarios": scenarios,
        "status": "offline, entries fixed; portfolio/re-entry effects are step 4",
    }
    out.mkdir(parents=True)
    (out / "replay.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    for name, sc in scenarios.items():
        print(name, json.dumps({k: v for k, v in sc["summary"].items() if k != "largest_winners"}))
    print("largest winners", scenarios["base_decision_bid"]["summary"]["largest_winners"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
