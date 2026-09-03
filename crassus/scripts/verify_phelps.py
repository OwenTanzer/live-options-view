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
from crassus.phelps import (  # noqa: E402
    PHELPS_MINUTES_DEFAULT,
    _entry_times,
    _fixed_window_entry_times,
    fixed_window_wrap,
    phelps_wrap,
)
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


def stale_quote(symbol: str = "x", bid: float = 1.0, ask: float = 1.1) -> Quote:
    # server_ts far after quote_ts -- age_seconds exceeds EXECUTION_QUOTE_MAX_AGE_S.
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


# --------------------------------------------------------------------------
# fixed_window_wrap (MOO-161)
# --------------------------------------------------------------------------


def test_fixed_window_wrap_suppresses_early_sell() -> None:
    _fixed_window_entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision(action="sell", symbol="HELD", quantity=1, reason="signal reversed", strategy_id="base", strategy_version="1.0.0")

    wrapped = fixed_window_wrap(base, strategy_id="base_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]

    ctx0 = make_ctx(username="fw1", trades=trades, now_et=t0)
    d0 = wrapped(ctx0)
    check("first cycle records entry and does not sell", d0.action == "no_trade", d0.reason)

    ctx1 = make_ctx(username="fw1", trades=trades, now_et=t0 + timedelta(minutes=10))
    d1 = wrapped(ctx1)
    check("a sell proposed at 10m (< default window) is suppressed", d1.action == "no_trade", d1.reason)
    check("suppressed decision is attributed to the wrapper strategy_id", d1.strategy_id == "base_fw")


def test_fixed_window_wrap_forces_close_even_when_base_still_holds() -> None:
    print("\nKey divergence from phelps_wrap: the base strategy never proposed a sell here at all")
    _fixed_window_entry_times.clear()

    def base_holds(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="still supports the position", strategy_id="base", strategy_version="1.0.0")

    wrapped = fixed_window_wrap(base_holds, strategy_id="base_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 3, "price": 1.0, "ts": t0.isoformat()}]
    quote_map = {"HELD": executable_quote(symbol="HELD")}

    ctx = make_ctx(username="fw2", trades=trades, now_et=t0 + timedelta(minutes=PHELPS_MINUTES_DEFAULT + 1), quote_map=quote_map)
    d = wrapped(ctx)
    check(
        "the entire position is force-closed at the boundary despite the base strategy proposing no_trade",
        d.action == "sell" and d.symbol == "HELD" and d.quantity == 3,
        d.reason,
    )
    check("forced close is attributed to the wrapper strategy_id", d.strategy_id == "base_fw")
    check("metadata records the base's decision at the boundary for auditing", d.metadata is not None and d.metadata.get("base_action_at_boundary") == "no_trade")


def test_fixed_window_wrap_never_blocks_buys_or_flat_no_trade() -> None:
    _fixed_window_entry_times.clear()

    def base_buy(ctx: StrategyContext) -> Decision:
        return Decision(action="buy", symbol="NEW", quantity=1, reason="entering", strategy_id="base", strategy_version="1.0.0")

    wrapped_buy = fixed_window_wrap(base_buy, strategy_id="buy_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx_flat = make_ctx(username="fw3", trades=[], now_et=t0)

    d_buy = wrapped_buy(ctx_flat)
    check("an opening buy passes through untouched while flat", d_buy.action == "buy" and d_buy.symbol == "NEW")


def test_fixed_window_wrap_retries_on_missing_quote() -> None:
    _fixed_window_entry_times.clear()

    def base_holds(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="still supports the position", strategy_id="base", strategy_version="1.0.0")

    wrapped = fixed_window_wrap(base_holds, strategy_id="base_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0, "ts": t0.isoformat()}]

    ctx = make_ctx(username="fw4", trades=trades, now_et=t0 + timedelta(minutes=PHELPS_MINUTES_DEFAULT + 1), quote_map={})
    d = wrapped(ctx)
    check(
        "a missing quote at the boundary records a pending retry instead of fabricating a fill",
        d.action == "no_trade" and d.metadata is not None and d.metadata.get("forced_close_pending") is True,
        d.reason,
    )


def test_fixed_window_wrap_retries_on_stale_quote() -> None:
    _fixed_window_entry_times.clear()

    def base_holds(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="still supports the position", strategy_id="base", strategy_version="1.0.0")

    wrapped = fixed_window_wrap(base_holds, strategy_id="base_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0, "ts": t0.isoformat()}]
    quote_map = {"HELD": stale_quote(symbol="HELD")}

    ctx = make_ctx(username="fw5", trades=trades, now_et=t0 + timedelta(minutes=PHELPS_MINUTES_DEFAULT + 1), quote_map=quote_map)
    d = wrapped(ctx)
    check(
        "a stale quote at the boundary records a pending retry instead of executing against it",
        d.action == "no_trade" and d.metadata is not None and d.metadata.get("forced_close_pending") is True,
        d.reason,
    )


def test_fixed_window_wrap_recovers_fill_time_from_trade_ts() -> None:
    print("\nRegression: fixed_window_wrap recovers entry time from the trade's own ts, like phelps_wrap")
    _fixed_window_entry_times.clear()

    def base_holds(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="still supports the position", strategy_id="base", strategy_version="1.0.0")

    wrapped = fixed_window_wrap(base_holds, strategy_id="base_fw", strategy_version="1.0.0")
    fill_time = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    now = fill_time + timedelta(minutes=30)  # already past the 27.5m default on the very first observed cycle
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0, "ts": fill_time.isoformat()}]
    quote_map = {"HELD": executable_quote(symbol="HELD")}

    ctx = make_ctx(username="fw6", trades=trades, now_et=now, quote_map=quote_map)
    d = wrapped(ctx)
    check(
        "forced closed on the first observed cycle because the real fill was already past the window",
        d.action == "sell" and d.symbol == "HELD",
        d.reason,
    )


def test_fixed_window_wrap_clears_state_on_flat() -> None:
    _fixed_window_entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="holding", strategy_id="base", strategy_version="1.0.0")

    wrapped = fixed_window_wrap(base, strategy_id="base_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]

    wrapped(make_ctx(username="fw7", trades=trades, now_et=t0))
    check("entry recorded while held", ("fw7", "HELD") in _fixed_window_entry_times)

    wrapped(make_ctx(username="fw7", trades=[], now_et=t0 + timedelta(minutes=1)))
    check("entry cleared once flat", ("fw7", "HELD") not in _fixed_window_entry_times)


def test_fixed_window_wrap_state_independent_from_phelps_wrap() -> None:
    print("\nRegression: fixed_window_wrap and phelps_wrap track entry times independently")
    _entry_times.clear()
    _fixed_window_entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision.no_trade(reason="holding", strategy_id="base", strategy_version="1.0.0")

    sanctuary = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")
    fixed_window = fixed_window_wrap(base, strategy_id="base_fw", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]

    sanctuary(make_ctx(username="fw8", trades=trades, now_et=t0))
    check("phelps_wrap's own table is populated", ("fw8", "HELD") in _entry_times)
    check("fixed_window_wrap's table is untouched by phelps_wrap", ("fw8", "HELD") not in _fixed_window_entry_times)

    fixed_window(make_ctx(username="fw8", trades=trades, now_et=t0))
    check("fixed_window_wrap's own table is now populated too", ("fw8", "HELD") in _fixed_window_entry_times)


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


def test_phelps_wrap_recovers_fill_time_from_trade_ts() -> None:
    print("\nRegression: entry time is recovered from the trade's own ts, not first-observation time")
    _entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision(action="sell", symbol="HELD", quantity=1, reason="signal reversed", strategy_id="base", strategy_version="1.0.0")

    wrapped = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")

    # Fill happened 30 minutes ago (past the 27.5m default window) but this
    # is the *first* cycle this process observes the position as held --
    # e.g. a restart, or simply the runner's own multi-minute cadence
    # meaning the position wasn't held on the cycle the buy was submitted.
    fill_time = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    now = fill_time + timedelta(minutes=30)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0, "ts": fill_time.isoformat()}]

    ctx = make_ctx(username="acct2", trades=trades, now_et=now)
    decision = wrapped(ctx)
    check(
        "released on the very first observed cycle because the real fill was already past the window",
        decision.action == "sell" and decision.symbol == "HELD",
        decision.reason,
    )
    check(
        "elapsed time in the release reflects the true fill time (~30m), not 0m",
        decision.metadata is not None and decision.metadata.get("phelps_elapsed_minutes", 0) >= 29.9,
        decision.metadata,
    )


def test_phelps_wrap_falls_back_to_now_when_ts_missing() -> None:
    print("\nRegression guard: a trade record with no ts still falls back to first-observed time")
    _entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision(action="sell", symbol="HELD", quantity=1, reason="signal reversed", strategy_id="base", strategy_version="1.0.0")

    wrapped = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0}]  # no ts

    ctx = make_ctx(username="acct3", trades=trades, now_et=now)
    decision = wrapped(ctx)
    check("no ts on the trade -- still defers via the first-observed fallback, not a crash", decision.action == "no_trade", decision.reason)


