#!/usr/bin/env python3
"""Integrated #97 lifecycle test: strategy -> Runner -> real local Worker -> ledger -> restart.

The Worker is the actual `backend/index.js` fetch handler served by a local
Node HTTP harness, with an in-memory KV/R2 and a deterministic local SPY quote
origin. No deployed Worker, account, external provider or production state is
used. The only Windows-only shim is disabling directory fsync, which this host
cannot perform; file writes and the normal JSONL ledger remain real.
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
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crassus"))
TMP = Path(tempfile.mkdtemp(prefix="sma97-integration-"))
os.environ["CRASSUS_DATA_ROOT"] = str(TMP)

# The POSIX directory fsync is unsupported on this Windows host; file writes and
# the ledger's own fsync remain enabled. This shim is test-only.
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
from crassus.config import Account  # noqa: E402
from crassus.daily_bars import DailyBar, is_trading_day, save_cached_bars  # noqa: E402
from crassus.runner import Runner  # noqa: E402
from crassus.strategies.sma200 import STRATEGY_ID  # noqa: E402
from harness_process import assert_alive, start_harness  # noqa: E402

ET = ZoneInfo("America/New_York")
BASE = ""
KEY = "sma-integration-key"
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global FAIL
    if condition:
        print(f"  [ok] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def trading_days(count: int, start: datetime) -> list[datetime]:
    out = []
    day = start.date()
    while len(out) < count:
        if is_trading_day(day):
            out.append(datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET))
        day += timedelta(days=1)
    return out


def bars(closes: list[float], start: datetime) -> list[DailyBar]:
    days = trading_days(len(closes), start)
    return [DailyBar(d.date(), float(c), float(c), "yahoo_chart_adjclose", float(c), float(c))
            for d, c in zip(days, closes)]


def next_trading_day(day) -> datetime:
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return datetime(cursor.year, cursor.month, cursor.day, 10, 0, tzinfo=ET)


def now_for(bar: DailyBar) -> datetime:
    return next_trading_day(bar.date)


def write_cache(root: Path, values: list[DailyBar], now: datetime) -> None:
    save_cached_bars(root / "state", "SPY", values, now)


def records(ledger_dir: Path) -> list[dict]:
    out = []
    for path in sorted(ledger_dir.glob("decisions-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def main() -> int:
    global BASE
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not available for the local Worker harness")
    harness, info, BASE = start_harness(
        node, ROOT / "crassus/scripts/sma200_worker_harness.mjs",
    )
    try:
        account = Account(
            alias="SMA integration", username="sma_integration",
            password="integration-password", strategy_id=STRATEGY_ID,
            params={"symbol": "SPY", "leverage": 2.0, "overnight_hold": True,
                    "bar_cache_hours": 24},
            paper_margin_limit=2.0, paper_financing_rate=0.06,
        )
        ledger_dir = TMP / "ledger"
        state_dir = TMP / "state"
        base_bars = bars([100.0] * 201, datetime(2025, 1, 2, tzinfo=ET))
        bull_bars = bars([100.0] * 201 + [101.0], datetime(2025, 1, 2, tzinfo=ET))
        bear_bars = bars([100.0] * 201 + [101.0, 99.0], datetime(2025, 1, 2, tzinfo=ET))

        now = now_for(base_bars[-1])
        clock.now_et = lambda: now
        write_cache(TMP, base_bars, now)
        runner = Runner([account], base_url=BASE, snapshot_url=BASE + "/intraday/latest.json",
                        ledger_dir=ledger_dir, state_dir=state_dir, bot_registration_key=KEY)
        runner.startup()
        assert_alive(harness)
        runner._run_account(account, None, "open")
        state = runner.sessions[account.alias].me()
        check("fresh account waits without a trade", len(state.trades) == 0, str(len(state.trades)))

        # New completed session + bullish crossover; next regular-session quote.
        now = now_for(bull_bars[-1])
        clock.now_et = lambda: now
        write_cache(TMP, bull_bars, now)
        runner._run_account(account, None, "open")
        after_buy = runner.sessions[account.alias].me()
        check("real Worker filled one 2x share entry", len(after_buy.trades) == 1, str(len(after_buy.trades)))
        check("entry is SPY shares at 2x negative cash",
              after_buy.trades[0]["sym"] == "SPY" and after_buy.trades[0]["instrument_type"] == "share"
              and after_buy.balance_cash < 0, json.dumps(after_buy.trades[0]))

        # Restart: same state/ledger, same completed session; no duplicate order.
        restarted = Runner([account], base_url=BASE, snapshot_url=BASE + "/intraday/latest.json",
                           ledger_dir=ledger_dir, state_dir=state_dir, bot_registration_key=KEY)
        restarted.startup()
        assert_alive(harness)
        restarted._run_account(account, None, "open")
        after_restart = restarted.sessions[account.alias].me()
        check("restart cannot duplicate the fill", len(after_restart.trades) == 1,
              str(len(after_restart.trades)))

        # Bearish crossover exits to cash; a final restart processes that bar.
        now = now_for(bear_bars[-1])
        clock.now_et = lambda: now
        write_cache(TMP, bear_bars, now)
        restarted._run_account(account, None, "open")
        after_sell = restarted.sessions[account.alias].me()
        check("bearish crossover sells the position", len(after_sell.trades) == 2,
              str(len(after_sell.trades)))
        check("sale returns to cash", after_sell.balance_cash > 0, str(after_sell.balance_cash))
        restarted._run_account(account, None, "open")
        final = restarted.sessions[account.alias].me()
        check("restart after exit does not duplicate the sell", len(final.trades) == 2,
              str(len(final.trades)))

        assert_alive(harness)
        reconcile = urllib.request.Request(
            BASE + "/api/bot-index/reconcile", method="POST",
            headers={"X-Bot-Registration-Key": KEY},
        )
        urllib.request.urlopen(reconcile).read()
        roster = json.loads(urllib.request.urlopen(BASE + "/api/bots").read())
        card = next((b for b in roster["bots"] if b["username"] == "sma_integration"), None)
        check("real Worker roster exposes strategy state and no open position",
              card is not None and card["strategy_state"]["target"] in ("cash", "hold")
              and card["strategy_state"]["position"] == "cash"
              and card["positions"] == [], json.dumps(card["strategy_state"] if card else None))

        ledger = records(ledger_dir)
        filled = [r for r in ledger if r["outcome_class"] == Outcome.FILLED]
        check("ledger records both fills exactly once", len(filled) == 2, str(len(filled)))
        check("ledger carries execution ids and decisions",
              all(r.get("execution_request_id") and r.get("decision") for r in filled))

        print(f"\nintegrated lifecycle: {'PASS' if FAIL == 0 else 'FAIL'} ({len(ledger)} ledger records)")
        return 1 if FAIL else 0
    finally:
        harness.terminate()
        try:
            harness.wait(timeout=5)
        except subprocess.TimeoutExpired:
            harness.kill()


if __name__ == "__main__":
    raise SystemExit(main())
