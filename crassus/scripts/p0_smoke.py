#!/usr/bin/env python3
"""P0 -- deployment smoke test.

Confirms that register / login / me / live-quotes / paper-trade actually work
against the *deployed* Worker, and that the KV namespaces they depend on are
really provisioned. This is a test of the deployment, not of the bot: it uses
a throwaway account and a single one-contract order.

Exit criterion (MOO-24): one manually initiated one-contract fill appears in
`GET /api/me`.

    python scripts/p0_smoke.py [--keep] [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from crassus import clock
from crassus.config import BASE_URL, SNAPSHOT_URL
from crassus.market import QuoteReader, SnapshotReader

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str = "") -> None:
    icon = {PASS: "✓", FAIL: "✗", WARN: "!"}[status]
    print(f"  [{icon}] {name}: {detail}" if detail else f"  [{icon}] {name}")
    results.append((name, status, detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--keep", action="store_true", help="Do not log out at the end")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"P0 deployment smoke test")
    print(f"  target : {base}")
    print(f"  time   : {clock.iso_et()} ({clock.session_phase()})\n")

    http = requests.Session()  # one cookie jar, exactly as a bot account would use
    username = f"p0_smoke_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex

    # --- 1. durable market snapshot -------------------------------------
    print("1. Market snapshot (R2)")
    snapshot = None
    try:
        snapshot = SnapshotReader(SNAPSHOT_URL).read()
        age = (
            clock.now_utc() - __import__("datetime").datetime.fromisoformat(snapshot.timestamp)
        ).total_seconds()
        report(
            "GET intraday/latest.json",
            PASS if age < 600 else WARN,
            f"{len(snapshot.rows)} rows, exp {snapshot.expiration}, "
            f"underlying {snapshot.underlying_price}, age {age:.0f}s",
        )
    except Exception as exc:
        report("GET intraday/latest.json", FAIL, str(exc))

    # --- 2. live quotes --------------------------------------------------
    print("\n2. Live quotes")
    symbol = None
    if snapshot:
        row = snapshot.atm("call")
        symbol = row["OptionSymbol"] if row else None
    if not symbol:
        report("GET /api/live-quotes", FAIL, "no quoted ATM call in snapshot")
    else:
        try:
            quote = QuoteReader(base).quotes([symbol]).get(symbol)
            if not quote:
                report("GET /api/live-quotes", FAIL, f"no quote returned for {symbol}")
            else:
                fresh = quote.is_executable
                report(
                    "GET /api/live-quotes",
                    PASS if fresh else WARN,
                    f"{symbol} bid={quote.bid} ask={quote.ask} age={quote.age_seconds:.0f}s"
                    + ("" if fresh else " -- too stale to execute (>15s); the Worker will 409"),
                )
        except Exception as exc:
            report("GET /api/live-quotes", FAIL, str(exc))

    # --- 3. account lifecycle -------------------------------------------
    print("\n3. Account lifecycle")
    creds = {"username": username, "password": password}
    registered = False
    try:
        r = http.post(f"{base}/api/register", json=creds, timeout=20)
        registered = r.status_code < 300
        report(
            "POST /api/register",
            PASS if registered else FAIL,
            f"HTTP {r.status_code} {r.text[:160]}",
        )
    except Exception as exc:
        report("POST /api/register", FAIL, str(exc))

    logged_in = False
    try:
        r = http.post(f"{base}/api/login", json=creds, timeout=20)
        logged_in = r.status_code < 300
        report("POST /api/login", PASS if logged_in else FAIL, f"HTTP {r.status_code}")
    except Exception as exc:
        report("POST /api/login", FAIL, str(exc))

    state = None
    try:
        r = http.get(f"{base}/api/me", timeout=20)
        if r.status_code < 300:
            state = r.json()
            report(
                "GET /api/me",
                PASS,
                f"balance_cash={state.get('balance_cash')} trades={len(state.get('trades') or [])}",
            )
        else:
            report("GET /api/me", FAIL, f"HTTP {r.status_code} {r.text[:160]}")
    except Exception as exc:
        report("GET /api/me", FAIL, str(exc))

    # --- 4. execution ----------------------------------------------------
    print("\n4. Execution")
    exec_id = str(uuid.uuid4())
    filled = False
    if not (logged_in and state is not None and symbol):
        report(
            "POST /api/paper-trade",
            FAIL,
            "skipped -- no authenticated session or no tradeable symbol",
        )
    else:
        try:
            r = http.post(
                f"{base}/api/paper-trade",
                json={"execution_request_id": exec_id, "sym": symbol, "side": "buy", "qty": 1},
                timeout=30,
            )
            filled = r.status_code < 300
            report(
                "POST /api/paper-trade",
                PASS if filled else FAIL,
                f"HTTP {r.status_code} {r.text[:220]}",
            )
        except Exception as exc:
            report("POST /api/paper-trade", FAIL, str(exc))

    # --- 5. the exit criterion ------------------------------------------
    print("\n5. Exit criterion -- fill visible in /api/me")
    criterion_met = False
    if filled:
        time.sleep(1)
        try:
            r = http.get(f"{base}/api/me", timeout=20)
            trades = (r.json().get("trades") or []) if r.status_code < 300 else []
            match = next((t for t in trades if exec_id in json.dumps(t, default=str)), None)
            criterion_met = match is not None
            report(
                "One-contract fill appears in /api/me",
                PASS if criterion_met else FAIL,
                json.dumps(match)[:220] if match else f"{len(trades)} trades, none matching {exec_id}",
            )
        except Exception as exc:
            report("One-contract fill appears in /api/me", FAIL, str(exc))
    else:
        report("One-contract fill appears in /api/me", FAIL, "skipped -- no fill to confirm")

    if logged_in and not args.keep:
        try:
            http.post(f"{base}/api/logout", timeout=10)
        except Exception:
            pass

    # --- verdict ---------------------------------------------------------
    failures = [r for r in results if r[1] == FAIL]
    warns = [r for r in results if r[1] == WARN]
    print("\n" + "=" * 66)
    print(f"P0 {'PASSED' if criterion_met else 'BLOCKED'} -- "
          f"{len(results) - len(failures) - len(warns)} passed, {len(warns)} warned, {len(failures)} failed")
    if failures:
        print("\nBlocking failures:")
        for name, _, detail in failures:
            print(f"  - {name}: {detail}")
    print("=" * 66)
    return 0 if criterion_met else 1


if __name__ == "__main__":
    sys.exit(main())