def test_phelps_pure_restart_recovery_grants_fresh_window() -> None:
    print("\nRegression: a freshly-reconstructed watch (restart) does not immediately close on equality")
    phelps_pure._watches.clear()
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    quote_map = {"QQQ240101C00400000": executable_quote()}

    # No pre-existing watch -- this is the restart-recovery branch. Anchor
    # gets set from this same cycle's underlying_price (400.0), so
    # current_price == anchor_price on this exact cycle.
    ctx = make_ctx(
        username="bowman", trades=trades, now_et=t0, underlying_price=400.0,
        snapshot_timestamp=t0.isoformat(), quote_map=quote_map,
    )
    decision = phelps_pure._decide(ctx)
    check(
        "does not immediately close on the equality case -- grants a fresh window instead",
        decision.action == "no_trade",
        decision.reason,
    )
    check("a fresh watch was recorded, dated this cycle", phelps_pure._watches.get("bowman") is not None and phelps_pure._watches["bowman"].entry_time == t0)


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


def test_phelps_pure_restart_recovers_entry_time_from_fill_ts() -> None:
    print("\nRegression: phelps_pure recovers entry_time from the fill ts on restart, not ctx.now_et")
    phelps_pure._watches.clear()  # no pre-existing watch -- simulates a restart
    fill_time = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    now = fill_time + timedelta(minutes=10)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0, "ts": fill_time.isoformat()}]

    ctx = make_ctx(
        username="restart_acct", trades=trades, now_et=now,
        underlying_price=400.0, snapshot_timestamp=now.isoformat(),
    )
    d = phelps_pure._decide(ctx)
    watch = phelps_pure._watches.get("restart_acct")
    check("a watch was reconstructed", watch is not None)
    check("entry_time recovered from the real fill ts, not ctx.now_et", watch is not None and watch.entry_time == fill_time, watch.entry_time if watch else None)
    check(
        "elapsed time reflects the true 10m since fill, not 0m since this restart cycle",
        d.metadata is not None and d.metadata.get("phelps_elapsed_minutes") == 10.0,
        d.metadata,
    )


