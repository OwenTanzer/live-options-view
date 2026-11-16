#!/usr/bin/env python3
"""S4 handoff: reproduce one Replay trade and one Simulation trade end to end, then recover them.

    python research/options_replay/handoff_demo.py --store research/options_replay/.cache/handoff.sqlite3

1. Server process A (operator reproduction: --day 2026-11-02 --start 14:44) runs a Replay trade:
   buy 1x C684 at the recorded ask, walk bar by bar (500 ms dwell) to 15:14, sell at the recorded bid.
2. Server process B (no forced day) runs a Simulation trade on generator sim-v2, seed 20261116:
   buy 1x C433 at 09:46, walk to 10:00, sell.
3. Both processes are killed. The records are read straight from the store (no server), and the
   simulated quotes are regenerated in this process.
4. Server process C resumes both sessions from the store and reveals them, and each fill is checked
   against its raw archived row (Replay) or an independent regeneration (Simulation).

Uses a fresh store path each run. Needs R2 read credentials only if the 2026-11-02 pack and the
simulation inputs are not cached yet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from s2c_cases import Server, free_port  # noqa: E402
import simulate as SIM  # noqa: E402
from store import TrainerStore  # noqa: E402


def walk(srv, sid, v, until):
    while v["session"]["clock_s"] < until:
        time.sleep(0.55)
        code, r = srv.call("/api/advance", {"s": sid, "seconds": until - v["session"]["clock_s"], "max_bars": 1,
                                            "expected_clock": v["session"]["clock_s"]})
        if code == 200:
            v = r
    return v


def trade(srv, sid, v, contract, until, tag):
    code, b = srv.call("/api/order", {"s": sid, "side": "buy", "contract": contract, "qty": 1,
                                      "request_id": f"handoff-{tag}-buy", "expected_clock": v["session"]["clock_s"]})
    assert code == 200, b
    v = walk(srv, sid, b, until)
    code, s = srv.call("/api/order", {"s": sid, "side": "sell", "contract": contract, "qty": 1,
                                      "request_id": f"handoff-{tag}-sell", "expected_clock": v["session"]["clock_s"]})
    assert code == 200, s
    return b, s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=str(HERE / ".cache" / "handoff.sqlite3"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if Path(args.store).exists():
        raise SystemExit(f"{args.store} exists; use a fresh store path")
    out = {}

    class Opt:  # Server() reads day/start/store from an args-like object
        pass

    a = Opt(); a.store, a.day, a.start = args.store, "2026-11-02", "14:44"                # noqa: E702
    srv = Server(a, free_port())
    _, v = srv.call("/api/new", {})
    rid = v["session"]["id"]
    b, s = trade(srv, rid, v, "C684", 15 * 3600 + 14 * 60, "replay")
    out["replay"] = {"session": rid, "buy": b["request"]["ack"], "sell": s["request"]["ack"],
                     "buy_fill": b["fills"][-1], "sell_fill": s["fills"][-1], "net": s["account"]["realized_net"]}
    srv.kill()

    a2 = Opt(); a2.store, a2.day, a2.start = args.store, None, None                          # noqa: E702
    srv = Server(a2, free_port())
    _, v = srv.call("/api/new", {"mode": "simulation", "seed": 20261116})
    sid = v["session"]["id"]
    assert v["session"]["simulation"]["generator_version"] == "sim-v2", v["session"]
    b, s = trade(srv, sid, v, "C433", 10 * 3600, "sim")
    out["simulation"] = {"session": sid, "seed": 20261116, "generator": "sim-v2", "buy": b["request"]["ack"],
                         "sell": s["request"]["ack"], "buy_fill": b["fills"][-1], "sell_fill": s["fills"][-1],
                         "net": s["account"]["realized_net"]}
    srv.kill()

    st = TrainerStore(args.store)                                     # no server running
    out["recovered_from_store"] = {k: [{"trade_id": t["trade_id"], "contract": t["contract"], "status": t["status"],
                                        "net": t["net"], "fills": [(f["fill_id"], f["side"], f["price"], f["decision_clock"],
                                                                    f["decision_state_verified"]) for f in t["fills"]]}
                                       for t in st.trades(session_id=out[k]["session"], include_state=False)]
                                   for k in ("replay", "simulation")}
    st.close()
    pack = SIM.Simulator(HERE / ".cache").generate(20261116, "sim-v2")
    snap = {x["key"]: x for x in pack["snapshots"]}
    out["simulation_regenerated"] = {k: snap[k]["q"]["C433"] for k in ("sim/sim-v2/20261116/0946", "sim/sim-v2/20261116/1000")}

    srv = Server(a2, free_port())
    for k in ("replay", "simulation"):
        _, w = srv.call("/api/resume", {"s": out[k]["session"]})
        _, r = srv.call("/api/reveal", {"s": out[k]["session"]})
        rc = r["reveal"]["reconciliation"]
        out[k]["resumed_net"] = w["account"]["realized_net"]
        out[k]["revealed"] = r["reveal"]["date"] or f"simulation seed {r['reveal']['simulation']['seed']}"
        out[k]["reconciliation"] = {"gross": rc["gross_premium"], "fees": rc["fees"], "net": rc["net"],
                                    "reconciles": rc["reconciles"],
                                    "fills_match_source": [l["raw_matches"] for l in rc["lines"]]}
    srv.kill()
    text = json.dumps(out, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    for k in ("replay", "simulation"):
        o = out[k]
        print(f"{k}: session {o['session']} buy {o['buy_fill']['contract']} @ {o['buy_fill']['price']} "
              f"({o['buy_fill']['time_of_day']}, {o['buy']['fill_id']}) sell @ {o['sell_fill']['price']} "
              f"({o['sell_fill']['time_of_day']}, {o['sell']['fill_id']}) net {o['net']}; trade {o['buy']['trade_id']}; "
              f"resumed net {o['resumed_net']}; revealed {o['revealed']}; reconciles {o['reconciliation']['reconciles']}; "
              f"fills match source {o['reconciliation']['fills_match_source']}")
    print("recovered from store:", json.dumps(out["recovered_from_store"]))
    print("simulation regenerated C433:", out["simulation_regenerated"])
    ok = all(out[k]["reconciliation"]["reconciles"] and all(out[k]["reconciliation"]["fills_match_source"])
             and out[k]["resumed_net"] == out[k]["net"] for k in ("replay", "simulation"))
    print("HANDOFF DEMO", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
