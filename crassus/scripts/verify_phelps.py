#!/usr/bin/env python3
"""Prove crassus/phelps.py's hold-time gate and phelps_pure_qqq's decision logic.

Hermetic like the other verify_*.py scripts: no network, no real snapshot
fetch, no registry side effects beyond what importing crassus.strategies
already does at process start.

    python scripts/verify_phelps.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.phelps import PHELPS_MINUTES_DEFAULT, _entry_times, phelps_wrap  # noqa: E402
from crassus.strategies import phelps_pure  # noqa: E402
from crassus.strategy import Decision, StrategyContext  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def make_snapshot(underlying_price: float, rows: list[dict], *, timestamp: str = "2024-01-01T15:00:00+00:00") -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": timestamp,
            "snapshot_time": timestamp,
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": rows,
        },
        raw=b"{}",
    )


def make_ctx(
    *,
    username: str = "crassus_test",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    underlying_price: float = 400.0,
    now_et: datetime,
    snapshot_timestamp: str = "2024-01-01T15:00:00+00:00",
    rows: list[dict] | None = None,
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW], timestamp=snapshot_timestamp)
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={"username": username},
        book=book,
        now_et=now_et,
        session_phase="open",
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def executable_quote(symbol: str = "x", bid: float = 1.0, ask: float = 1.1) -> Quote:
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


# --------------------------------------------------------------------------
# phelps_wrap
# --------------------------------------------------------------------------


def test_phelps_wrap_defers_early_close() -> None:
    _entry_times.clear()
    base_calls = []

    def base(ctx: StrategyContext) -> Decision:
        base_calls.append(1)
        return Decision(action="sell", symbol="HELD", quantity=1, reason="signal reversed", strategy_id="base", strategy_version="1.0.0")

    wrapped = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")

    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]

    ctx0 = make_ctx(username="acct1", trades=trades, now_et=t0)
    d0 = wrapped(ctx0)
    check("first cycle records entry and does not sell", d0.action == "no_trade", d0.reason)

    ctx1 = make_ctx(username="acct1", trades=trades, now_et=t0 + timedelta(minutes=10))
    d1 = wrapped(ctx1)
    check("close proposed at 10m (< default window) is deferred", d1.action == "no_trade", d1.reason)
    check("deferred decision is attributed to the wrapper strategy_id", d1.strategy_id == "base_phelps")
    check("deferred metadata records elapsed/window", d1.metadata is not None and "phelps_elapsed_minutes" in d1.metadata)

    ctx2 = make_ctx(username="acct1", trades=trades, now_et=t0 + timedelta(minutes=PHELPS_MINUTES_DEFAULT + 1))
    d2 = wrapped(ctx2)
    check("close proposed after the window elapses is released", d2.action == "sell" and d2.symbol == "HELD")
    check("released decision is attributed to the wrapper strategy_id", d2.strategy_id == "base_phelps")


def test_phelps_wrap_never_blocks_buys_or_flat_no_trade() -> None:
    _entry_times.clear()

    def base_buy(ctx: StrategyContext) -> Decision:
        return Decision(action="buy", symbol="NEW", quantity=1, reason="entering", strategy_id="base", strategy_version="1.0.0")

    def base_no_trade(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="nothing to do", strategy_id="base", strategy_version="1.0.0")

    wrapped_buy = phelps_wrap(base_buy, strategy_id="buy_phelps", strategy_version="1.0.0")
    wrapped_flat = phelps_wrap(base_no_trade, strategy_id="flat_phelps", strategy_version="1.0.0")

    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx_flat = make_ctx(username="acct2", trades=[], now_et=t0)

    d_buy = wrapped_buy(ctx_flat)
    check("an opening buy passes through untouched", d_buy.action == "buy" and d_buy.symbol == "NEW")

    d_flat = wrapped_flat(ctx_flat)
    check("a flat no_trade passes through untouched", d_flat.action == "no_trade")


def test_phelps_wrap_respects_custom_window_param() -> None:
    _entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision(action="sell", symbol="HELD", quantity=1, reason="signal reversed", strategy_id="base", strategy_version="1.0.0")

    wrapped = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]

    ctx0 = make_ctx(username="acct3", trades=trades, now_et=t0, params={"phelps_minutes": 5.0})
    wrapped(ctx0)
    ctx1 = make_ctx(username="acct3", trades=trades, now_et=t0 + timedelta(minutes=6), params={"phelps_minutes": 5.0})
    d1 = wrapped(ctx1)
    check("a shorter configured phelps_minutes releases sooner", d1.action == "sell")


def test_phelps_wrap_clears_state_on_flat() -> None:
    _entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="holding", strategy_id="base", strategy_version="1.0.0")

    wrapped = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]

    wrapped(make_ctx(username="acct4", trades=trades, now_et=t0))
    check("entry recorded while held", ("acct4", "HELD") in _entry_times)

    wrapped(make_ctx(username="acct4", trades=[], now_et=t0 + timedelta(minutes=1)))
    check("entry cleared once flat", ("acct4", "HELD") not in _entry_times)


# --------------------------------------------------------------------------
# phelps_pure_qqq
# --------------------------------------------------------------------------


def test_phelps_pure_enters_on_displacement() -> None:
    phelps_pure._tracker._points.clear()
    phelps_pure._last_recorded_snapshot = None
    phelps_pure._watches.clear()

    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    quote_map = {"QQQ240101C00400000": executable_quote()}

    # Warm-up: one observation, no displacement possible yet.
    ctx0 = make_ctx(
        username="bowman", trades=[], now_et=t0, underlying_price=400.0,
        snapshot_timestamp=t0.isoformat(), quote_map=quote_map,
    )
    d0 = phelps_pure._decide(ctx0)
    check("no trade while warming up", d0.action == "no_trade")

    # 5 minutes later, price has moved up 0.30% -- above the default 0.15% threshold.
    t1 = t0 + timedelta(minutes=6)
    ctx1 = make_ctx(
        username="bowman", trades=[], now_et=t1, underlying_price=401.2,
        snapshot_timestamp=t1.isoformat(), quote_map=quote_map,
        rows=[CALL_ROW, PUT_ROW],
    )
    d1 = phelps_pure._decide(ctx1)
    check("displacement triggers a buy", d1.action == "buy" and d1.symbol == "QQQ240101C00400000", d1.reason)
    check("a watch is recorded for the account", "bowman" in phelps_pure._watches)


def test_phelps_pure_holds_through_window_then_releases() -> None:
    phelps_pure._watches.clear()
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    phelps_pure._watches["bowman"] = phelps_pure._Watch(
        symbol="QQQ240101C00400000", entry_time=t0, anchor_price=400.0, direction="up",
    )
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    quote_map = {"QQQ240101C00400000": executable_quote()}

    ctx_mid = make_ctx(
        username="bowman", trades=trades, now_et=t0 + timedelta(minutes=10),
        underlying_price=402.0, snapshot_timestamp=(t0 + timedelta(minutes=10)).isoformat(),
        quote_map=quote_map,
    )
    d_mid = phelps_pure._decide(ctx_mid)
    check("held through the window despite being well past entry price", d_mid.action == "no_trade", d_mid.reason)

    ctx_late = make_ctx(
        username="bowman", trades=trades, now_et=t0 + timedelta(minutes=PHELPS_MINUTES_DEFAULT + 1),
        underlying_price=402.0, snapshot_timestamp=(t0 + timedelta(minutes=PHELPS_MINUTES_DEFAULT + 1)).isoformat(),
        quote_map=quote_map,
    )
    d_late = phelps_pure._decide(ctx_late)
    check("released after the window elapses", d_late.action == "sell", d_late.reason)


def test_phelps_pure_invalidates_on_full_retrace() -> None:
    phelps_pure._watches.clear()
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    phelps_pure._watches["bowman"] = phelps_pure._Watch(
        symbol="QQQ240101C00400000", entry_time=t0, anchor_price=400.0, direction="up",
    )
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    quote_map = {"QQQ240101C00400000": executable_quote()}

    ctx = make_ctx(
        username="bowman", trades=trades, now_et=t0 + timedelta(minutes=3),
        underlying_price=399.5, snapshot_timestamp=(t0 + timedelta(minutes=3)).isoformat(),
        quote_map=quote_map,
    )
    d = phelps_pure._decide(ctx)
    check("full retrace closes immediately, well inside the window", d.action == "sell", d.reason)


test_phelps_wrap_defers_early_close()
test_phelps_wrap_never_blocks_buys_or_flat_no_trade()
test_phelps_wrap_respects_custom_window_param()
test_phelps_wrap_clears_state_on_flat()
test_phelps_pure_enters_on_displacement()
test_phelps_pure_holds_through_window_then_releases()
test_phelps_pure_invalidates_on_full_retrace()

print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