def test_phelps_pure_restart_falls_back_to_now_when_ts_missing() -> None:
    print("\nRegression guard: phelps_pure still falls back to ctx.now_et when no trade ts exists")
    phelps_pure._watches.clear()
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]  # no ts

    ctx = make_ctx(username="no_ts_acct", trades=trades, now_et=now, underlying_price=400.0, snapshot_timestamp=now.isoformat())
    phelps_pure._decide(ctx)
    watch = phelps_pure._watches.get("no_ts_acct")
    check("falls back to ctx.now_et when no ts is available", watch is not None and watch.entry_time == now)


def test_phelps_minutes_validation() -> None:
    print("\nRegression: phelps_minutes is validated in both phelps_wrap and phelps_pure")
    from crassus.phelps import PHELPS_MINUTES_DEFAULT, resolve_phelps_minutes

    for label, bad_value in (("non-numeric string", "soon"), ("NaN", float("nan")), ("+inf", float("inf")), ("negative", -5), ("zero", 0)):
        resolved = resolve_phelps_minutes({"phelps_minutes": bad_value})
        check(f"{label} normalizes to the default", resolved == PHELPS_MINUTES_DEFAULT, (label, bad_value, resolved))

    check("a valid override is still honored", resolve_phelps_minutes({"phelps_minutes": 10.0}) == 10.0)
    check("a numeric string is coerced, not rejected", resolve_phelps_minutes({"phelps_minutes": "10"}) == 10.0)

    # End-to-end through phelps_wrap: a NaN phelps_minutes must not make the
    # window "elapse" immediately (NaN fails every comparison, which could
    # otherwise release a position on the very first cycle).
    _entry_times.clear()

    def base(ctx: StrategyContext) -> Decision:
        return Decision(action="sell", symbol="HELD", quantity=1, reason="signal reversed", strategy_id="base", strategy_version="1.0.0")

    wrapped = phelps_wrap(base, strategy_id="base_phelps", strategy_version="1.0.0")
    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "HELD", "side": "buy", "qty": 1, "price": 1.0, "ts": t0.isoformat()}]
    ctx = make_ctx(username="nan_acct", trades=trades, now_et=t0 + timedelta(minutes=1), params={"phelps_minutes": float("nan")})
    d = wrapped(ctx)
    check("a NaN phelps_minutes falls back to the default rather than releasing immediately", d.action == "no_trade", d.reason)


