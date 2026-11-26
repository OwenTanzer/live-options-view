#!/usr/bin/env python3
"""Prove `oi_skew_qqq` keeps independent session state per account and band.

Hermetic: hand-built snapshots and contexts only. The runner evaluates every
configured account against the same snapshot in one process; the strategy
used to keep one module-level `SessionImbalanceTracker`, so every account
after the first read the snapshot as "not newer than the last recorded
reading" and never traded, and an account with a different `near_money_pct`
inherited another band's session anchor (#107 comment, finding F3).

Trackers are now keyed by (account alias, near-money band), with the account
state's username as a fallback and one shared "unknown" key for hand-built
single-account contexts (`scripts/verify_oi_skew.py` proves that older path).

    python scripts/verify_oi_skew_accounts.py
"""

from __future__ import annotations

import json
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crassus.strategies  # noqa: E402,F401  (registers every strategy id)
from crassus.config import DEFAULT_ACCOUNTS_CATALOG, load_accounts
from crassus.exits import resolve_policy
from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.replay import registered_strategy_ids, reset_strategy_state  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


DAY = "2026-01-05"


def _ts(minute: int) -> str:
    base = datetime.fromisoformat(f"{DAY}T09:00:00+00:00")
    return (base + timedelta(minutes=minute)).isoformat()


def oi_snapshot(timestamp: str, call_oi: float, put_oi: float,
                edge_call_oi: float = 0.0, edge_put_oi: float = 0.0) -> MarketSnapshot:
    """Two-band fixture: strikes 392..408 inside 2%, 384/416 only inside 4%."""
    rows = []
    for strike in (392.0, 396.0, 400.0, 404.0, 408.0):
        rows.append({"OptionSymbol": f"QQQ260101C{int(strike*1000):08d}", "Strike": strike,
                     "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": call_oi})
        rows.append({"OptionSymbol": f"QQQ260101P{int(strike*1000):08d}", "Strike": strike,
                     "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": put_oi})
    for strike in (384.0, 416.0):
        rows.append({"OptionSymbol": f"QQQ260101C{int(strike*1000):08d}", "Strike": strike,
                     "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": edge_call_oi})
        rows.append({"OptionSymbol": f"QQQ260101P{int(strike*1000):08d}", "Strike": strike,
                     "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": edge_put_oi})
    return MarketSnapshot.from_payload(
        "test://oi", {"timestamp": timestamp, "snapshot_time": timestamp,
                      "expiration": "2026-01-01", "underlying_price": 400.0,
                      "rows": rows}, raw=b"{}",
    )


def ctx_for(snapshot: MarketSnapshot, params: dict | None = None,
            alias: str | None = None, username: str | None = None) -> StrategyContext:
    symbols = {r["OptionSymbol"] for r in snapshot.rows}
    ts = snapshot.timestamp

    def quotes(syms):
        return {
            s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts=ts, server_ts=ts)
            for s in syms if s in symbols
        }

    return StrategyContext(
        snapshot=snapshot,
        account_state={"username": username} if username else {},
        book=Book([]),
        now_et=datetime.fromisoformat(ts),
        session_phase="open",
        quotes=quotes,
        params=params or {},
        account_alias=alias,
    )


def oi_decide(snapshot, pct, alias=None, username=None) -> dict:
    return REGISTRY["oi_skew_qqq"](
        ctx_for(snapshot, {"near_money_pct": pct}, alias=alias, username=username)
    ).to_dict()


# Snapshot 1 anchors each account; snapshot 2 moves the 2% band sharply so
# the same-band accounts should reach a buy together, from their own anchors.
snap_1 = oi_snapshot(_ts(40), call_oi=100, put_oi=80, edge_call_oi=500, edge_put_oi=20)
snap_2 = oi_snapshot(_ts(45), call_oi=300, put_oi=60, edge_call_oi=500, edge_put_oi=20)

ACCOUNTS = {"A": (0.02, "A"), "B": (0.02, "B"), "C": (0.04, "C")}

reset_strategy_state()
first = {name: oi_decide(snap_1, pct, alias) for name, (pct, alias) in ACCOUNTS.items()}
second = {name: oi_decide(snap_2, pct, alias) for name, (pct, alias) in ACCOUNTS.items()}

check("same-band second account is not treated as stale",
      "not newer" not in second["B"]["reason"] and "stale" not in second["B"]["reason"],
      second["B"]["reason"][:90])
check("same-band accounts share the same anchor",
      first["A"]["metadata"]["session_start_imbalance"] is not None
      and first["A"]["metadata"]["session_start_imbalance"] == first["B"]["metadata"]["session_start_imbalance"])
