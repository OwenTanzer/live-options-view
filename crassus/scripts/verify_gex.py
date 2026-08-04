#!/usr/bin/env python3
"""Prove the gex (gamma_exposure_qqq) strategy's GEX math and decision logic.

Hermetic like verify_momentum_qqq.py, which this mirrors in shape (see
gex.py's module docstring for why the position-management shape is
identical to momentum_qqq's): no network access, no real snapshot fetch.
`compute_gex()` is exercised with hand-built strike tables computable by
hand; `_decide_core()` is exercised directly with hand-built `GexResult`/
trend objects -- it has no network or clock dependency. `_decide()` itself
is exercised for the snapshot-timestamp recording, dedup, staleness, and
full-cycle trend behavior, same division of labor as momentum_qqq's script.

    python scripts/verify_gex.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import gex  # noqa: E402
from crassus.strategies.gex import GexReading, GexResult, compute_gex  # noqa: E402
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
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW], timestamp=snapshot_timestamp)
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={},
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


def _gex_result(**overrides) -> GexResult:
    base = dict(status="ok", net_gex=20000.0, flip_strike=108.33, regime="negative", usable_strikes=4, per_strike={})
    base.update(overrides)
    return GexResult(**base)


def _trend(**overrides) -> dict:
    base = dict(status="ok", trend="worsening", reading_count=2, previous_net_gex=20000.0, latest_net_gex=10000.0)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# compute_gex() -- pure GEX math
# ---------------------------------------------------------------------------

# Four-strike hand table used across several scenarios:
#   strike 95:  put,  Gamma=0.05, OI=100 -> contribution = 0.05*100*10000*-1  = -50000
#   strike 100: put,  Gamma=0.03, OI=100 -> contribution = 0.03*100*10000*-1  = -30000
#   strike 105: call, Gamma=0.04, OI=100 -> contribution = 0.04*100*10000*+1  = +40000
#   strike 110: call, Gamma=0.06, OI=100 -> contribution = 0.06*100*10000*+1  = +60000
# (U=100, so U**2*100*0.01 == 10000, hence "Gamma*OI*10000*sign")
# net_gex = -50000-30000+40000+60000 = 20000
# cumulative: 95:-50000  100:-80000  105:-40000  110:+20000
# sign change between 105 (-40000) and 110 (+20000):
#   flip = 105 + (0-(-40000))/(20000-(-40000)) * (110-105) = 105 + 40000/60000*5 = 108.3333...
TABLE_A = [
    {"OptionSymbol": "QQQ240101P00095000", "Strike": 95.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.05, "OpenInterest": 100},
    {"OptionSymbol": "QQQ240101P00100000", "Strike": 100.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.03, "OpenInterest": 100},
    {"OptionSymbol": "QQQ240101C00105000", "Strike": 105.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.04, "OpenInterest": 100},
    {"OptionSymbol": "QQQ240101C00110000", "Strike": 110.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.06, "OpenInterest": 100},
]

# Same shape, heavier put side at 95 -> a lower (more negative-moving) net
# GEX and a slightly higher flip strike:
#   strike 95: put, Gamma=0.06, OI=100 -> -60000
#   others unchanged -> net_gex = -60000-30000+40000+60000 = 10000
#   cumulative: 95:-60000 100:-90000 105:-50000 110:+10000
#   flip = 105 + 50000/60000*5 = 109.1666...
TABLE_B = [
    {"OptionSymbol": "QQQ240101P00095000", "Strike": 95.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.06, "OpenInterest": 100},
    {"OptionSymbol": "QQQ240101P00100000", "Strike": 100.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.03, "OpenInterest": 100},
    {"OptionSymbol": "QQQ240101C00105000", "Strike": 105.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.04, "OpenInterest": 100},
    {"OptionSymbol": "QQQ240101C00110000", "Strike": 110.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "Gamma": 0.06, "OpenInterest": 100},
]


def scenario_registered() -> None:
    print("\n1. Registration")
    check("gamma_exposure_qqq is registered", "gamma_exposure_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["gamma_exposure_qqq"], "strategy_id", None) == gex.STRATEGY_ID
        and getattr(REGISTRY["gamma_exposure_qqq"], "strategy_version", None) == gex.STRATEGY_VERSION,
    )


def scenario_sign_convention() -> None:
    print("\n2. compute_gex(): sign convention -- one call, one put, hand-computed by hand")
    # U=10 -> U**2*100*0.01 = 100, so contribution = Gamma*OI*100*sign
    # call: Gamma=0.02, OI=50 -> 0.02*50*100 = +100
    # put:  Gamma=0.02, OI=50 -> 0.02*50*100 = -100
    rows = [
        {"OptionSymbol": "Q1", "Strike": 100.0, "Type": "call", "Gamma": 0.02, "OpenInterest": 50},
        {"OptionSymbol": "Q2", "Strike": 100.0, "Type": "put", "Gamma": 0.02, "OpenInterest": 50},
    ]
    result = compute_gex(rows, underlying_price=10.0, min_strikes_with_gamma=1)
    check("status is ok", result.status == "ok", result.status)
    check("call and put contributions net to zero at the same strike", abs(result.net_gex - 0.0) < 1e-6, result.net_gex)
    check("per_strike carries the netted contribution", abs(result.per_strike[100.0] - 0.0) < 1e-6, result.per_strike)

    call_only = compute_gex(rows[:1], underlying_price=10.0, min_strikes_with_gamma=1)
    check("a lone call contributes positively", call_only.net_gex > 0 and abs(call_only.net_gex - 100.0) < 1e-6, call_only.net_gex)
    put_only = compute_gex(rows[1:], underlying_price=10.0, min_strikes_with_gamma=1)
    check("a lone put contributes negatively", put_only.net_gex < 0 and abs(put_only.net_gex - (-100.0)) < 1e-6, put_only.net_gex)


def scenario_insufficient_data() -> None:
    print("\n3. compute_gex(): fewer usable strikes than min_strikes_with_gamma")
    rows = [
        {"OptionSymbol": "Q1", "Strike": 100.0, "Type": "call", "Gamma": 0.02, "OpenInterest": 50},
        {"OptionSymbol": "Q2", "Strike": 105.0, "Type": "put", "Gamma": 0.02, "OpenInterest": 50},
    ]
    result = compute_gex(rows, underlying_price=10.0, min_strikes_with_gamma=5)
    check("status is insufficient_data", result.status == "insufficient_data", result.status)
    check("net_gex is None", result.net_gex is None)
    check("flip_strike is None", result.flip_strike is None)
    check("regime is undetermined", result.regime == "undetermined", result.regime)
    check("usable_strikes reflects what was actually usable", result.usable_strikes == 2, result.usable_strikes)


def scenario_missing_greeks_excluded() -> None:
    print("\n3b. compute_gex(): rows missing Gamma/OpenInterest/Type are excluded, not crashed on")
    rows = [
        {"OptionSymbol": "Q1", "Strike": 100.0, "Type": "call", "Gamma": 0.02, "OpenInterest": 50},
        {"OptionSymbol": "Q2", "Strike": 101.0, "Type": "call", "Gamma": None, "OpenInterest": 50},
        {"OptionSymbol": "Q3", "Strike": 102.0, "Type": "call", "Gamma": 0.02, "OpenInterest": None},
        {"OptionSymbol": "Q4", "Strike": 103.0, "Gamma": 0.02, "OpenInterest": 50},  # no Type
    ]
    result = compute_gex(rows, underlying_price=10.0, min_strikes_with_gamma=1)
    check("only the one fully-usable row counts", result.usable_strikes == 1, result.usable_strikes)


def scenario_flip_interpolation() -> None:
    print("\n4. compute_gex(): flip-strike interpolation on the hand-built 4-strike table")
    # underlying_price=100.0 matches TABLE_A's hand-computed comment
    # (net_gex, cumulative walk, and flip are all derived assuming U=100);
    # the flip strike itself is independent of U (a common scalar factor
    # cancels out of the sign-crossing interpolation), so it is exercised
    # at 100.0 here and re-used for the regime checks below.
    result = compute_gex(TABLE_A, underlying_price=100.0, min_strikes_with_gamma=4)
    check("status is ok", result.status == "ok", result.status)
    check("net_gex matches hand computation (20000)", abs(result.net_gex - 20000.0) < 1e-6, result.net_gex)
    check(
        "flip_strike interpolates to 108.3333... between straddling strikes 105 and 110",
        abs(result.flip_strike - (105.0 + 40000.0 / 60000.0 * 5.0)) < 1e-6,
        result.flip_strike,
    )
    check("underlying below the flip -> negative regime", result.regime == "negative", result.regime)

    above = compute_gex(TABLE_A, underlying_price=110.0, min_strikes_with_gamma=4)
    check("underlying at/above the flip -> positive regime", above.regime == "positive", above.regime)


def scenario_no_crossing_undetermined() -> None:
    print("\n5. compute_gex(): cumulative GEX never crosses zero -> no flip in range")
    rows = [
        {"OptionSymbol": "Q1", "Strike": 100.0, "Type": "call", "Gamma": 0.02, "OpenInterest": 50},
        {"OptionSymbol": "Q2", "Strike": 105.0, "Type": "call", "Gamma": 0.02, "OpenInterest": 50},
        {"OptionSymbol": "Q3", "Strike": 110.0, "Type": "call", "Gamma": 0.02, "OpenInterest": 50},
    ]
    result = compute_gex(rows, underlying_price=105.0, min_strikes_with_gamma=3)
    check("status is ok (data was usable)", result.status == "ok", result.status)
    check("flip_strike is None -- cumulative GEX stays positive throughout", result.flip_strike is None, result.flip_strike)
    check("regime is undetermined", result.regime == "undetermined", result.regime)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n6. Market not open declines without touching GEX")
    ctx = make_ctx(session_phase="premarket")
    decision = gex._decide_core(ctx, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n7. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = gex._decide_core(ctx, _gex_result(), _trend())
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unexpected_short_stands_down() -> None:
    print("\n8. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = gex._decide_core(ctx, _gex_result(), _trend())
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n9. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = gex._decide_core(ctx, _gex_result(), _trend())
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def scenario_stale_source_declines_while_flat() -> None:
    print("\n10. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = gex._decide_core(ctx, None, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)
    check("reason cites the stale source", "stale" in decision.reason.lower())
    check("metadata still carries regime/net_gex/flip_strike keys (as None/undetermined)",
          decision.metadata is not None and "net_gex" in decision.metadata and "regime" in decision.metadata
          and "flip_strike" in decision.metadata and "net_gex_trend" in decision.metadata)


def scenario_stale_source_while_positioned_retains_not_sells() -> None:
    print("\n11. Stale source while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = gex._decide_core(ctx, None, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_insufficient_data_while_flat() -> None:
    print("\n12. Insufficient GEX data while flat declines")
    ctx = make_ctx(session_phase="open")
    result = _gex_result(status="insufficient_data", net_gex=None, flip_strike=None, regime="undetermined", usable_strikes=2)
    decision = gex._decide_core(ctx, result, None)
    check("no_trade on insufficient data", not decision.is_trade)
    check("reason cites the strike count", "2" in decision.reason)


def scenario_insufficient_data_while_positioned_closes() -> None:
    print("\n13. Insufficient GEX data while holding a position -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    result = _gex_result(status="insufficient_data", net_gex=None, flip_strike=None, regime="undetermined", usable_strikes=1)
    decision = gex._decide_core(ctx, result, None)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_undetermined_regime_no_trade() -> None:
    print("\n14. No flip found (undetermined regime) declines while flat")
    ctx = make_ctx(session_phase="open")
    result = _gex_result(status="ok", net_gex=5000.0, flip_strike=None, regime="undetermined", usable_strikes=5)
    decision = gex._decide_core(ctx, result, None)
    check("no_trade when regime is undetermined", not decision.is_trade)
    check("reason cites no flip found", "flip" in decision.reason.lower())


def scenario_positive_regime_no_trade_while_flat() -> None:
    print("\n15. Positive/long-gamma regime -- stand down, no directional edge from regime alone")
    ctx = make_ctx(session_phase="open")
    result = _gex_result(status="ok", net_gex=50000.0, flip_strike=390.0, regime="positive", usable_strikes=5)
    decision = gex._decide_core(ctx, result, None)
    check("no_trade in a positive-gamma regime", not decision.is_trade)
    check("reason names the positive/long-gamma regime", "positive" in decision.reason.lower())
    check("metadata surfaces regime='positive'", decision.metadata["regime"] == "positive")


def scenario_positive_regime_while_positioned_closes() -> None:
    print("\n16. Positive regime while holding a put -- close it, positive regime doesn't support any position")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    result = _gex_result(status="ok", net_gex=50000.0, flip_strike=390.0, regime="positive", usable_strikes=5)
    decision = gex._decide_core(ctx, result, None)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held put", decision.symbol == "QQQ240101P00400000")


def scenario_negative_regime_warming_up() -> None:
    print("\n17. Negative regime but fewer than two net-GEX readings -- warming up, no trade")
    ctx = make_ctx(session_phase="open")
    result = _gex_result(regime="negative", flip_strike=420.0)
    trend = _trend(status="warming_up", trend=None, reading_count=1, previous_net_gex=None, latest_net_gex=20000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("no_trade while warming up", not decision.is_trade)
    check("reason cites warming up", "warming up" in decision.reason.lower())


def scenario_negative_regime_improving_no_trade() -> None:
    print("\n18. Negative regime, net GEX improving (recovering toward flip) -- no strong signal")
    ctx = make_ctx(session_phase="open")
    result = _gex_result(regime="negative", flip_strike=420.0)
    trend = _trend(trend="improving", previous_net_gex=10000.0, latest_net_gex=20000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("no_trade when net GEX is improving, not worsening", not decision.is_trade)
    check("reason cites the trend", "improving" in decision.reason.lower())


def scenario_negative_regime_flat_trend_no_trade() -> None:
    print("\n18b. Negative regime, net GEX flat (unchanged) -- no strong signal")
    ctx = make_ctx(session_phase="open")
    result = _gex_result(regime="negative", flip_strike=420.0)
    trend = _trend(trend="flat", previous_net_gex=20000.0, latest_net_gex=20000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("no_trade when net GEX trend is flat", not decision.is_trade)


def scenario_negative_regime_worsening_opens_put() -> None:
    print("\n19. Negative regime + worsening net GEX + flat + executable quote -> buy one put")
    ctx = make_ctx(session_phase="open", underlying_price=400.0, quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    result = _gex_result(regime="negative", flip_strike=420.0, net_gex=10000.0)
    trend = _trend(trend="worsening", previous_net_gex=20000.0, latest_net_gex=10000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("metadata includes net_gex/flip_strike/regime/trend", {
        "net_gex", "flip_strike", "regime", "net_gex_trend"
    }.issubset(decision.metadata.keys()))


def scenario_negative_regime_worsening_already_holding_put() -> None:
    print("\n20. Already holding a put -- worsening negative regime still supports it, no pyramiding")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    result = _gex_result(regime="negative", flip_strike=420.0, net_gex=10000.0)
    trend = _trend(trend="worsening", previous_net_gex=20000.0, latest_net_gex=10000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_negative_regime_worsening_holding_call_closes_first() -> None:
    print("\n21. Holding a call while the regime now supports a put -- close the call first")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    result = _gex_result(regime="negative", flip_strike=420.0, net_gex=10000.0)
    trend = _trend(trend="worsening", previous_net_gex=20000.0, latest_net_gex=10000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale call position", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_negative_regime_worsening_stale_quote_declines() -> None:
    print("\n22. Worsening negative regime but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": stale_quote("QQQ240101P00400000")})
    result = _gex_result(regime="negative", flip_strike=420.0, net_gex=10000.0)
    trend = _trend(trend="worsening", previous_net_gex=20000.0, latest_net_gex=10000.0)
    decision = gex._decide_core(ctx, result, trend)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


# ---------------------------------------------------------------------------
# _decide() -- snapshot-timestamp recording, dedup, staleness, and the
# full-cycle net-GEX trend (mirrors momentum_qqq's _decide scenarios)
# ---------------------------------------------------------------------------


def _reset_history() -> None:
    gex._history = []
    gex._last_recorded_snapshot = None


def scenario_decide_records_using_snapshot_timestamp_not_now_et() -> None:
    print("\n23. _decide(): records observed_at from snapshot.timestamp, not ctx.now_et")
    _reset_history()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00",
        rows=TABLE_A, underlying_price=100.0, params={"min_strikes_with_gamma": 4},
    )
    gex._decide(ctx)
    check("exactly one reading recorded", len(gex._history) == 1, len(gex._history))
    check(
        "recorded observed_at matches the snapshot's own timestamp, not the runner's now_et",
        gex._history[0].observed_at == datetime(2026, 1, 1, 14, 58, tzinfo=timezone.utc),
        gex._history[0].observed_at,
    )
    check("recorded net_gex matches the hand-computed value (20000)", abs(gex._history[0].net_gex - 20000.0) < 1e-6)


def scenario_decide_dedupes_identical_snapshot() -> None:
    print("\n24. _decide(): repeated reads of the same unchanged snapshot are not double-recorded")
    _reset_history()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx1 = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T15:00:00+00:00",
        rows=TABLE_A, underlying_price=105.0, params={"min_strikes_with_gamma": 4},
    )
    ctx2 = make_ctx(
        session_phase="open", now_et=now + timedelta(minutes=1), snapshot_timestamp="2026-01-01T15:00:00+00:00",
        rows=TABLE_A, underlying_price=105.0, params={"min_strikes_with_gamma": 4},
    )
    gex._decide(ctx1)
    gex._decide(ctx2)
    check(
        "only one reading recorded despite two decide() calls against the same (unrepublished) snapshot",
        len(gex._history) == 1,
        len(gex._history),
    )


def scenario_decide_rejects_stale_snapshot_while_flat() -> None:
    print("\n25. _decide(): a snapshot far older than the runner's clock is rejected as a stale source, not recorded")
    _reset_history()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00")  # 6 hours old
    decision = gex._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", len(gex._history) == 0, len(gex._history))
    check("reason cites the stale source", "stale" in decision.reason.lower() or "stalled" in decision.reason.lower(), decision.reason)


def scenario_decide_rejects_stale_snapshot_while_positioned() -> None:
    print("\n26. _decide(): a stale source snapshot while holding a position retains it rather than closing on a fabricated read")
    _reset_history()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00",
        trades=trades, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = gex._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)


def scenario_decide_full_cycle_worsening_opens_put() -> None:
    print("\n27. _decide(): two cycles with worsening net GEX in a negative regime -> second cycle buys a put")
    _reset_history()
    now1 = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    now2 = now1 + timedelta(minutes=1)
    ctx1 = make_ctx(
        session_phase="open", now_et=now1, snapshot_timestamp="2026-01-01T14:59:30+00:00",
        rows=TABLE_A, underlying_price=105.0, params={"min_strikes_with_gamma": 4},
    )
    decision1 = gex._decide(ctx1)
    check("first cycle (only one reading) doesn't trade", not decision1.is_trade, decision1.to_dict())

    ctx2 = make_ctx(
        session_phase="open", now_et=now2, snapshot_timestamp="2026-01-01T15:00:30+00:00",
        rows=TABLE_B, underlying_price=105.0, params={"min_strikes_with_gamma": 4},
        quote_map={"QQQ240101P00100000": fresh_quote("QQQ240101P00100000")},
    )
    # TABLE_B nets to 10000 (down from TABLE_A's 20000) -- net GEX getting
    # more negative (worsening) -- with the underlying still below the new
    # (slightly higher) flip strike, still a negative regime. TABLE_A/TABLE_B
    # only quote puts at strikes 95/100 (calls sit at 105/110), so the ATM put
    # nearest the 105 underlying is the 100 strike, not a nonexistent 105 put.
    decision2 = gex._decide(ctx2)
    check("second cycle recognizes the worsening trend and buys a put", decision2.action == "buy", decision2.to_dict())
    check("targets the ATM put at the traded strike", decision2.symbol == "QQQ240101P00100000", decision2.symbol)
    check("metadata reports the worsening trend", decision2.metadata.get("net_gex_trend") == "worsening", decision2.metadata)


def scenario_decide_full_cycle_improving_no_trade() -> None:
    print("\n28. _decide(): two cycles with improving net GEX in a negative regime -> second cycle stands down")
    _reset_history()
    now1 = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    now2 = now1 + timedelta(minutes=1)
    ctx1 = make_ctx(
        session_phase="open", now_et=now1, snapshot_timestamp="2026-01-01T14:59:30+00:00",
        rows=TABLE_B, underlying_price=105.0, params={"min_strikes_with_gamma": 4},
    )
    gex._decide(ctx1)

    ctx2 = make_ctx(
        session_phase="open", now_et=now2, snapshot_timestamp="2026-01-01T15:00:30+00:00",
        rows=TABLE_A, underlying_price=105.0, params={"min_strikes_with_gamma": 4},
    )
    # TABLE_A nets to 20000, up from TABLE_B's 10000 -- net GEX improving,
    # not worsening -- even though the regime is still negative.
    decision2 = gex._decide(ctx2)
    check("second cycle does not trade on an improving trend", not decision2.is_trade, decision2.to_dict())
    check("metadata reports the improving trend", decision2.metadata.get("net_gex_trend") == "improving", decision2.metadata)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_sign_convention,
        scenario_insufficient_data,
        scenario_missing_greeks_excluded,
        scenario_flip_interpolation,
        scenario_no_crossing_undetermined,
        scenario_market_closed,
        scenario_multiple_open_positions_stand_down,
        scenario_unexpected_short_stands_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_stale_source_declines_while_flat,
        scenario_stale_source_while_positioned_retains_not_sells,
        scenario_insufficient_data_while_flat,
        scenario_insufficient_data_while_positioned_closes,
        scenario_undetermined_regime_no_trade,
        scenario_positive_regime_no_trade_while_flat,
        scenario_positive_regime_while_positioned_closes,
        scenario_negative_regime_warming_up,
        scenario_negative_regime_improving_no_trade,
        scenario_negative_regime_flat_trend_no_trade,
        scenario_negative_regime_worsening_opens_put,
        scenario_negative_regime_worsening_already_holding_put,
        scenario_negative_regime_worsening_holding_call_closes_first,
        scenario_negative_regime_worsening_stale_quote_declines,
        scenario_decide_records_using_snapshot_timestamp_not_now_et,
        scenario_decide_dedupes_identical_snapshot,
        scenario_decide_rejects_stale_snapshot_while_flat,
        scenario_decide_rejects_stale_snapshot_while_positioned,
        scenario_decide_full_cycle_worsening_opens_put,
        scenario_decide_full_cycle_improving_no_trade,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