def test_phelps_pure_entry_rejected_without_live_quote() -> None:
    print("\nRegression: a qualifying displacement with no live quote is rejected (dry run), not a crash or a phantom watch")
    phelps_pure._tracker._points.clear()
    phelps_pure._last_recorded_snapshot = None
    phelps_pure._watches.clear()

    t0 = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx0 = make_ctx(username="noquote", trades=[], now_et=t0, underlying_price=400.0, snapshot_timestamp=t0.isoformat())
    phelps_pure._decide(ctx0)

    t1 = t0 + timedelta(minutes=6)
    ctx1 = make_ctx(
        username="noquote", trades=[], now_et=t1, underlying_price=401.2,
        snapshot_timestamp=t1.isoformat(), quote_map={},  # no live quote for the row this would trade
        rows=[CALL_ROW, PUT_ROW],
    )
    d1 = phelps_pure._decide(ctx1)
    check("no trade -- displacement qualifies but there's no live quote to enter with", d1.action == "no_trade", d1.reason)
    check("no watch recorded for a rejected entry", "noquote" not in phelps_pure._watches)


test_phelps_wrap_defers_early_close()
test_phelps_wrap_never_blocks_buys_or_flat_no_trade()
test_phelps_wrap_respects_custom_window_param()
test_phelps_wrap_clears_state_on_flat()
test_phelps_wrap_recovers_fill_time_from_trade_ts()
test_phelps_wrap_falls_back_to_now_when_ts_missing()
test_fixed_window_wrap_suppresses_early_sell()
test_fixed_window_wrap_forces_close_even_when_base_still_holds()
test_fixed_window_wrap_never_blocks_buys_or_flat_no_trade()
test_fixed_window_wrap_retries_on_missing_quote()
test_fixed_window_wrap_retries_on_stale_quote()
test_fixed_window_wrap_recovers_fill_time_from_trade_ts()
test_fixed_window_wrap_clears_state_on_flat()
test_fixed_window_wrap_state_independent_from_phelps_wrap()
test_phelps_pure_enters_on_displacement()
test_phelps_pure_holds_through_window_then_releases()
test_phelps_pure_restart_recovery_grants_fresh_window()
test_phelps_pure_restart_recovers_entry_time_from_fill_ts()
test_phelps_pure_restart_falls_back_to_now_when_ts_missing()
test_phelps_minutes_validation()
test_phelps_pure_entry_rejected_without_live_quote()
test_phelps_pure_invalidates_on_full_retrace()

print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