check("different-band account anchors on its own band",
      first["C"]["metadata"]["session_start_imbalance"] != first["A"]["metadata"]["session_start_imbalance"],
      f"2%={first['A']['metadata']['session_start_imbalance']} 4%={first['C']['metadata']['session_start_imbalance']}")
check("same-band accounts reach the same directional decision",
      second["A"]["action"] == second["B"]["action"] == "buy",
      f"A={second['A']['action']} B={second['B']['action']}")
check("different-band account uses its own imbalance",
      second["C"]["metadata"]["imbalance_ratio"] != second["A"]["metadata"]["imbalance_ratio"],
      f"2%={second['A']['metadata']['imbalance_ratio']} 4%={second['C']['metadata']['imbalance_ratio']}")

# A third same-band account arriving later in the same cycle sequence is a
# fresh consumer of the same snapshot, not a duplicate of A or B.
late = oi_decide(snap_1, 0.02, alias="D")
check("a later same-band account still records the snapshot",
      "not newer" not in late["reason"], late["reason"][:80])

# Account identity falls back to the account-state username for callers that
# do not pass an alias, and still isolates two distinct usernames.
reset_strategy_state()
oi_decide(snap_1, 0.02, username="acct-1")
fallback = oi_decide(snap_1, 0.02, username="acct-2")
check("username fallback isolates distinct accounts",
      "not newer" not in fallback["reason"], fallback["reason"][:80])

# The explicit replay boundary re-anchors, and the strategy is registered.
reset_strategy_state()
replayed = oi_decide(snap_1, 0.02, alias="A")
check("reset re-anchors an independent replay", "not newer" not in replayed["reason"],
      replayed["reason"][:80])
check("oi_skew_qqq registers a state resetter",
      "oi_skew_qqq" in set(registered_strategy_ids()), str(registered_strategy_ids()))


# Exercise the same loader and registered strategy as a private paper roster.
# Dummy credentials exist only in this test. The parent also carries a
# non-default cutoff and stop so accidental fallback to defaults is visible.
catalog = json.loads(DEFAULT_ACCOUNTS_CATALOG.read_text())["accounts"]
parent, absolute = [next(e for e in catalog if e["username"] == name)
                    for name in ("crassus_oiskew", "crassus_oiskew_absolute")]
check("paper sibling is dormant and has its own credential",
      absolute["active"] is False and absolute["password_env"] != parent["password_env"])
parent = {**parent, "params": {"imbalance_threshold": 0.5, "exits": {"stop_loss_pct": -0.35}}}
with TemporaryDirectory() as tmp, patch.dict("os.environ", {
        parent["password_env"]: "fixture-parent", absolute["password_env"]: "fixture-absolute",
        "CRASSUS_NEWTON_CONFIRMED": "0"}):
    roster = Path(tmp) / "accounts.json"
    roster.write_text(json.dumps({"accounts": [parent, absolute]}))
    check("inactive challenger stays out of the runtime roster", len(load_accounts(roster)) == 1)
    roster.write_text(json.dumps({"accounts": [parent, {**absolute, "active": True}]}))
    control, challenger = load_accounts(roster)

check("configured absolute cutoff and risk policy are inherited",
      challenger.params["imbalance_threshold"] == 0.5
      and resolve_policy(challenger.strategy_id, challenger.params, activated=True)
      == resolve_policy(control.strategy_id, control.params, activated=True))
for call_oi, put_oi, letter in ((300, 100, "C"), (100, 300, "P")):
    reset_strategy_state()
    snap = oi_snapshot(_ts(45), call_oi, put_oi)
    ctx = ctx_for(snap, challenger.params, alias=challenger.alias)
    control_ctx = ctx_for(snap, control.params, alias=control.alias)
    signal = REGISTRY[challenger.strategy_id]
    opening = signal(ctx)
    check(f"{letter} at exact absolute cutoff enters on the opening reading",
          opening.action == "buy" and opening.symbol == f"QQQ260101{letter}00400000"
          and opening.metadata["delta_from_session_start"] == 0)
    check("matched parent requires movement", signal(control_ctx).action == "no_trade")
    check("duplicate snapshot cannot re-enter", signal(ctx).action == "no_trade")
    flat = signal(ctx_for(oi_snapshot(_ts(50), 140, 60), challenger.params, alias=challenger.alias))
    check("0.40 imbalance below the configured 0.50 cutoff cannot enter", flat.action == "no_trade")


print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
