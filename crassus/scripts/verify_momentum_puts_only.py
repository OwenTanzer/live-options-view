#!/usr/bin/env python3
"""Prove the momentum_puts_only_qqq strategy's decision logic.

Hermetic, mirroring verify_momentum_qqq.py's shape (same underlying
momentum math, already covered there) but focused on what actually differs:
this strategy never opens a call, treats a bullish reading the same as
neutral, and stands down rather than acting on an unexpected held call.

    python scripts/verify_momentum_puts_only.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import MomentumSignal, PriceHistoryTracker  # noqa: E402
from crassus.strategies import momentum_puts_only as mpo  # noqa: E402
from crassus.strategies import phelps_variants  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

# Same fixture as verify_vwap_rvol.py's FULL_UM_PAYLOAD -- reused rather than
# imported so this file stays hermetic and independently readable.
FULL_UM_PAYLOAD = {
    "symbol": "QQQ",
    "spot": 402.0, "spot_ts": "2026-07-30T14:32:00+00:00",
    "vwap": 400.0, "vwap_ts": "2026-07-30T14:32:00+00:00", "vwap_session_date": "2026-07-30",
    "price_vs_vwap_abs": 2.0, "price_vs_vwap_pct": 0.5,
    "session_volume": 41823400, "session_volume_ts": "2026-07-30T14:32:00+00:00",
    "rvol": {
        "status": "ok", "multiple": 1.5, "bucket_label": "10:30",
        "baseline_volume": 700000, "baseline_days_used": 10,
        "baseline_lookback_days": 20, "baseline_updated_through": "2026-07-29",
    },
    "momentum": {
        "status": "ok", "return_pct": 0.42, "lookback_minutes": 60.0,
        "anchor_age_minutes": 61.0, "sample_count": 30, "direction": "up",
    },
    "source": "dxlink", "freshness": "live",
}

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def make_snapshot(
    underlying_price: float, rows: list[dict], *,
    timestamp: str = "2024-01-01T15:00:00+00:00",
    underlying_market_payload: dict | None = None,
) -> MarketSnapshot:
    payload = {
        "timestamp": timestamp,
        "snapshot_time": timestamp,
        "expiration": "2024-01-01",
        "underlying_price": underlying_price,
        "rows": rows,
    }
    if underlying_market_payload is not None:
        payload["underlying_market"] = underlying_market_payload
    return MarketSnapshot.from_payload(url="test://snapshot", payload=payload, raw=b"{}")


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = 400.0,
    now_et: datetime | None = None,
    snapshot_timestamp: str = "2024-01-01T15:00:00+00:00",
    underlying_market_payload: dict | None = None,
) -> StrategyContext:
    snapshot = make_snapshot(
        underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW],
        timestamp=snapshot_timestamp, underlying_market_payload=underlying_market_payload,
    )
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={"username": "crassus_persephone"},
        book=book,
        now_et=now_et,
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


def _signal(return_pct: float | None, *, status: str = "ok", sample_count: int = 10, anchor_age_minutes: float = 60.0) -> MomentumSignal:
    current_price = 400.0
    anchor_price = current_price / (1.0 + return_pct) if return_pct is not None else None
    return MomentumSignal(
        lookback_minutes=60.0,
        current_price=current_price,
        anchor_price=anchor_price,
        return_pct=return_pct,
        sample_count=sample_count,
        anchor_age_minutes=anchor_age_minutes,
        status=status,
    )


def scenario_registered() -> None:
    print("\n1. Registration")
    check("momentum_puts_only_qqq is registered", "momentum_puts_only_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["momentum_puts_only_qqq"], "strategy_id", None) == mpo.STRATEGY_ID
        and getattr(REGISTRY["momentum_puts_only_qqq"], "strategy_version", None) == mpo.STRATEGY_VERSION,
    )
    check(
        "momentum_puts_only_qqq_phelps is registered (phelps_variants.py)",
        "momentum_puts_only_qqq_phelps" in REGISTRY,
    )
    check(
        "Phelps twin's registered version composes the base version",
        REGISTRY["momentum_puts_only_qqq_phelps"].strategy_version
        == phelps_variants.phelps_variant_version(mpo.STRATEGY_VERSION),
    )


def scenario_market_closed() -> None:
    print("\n2. Market not open declines without touching the signal")
    ctx = make_ctx(session_phase="premarket")
    decision = mpo._decide_core(ctx, None)
    check("no_trade when market isn't open", not decision.is_trade)


def scenario_bullish_while_flat_declines() -> None:
    print("\n3. Bullish trailing return while flat -- no call, no trade at all")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = mpo._decide_core(ctx, _signal(0.01))
    check("no_trade -- this strategy never opens a call", not decision.is_trade, decision.to_dict())


def scenario_bullish_while_holding_put_closes() -> None:
    print("\n4. Bullish trailing return while holding a put -- no longer supported, close it")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mpo._decide_core(ctx, _signal(0.01))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_neutral_while_flat_declines() -> None:
    print("\n5. Neutral trailing return while flat declines")
    ctx = make_ctx(session_phase="open")
    decision = mpo._decide_core(ctx, _signal(0.0))
    check("no_trade in the neutral band", not decision.is_trade)


def scenario_bearish_opens_put() -> None:
    print("\n6. Bearish trailing return + flat + executable quote -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_bearish_stale_quote_declines() -> None:
    print("\n7. Bearish signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": stale_quote("QQQ240101P00400000")})
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_holding_put_no_pyramiding() -> None:
    print("\n8. Already holding a put while bearish -- no second contract")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_unexpected_call_stands_down() -> None:
    print("\n9. Book somehow holds a call -- stand down rather than force-close a position this strategy never opens")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mpo._decide_core(ctx, _signal(0.01))
    check("no_trade rather than selling the call", not decision.is_trade, decision.to_dict())
    check("reason explains it's not a put", "not a put" in decision.reason.lower(), decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n10. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101P00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n11. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade with more than one open position", not decision.is_trade)


def scenario_stale_source_while_flat_declines() -> None:
    print("\n12. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = mpo._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)


def scenario_stale_source_while_positioned_retains() -> None:
    print("\n13. Stale source while holding a put retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mpo._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_custom_bearish_threshold() -> None:
    print("\n14. Custom bearish_threshold is honored")
    ctx = make_ctx(
        session_phase="open",
        params={"bearish_threshold": -0.02},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade -- -0.01 return doesn't clear the widened -0.02 threshold", not decision.is_trade, decision.to_dict())


def scenario_vwap_gate_vetoes_bearish_put() -> None:
    print("\n15. _decide_core(): vwap_confirmation_required vetoes a put when price disagrees with VWAP")
    ctx = make_ctx(
        session_phase="open",
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": 0.5},  # above vwap, bearish signal disagrees
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade -- vwap disagrees with the bearish signal", not decision.is_trade, decision.to_dict())
    check("reason cites vwap", "vwap" in decision.reason.lower(), decision.reason)


def scenario_vwap_gate_allows_agreeing_bearish_put() -> None:
    print("\n16. _decide_core(): vwap_confirmation_required allows a put when price agrees with VWAP")
    ctx = make_ctx(
        session_phase="open",
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": -0.5},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("buys the put -- vwap agrees with the bearish signal", decision.action == "buy", decision.to_dict())


def scenario_rvol_gate_vetoes_low_participation() -> None:
    print("\n17. _decide_core(): rvol_floor vetoes a put when RVOL is below the floor")
    ctx = make_ctx(
        session_phase="open",
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "multiple": 0.5}},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade -- rvol below floor", not decision.is_trade, decision.to_dict())
    check("reason cites rvol", "rvol" in decision.reason.lower(), decision.reason)


def scenario_rvol_gate_allows_high_participation() -> None:
    print("\n18. _decide_core(): rvol_floor allows a put when RVOL clears the floor")
    ctx = make_ctx(
        session_phase="open",
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "multiple": 1.5}},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("buys the put -- rvol clears the floor", decision.action == "buy", decision.to_dict())


def scenario_gate_veto_closes_held_put() -> None:
    print("\n19. _decide_core(): a gate veto closes a held put momentum alone would keep")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": 0.5},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("action is sell -- gate veto closes even though momentum still supports it",
          decision.action == "sell", decision.to_dict())
    check("closes the actual held put", decision.symbol == "QQQ240101P00400000")


def scenario_gate_status_not_ok_retains_held_put() -> None:
    print("\n20. _decide_core(): RVOL still insufficient_history while holding -- retains, does not close")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "status": "insufficient_history"}},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("action is no_trade, not sell -- absence of a fresh gate read isn't evidence against",
          decision.action == "no_trade", decision.to_dict())
    check("reason mentions retaining", "retaining" in decision.reason.lower(), decision.reason)


def scenario_gate_status_not_ok_declines_while_flat() -> None:
    print("\n21. _decide_core(): RVOL still insufficient_history while flat -- declines, doesn't fabricate a buy")
    ctx = make_ctx(
        session_phase="open",
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "status": "insufficient_history"}},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade while flat", not decision.is_trade, decision.to_dict())


def scenario_gate_missing_underlying_market_treated_as_no_data() -> None:
    print("\n22. _decide_core(): gate enabled but underlying_market itself missing -- treated as no_data, not a crash")
    ctx = make_ctx(
        session_phase="open",
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload=None,
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("no_trade rather than crashing on a None underlying_market", not decision.is_trade, decision.to_dict())
    check("reason cites unavailable data", "unavailable" in decision.reason.lower(), decision.reason)


def scenario_gates_off_by_default_no_behavior_change() -> None:
    print("\n23. _decide_core(): gates off by default -- a disagreeing VWAP/RVOL doesn't matter when params don't opt in")
    ctx = make_ctx(
        session_phase="open",
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": 0.5,
                                    "rvol": {**FULL_UM_PAYLOAD["rvol"], "multiple": 0.1}},
    )
    decision = mpo._decide_core(ctx, _signal(-0.01))
    check("buys the put -- gates never consulted without opting in", decision.action == "buy", decision.to_dict())


def _reset_tracker() -> None:
    mpo._tracker = PriceHistoryTracker(retain_minutes=1440.0)
    mpo._last_recorded_snapshot = None


def scenario_decide_records_using_snapshot_timestamp() -> None:
    print("\n24. _decide(): records observed_at from snapshot.timestamp, not ctx.now_et")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")
    mpo._decide(ctx)
    points = mpo._tracker.snapshot()
    check("exactly one point recorded", len(points) == 1, len(points))
    check(
        "recorded observed_at matches the snapshot's own timestamp, not the runner's now_et",
        points[0].observed_at == datetime(2026, 1, 1, 14, 58, tzinfo=timezone.utc),
        points[0].observed_at,
    )


def scenario_decide_rejects_stale_snapshot_while_flat() -> None:
    print("\n25. _decide(): a snapshot far older than the runner's clock is rejected as a stale source, not recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00")
    decision = mpo._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", len(mpo._tracker.snapshot()) == 0, len(mpo._tracker.snapshot()))


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_market_closed,
        scenario_bullish_while_flat_declines,
        scenario_bullish_while_holding_put_closes,
        scenario_neutral_while_flat_declines,
        scenario_bearish_opens_put,
        scenario_bearish_stale_quote_declines,
        scenario_already_holding_put_no_pyramiding,
        scenario_unexpected_call_stands_down,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_stale_source_while_flat_declines,
        scenario_stale_source_while_positioned_retains,
        scenario_custom_bearish_threshold,
        scenario_vwap_gate_vetoes_bearish_put,
        scenario_vwap_gate_allows_agreeing_bearish_put,
        scenario_rvol_gate_vetoes_low_participation,
        scenario_rvol_gate_allows_high_participation,
        scenario_gate_veto_closes_held_put,
        scenario_gate_status_not_ok_retains_held_put,
        scenario_gate_status_not_ok_declines_while_flat,
        scenario_gate_missing_underlying_market_treated_as_no_data,
        scenario_gates_off_by_default_no_behavior_change,
        scenario_decide_records_using_snapshot_timestamp,
        scenario_decide_rejects_stale_snapshot_while_flat,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
