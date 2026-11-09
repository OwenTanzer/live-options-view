#!/usr/bin/env python3
"""G4 isolated integrated lifecycle: real Crassus Runner -> real Worker HTTP.

The Worker is the actual backend/index.js fetch handler behind a local Node
harness with in-memory KV/R2 and a deterministic QQQ option quote origin. The
strategy is the shipped smoke_atm_roundtrip_phelps wrapper, driven by the real
Runner over a real decision ledger and durable state. No deployed service,
account, external provider or production state is used.

    python scripts/verify_paper_lifecycle.py

Sequence: register -> enter 711 -> ATM changes to 712 while 711 is held ->
Phelps defers the exit across a recreated Runner/reconnect -> the 711 sell is
released after the window -> 712 can be entered only once flat -> settle ->
roster freshness from multiple clients. Also asserts no duplicate fills or
ledger records and reports KV/R2 operation counts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
TMP = Path(tempfile.mkdtemp(prefix="lifecycle41-"))
os.environ["CRASSUS_DATA_ROOT"] = str(TMP)

import crassus.durability as durability  # noqa: E402
import crassus.audit as audit_module  # noqa: E402
import crassus.client as client_module  # noqa: E402

if sys.platform == "win32":
    # Windows cannot fsync a directory; POSIX must exercise the real helper.
    durability.sync_directory = lambda _path: None
    audit_module.sync_directory = lambda _path: None
    client_module.sync_directory = lambda _path: None

from crassus import clock  # noqa: E402
from crassus.audit import Outcome  # noqa: E402
from crassus.client import Book  # noqa: E402
from crassus.config import Account  # noqa: E402
from crassus.market import MarketSnapshot  # noqa: E402
from crassus.runner import Runner  # noqa: E402
from harness_process import assert_alive, start_harness  # noqa: E402

ET = ZoneInfo("America/New_York")
BASE = ""
KEY = "lifecycle-integration-key"
ALIAS = "Lifecycle"
USERNAME = "lifecycle_integration"
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global FAIL
    if condition:
        print(f"  [ok] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def snapshot(spot: float) -> MarketSnapshot:
    now = clock.now_et().isoformat()
    rows = [
        {"OptionSymbol": f"QQQ261109C00{strike}000", "Strike": float(strike),
         "Type": "call", "Bid": 2.4, "Ask": 2.5, "Volume": 100, "OpenInterest": 100}
        for strike in (711, 712)
    ]
    payload = {
        "timestamp": now, "snapshot_time": now, "expiration": "2026-11-09",
        "underlying_price": spot, "rows": rows,
        "snapshot_key": "intraday/20261109/lifecycle.csv",
    }
    raw = json.dumps(payload).encode()
    return MarketSnapshot.from_payload(BASE + "/intraday/latest.json", payload, raw)


def assert_balance(actual: float, expected: float) -> None:
    if actual != expected:
        raise AssertionError(f"balance {actual} != {expected}")


def positions(state) -> dict[str, int]:
    return {symbol: p.quantity for symbol, p in Book(state.trades).positions.items() if p.quantity}


def records(ledger_dir: Path) -> list[dict]:
    out = []
    for path in sorted(ledger_dir.glob("decisions-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def stats() -> dict:
    return json.loads(urllib.request.urlopen(BASE + "/__control/stats", timeout=2).read())


def roster() -> dict:
    return json.loads(urllib.request.urlopen(BASE + "/api/bots", timeout=2).read())


def main() -> int:
    global BASE
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not available")
    harness, info, BASE = start_harness(
        node, ROOT / "crassus/scripts/lifecycle_worker_harness.mjs",
    )
    try:
        account = Account(
            alias=ALIAS, username=USERNAME, password="integration-password",
            strategy_id="smoke_atm_roundtrip_phelps",
            params={"phelps_minutes": 5.0},
        )
        ledger_dir = TMP / "ledger"
        state_dir = TMP / "state"
        # Match the isolated Worker clock, independent of CI wall time.
        start = datetime(2026, 11, 9, 10, 0, tzinfo=ET)
        clock.now_et = lambda: start
        runner = Runner([account], base_url=BASE, snapshot_url=BASE + "/intraday/latest.json",
                        ledger_dir=ledger_dir, state_dir=state_dir, bot_registration_key=KEY)
        runner.startup()

        assert_alive(harness)
        # Entry at 711.
        runner._run_account(account, snapshot(711.5), "open")
        state = runner.sessions[ALIAS].me()
        check("registered and entered 711", positions(state) == {"QQQ261109C00711000": 1}, json.dumps(state.trades[-1:]))

        assert_alive(harness)
        # ATM moves to 712 while 711 is held: Phelps must defer, not stack.
        clock.now_et = lambda: start + timedelta(minutes=1)
        runner._run_account(account, snapshot(712.5), "open")
        state = runner.sessions[ALIAS].me()
        check("ATM change does not stack 712 while 711 is held",
              positions(state) == {"QQQ261109C00711000": 1}, json.dumps(positions(state)))

        assert_alive(harness)
        # Recreate the Runner: a fresh login and the same durable ledger/state
        # still defer the intended 711 exit; no duplicate fill appears.
        restarted = Runner([account], base_url=BASE, snapshot_url=BASE + "/intraday/latest.json",
                           ledger_dir=ledger_dir, state_dir=state_dir, bot_registration_key=KEY)
        restarted.startup()
        restarted._run_account(account, snapshot(712.5), "open")
        state = restarted.sessions[ALIAS].me()
        check("restart/reconnect still holds 711 and defers exit",
              positions(state) == {"QQQ261109C00711000": 1} and len(state.trades) == 1,
              json.dumps(positions(state)))

        assert_alive(harness)
        # Past the Phelps window: the held 711 sell is released.
        clock.now_et = lambda: start + timedelta(minutes=7)
        restarted._run_account(account, snapshot(712.5), "open")
        state = restarted.sessions[ALIAS].me()
        check("deferred exit sells held 711", positions(state) == {}, json.dumps(state.trades[-1:]))
        check("no 712 entry before flat",
              [(t.get("sym"), t.get("side")) for t in state.trades] ==
              [("QQQ261109C00711000", "buy"), ("QQQ261109C00711000", "sell")],
              json.dumps([(t.get("sym"), t.get("side")) for t in state.trades]))

        assert_alive(harness)
        # Now flat, the new ATM 712 can be entered.
        restarted._run_account(account, snapshot(712.5), "open")
        state = restarted.sessions[ALIAS].me()
        check("enters 712 only after 711 is flat",
              positions(state) == {"QQQ261109C00712000": 1}, json.dumps(positions(state)))

        assert_alive(harness)
        # Settlement next session: 712 is OTM at spot 400, so the book flattens.
        settle = restarted.sessions[ALIAS]._request(
            "POST", "/api/settle",
            json={"as_of": "2026-11-10", "spot_marks": {"2026-11-09": 400}},
        )
        check("settlement accepted", settle.status_code == 200, str(settle.status_code))
        state = restarted.sessions[ALIAS].me()
        check("settlement leaves no open position", positions(state) == {}, json.dumps(positions(state)))
        assert_balance(state.balance_cash, 9740.0)
        check("settled balance is exact", state.balance_cash == 9740.0, str(state.balance_cash))
        try:
            assert_balance(state.balance_cash, 9999.0)
        except AssertionError:
            check("negative control: wrong balance fails", True)
        else:
            check("negative control: wrong balance fails", False)

        assert_alive(harness)
        # Roster freshness from multiple clients; balances/positions are live.
        before = stats()
        cards = []
        for _ in range(10):
            cards.append(roster())
        after = stats()
        card = next(b for b in cards[-1]["bots"] if b["username"] == USERNAME)
        check("roster keeps fresh settled balance", card["balance_cash"] == state.balance_cash,
              f"{card['balance_cash']} vs {state.balance_cash}")
        check("roster exposes no stale holding", card["positions"] == [], json.dumps(card["positions"]))
        check("multi-client roster does not enumerate KV", stats()["lists"] == before["lists"],
              f"{before['lists']} -> {after['lists']}")

        ledger = records(ledger_dir)
        filled = [r for r in ledger if r["outcome_class"] == Outcome.FILLED]
        check("three fills recorded exactly once", len(filled) == 3, str(len(filled)))
        check("execution ids and decision ids are unique",
              len({r.get("execution_request_id") for r in filled}) == 3 and
              len({r.get("decision_id") for r in filled}) == 3,
              json.dumps([r.get("execution_request_id") for r in filled]))
        chronological = sorted(filled, key=lambda r: r.get("timestamp_utc", ""))
        check("ledger records buy 711, sell 711, buy 712 in order",
              [(r.get("decision", {}).get("action"), r.get("decision", {}).get("symbol")) for r in chronological] ==
              [("buy", "QQQ261109C00711000"), ("sell", "QQQ261109C00711000"), ("buy", "QQQ261109C00712000")],
              json.dumps([(r.get("decision", {}).get("action"), r.get("decision", {}).get("symbol")) for r in chronological]))

        print(json.dumps({
            "sequence": ["registered", "entered_711", "atm_changed_712_deferred", "restart_deferred",
                         "sold_711", "entered_712", "settled_flat", "roster_fresh"],
            "settled_balance": state.balance_cash,
            "operation_counts": {"kv_lists": after["lists"], "r2_gets": after["gets"], "r2_puts": after["puts"]},
            "ledger_records": len(ledger),
        }, indent=2))
        print(f"\nlifecycle: {'PASS' if FAIL == 0 else 'FAIL'}")
        return 1 if FAIL else 0
    finally:
        harness.terminate()
        try:
            harness.wait(timeout=5)
        except subprocess.TimeoutExpired:
            harness.kill()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
