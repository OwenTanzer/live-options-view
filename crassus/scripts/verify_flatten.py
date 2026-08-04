#!/usr/bin/env python3
"""Prove `maybe_flatten`'s end-of-day close-out logic.

Hermetic like verify_momentum_qqq.py: no network access, no real snapshot
fetch. `maybe_flatten` takes a `StrategyContext` and a params dict and makes
no I/O beyond `ctx.quotes()`, which every scenario here stubs directly.

    python scripts/verify_flatten.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.flatten import STRATEGY_ID, maybe_flatten  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402

ET = ZoneInfo("America/New_York")

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def make_snapshot(underlying_price: float = 400.0) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": [],
        },
        raw=b"{}",
    )


LONG_CALL = {
    "sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0,
    "strike": 400.0, "type": "call", "exp": "2024-01-01",
    "instrument_type": "option", "multiplier": 100, "ts": "2024-01-01T10:00:00Z",
}
SHORT_PUT = {
    "sym": "QQQ240101P00400000", "side": "sell", "qty": 1, "price": 1.0,
    "strike": 400.0, "type": "put", "exp": "2024-01-01",
    "instrument_type": "option", "multiplier": 100, "ts": "2024-01-01T10:00:00Z",
}


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    now_et: datetime | None = None,
) -> StrategyContext:
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=make_snapshot(),
        account_state={},
        book=Book(trades or []),
        now_et=now_et or datetime(2024, 1, 1, 15, 50, tzinfo=ET),
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


def scenario_no_op_without_param() -> None:
    print("\n1. No flatten_minutes_before_close set -- always None, regardless of how close to the bell")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        now_et=datetime(2024, 1, 1, 15, 59, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, {})
    check("no decision when the param is unset", decision is None)


def scenario_outside_window() -> None:
    print("\n2. Outside the flatten window -- falls through to the strategy")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 30, tzinfo=ET),  # 30 min to close
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision 30 minutes out with a 15-minute window", decision is None)


def scenario_flat_book_is_a_no_op() -> None:
    print("\n3. No open position -- nothing to flatten even inside the window")
    ctx = make_ctx(
        trades=[],
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 55, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision on a flat book", decision is None)


def scenario_not_open_is_a_no_op() -> None:
    print("\n4. session_phase != open -- never fires (nothing to flatten toward)")
    ctx = make_ctx(
        session_phase="afterhours",
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 16, 5, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision outside the open session", decision is None)


def scenario_closes_a_held_long() -> None:
    print("\n5. Inside the window with a held long -- sells to close")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),  # 10 min to close
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("a decision is proposed", decision is not None)
    check("sells the held long", decision.action == "sell" and decision.symbol == "QQQ240101C00400000")
    check("quantity matches the held size", decision.quantity == 1)
    check("strategy_id identifies this as a flatten, not the account's own strategy", decision.strategy_id == STRATEGY_ID)


def scenario_closes_a_held_short() -> None:
    print("\n6. Inside the window with a held short -- buys to close")
    ctx = make_ctx(
        trades=[SHORT_PUT],
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("buys to close the held short", decision is not None and decision.action == "buy" and decision.symbol == "QQQ240101P00400000")


def scenario_missing_quote_defers() -> None:
    print("\n7. No live quote for the held symbol -- defers to the next cycle rather than guessing")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision without a live quote", decision is None)


def scenario_stale_quote_defers() -> None:
    print("\n8. A stale quote for the held symbol -- defers rather than executing against it")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision on a stale quote", decision is None)


def scenario_exactly_at_threshold_fires() -> None:
    print("\n9. Exactly at the threshold boundary -- fires (inclusive)")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 45, tzinfo=ET),  # exactly 15 min to close
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("fires at the boundary itself", decision is not None)


def main() -> int:
    for scenario in (
        scenario_no_op_without_param,
        scenario_outside_window,
        scenario_flat_book_is_a_no_op,
        scenario_not_open_is_a_no_op,
        scenario_closes_a_held_long,
        scenario_closes_a_held_short,
        scenario_missing_quote_defers,
        scenario_stale_quote_defers,
        scenario_exactly_at_threshold_fires,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
