#!/usr/bin/env python3
"""Prove the oi_skew_qqq strategy's decision logic and OI math.

Hermetic like verify_trump_whisperer.py / verify_reddit_sentiment.py: no
network access. `_near_money_oi()` is exercised with a hand-built rows
table; `SessionImbalanceTracker` is exercised directly for its dedup/reset
discipline; the strategy's `_decide_core()` is exercised with a hand-built
`StrategyContext` and an explicit tracker instance -- it takes no reader
and makes no I/O.

    python scripts/verify_oi_skew.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import oi_skew as oisk  # noqa: E402
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


def make_snapshot(underlying_price: float, rows: list[dict], timestamp: str = "2024-01-01T15:00:00+00:00") -> MarketSnapshot:
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


# Near-money band is +/-2% of 400 = [392, 408]. Strikes 393/397/400/403/407
# are in-band (5 strikes, clearing the default min_band_strikes=4); 380/420
# are out-of-band and must not contribute to the sums.
def _row(symbol_strike: str, strike: float, option_type: str, oi: int) -> dict:
    letter = "C" if option_type == "call" else "P"
    return {
        "OptionSymbol": f"QQQ240101{letter}{symbol_strike}",
        "Strike": strike,
        "Type": option_type,
        "Bid": 1.0,
        "Ask": 1.1,
        "OpenInterest": oi,
    }


CALL_400 = _row("00400000", 400.0, "call", 1000)
PUT_400 = _row("00400000", 400.0, "put", 200)
CALL_420_OUT_OF_BAND = _row("00420000", 420.0, "call", 5000)
PUT_380_OUT_OF_BAND = _row("00380000", 380.0, "put", 5000)

SKEWED_BULLISH_ROWS = [
    _row("00393000", 393.0, "call", 400), _row("00393000", 393.0, "put", 100),
    _row("00397000", 397.0, "call", 500), _row("00397000", 397.0, "put", 100),
    CALL_400, PUT_400,
    _row("00403000", 403.0, "call", 300), _row("00403000", 403.0, "put", 50),
    _row("00407000", 407.0, "call", 200), _row("00407000", 407.0, "put", 50),
    CALL_420_OUT_OF_BAND, PUT_380_OUT_OF_BAND,
]
# call_oi = 400+500+1000+300+200 = 2400, put_oi = 100+100+200+50+50 = 500
# total = 2900, imbalance = (2400-500)/2900 = 0.6551724137931034, band_strikes = 5

SKEWED_BEARISH_ROWS = [
    _row("00393000", 393.0, "call", 100), _row("00393000", 393.0, "put", 400),
    _row("00397000", 397.0, "call", 100), _row("00397000", 397.0, "put", 500),
    _row("00400000", 400.0, "call", 200), _row("00400000", 400.0, "put", 1000),
    _row("00403000", 403.0, "call", 50), _row("00403000", 403.0, "put", 300),
    _row("00407000", 407.0, "call", 50), _row("00407000", 407.0, "put", 200),
]
# call_oi = 500, put_oi = 2400, total = 2900, imbalance = -0.6551724137931034

FLAT_ROWS = [
    _row("00393000", 393.0, "call", 100), _row("00393000", 393.0, "put", 100),
    _row("00397000", 397.0, "call", 200), _row("00397000", 397.0, "put", 200),
    _row("00400000", 400.0, "call", 500), _row("00400000", 400.0, "put", 500),
    _row("00403000", 403.0, "call", 150), _row("00403000", 403.0, "put", 150),
    _row("00407000", 407.0, "call", 50), _row("00407000", 407.0, "put", 50),
]
# call_oi == put_oi at every strike -> imbalance = 0.0, band_strikes = 5

THIN_ROWS = [CALL_400, PUT_400]  # only one strike in band -> band_strikes = 1 < default min of 4

NO_OI_ROWS = [
    {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 0},
    {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 0},
]


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = 400.0,
    timestamp: str = "2024-01-01T15:00:00+00:00",
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else SKEWED_BULLISH_ROWS, timestamp)
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


def seeded_tracker(readings: list[tuple[str, float]]) -> oisk.SessionImbalanceTracker:
    """A tracker pre-loaded with a sequence of (timestamp, imbalance) reads,
    simulating prior cycles this session before the cycle under test."""
    tracker = oisk.SessionImbalanceTracker()
    for ts, imbalance in readings:
        tracker.observe(ts, imbalance)
    return tracker


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("oi_skew_qqq is registered", "oi_skew_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["oi_skew_qqq"], "strategy_id", None) == oisk.STRATEGY_ID
        and getattr(REGISTRY["oi_skew_qqq"], "strategy_version", None) == oisk.STRATEGY_VERSION,
    )


# ---------------------------------------------------------------------------
# _near_money_oi() -- pure OI aggregation math
# ---------------------------------------------------------------------------


def scenario_near_money_oi_math() -> None:
    print("\n2. _near_money_oi(): band filtering and call/put sums")
    snapshot = make_snapshot(400.0, SKEWED_BULLISH_ROWS)
    call_oi, put_oi, band_strikes = oisk._near_money_oi(snapshot, 0.02)
    check("call_oi sums only in-band call rows", call_oi == 2400.0, call_oi)
    check("put_oi sums only in-band put rows", put_oi == 500.0, put_oi)
    check("band_strikes counts distinct in-band strikes with OI", band_strikes == 5, band_strikes)


def scenario_near_money_oi_excludes_zero_oi() -> None:
    print("\n3. _near_money_oi(): zero-OI rows don't count toward band coverage")
    snapshot = make_snapshot(400.0, NO_OI_ROWS)
    call_oi, put_oi, band_strikes = oisk._near_money_oi(snapshot, 0.02)
    check("call_oi is zero", call_oi == 0.0)
    check("put_oi is zero", put_oi == 0.0)
    check("band_strikes is zero -- no OI anywhere in band", band_strikes == 0, band_strikes)


# ---------------------------------------------------------------------------
# SessionImbalanceTracker -- dedup/reset discipline
# ---------------------------------------------------------------------------


def scenario_tracker_records_new_and_skips_duplicate() -> None:
    print("\n4. SessionImbalanceTracker: records strictly-newer timestamps only")
    tracker = oisk.SessionImbalanceTracker()
    check("first observation recorded", tracker.observe("2024-01-01T14:00:00+00:00", 0.1) is True)
    check("duplicate timestamp not recorded", tracker.observe("2024-01-01T14:00:00+00:00", 0.5) is False)
    check("out-of-order (older) timestamp not recorded", tracker.observe("2024-01-01T13:00:00+00:00", 0.5) is False)
    check("newer timestamp recorded", tracker.observe("2024-01-01T14:01:00+00:00", 0.2) is True)
    check("session_start_imbalance is the first recorded reading", tracker.session_start_imbalance == 0.1)


def scenario_tracker_rejects_missing_timestamp() -> None:
    print("\n5. SessionImbalanceTracker: falsy timestamp is never recorded")
    tracker = oisk.SessionImbalanceTracker()
    check("empty-string timestamp not recorded", tracker.observe("", 0.5) is False)
    check("has_history is False", tracker.has_history is False)


def scenario_tracker_resets_on_new_session_date() -> None:
    print("\n6. SessionImbalanceTracker: new calendar date resets session history")
    tracker = oisk.SessionImbalanceTracker()
    tracker.observe("2024-01-01T14:00:00+00:00", 0.1)
    tracker.observe("2024-01-01T15:00:00+00:00", 0.6)
    check("session_start before rollover", tracker.session_start_imbalance == 0.1)
    tracker.observe("2024-01-02T14:00:00+00:00", 0.4)
    check("session_start resets to the new day's first reading", tracker.session_start_imbalance == 0.4)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n7. Market not open declines")
    ctx = make_ctx(session_phase="premarket")
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)
    check("metadata still carries imbalance fields", "imbalance_ratio" in decision.metadata)


def scenario_insufficient_band_coverage() -> None:
    print("\n8. Too few near-money strikes with OI -> no_trade")
    ctx = make_ctx(rows=THIN_ROWS)
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.0)])
    decision = oisk._decide_core(ctx, tracker)
    check("no_trade on insufficient band coverage", not decision.is_trade)
    check("reason cites band coverage", "band coverage" in decision.reason.lower(), decision.reason)
    check("band_strikes recorded in metadata", decision.metadata["band_strikes"] == 1, decision.metadata)


def scenario_no_oi_in_band() -> None:
    print("\n9. Zero OI anywhere in the near-money band -> no_trade")
    ctx = make_ctx(rows=NO_OI_ROWS)
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade with no OI in band", not decision.is_trade)
    check("imbalance_ratio is None in metadata", decision.metadata["imbalance_ratio"] is None)


def scenario_threshold_not_met() -> None:
    print("\n10. Imbalance below threshold -> no_trade")
    ctx = make_ctx(rows=FLAT_ROWS)  # imbalance = 0.0
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade below imbalance_threshold", not decision.is_trade)
    check("reason cites the threshold", "threshold" in decision.reason.lower(), decision.reason)
    check("imbalance_ratio is 0.0", decision.metadata["imbalance_ratio"] == 0.0)


def scenario_threshold_met_no_drift() -> None:
    print("\n11. Threshold cleared but no session drift -> no_trade")
    # Session started at essentially the same skew as now -- no meaningful
    # change from session start, so this should not trade even though the
    # absolute imbalance clears the threshold.
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.6551724137931034)])
    ctx = make_ctx(rows=SKEWED_BULLISH_ROWS, timestamp="2024-01-01T15:00:00+00:00")
    decision = oisk._decide_core(ctx, tracker)
    check("no_trade despite threshold being cleared", not decision.is_trade, decision.to_dict())
    check("reason cites session drift", "session" in decision.reason.lower(), decision.reason)
    check(
        "delta_from_session_start is near zero",
        abs(decision.metadata["delta_from_session_start"]) < 1e-6,
        decision.metadata,
    )


def scenario_threshold_met_with_drift_buys_call() -> None:
    print("\n12. Threshold cleared and imbalance drifted bullish from session start -> buy call")
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.0)])
    ctx = make_ctx(
        rows=SKEWED_BULLISH_ROWS,
        timestamp="2024-01-01T15:00:00+00:00",
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = oisk._decide_core(ctx, tracker)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("metadata carries all required OI fields", all(
        k in decision.metadata for k in (
            "near_money_call_oi", "near_money_put_oi", "imbalance_ratio",
            "session_start_imbalance", "delta_from_session_start",
        )
    ), decision.metadata)


def scenario_threshold_met_with_drift_buys_put() -> None:
    print("\n13. Threshold cleared and imbalance drifted bearish from session start -> buy put")
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.0)])
    ctx = make_ctx(
        rows=SKEWED_BEARISH_ROWS,
        timestamp="2024-01-01T15:00:00+00:00",
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = oisk._decide_core(ctx, tracker)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n14. Signal supports a trade but quote is stale -> no_trade")
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.0)])
    ctx = make_ctx(
        rows=SKEWED_BULLISH_ROWS,
        timestamp="2024-01-01T15:00:00+00:00",
        quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")},
    )
    decision = oisk._decide_core(ctx, tracker)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_held_position_closed_when_unsupported() -> None:
    print("\n15. Held call, imbalance now flat -> close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        rows=FLAT_ROWS,
        trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_held_position_closed_on_opposite_skew() -> None:
    print("\n16. Held put, skew now strongly bullish with drift -> close the put")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.0)])
    ctx = make_ctx(
        rows=SKEWED_BULLISH_ROWS,
        trades=trades,
        timestamp="2024-01-01T15:00:00+00:00",
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = oisk._decide_core(ctx, tracker)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_held_position_retained_when_still_supported() -> None:
    print("\n17. Held call, skew still bullish and drifted -> retain, no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    tracker = seeded_tracker([("2024-01-01T14:00:00+00:00", 0.0)])
    ctx = make_ctx(rows=SKEWED_BULLISH_ROWS, trades=trades, timestamp="2024-01-01T15:00:00+00:00")
    decision = oisk._decide_core(ctx, tracker)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_held_position_retained_on_stale_snapshot() -> None:
    print("\n18. Held call, snapshot timestamp is a duplicate/stale read -> retain")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    # Seed the tracker's last observation at the exact same timestamp the
    # incoming snapshot carries, so observe() reports "not new."
    tracker = seeded_tracker([("2024-01-01T15:00:00+00:00", 0.6551724137931034)])
    ctx = make_ctx(rows=SKEWED_BULLISH_ROWS, trades=trades, timestamp="2024-01-01T15:00:00+00:00")
    decision = oisk._decide_core(ctx, tracker)
    check("no_trade rather than closing on a stale snapshot", not decision.is_trade, decision.to_dict())
    check("retains rather than sells", decision.action != "sell")
    check("reason cites staleness/retention", "stale" in decision.reason.lower() or "retaining" in decision.reason.lower(), decision.reason)


def scenario_held_position_retained_on_missing_timestamp() -> None:
    print("\n19. Held call, snapshot has no timestamp -> retain")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(rows=SKEWED_BULLISH_ROWS, trades=trades, timestamp="")
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade rather than closing on a missing timestamp", not decision.is_trade)
    check("retains rather than sells", decision.action != "sell")


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n20. More than one open position -- stand down")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(trades=trades)
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unexpected_short_stands_down() -> None:
    print("\n21. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(trades=trades)
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n22. Held symbol doesn't parse as an OCC option -- stand down")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(trades=trades)
    decision = oisk._decide_core(ctx, oisk.SessionImbalanceTracker())
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_near_money_oi_math,
        scenario_near_money_oi_excludes_zero_oi,
        scenario_tracker_records_new_and_skips_duplicate,
        scenario_tracker_rejects_missing_timestamp,
        scenario_tracker_resets_on_new_session_date,
        scenario_market_closed,
        scenario_insufficient_band_coverage,
        scenario_no_oi_in_band,
        scenario_threshold_not_met,
        scenario_threshold_met_no_drift,
        scenario_threshold_met_with_drift_buys_call,
        scenario_threshold_met_with_drift_buys_put,
        scenario_stale_quote_declines,
        scenario_held_position_closed_when_unsupported,
        scenario_held_position_closed_on_opposite_skew,
        scenario_held_position_retained_when_still_supported,
        scenario_held_position_retained_on_stale_snapshot,
        scenario_held_position_retained_on_missing_timestamp,
        scenario_multiple_open_positions_stand_down,
        scenario_unexpected_short_stands_down,
        scenario_unrecognized_symbol_stands_down,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
