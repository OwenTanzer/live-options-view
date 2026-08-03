#!/usr/bin/env python3
"""Prove the max_pain_qqq strategy's payout math and decision logic.

Hermetic like verify_reddit_sentiment.py / verify_trump_whisperer.py: no
network access. `max_pain._compute_max_pain()` is exercised with a small
hand-built open-interest table whose minimum is known by hand; the
strategy's `_decide_core()` is exercised directly with a hand-built
`StrategyContext` -- it takes no reader and makes no I/O of its own.

    python scripts/verify_max_pain.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import max_pain as mp  # noqa: E402
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


def make_snapshot(underlying_price: float, rows: list[dict]) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": rows,
        },
        raw=b"{}",
    )


def occ(strike: int, kind: str) -> str:
    letter = "C" if kind == "call" else "P"
    return f"QQQ240101{letter}{strike * 1000:08d}"


def row(strike: int, kind: str, oi: int) -> dict:
    return {
        "OptionSymbol": occ(strike, kind),
        "Strike": float(strike),
        "Type": kind,
        "Bid": 1.0,
        "Ask": 1.1,
        "OpenInterest": oi,
    }


# ---------------------------------------------------------------------------
# A small hand-built chain whose max-pain strike is known by hand:
#   calls: 395 OI=10, 400 OI=5, 405 OI=1
#   puts:  395 OI=1,  400 OI=5, 405 OI=10
#
# payout(395) = 35, payout(400) = 10, payout(405) = 35 -- 400 minimizes it.
# ---------------------------------------------------------------------------
SMALL_CHAIN = [
    row(395, "call", 10), row(400, "call", 5), row(405, "call", 1),
    row(395, "put", 1), row(400, "put", 5), row(405, "put", 10),
]

# ---------------------------------------------------------------------------
# A 5-strike symmetric chain (same OI=100 both sides at every strike) whose
# max-pain strike is the median strike, 400, by symmetry -- used for the
# trading-decision scenarios, which need >= DEFAULT_MIN_STRIKES_WITH_OI (5).
# ---------------------------------------------------------------------------
WIDE_CHAIN = [row(s, "call", 100) for s in (380, 390, 400, 410, 420)] + [
    row(s, "put", 100) for s in (380, 390, 400, 410, 420)
]

# A thin chain: only 3 distinct strikes carry OI on both sides, below the
# default min_strikes_with_oi of 5.
THIN_CHAIN = SMALL_CHAIN


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict],
    underlying_price: float,
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows)
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


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("max_pain_qqq is registered", "max_pain_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["max_pain_qqq"], "strategy_id", None) == mp.STRATEGY_ID
        and getattr(REGISTRY["max_pain_qqq"], "strategy_version", None) == mp.STRATEGY_VERSION,
    )


def scenario_compute_max_pain_small_chain() -> None:
    print("\n2. _compute_max_pain(): known-by-hand minimum on a small chain")
    strike, n_both_sides = mp._compute_max_pain(SMALL_CHAIN)
    check("max-pain strike is 400 (hand-computed minimum payout)", strike == 400.0, strike)
    check("counts 3 strikes with OI on both sides", n_both_sides == 3, n_both_sides)


def scenario_compute_max_pain_symmetric_chain() -> None:
    print("\n3. _compute_max_pain(): symmetric OI -> median strike wins")
    strike, n_both_sides = mp._compute_max_pain(WIDE_CHAIN)
    check("max-pain strike is the median strike (400)", strike == 400.0, strike)
    check("counts 5 strikes with OI on both sides", n_both_sides == 5, n_both_sides)


def scenario_compute_max_pain_no_strikes() -> None:
    print("\n4. _compute_max_pain(): no rows at all -> None, not a crash")
    strike, n_both_sides = mp._compute_max_pain([])
    check("strike is None with no data", strike is None)
    check("both-sides count is zero", n_both_sides == 0, n_both_sides)


def scenario_market_closed() -> None:
    print("\n5. Market not open declines")
    ctx = make_ctx(session_phase="premarket", rows=WIDE_CHAIN, underlying_price=404.0)
    decision = mp._decide_core(ctx)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_insufficient_oi_flat() -> None:
    print("\n6. Insufficient OI data while flat -> no_trade, metadata still carries the numbers")
    ctx = make_ctx(rows=THIN_CHAIN, underlying_price=404.0)
    decision = mp._decide_core(ctx)
    check("no_trade with too few strikes carrying OI on both sides", not decision.is_trade)
    check("reason cites the strike count", "3" in decision.reason and "need 5" in decision.reason, decision.reason)
    check("metadata carries max_pain_strike", decision.metadata["max_pain_strike"] == 400.0, decision.metadata)
    check("metadata carries underlying_price", decision.metadata["underlying_price"] == 404.0)
    check("metadata carries deviation_pct", decision.metadata["deviation_pct"] is not None)


def scenario_within_threshold_flat() -> None:
    print("\n7. Underlying within the pin threshold of max pain -> no_trade")
    # deviation = (400.3 - 400) / 400.3 * 100 ~= 0.075%, inside default 0.15%.
    ctx = make_ctx(rows=WIDE_CHAIN, underlying_price=400.3)
    decision = mp._decide_core(ctx)
    check("no_trade within the threshold band", not decision.is_trade)
    check("reason cites no directional pin bias", "no directional pin bias" in decision.reason.lower(), decision.reason)
    check("metadata carries max_pain_strike=400", decision.metadata["max_pain_strike"] == 400.0)


def scenario_above_max_pain_opens_put() -> None:
    print("\n8. Underlying meaningfully above max pain -> buy one put")
    # deviation = (404 - 400) / 404 * 100 ~= 0.990%, well past 0.15%.
    put_symbol = occ(400, "put")
    ctx = make_ctx(
        rows=WIDE_CHAIN, underlying_price=404.0,
        quote_map={put_symbol: fresh_quote(put_symbol)},
    )
    decision = mp._decide_core(ctx)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == put_symbol, decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("metadata records the max-pain strike", decision.metadata["max_pain_strike"] == 400.0)


def scenario_below_max_pain_opens_call() -> None:
    print("\n9. Underlying meaningfully below max pain -> buy one call")
    # deviation = (396 - 400) / 396 * 100 ~= -1.010%, well past 0.15%.
    call_symbol = occ(400, "call")
    ctx = make_ctx(
        rows=WIDE_CHAIN, underlying_price=396.0,
        quote_map={call_symbol: fresh_quote(call_symbol)},
    )
    decision = mp._decide_core(ctx)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == call_symbol, decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n10. Directional signal present but stale quote declines rather than risking a 409")
    put_symbol = occ(400, "put")
    ctx = make_ctx(
        rows=WIDE_CHAIN, underlying_price=404.0,
        quote_map={put_symbol: stale_quote(put_symbol)},
    )
    decision = mp._decide_core(ctx)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("\n11. Already holding the supported side -- no pyramiding")
    put_symbol = occ(400, "put")
    trades = [{"sym": put_symbol, "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(rows=WIDE_CHAIN, underlying_price=404.0, trades=trades)
    decision = mp._decide_core(ctx)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_signal_flip_closes_position() -> None:
    print("\n12. Bias flips direction while holding the other side -- close first")
    call_symbol = occ(400, "call")
    trades = [{"sym": call_symbol, "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        rows=WIDE_CHAIN, underlying_price=404.0, trades=trades,
        quote_map={call_symbol: fresh_quote(call_symbol)},
    )
    decision = mp._decide_core(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale held call", decision.symbol == call_symbol, decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_signal_disappears_closes_position() -> None:
    print("\n13. Bias goes quiet (within threshold) while holding a position -- close it, don't just decline")
    put_symbol = occ(400, "put")
    trades = [{"sym": put_symbol, "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        rows=WIDE_CHAIN, underlying_price=400.3, trades=trades,
        quote_map={put_symbol: fresh_quote(put_symbol)},
    )
    decision = mp._decide_core(ctx)
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held put", decision.symbol == put_symbol, decision.symbol)


def scenario_insufficient_oi_while_positioned_closes() -> None:
    print("\n14. OI data goes thin while holding a position -- close rather than freeze holding it")
    put_symbol = occ(400, "put")
    trades = [{"sym": put_symbol, "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        rows=THIN_CHAIN, underlying_price=404.0, trades=trades,
        quote_map={put_symbol: fresh_quote(put_symbol)},
    )
    decision = mp._decide_core(ctx)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held put", decision.symbol == put_symbol, decision.symbol)


def scenario_held_position_retained_when_still_supported() -> None:
    print("\n15. Held position still supported across a cycle -- retained, not closed")
    put_symbol = occ(400, "put")
    trades = [{"sym": put_symbol, "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(rows=WIDE_CHAIN, underlying_price=404.0, trades=trades)
    decision = mp._decide_core(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason says already holding, i.e. retained", "Already holding" in decision.reason, decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n16. Unexpected short position -- stand down, don't compound it")
    call_symbol = occ(400, "call")
    trades = [{"sym": call_symbol, "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(rows=WIDE_CHAIN, underlying_price=404.0, trades=trades)
    decision = mp._decide_core(ctx)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n17. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(rows=WIDE_CHAIN, underlying_price=404.0, trades=trades)
    decision = mp._decide_core(ctx)
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n18. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": occ(400, "call"), "side": "buy", "qty": 1, "price": 1.0},
        {"sym": occ(400, "put"), "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(rows=WIDE_CHAIN, underlying_price=404.0, trades=trades)
    decision = mp._decide_core(ctx)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_compute_max_pain_small_chain,
        scenario_compute_max_pain_symmetric_chain,
        scenario_compute_max_pain_no_strikes,
        scenario_market_closed,
        scenario_insufficient_oi_flat,
        scenario_within_threshold_flat,
        scenario_above_max_pain_opens_put,
        scenario_below_max_pain_opens_call,
        scenario_stale_quote_declines,
        scenario_already_positioned_holds,
        scenario_signal_flip_closes_position,
        scenario_signal_disappears_closes_position,
        scenario_insufficient_oi_while_positioned_closes,
        scenario_held_position_retained_when_still_supported,
        scenario_unexpected_short_stands_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_multiple_open_positions_stand_down,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
