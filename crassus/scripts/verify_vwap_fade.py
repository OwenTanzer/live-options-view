#!/usr/bin/env python3
"""Prove the vwap_fade_qqq strategy's decision logic.

Hermetic like verify_momentum_qqq.py, which this mirrors in shape: no
network access, no real snapshot fetch. `_decide()` is exercised directly
against hand-built `StrategyContext`s carrying a `MarketSnapshot` whose
`underlying_market` payload is built inline to match
`UnderlyingMarket.from_payload`'s expected dict shape.

    python scripts/verify_vwap_fade.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import vwap_fade as vf  # noqa: E402
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


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def underlying_market_payload(
    *,
    freshness: str = "live",
    rvol_status: str = "ok",
    price_vs_vwap_pct: float | None = 0.0,
    rvol_multiple: float | None = 1.0,
    vwap: float | None = 400.0,
    spot: float = 400.0,
) -> dict:
    return {
        "symbol": "QQQ",
        "spot": spot,
        "spot_ts": "2024-01-01T15:00:00+00:00",
        "vwap": vwap,
        "vwap_ts": "2024-01-01T15:00:00+00:00",
        "vwap_session_date": "2024-01-01",
        "vwap_session_started_at": "2024-01-01T13:30:00+00:00",
        "vwap_partial_session": False,
        "price_vs_vwap_abs": None,
        "price_vs_vwap_pct": price_vs_vwap_pct,
        "session_volume": 1000000,
        "session_volume_ts": "2024-01-01T15:00:00+00:00",
        "rvol": {
            "status": rvol_status,
            "multiple": rvol_multiple,
            "bucket_label": "15:00",
            "baseline_volume": 900000,
            "baseline_days_used": 20,
            "baseline_lookback_days": 20,
        },
        "momentum": {
            "status": "ok",
            "return_pct": 0.0,
            "lookback_minutes": 60.0,
            "anchor_age_minutes": 60.0,
            "sample_count": 10,
            "direction": "flat",
        },
        "source": "test",
        "freshness": freshness,
    }


def make_snapshot(
    underlying_price: float,
    rows: list[dict],
    *,
    underlying_market: dict | None = None,
    timestamp: str = "2024-01-01T15:00:00+00:00",
) -> MarketSnapshot:
    payload = {
        "timestamp": timestamp,
        "snapshot_time": timestamp,
        "expiration": "2024-01-01",
        "underlying_price": underlying_price,
        "rows": rows,
    }
    if underlying_market is not None:
        payload["underlying_market"] = underlying_market
    return MarketSnapshot.from_payload(url="test://snapshot", payload=payload, raw=b"{}")


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = 400.0,
    underlying_market: dict | None = None,
) -> StrategyContext:
    snapshot = make_snapshot(
        underlying_price,
        rows if rows is not None else [CALL_ROW, PUT_ROW],
        underlying_market=underlying_market,
    )
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={},
        book=book,
        now_et=None,
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


def scenario_registered() -> None:
    print("\n1. Registration")
    check("vwap_fade_qqq is registered", "vwap_fade_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["vwap_fade_qqq"], "strategy_id", None) == vf.STRATEGY_ID
        and getattr(REGISTRY["vwap_fade_qqq"], "strategy_version", None) == vf.STRATEGY_VERSION,
    )


def scenario_market_closed() -> None:
    print("\n2. Market not open declines")
    ctx = make_ctx(session_phase="premarket")
    decision = vf._decide(ctx)
    check("no_trade when market isn't open", not decision.is_trade)


def scenario_missing_underlying_market_flat() -> None:
    print("\n3. Missing underlying_market while flat -- no_trade")
    ctx = make_ctx(underlying_market=None)
    decision = vf._decide(ctx)
    check("no_trade with no underlying market data", not decision.is_trade)
    check("reason cites missing data", "No underlying market data" in decision.reason)


def scenario_missing_underlying_market_positioned_retains() -> None:
    print("\n4. Missing underlying_market while holding a position -- retains, doesn't close")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market=None,
    )
    decision = vf._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining", "Retaining" in decision.reason, decision.reason)


def scenario_stale_freshness_flat() -> None:
    print("\n5. underlying_market present but not live -- no_trade while flat")
    ctx = make_ctx(underlying_market=underlying_market_payload(freshness="stale"))
    decision = vf._decide(ctx)
    check("no_trade on stale freshness", not decision.is_trade)
    check("reason cites freshness", "not live" in decision.reason.lower())


def scenario_stale_freshness_positioned_retains() -> None:
    print("\n6. Stale freshness while holding a position -- retains")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market=underlying_market_payload(freshness="stale"),
    )
    decision = vf._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining", "Retaining" in decision.reason, decision.reason)


def scenario_rvol_not_ok_flat() -> None:
    print("\n7. rvol_status not 'ok' -- no_trade while flat")
    ctx = make_ctx(underlying_market=underlying_market_payload(rvol_status="insufficient_history"))
    decision = vf._decide(ctx)
    check("no_trade when RVOL baseline not trustworthy", not decision.is_trade)
    check("reason cites rvol_status", "rvol_status" in decision.reason)


def scenario_rvol_not_ok_positioned_retains() -> None:
    print("\n8. rvol_status not 'ok' while holding a position -- retains")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market=underlying_market_payload(rvol_status="no_data"),
    )
    decision = vf._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining", "Retaining" in decision.reason, decision.reason)


def scenario_within_band_no_trade() -> None:
    print("\n9. Deviation within the fade band -- no_trade while flat")
    ctx = make_ctx(underlying_market=underlying_market_payload(price_vs_vwap_pct=0.1, rvol_multiple=0.8))
    decision = vf._decide(ctx)
    check("no_trade -- inside the band", not decision.is_trade, decision.to_dict())
    check("reason cites the fade band", "fade band" in decision.reason)


def scenario_within_band_positioned_closes() -> None:
    print("\n10. Deviation collapses back within band while holding a position -- closes it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.1, rvol_multiple=0.8),
    )
    decision = vf._decide(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000")


def scenario_extreme_above_low_rvol_buys_put() -> None:
    print("\n11. Price far above VWAP + RVOL below ceiling -- buy put (fade the extension down)")
    ctx = make_ctx(
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.0),
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = vf._decide(ctx)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_extreme_below_low_rvol_buys_call() -> None:
    print("\n12. Price far below VWAP + RVOL below ceiling -- buy call (fade the extension up)")
    ctx = make_ctx(
        underlying_market=underlying_market_payload(price_vs_vwap_pct=-0.9, rvol_multiple=1.2),
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vf._decide(ctx)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_extreme_deviation_rvol_at_ceiling_no_trade() -> None:
    print("\n13. Extreme deviation but RVOL at the ceiling -- no_trade (looks confirmed, don't fight it)")
    ctx = make_ctx(underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.5))
    decision = vf._decide(ctx)
    check("no_trade -- RVOL at ceiling", not decision.is_trade, decision.to_dict())
    check("reason cites the ceiling", "ceiling" in decision.reason.lower())


def scenario_extreme_deviation_rvol_above_ceiling_no_trade() -> None:
    print("\n14. Extreme deviation but RVOL above the ceiling -- no_trade")
    ctx = make_ctx(underlying_market=underlying_market_payload(price_vs_vwap_pct=-1.0, rvol_multiple=2.5))
    decision = vf._decide(ctx)
    check("no_trade -- RVOL above ceiling", not decision.is_trade, decision.to_dict())


def scenario_rvol_ceiling_breach_while_positioned_closes() -> None:
    print("\n15. RVOL climbs to/above ceiling while holding a fade position -- closes it")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.9, rvol_multiple=1.6),
    )
    decision = vf._decide(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held put", decision.symbol == "QQQ240101P00400000")


def scenario_already_positioned_holds() -> None:
    print("\n16. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.0),
    )
    decision = vf._decide(ctx)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_setup_flips_direction_closes_opposite() -> None:
    print("\n17. Fade setup flips direction while holding the other side -- closes first")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.0),
    )
    decision = vf._decide(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale call position", decision.symbol == "QQQ240101C00400000")


def scenario_stale_quote_declines() -> None:
    print("\n18. Fade setup supports a trade but stale quote declines")
    ctx = make_ctx(
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.0),
        quote_map={"QQQ240101P00400000": stale_quote("QQQ240101P00400000")},
    )
    decision = vf._decide(ctx)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n19. Unexpected short position -- stand down")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(trades=trades, underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.0))
    decision = vf._decide(ctx)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n20. More than one open position -- stand down")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(trades=trades, underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.0))
    decision = vf._decide(ctx)
    check("no_trade with more than one open position", not decision.is_trade)


def scenario_custom_thresholds() -> None:
    print("\n21. Custom fade_threshold_pct/rvol_ceiling params are honored")
    ctx = make_ctx(
        params={"fade_threshold_pct": 1.0, "rvol_ceiling": 2.0},
        underlying_market=underlying_market_payload(price_vs_vwap_pct=0.8, rvol_multiple=1.8),
    )
    decision = vf._decide(ctx)
    check(
        "no_trade -- 0.8% deviation doesn't clear the widened 1.0% threshold",
        not decision.is_trade,
        decision.to_dict(),
    )

    ctx2 = make_ctx(
        params={"fade_threshold_pct": 1.0, "rvol_ceiling": 2.0},
        underlying_market=underlying_market_payload(price_vs_vwap_pct=1.2, rvol_multiple=1.8),
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision2 = vf._decide(ctx2)
    check(
        "buy -- 1.2% deviation clears widened threshold and RVOL 1.8 is below widened 2.0 ceiling",
        decision2.action == "buy",
        decision2.to_dict(),
    )


def scenario_metadata_present_on_every_branch() -> None:
    print("\n22. Metadata carries the key fields on every branch")
    ctx = make_ctx(underlying_market=underlying_market_payload(price_vs_vwap_pct=0.1, rvol_multiple=0.8))
    decision = vf._decide(ctx)
    meta = decision.metadata or {}
    for key in ("price_vs_vwap_pct", "vwap", "rvol_multiple", "rvol_ceiling", "fade_threshold_pct", "freshness", "rvol_status"):
        check(f"metadata carries {key}", key in meta, meta)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_market_closed,
        scenario_missing_underlying_market_flat,
        scenario_missing_underlying_market_positioned_retains,
        scenario_stale_freshness_flat,
        scenario_stale_freshness_positioned_retains,
        scenario_rvol_not_ok_flat,
        scenario_rvol_not_ok_positioned_retains,
        scenario_within_band_no_trade,
        scenario_within_band_positioned_closes,
        scenario_extreme_above_low_rvol_buys_put,
        scenario_extreme_below_low_rvol_buys_call,
        scenario_extreme_deviation_rvol_at_ceiling_no_trade,
        scenario_extreme_deviation_rvol_above_ceiling_no_trade,
        scenario_rvol_ceiling_breach_while_positioned_closes,
        scenario_already_positioned_holds,
        scenario_setup_flips_direction_closes_opposite,
        scenario_stale_quote_declines,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_custom_thresholds,
        scenario_metadata_present_on_every_branch,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
