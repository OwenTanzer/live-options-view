#!/usr/bin/env python3
"""Reproduce the S2c interrupted-save cases end to end and print a case-to-result table.

Runs two real server processes in turn against one isolated trainer store, with
fault injection switched on for this run only:

    python research/options_replay/s2c_cases.py --day 2026-11-02 --start 14:44 \\
        --store research/options_replay/.cache/s2c_cases.sqlite3

Process 1 places orders through: an acknowledged write, a failed write (rolled
back) and its retry, a committed write whose response is dropped and its retry,
five simultaneous duplicates, an undo and its retry, a refused late undo, and two
note revisions. Process 1 is then killed. Process 2 receives stale retries,
replays of pre-restart request ids and a stale note edit. Then the unrevealed
session is resumed, finished and revealed. Finally the store is read directly
(no server) and every case is checked against it.

R2 read credentials are needed only if the day pack is not cached yet.
--day/--start are operator-reproduction flags: whoever runs this knows the date.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from store import TrainerStore  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, args, port):
        cmd = [sys.executable, "-W", "ignore", str(HERE / "server.py"), "--port", str(port),
               "--store", args.store, "--allow-fault-injection"]
        if args.day:
            cmd += ["--day", args.day]
        if args.start:
            cmd += ["--start", args.start]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        raise SystemExit("server did not start")

    def kill(self):
        self.proc.kill()
        self.proc.wait()

    def call(self, path, body=None, fault=None):
        headers = {"Content-Type": "application/json"}
        if fault:
            headers["X-Replay-Fault"] = fault
        req = urllib.request.Request(self.base + path, None if body is None else json.dumps(body).encode(), headers)
        try:
            return 200, json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default="2026-11-02")
    ap.add_argument("--start", default="14:44")
    ap.add_argument("--store", default=str(HERE / ".cache" / "s2c_cases.sqlite3"))
    ap.add_argument("--out", default=None, help="write the full result JSON here")
    args = ap.parse_args()
    if Path(args.store).exists():
        raise SystemExit(f"{args.store} exists; use a fresh store path so the counts are this run's only")
    rows, ids = [], {}

    def case(name, expected, result, ok):
        rows.append({"case": name, "expected": expected, "result": result, "pass": bool(ok)})
        print(("PASS " if ok else "FAIL ") + name + ": " + result, flush=True)

    def order(srv, v, side, c, rid, fault=None):
        return srv.call("/api/order", {"s": sid, "side": side, "contract": c, "qty": 1, "request_id": rid,
                                       "expected_clock": v["session"]["clock_s"]}, fault)

    def counts():
        st = TrainerStore(args.store)
        try:
            db = st._db
            return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("trade", "fill", "fill_void", "request", "note_revision")}
        finally:
            st.close()

    # ---------------- process 1 ----------------
    p1 = Server(args, free_port())
    _, v = p1.call("/api/new", {})
    sid = ids["session"] = v["session"]["id"]
    atm = min((r for r in v["board"]), key=lambda r: abs(r["strike"] - (v["spot"]["now"] or 0)))["strike"]
    k = lambda d: f"{int(atm + d)}"                                                     # noqa: E731
    C0, P0, C1, C2 = "C" + k(0), "P" + k(0), "C" + k(1), "C" + k(2)

    first_clock = v["session"]["clock_s"]
    code, r = order(p1, v, "buy", C0, "s2c-ack-000001")
    ids["T1 trade (acknowledged)"] = r["request"]["ack"]["trade_id"]
    ids["T1 buy fill"] = r["request"]["ack"]["fill_id"]
    case("1 pending -> acknowledged", "fill + request id committed together; 'saved' only after commit",
         f"HTTP {code}, ack {r['request']['ack']['fill_id']}", code == 200 and not r["request"]["replayed"])
    v = r

    before = counts()
    code, r = order(p1, v, "buy", P0, "s2c-fail-00001", fault="fail_write")
    after = counts()
    case("2 failed write", "503 not saved; nothing written; ledger unchanged",
         f"HTTP {code} not_saved={r.get('not_saved')}; store unchanged={before == after}",
         code == 503 and r.get("not_saved") and before == after)
    code, r = order(p1, v, "buy", P0, "s2c-fail-00001")
    ids["T2 trade (saved on retry after failure)"] = r["request"]["ack"]["trade_id"]
    case("2b retry after failed write", "same request id now saves exactly once",
         f"HTTP {code}, fill {r['request']['ack']['fill_id']}, fills {counts()['fill']}",
         code == 200 and counts()["fill"] == 2)
    v = r

    lost = None
    try:
        order(p1, v, "sell", P0, "s2c-lost-00001", fault="drop_response")
    except (http.client.RemoteDisconnected, urllib.error.URLError, ConnectionError) as e:
        lost = type(e).__name__
    committed = counts()["fill"] == 3
    code, r = order(p1, v, "sell", P0, "s2c-lost-00001")
    ids["T2 sell fill (response lost, recovered)"] = r["request"]["ack"]["fill_id"]
    case("3 committed write, response lost", "client sees no response; retry with same id returns the original ack",
         f"client: {lost}; committed={committed}; retry replayed={r['request']['replayed']}; fills {counts()['fill']}",
         lost and committed and r["request"]["replayed"] and counts()["fill"] == 3)
    v = r

    results = []
    threads = [threading.Thread(target=lambda: results.append(order(p1, v, "buy", C1, "s2c-dup-000001")))
               for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    fids = {x[1]["request"]["ack"]["fill_id"] for x in results if x[0] == 200}
    ids["T3 trade (duplicates, later undone)"] = results[0][1]["request"]["ack"]["trade_id"]
    dup_fill = fids.pop() if len(fids) == 1 else None
    case("4 duplicate submissions (5 at once)", "one fill; all five answers carry the same fill id",
         f"{len(results)} answers, distinct fill ids {len(fids) + (1 if dup_fill else 0)}, "
         f"fresh {sum(1 for x in results if not x[1]['request']['replayed'])}, fills {counts()['fill']}",
         dup_fill and counts()["fill"] == 4 and sum(1 for x in results if not x[1]["request"]["replayed"]) == 1)
    v = next(x[1] for x in results if not x[1]["request"]["replayed"])
    code, r = order(p1, v, "buy", P0, "s2c-dup-000001")
    case("4b same id, different order", "refused, nothing written", f"HTTP {code}: {r.get('error')}",
         code == 409 and counts()["fill"] == 4)

    body = {"s": sid, "fill_id": dup_fill, "request_id": "s2c-undo-00001", "expected_clock": v["session"]["clock_s"]}
    code, r = p1.call("/api/undo", body)
    ids["T3 void (undo)"] = r["request"]["ack"]["void_id"]
    code2, r2 = p1.call("/api/undo", body)
    case("5 undo", "void record added; fill kept; ledger rebuilt; retried undo replayed",
         f"HTTP {code} void {r['request']['ack']['void_id']}; retry replayed={r2['request']['replayed']}; "
         f"fills {counts()['fill']} voids {counts()['fill_void']}",
         code == 200 and r2["request"]["replayed"] and counts()["fill"] == 4 and counts()["fill_void"] == 1)
    v = r2

    code, r = order(p1, v, "buy", C2, "s2c-late-00001")
    ids["T4 trade (late undo refused)"] = r["request"]["ack"]["trade_id"]
    late_fill = r["request"]["ack"]["fill_id"]
    pre_step_clock = r["session"]["clock_s"]
    time.sleep(0.6)
    _, v = p1.call("/api/step", {"s": sid, "expected_clock": pre_step_clock})
    code, r = p1.call("/api/undo", {"s": sid, "fill_id": late_fill, "request_id": "s2c-undo-00002",
                                    "expected_clock": v["session"]["clock_s"]})
    case("6 undo after the clock moved", "refused (no hindsight undo); no void written",
         f"HTTP {code}: {r.get('error')}", code == 409 and counts()["fill_void"] == 1)

    t1 = ids["T1 trade (acknowledged)"]
    _, n1 = p1.call("/api/note", {"trade_id": t1, "text": "Long the ATM call into the bounce.", "base_rev": 0,
                                  "request_id": "s2c-note-00001"})
    _, n2 = p1.call("/api/note", {"trade_id": t1, "text": "Long the ATM call into the bounce. Out on a lower low.",
                                  "base_rev": 1, "request_id": "s2c-note-00002"})
    ids["T1 note"] = n2["note_id"]
    _, v1 = p1.call("/api/view?s=" + sid)
    snap1 = {"clock": v1["session"]["clock_s"], "positions": v1["positions"], "account": v1["account"],
             "fills": [(f["saved"]["fill_id"], bool(f.get("voided"))) for f in v1["fills"]]}
    p1.kill()                                                    # real process death, nothing flushed by hand

    # ---------------- process 2 ----------------
    p2 = Server(args, free_port())
    code, r = p2.call("/api/step", {"s": sid, "expected_clock": pre_step_clock})
    case("7a stale step after reconnect", "refused with the current view (no double move)",
         f"HTTP {code} stale={r.get('stale')}; server clock {r['view']['session']['time_of_day']}",
         code == 409 and r.get("stale") and r["view"]["session"]["clock_s"] == snap1["clock"])
    code, r = order(p2, {"session": {"clock_s": first_clock}}, "buy", C0, "s2c-ack-000001")  # identical body
    case("7b retry of a pre-restart order after reconnect", "original ack returned; not applied again",
         f"HTTP {code} replayed={r.get('request', {}).get('replayed')} fill {r.get('request', {}).get('ack', {}).get('fill_id')}",
         code == 200 and r["request"]["replayed"] and r["request"]["ack"]["fill_id"] == ids["T1 buy fill"]
         and counts()["fill"] == 5)
    code, r = order(p2, {"session": {"clock_s": first_clock}}, "buy", P0, "s2c-ack-000001")
    case("7c same pre-restart id, changed body", "refused, not applied",
         f"HTTP {code}: {r.get('error')}", code == 409 and counts()["fill"] == 5)
    code, r = order(p2, {"session": {"clock_s": pre_step_clock}}, "buy", C0, "s2c-stale-0001")
    case("7d new order built on a stale clock", "refused as stale; nothing written",
         f"HTTP {code} stale={r.get('stale')}", code == 409 and r.get("stale") and counts()["fill"] == 5)
    code, r = p2.call("/api/note", {"trade_id": t1, "text": "Long the ATM call into the bounce.", "base_rev": 0,
                                    "request_id": "s2c-note-00001"})
    case("7e retried note revision after reconnect", "original rev 1 ack returned; no extra revision",
         f"HTTP {code} rev {r.get('rev')} replayed={r.get('replayed')}",
         code == 200 and r.get("replayed") and r.get("rev") == 1 and counts()["note_revision"] == 2)
    code, r = p2.call("/api/note", {"trade_id": t1, "text": "an edit from a stale tab", "base_rev": 1,
                                    "request_id": "s2c-note-00003"})
    case("7f stale note edit", "refused as a conflict (current rev returned); nothing overwritten",
         f"HTTP {code} conflict={r.get('conflict')} current rev {r.get('current', {}).get('rev')}",
         code == 409 and r.get("conflict") and counts()["note_revision"] == 2)

    code, w = p2.call("/api/resume", {"s": sid})
    snap2 = {"clock": w["session"]["clock_s"], "positions": w["positions"], "account": w["account"],
             "fills": [(f["saved"]["fill_id"], bool(f.get("voided"))) for f in w["fills"]]}
    leak = any(tok in json.dumps({k: w[k] for k in ("session", "board", "positions", "fills", "quotes")})
               for tok in (args.day, args.day.replace("-", ""), "intraday/"))
    case("8 unrevealed session resumes after restart", "same clock, positions, cash and fills (voids kept); date hidden",
         f"HTTP {code}; identical={snap1 == snap2}; date/source visible={leak}", code == 200 and snap1 == snap2 and not leak)
    v = w
    for c, rid in ((C0, "s2c-fin-000001"), (C2, "s2c-fin-000002")):
        time.sleep(0.05)
        code, v = order(p2, v, "sell", c, rid)
    code, rv = p2.call("/api/reveal", {"s": sid})
    rec = rv["reveal"]["reconciliation"]
    case("8b finish + reveal the resumed session", "positions closed; reveal reconciles; date shown only now",
         f"HTTP {code}; net {rec['net']} reconciles={rec['reconciles']}; revealed {rv['reveal']['weekday']}",
         code == 200 and rec["reconciles"] and v["positions"] == [])
    p2.kill()

    # ---------------- independent retrieval (no server) ----------------
    st = TrainerStore(args.store)
    trades = st.trades(session_id=sid, include_state=False)
    with st._lock:
        per_req = st._db.execute("SELECT request_id, kind FROM request ORDER BY created_at").fetchall()
    st.close()
    final = [{"trade_id": t["trade_id"], "contract": t["contract"], "status": t["status"], "net": t["net"],
              "fills": [(f["fill_id"], f["side"], f["price"], f["voided"], f["decision_state_verified"])
                        for f in t["fills"]],
              "note_revs": [r["rev"] for r in t["note"]["revisions"]] if t["note"] else []} for t in trades]
    once = len(per_req) == len({r["request_id"] for r in per_req})
    case("9 independent retrieval", "each request id once; intended final state; hashes verify",
         f"{len(trades)} trades {[t['status'] for t in trades]}; {len(per_req)} request ids unique={once}; "
         f"all decision states verified={all(f[4] for t in final for f in t['fills'])}",
         once and [t["status"] for t in trades] == ["closed", "closed", "voided", "closed"]
         and all(f[4] for t in final for f in t["fills"]))
    out = {"store": str(Path(args.store).resolve()), "session": sid, "cases": rows, "ids": ids,
           "final_records": final, "request_log": [tuple(r) for r in per_req]}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
    print("\n| # | Case | Expected | Result | |\n|---|---|---|---|---|")
    for i, r in enumerate(rows):
        print(f"| {i + 1} | {r['case']} | {r['expected']} | {r['result']} | {'PASS' if r['pass'] else 'FAIL'} |")
    print("\nRecord IDs:")
    for k_, v_ in ids.items():
        if not k_.startswith("_"):
            print(f"  {k_}: {v_}")
    for t in final:
        print(f"  {t['trade_id']} {t['contract']} {t['status']} net {t['net']} fills "
              f"{[(f[0], f[1], f[2], 'VOID' if f[3] else '') for f in t['fills']]} note revs {t['note_revs']}")
    return 0 if all(r["pass"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
