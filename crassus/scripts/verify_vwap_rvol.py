#!/usr/bin/env python3
"""Prove the VWAP/RVOL data contract and momentum_qqq's optional gates.

Hermetic like verify_momentum_qqq.py, which this mirrors in style: no
network access, no real snapshot fetch. `UnderlyingMarket.from_payload()` and
`vwap_rvol.evaluate_gate()` are exercised with hand-built payloads/records;
`momentum_qqq._decide_core()` is exercised directly with a hand-built
`MomentumSignal` plus a `MarketSnapshot` carrying a hand-built
`underlying_market` -- no tracker, no collector, no I/O.

    python scripts/verify_vwap_rvol.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote, UnderlyingMarket  # noqa: E402
from crassus.momentum import MomentumSignal  # noqa: E402
from crassus.strategies import momentum_qqq as mq  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402
from crassus.vwap_rvol import evaluate_gate  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


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
    "source": "dxlink", "freshness": "live",
}


def um(**overrides) -> UnderlyingMarket:
    payload = dict(FULL_UM_PAYLOAD)
    rvol = dict(payload["rvol"])
    for key in list(overrides):
        if key.startswith("rvol_"):
            rvol[key[len("rvol_"):]] = overrides.pop(key)
    payload["rvol"] = rvol
    payload.update(overrides)
    return UnderlyingMarket.from_payload(payload)


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def make_snapshot(underlying_market_payload: dict | None, underlying_price: float = 400.0) -> MarketSnapshot:
    payload = {
        "timestamp": "2024-01-01T15:00:00+00:00",
        "snapshot_time": "2024-01-01T15:00:00+00:00",
        "expiration": "2024-01-01",
        "underlying_price": underlying_price,
        "rows": [CALL_ROW, PUT_ROW],
    }
    if underlying_market_payload is not None:
        payload["underlying_market"] = underlying_market_payload
    return MarketSnapshot.from_payload(url="test://snapshot", payload=payload, raw=b"{}")


def make_ctx(*, trades: list[dict] | None = None, quote_map: dict[str, Quote] | None = None,
             params: dict | None = None, underlying_market_payload=FULL_UM_PAYLOAD) -> StrategyContext:
    snapshot = make_snapshot(underlying_market_payload)
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot, account_state={}, book=book, now_et=None, session_phase="open",
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def bullish_signal() -> MomentumSignal:
    return MomentumSignal(
        lookback_minutes=60.0, current_price=404.0, anchor_price=400.0, return_pct=0.01,
        sample_count=10, anchor_age_minutes=60.0, status="ok",
    )


def bearish_signal() -> MomentumSignal:
    return MomentumSignal(
        lookback_minutes=60.0, current_price=396.0, anchor_price=400.0, return_pct=-0.01,
        sample_count=10, anchor_age_minutes=60.0, status="ok",
    )


# ---------------------------------------------------------------------------
# UnderlyingMarket.from_payload
# ---------------------------------------------------------------------------


def scenario_from_payload_full() -> None:
    print("\n1. UnderlyingMarket.from_payload(): parses a full payload")
    parsed = UnderlyingMarket.from_payload(FULL_UM_PAYLOAD)
    check("symbol", parsed.symbol == "QQQ")
    check("spot", parsed.spot == 402.0)
    check("vwap", parsed.vwap == 400.0)
    check("price_vs_vwap_pct", parsed.price_vs_vwap_pct == 0.5)
    check("session_volume", parsed.session_volume == 41823400)
    check("rvol_status unpacked from nested rvol", parsed.rvol_status == "ok")
    check("rvol_multiple unpacked from nested rvol", parsed.rvol_multiple == 1.5)
    check("rvol_baseline_days_used unpacked", parsed.rvol_baseline_days_used == 10)
    check("freshness", parsed.freshness == "live")


def scenario_from_payload_missing() -> None:
    print("\n2. UnderlyingMarket.from_payload(): None/missing input returns None (older-snapshot back-compat)")
    check("None input", UnderlyingMarket.from_payload(None) is None)
    check("empty dict input", UnderlyingMarket.from_payload({}) is None)


def scenario_market_snapshot_without_underlying_market() -> None:
    print("\n3. MarketSnapshot.from_payload(): a payload with no underlying_market key parses cleanly")
    snap = make_snapshot(None)
    check("underlying_market is None", snap.underlying_market is None)
    check("underlying_price still parses (unaffected)", snap.underlying_price == 400.0)


# ---------------------------------------------------------------------------
# evaluate_gate()
# ---------------------------------------------------------------------------


def scenario_gate_none_underlying_market() -> None:
    print("\n4. evaluate_gate(): underlying_market is None -> no_data pass-through")
    gate = evaluate_gate(None, "up", rvol_floor=1.0, require_vwap_agreement=True)
    check("status is no_data", gate.status == "no_data")
    check("vwap_agrees is None, not fabricated", gate.vwap_agrees is None)
    check("rvol_participation_ok is None, not fabricated", gate.rvol_participation_ok is None)


def scenario_gate_rvol_status_passthrough() -> None:
    print("\n5. evaluate_gate(): rvol_status != 'ok' passes through, never fabricates a verdict")
    for status in ("no_data", "insufficient_history"):
        gate = evaluate_gate(um(rvol_status=status), "up", rvol_floor=1.0, require_vwap_agreement=True)
        check(f"status={status} passed through", gate.status == status, gate.status)
        check(f"vwap_agrees is None for status={status}", gate.vwap_agrees is None)
        check(f"rvol_participation_ok is None for status={status}", gate.rvol_participation_ok is None)


def scenario_gate_vwap_agreement_up() -> None:
    print("\n6. evaluate_gate(): VWAP agreement, direction=up")
    above = evaluate_gate(um(price_vs_vwap_pct=0.5), "up", rvol_floor=None, require_vwap_agreement=True)
    check("price above vwap agrees with up", above.vwap_agrees is True)
    below = evaluate_gate(um(price_vs_vwap_pct=-0.5), "up", rvol_floor=None, require_vwap_agreement=True)
    check("price below vwap disagrees with up", below.vwap_agrees is False)


def scenario_gate_vwap_agreement_down() -> None:
    print("\n7. evaluate_gate(): VWAP agreement, direction=down")
    below = evaluate_gate(um(price_vs_vwap_pct=-0.5), "down", rvol_floor=None, require_vwap_agreement=True)
    check("price below vwap agrees with down", below.vwap_agrees is True)
    above = evaluate_gate(um(price_vs_vwap_pct=0.5), "down", rvol_floor=None, require_vwap_agreement=True)
    check("price above vwap disagrees with down", above.vwap_agrees is False)


def scenario_gate_vwap_disabled() -> None:
    print("\n8. evaluate_gate(): require_vwap_agreement=False disables the vwap check")
    gate = evaluate_gate(um(price_vs_vwap_pct=-0.5), "up", rvol_floor=None, require_vwap_agreement=False)
    check("vwap_agrees is None when disabled", gate.vwap_agrees is None)


def scenario_gate_rvol_floor() -> None:
    print("\n9. evaluate_gate(): RVOL floor above/below")
    above = evaluate_gate(um(rvol_multiple=1.5), "up", rvol_floor=1.2, require_vwap_agreement=False)
    check("1.5x clears a 1.2x floor", above.rvol_participation_ok is True)
    below = evaluate_gate(um(rvol_multiple=1.0), "up", rvol_floor=1.2, require_vwap_agreement=False)
    check("1.0x fails a 1.2x floor", below.rvol_participation_ok is False)


def scenario_gate_rvol_disabled() -> None:
    print("\n10. evaluate_gate(): rvol_floor=None disables the rvol check")
    gate = evaluate_gate(um(rvol_multiple=0.1), "up", rvol_floor=None, require_vwap_agreement=False)
    check("rvol_participation_ok is None when disabled", gate.rvol_participation_ok is None)


# ---------------------------------------------------------------------------
# momentum_qqq._decide_core() with the gates enabled
# ---------------------------------------------------------------------------


def scenario_gates_off_by_default_no_behavior_change() -> None:
    print("\n11. _decide_core(): omitting both params is identical to no gate at all (regression guard)")
    ctx = make_ctx(quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
                    underlying_market_payload=None)  # even with NO underlying_market data available
    decision = mq._decide_core(ctx, bullish_signal())
    check("still buys on momentum alone -- gates never evaluated", decision.action == "buy", decision.to_dict())


def scenario_vwap_gate_vetoes_otherwise_supported_buy() -> None:
    print("\n12. _decide_core(): vwap_confirmation_required vetoes a buy when price disagrees with VWAP")
    ctx = make_ctx(
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": -0.5},  # below vwap, bullish signal disagrees
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("no_trade -- vwap disagrees with the bullish signal", not decision.is_trade, decision.to_dict())
    check("reason cites vwap", "vwap" in decision.reason.lower(), decision.reason)


def scenario_vwap_gate_allows_agreeing_buy() -> None:
    print("\n13. _decide_core(): vwap_confirmation_required allows a buy when price agrees with VWAP")
    ctx = make_ctx(
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": 0.5},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("buys -- vwap agrees with the bullish signal", decision.action == "buy", decision.to_dict())


def scenario_rvol_gate_vetoes_low_participation() -> None:
    print("\n14. _decide_core(): rvol_floor vetoes a buy when RVOL is below the floor")
    ctx = make_ctx(
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "multiple": 0.5}},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("no_trade -- rvol below floor", not decision.is_trade, decision.to_dict())
    check("reason cites rvol", "rvol" in decision.reason.lower(), decision.reason)


def scenario_rvol_gate_allows_high_participation() -> None:
    print("\n15. _decide_core(): rvol_floor allows a buy when RVOL clears the floor")
    ctx = make_ctx(
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "multiple": 1.5}},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("buys -- rvol clears the floor", decision.action == "buy", decision.to_dict())


def scenario_gate_veto_closes_held_position() -> None:
    print("\n16. _decide_core(): a gate veto closes a held position momentum alone would keep")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": -0.5},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("action is sell -- gate veto closes even though momentum still supports it",
          decision.action == "sell", decision.to_dict())
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_gate_status_not_ok_retains_held_position() -> None:
    print("\n17. _decide_core(): RVOL still insufficient_history while holding -- retains, does not close")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "status": "insufficient_history"}},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("action is no_trade, not sell -- absence of a fresh gate read isn't evidence against",
          decision.action == "no_trade", decision.to_dict())
    check("reason mentions retaining", "retaining" in decision.reason.lower(), decision.reason)


def scenario_gate_status_not_ok_declines_while_flat() -> None:
    print("\n18. _decide_core(): RVOL still insufficient_history while flat -- declines, doesn't fabricate a buy")
    ctx = make_ctx(
        params={"rvol_floor": 1.2},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "rvol": {**FULL_UM_PAYLOAD["rvol"], "status": "insufficient_history"}},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("no_trade while flat", not decision.is_trade, decision.to_dict())


def scenario_gate_status_no_data_missing_underlying_market() -> None:
    print("\n19. _decide_core(): gate enabled but underlying_market itself missing -- treated as no_data, not a crash")
    ctx = make_ctx(
        params={"vwap_confirmation_required": True},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market_payload=None,
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("no_trade rather than crashing on a None underlying_market", not decision.is_trade, decision.to_dict())
    check("reason cites unavailable data", "unavailable" in decision.reason.lower(), decision.reason)


def scenario_both_gates_enabled_bearish_direction() -> None:
    print("\n20. _decide_core(): both gates enabled together, bearish direction, both satisfied -> sell-side buy proceeds")
    ctx = make_ctx(
        params={"vwap_confirmation_required": True, "rvol_floor": 1.2},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market_payload={**FULL_UM_PAYLOAD, "price_vs_vwap_pct": -0.5,
                                    "rvol": {**FULL_UM_PAYLOAD["rvol"], "multiple": 1.5}},
    )
    decision = mq._decide_core(ctx, bearish_signal())
    check("buys a put -- vwap agrees (below vwap, bearish) and rvol clears the floor",
          decision.action == "buy" and decision.symbol == "QQQ240101P00400000", decision.to_dict())


def main() -> int:
    for scenario in (
        scenario_from_payload_full,
        scenario_from_payload_missing,
        scenario_market_snapshot_without_underlying_market,
        scenario_gate_none_underlying_market,
        scenario_gate_rvol_status_passthrough,
        scenario_gate_vwap_agreement_up,
        scenario_gate_vwap_agreement_down,
        scenario_gate_vwap_disabled,
        scenario_gate_rvol_floor,
        scenario_gate_rvol_disabled,
        scenario_gates_off_by_default_no_behavior_change,
        scenario_vwap_gate_vetoes_otherwise_supported_buy,
        scenario_vwap_gate_allows_agreeing_buy,
        scenario_rvol_gate_vetoes_low_participation,
        scenario_rvol_gate_allows_high_participation,
        scenario_gate_veto_closes_held_position,
        scenario_gate_status_not_ok_retains_held_position,
        scenario_gate_status_not_ok_declines_while_flat,
        scenario_gate_status_no_data_missing_underlying_market,
        scenario_both_gates_enabled_bearish_direction,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
