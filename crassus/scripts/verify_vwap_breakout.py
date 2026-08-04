#!/usr/bin/env python3
"""Prove the vwap_breakout_qqq strategy's decision logic.

Hermetic like verify_momentum_qqq.py and verify_vwap_rvol.py, which this
mirrors in style: no network access, no real snapshot fetch.
`vwap_breakout._decide_core()` is exercised directly against a hand-built
`MarketSnapshot` carrying a hand-built `underlying_market` payload -- no
tracker, no collector, no I/O.

    python scripts/verify_vwap_breakout.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import vwap_breakout as vb  # noqa: E402
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


FULL_UM_PAYLOAD = {
    "symbol": "QQQ",
    "spot": 400.6, "spot_ts": "2026-07-30T14:32:00+00:00",
    "vwap": 400.0, "vwap_ts": "2026-07-30T14:32:00+00:00", "vwap_session_date": "2026-07-30",
    "price_vs_vwap_abs": 0.6, "price_vs_vwap_pct": 0.2,  # above the default 0.15% band
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


def um_payload(**overrides) -> dict:
    payload = {k: v for k, v in FULL_UM_PAYLOAD.items() if k not in ("rvol", "momentum")}
    payload["rvol"] = dict(FULL_UM_PAYLOAD["rvol"])
    payload["momentum"] = dict(FULL_UM_PAYLOAD["momentum"])
    for key in list(overrides):
        if key.startswith("rvol_"):
            payload["rvol"][key[len("rvol_"):]] = overrides.pop(key)
    payload.update(overrides)
    return payload


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def make_snapshot(underlying_market_payload: dict | None, underlying_price: float = 400.6) -> MarketSnapshot:
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


def make_ctx(
    *,
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    underlying_market_payload=None,
    session_phase: str = "open",
) -> StrategyContext:
    snapshot = make_snapshot(underlying_market_payload)
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot, account_state={}, book=book, now_et=None, session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("vwap_breakout_qqq is registered", "vwap_breakout_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["vwap_breakout_qqq"], "strategy_id", None) == vb.STRATEGY_ID
        and getattr(REGISTRY["vwap_breakout_qqq"], "strategy_version", None) == vb.STRATEGY_VERSION,
    )


def scenario_missing_underlying_market_declines() -> None:
    print("\n2. Missing underlying_market -> no_trade while flat")
    ctx = make_ctx(underlying_market_payload=None)
    decision = vb._decide_core(ctx)
    check("no_trade", not decision.is_trade)
    check("reason cites missing data", "No underlying_market" in decision.reason, decision.reason)


def scenario_missing_underlying_market_retains_position() -> None:
    print("\n3. Missing underlying_market while holding a position -- retain, don't close")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades, underlying_market_payload=None,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining", "retaining" in decision.reason.lower(), decision.reason)


def scenario_stale_freshness_declines() -> None:
    print("\n4. freshness != 'live' -> no_trade while flat")
    ctx = make_ctx(underlying_market_payload=um_payload(freshness="stale"))
    decision = vb._decide_core(ctx)
    check("no_trade", not decision.is_trade)
    check("reason cites freshness", "freshness" in decision.reason.lower(), decision.reason)


def scenario_stale_freshness_retains_position() -> None:
    print("\n5. freshness != 'live' while holding a position -- retain, don't close")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades, underlying_market_payload=um_payload(freshness="stale"),
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining", "retaining" in decision.reason.lower(), decision.reason)


def scenario_rvol_not_ok_declines() -> None:
    print("\n6. rvol_status != 'ok' -> no_trade while flat")
    for status in ("no_data", "insufficient_history"):
        ctx = make_ctx(underlying_market_payload=um_payload(rvol_status=status))
        decision = vb._decide_core(ctx)
        check(f"no_trade for rvol_status={status}", not decision.is_trade)
        check(f"reason cites RVOL status for {status}", "RVOL status" in decision.reason, decision.reason)


def scenario_rvol_not_ok_retains_position() -> None:
    print("\n7. rvol_status != 'ok' while holding a position -- retain, don't close")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades, underlying_market_payload=um_payload(rvol_status="no_data"),
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining", "retaining" in decision.reason.lower(), decision.reason)


def scenario_bullish_breakout_opens_call() -> None:
    print("\n8. Price above VWAP + RVOL confirmed -> buy one call")
    ctx = make_ctx(
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0),
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("metadata carries price_vs_vwap_pct", decision.metadata.get("price_vs_vwap_pct") == 0.5)
    check("metadata carries rvol_multiple", decision.metadata.get("rvol_multiple") == 2.0)


def scenario_bearish_breakout_opens_put() -> None:
    print("\n9. Price below VWAP + RVOL confirmed -> buy one put")
    ctx = make_ctx(
        underlying_market_payload=um_payload(price_vs_vwap_pct=-0.5, rvol_multiple=2.0),
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_breakout_without_rvol_confirmation_declines() -> None:
    print("\n10. Price above VWAP but RVOL below floor -> no_trade")
    ctx = make_ctx(underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=0.8))
    decision = vb._decide_core(ctx)
    check("no_trade", not decision.is_trade)
    check("reason cites volume confirmation", "volume" in decision.reason.lower(), decision.reason)


def scenario_price_inside_band_declines() -> None:
    print("\n11. Price within the VWAP band -> no_trade")
    ctx = make_ctx(underlying_market_payload=um_payload(price_vs_vwap_pct=0.05, rvol_multiple=2.0))
    decision = vb._decide_core(ctx)
    check("no_trade", not decision.is_trade)
    check("reason cites the band", "band" in decision.reason.lower(), decision.reason)


def scenario_held_position_closed_when_signal_disagrees() -> None:
    print("\n12. Holding a put while breakout now points bullish -- close it")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0),
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_held_position_closed_when_rvol_loses_confirmation() -> None:
    print("\n13. Holding a call, price still above VWAP but RVOL drops below floor -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=0.5),
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_held_position_closed_when_price_returns_to_band() -> None:
    print("\n14. Holding a call, price returns inside the VWAP band -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.05, rvol_multiple=2.0),
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vb._decide_core(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_held_position_retained_when_still_supported() -> None:
    print("\n15. Holding a call, breakout still bullish and confirmed -- no pyramiding, retain")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0),
    )
    decision = vb._decide_core(ctx)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason, decision.reason)


def scenario_market_closed_declines() -> None:
    print("\n16. Market not open declines without evaluating the signal")
    ctx = make_ctx(session_phase="premarket", underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0))
    decision = vb._decide_core(ctx)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_stale_quote_declines() -> None:
    print("\n17. Bullish, confirmed breakout but stale quote declines rather than risking a 409")
    stale = Quote(symbol="QQQ240101C00400000", bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")
    ctx = make_ctx(
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0),
        quote_map={"QQQ240101C00400000": stale},
    )
    decision = vb._decide_core(ctx)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n18. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(trades=trades, underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0))
    decision = vb._decide_core(ctx)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_custom_threshold_and_floor_honored() -> None:
    print("\n19. Custom threshold/floor params are honored")
    ctx = make_ctx(
        params={"vwap_breakout_threshold_pct": 1.0, "rvol_floor": 3.0},
        underlying_market_payload=um_payload(price_vs_vwap_pct=0.5, rvol_multiple=2.0),
    )
    decision = vb._decide_core(ctx)
    check(
        "no_trade -- 0.5% move doesn't clear the widened 1.0% threshold",
        not decision.is_trade, decision.to_dict(),
    )


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_missing_underlying_market_declines,
        scenario_missing_underlying_market_retains_position,
        scenario_stale_freshness_declines,
        scenario_stale_freshness_retains_position,
        scenario_rvol_not_ok_declines,
        scenario_rvol_not_ok_retains_position,
        scenario_bullish_breakout_opens_call,
        scenario_bearish_breakout_opens_put,
        scenario_breakout_without_rvol_confirmation_declines,
        scenario_price_inside_band_declines,
        scenario_held_position_closed_when_signal_disagrees,
        scenario_held_position_closed_when_rvol_loses_confirmation,
        scenario_held_position_closed_when_price_returns_to_band,
        scenario_held_position_retained_when_still_supported,
        scenario_market_closed_declines,
        scenario_stale_quote_declines,
        scenario_unexpected_short_stands_down,
        scenario_custom_threshold_and_floor_honored,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
