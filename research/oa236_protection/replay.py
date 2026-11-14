#!/usr/bin/env python3
"""Offline OA-236 protection replay over OI Skew's actual paper trips (entries fixed).

For each actual round trip, from its own fill:
  * QQQ 5-minute bars are built from the collector chain snapshots' spot
    samples (~64 s cadence; first row's UnderlyingPrice). High/low therefore
    come from ~5 samples per bar -- a PROXY for true bar extremes.
  * The OA-229 challenger (directional-move-lab research/oa229-direction-v1,
    unchanged, imported by path) labels each completed bar for the held side.
  * crassus.profit_protection.ProtectionChain (oa236-protect-v1) decides at
    each completed bar strictly before the actual exit, using the held
    contract's snapshot bid at the bar close (<= --max-quote-age s old).
The protection exit replaces the actual exit only if it comes first; fills at
that bid (or one snapshot later for the latency scenario). Re-entry economics
need quotes for a NEW contract after the exit and belong to step 4; here we
only count the qualifying re-entry opportunities (confirmed recovery with the
original skew still valid) inside the actual holding window.

    python research/oa236_protection/replay.py --trips round_trips.csv \\
      --quotes quote_paths.json --underlying underlying_paths.json \\
      --ledger a.jsonl --ledger b.jsonl --dml path/to/directional-move-lab --out out_dir
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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
from crassus.profit_protection import Observation, ProtectConfig, ProtectionChain  # noqa: E402

ET = ZoneInfo("America/New_York")
CONTRACT = 100


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def build_bars(day, samples, Bar, source_digest):
    """Completed 5-minute RTH bars from spot samples; empty buckets are gaps."""
    d = datetime.strptime(day, "%Y-%m-%d")
    open_ = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(timezone.utc)
    close_ = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
    buckets = {}
    for s in samples:
        t = ts(s["t"])
        if not open_ <= t < close_:
            continue
        k = int((t - open_).total_seconds() // 300)
        buckets.setdefault(k, []).append((t, s["spot"]))
    bars = []
    for k in sorted(buckets):
        pts = sorted(buckets[k])
        px = [p for _, p in pts]
        start = open_ + timedelta(minutes=5 * k)
        bars.append(Bar(instrument="QQQ", session=day, event_start=start, event_end=start + timedelta(minutes=5),
                        available_at=start + timedelta(minutes=5), session_open=open_, session_close=close_,
                        open=px[0], high=max(px), low=min(px), close=px[-1], volume=0.0,
                        source="collector-snapshot-spot", source_digest=source_digest,
                        quality_flags=("availability_assumed", "extremes_from_samples")))
    return bars


def bid_at(path, t, max_age):
    times = [p["_t"] for p in path]
    i = bisect_right(times, t) - 1
    while i >= 0 and (path[i].get("absent") or not path[i].get("bid")):
        i -= 1
    if i < 0 or (t - path[i]["_t"]).total_seconds() > max_age:
        return None, None
    return path[i]["bid"], i


def skew_at(cycles, t):
    keys = [c[0] for c in cycles]
    i = bisect_right(keys, t) - 1
    return cycles[i][1] if i >= 0 else None


def run(a):
    sys.path.insert(0, str(Path(a.dml) / "research" / "oa229-direction-v1"))
    sys.path.insert(0, str(Path(a.dml) / "research" / "oa229-integrated"))
    from indexlab.replay import Bar  # noqa: E402
    from direction.adapter import ChallengerConfig, ChallengerDirection, Position  # noqa: E402

    trips = list(csv.DictReader(open(a.trips, encoding="utf-8")))
    quotes = json.loads(Path(a.quotes).read_text())
    under = json.loads(Path(a.underlying).read_text())
    under_sha = hashlib.sha256(Path(a.underlying).read_bytes()).hexdigest()
    cycles = []
    for p in a.ledger:
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            m = ((r.get("decision") or {}).get("metadata") or {})
            if r.get("account_alias") == "OI Skew" and r.get("timestamp_utc") and "imbalance_ratio" in m:
                cycles.append((ts(r["timestamp_utc"]), m.get("imbalance_ratio")))
    cycles.sort(key=lambda x: x[0])

    out = {}
    for variant_name, latency in (("decision_bid", 0), ("latency_1snapshot", 1)):
        rows = []
        for t in trips:
            day = t["day_et"]
            samples = under.get(day.replace("-", ""), [])
            bars = build_bars(day, samples, Bar, under_sha)
            entry, exit_ = ts(t["entry_utc"]), ts(t["exit_utc"])
            side = t["kind"]
            basis = float(t["entry_price"])
            path = [dict(p, _t=ts(p["t"])) for p in quotes[t["n"]]["path"]]
            rec = {"n": t["n"], "day": day, "side": side, "basis": basis,
                   "actual_exit_price": float(t["exit_price"]), "actual_return_pct": float(t["return_pct"]),
                   "actual_exit_by": t["exit_by"] or "unattributed", "bars_in_hold": 0, "phases": {},
                   "armed": False, "protect_exit": None, "pending": 0, "reentry_opportunities": 0}
            if not bars:
                rec["skipped"] = "no underlying bars for the session"
                rows.append(rec)
                continue
            # Entry reference: the last spot sample at/before the fill; an
            # entry in the first five minutes has no completed bar yet, and
            # the challenger then reports `unknown` until its scale warms up
            # (the price floor still applies). Never a later sample.
            prior = [x for x in samples if ts(x["t"]) <= entry]
            ref = prior[-1]["spot"] if prior else bars[0].open
            rec["entry_reference"] = "spot_sample_at_or_before_fill" if prior else "first_bar_open_after_fill"
            pos = Position(side, entry, ref, day)
            ch = ChallengerDirection(pos, ChallengerConfig())
            imb0 = skew_at(cycles, entry)
            sign0 = (1 if (imb0 or 0) > 0 else -1) if imb0 else (1 if side == "call" else -1)
            pc = ProtectionChain(side, 1, basis, 0, sign0, ProtectConfig(variant="protect_reentry"))
            last = None
            for j, b in enumerate(bars):
                if last is not None and b.available_at <= last.available_at:
                    continue
                r = ch.step(b, cutoff=b.available_at)
                last = b
                if r is None or b.available_at >= exit_:
                    if b.available_at >= exit_:
                        break
                    continue
                rec["bars_in_hold"] += 1
                rec["phases"][r["phase"]] = rec["phases"].get(r["phase"], 0) + 1
                bid, idx = bid_at(path, b.available_at, a.max_quote_age)
                imb = skew_at(cycles, b.available_at)
                mtc = (b.session_close - b.available_at).total_seconds() / 60
                ev = r["row_id"] if r["phase"] == "recovery" else None
                d = pc.evaluate(Observation(j, r["phase"], ev, bid, bid, imb, 0.3, mtc))
                rec["armed"] = rec["armed"] or pc.armed or d.action == "exit"
                if d.action == "pending":
                    rec["pending"] += 1
                if d.action == "reenter":
                    rec["reentry_opportunities"] += 1
                    pc.external_exit("replay_stops_after_first_reentry_opportunity")
                if d.action == "exit" and rec["protect_exit"] is None:
                    fill_i = idx
                    if latency:
                        later = [k for k in range(idx + 1, len(path)) if not path[k].get("absent") and path[k].get("bid")]
                        fill_i = later[latency - 1] if len(later) >= latency else idx
                    fill = path[fill_i]["bid"]
                    rec["protect_exit"] = {
                        "bar_end": b.available_at.isoformat(), "trigger": d.trigger, "decision_bid": bid,
                        "fill": fill, "net_pct_at_decision": d.metadata.get("net_pct"),
                        "protect_return_pct": round((fill - basis) / basis * 100, 2),
                        "pnl_change_usd": round((fill - float(t["exit_price"])) * CONTRACT, 2)}
            rows.append(rec)
        ex = [r for r in rows if r["protect_exit"]]
        s = {
            "trips": len(rows), "skipped": sum(1 for r in rows if r.get("skipped")),
            "armed": sum(1 for r in rows if r["armed"]), "protect_exits": len(ex),
            "by_trigger": {k: sum(1 for r in ex if r["protect_exit"]["trigger"] == k)
                           for k in ("direction_breakdown", "price_floor")},
            "pnl_change_usd_total": round(sum(r["protect_exit"]["pnl_change_usd"] for r in ex), 2),
            "helped": sum(1 for r in ex if r["protect_exit"]["pnl_change_usd"] > 0),
            "hurt": sum(1 for r in ex if r["protect_exit"]["pnl_change_usd"] < 0),
            "giveback_avoided_usd": round(sum(r["protect_exit"]["pnl_change_usd"] for r in ex
                                              if r["protect_exit"]["pnl_change_usd"] > 0), 2),
            "lost_tail_gains_usd": round(sum(-r["protect_exit"]["pnl_change_usd"] for r in ex
                                             if r["protect_exit"]["pnl_change_usd"] < 0), 2),
            "pending_bars": sum(r["pending"] for r in rows),
            "reentry_opportunities_inside_hold": sum(r["reentry_opportunities"] for r in rows),
            "phase_bars": {ph: sum(r["phases"].get(ph, 0) for r in rows)
                           for ph in ("continuation", "pullback", "breakdown", "recovery", "stale", "unknown")},
        }
        top = sorted(rows, key=lambda r: r["actual_return_pct"], reverse=True)[:5]
        s["largest_winners"] = [{"n": r["n"], "actual_return_pct": r["actual_return_pct"],
                                 "protect_return_pct": (r["protect_exit"] or {}).get("protect_return_pct")}
                                for r in top]
        if ex:
            s["protect_return_pct_median"] = st.median(r["protect_exit"]["protect_return_pct"] for r in ex)
        out[variant_name] = {"summary": s, "rows": rows}
    result = {"schema": "oa236_protection_replay.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "config": ProtectConfig(variant="protect_reentry").__dict__, "max_quote_age_s": a.max_quote_age,
              "inputs_sha256": {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                for p in [a.trips, a.quotes, a.underlying, *a.ledger]},
              "scenarios": out,
              "status": "offline; entries fixed; protection only; bars and bids are snapshot proxies"}
    o = Path(a.out)
    if o.exists():
        raise SystemExit(f"{o} exists")
    o.mkdir(parents=True)
    (o / "replay.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    for k, v in out.items():
        print(k, json.dumps({x: y for x, y in v["summary"].items() if x != "largest_winners"}))
    print("largest winners", out["decision_bid"]["summary"]["largest_winners"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trips", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--underlying", required=True)
    ap.add_argument("--ledger", action="append", required=True)
    ap.add_argument("--dml", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-quote-age", type=float, default=90.0)
    run(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
