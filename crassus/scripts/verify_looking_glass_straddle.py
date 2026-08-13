#!/usr/bin/env python3
"""Prove the looking_glass_straddle strategy's entry/core/runner/terminal
state machine (see crassus/strategies/looking_glass_straddle.py).

Hermetic like verify_flatten.py / verify_max_pain.py: no network access, no
real snapshot fetch. The strategy takes a `StrategyContext` and makes no I/O
beyond `ctx.quotes()`, which every scenario here stubs directly.

    python scripts/verify_looking_glass_straddle.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies.looking_glass_straddle import STRATEGY_ID, _decide  # noqa: E402
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


CALL_SYM = "QQQ240101C00400000"
PUT_SYM = "QQQ240101P00400000"

DEFAULT_PARAMS = {
    "num_straddles": 4,
    "core_sell_count": 3,
    "entry_time_et": "09:30",
    "terminal_exit_time_et": "15:55",
    "core_profit_threshold_pct": 0.20,
    "max_entry_leg_spread_pct": 0.15,
}


def make_snapshot(underlying_price: float = 400.0, rows: list[dict] | None = None) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": rows if rows is not None else [
                {"OptionSymbol": CALL_SYM, "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.05},
                {"OptionSymbol": PUT_SYM, "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.05},
            ],
        },
        raw=b"{}",
    )


def quote(symbol: str, bid: float, ask: float, fresh: bool = True) -> Quote:
    server_ts = "2024-01-01T15:00:05" if fresh else "2024-01-01T15:05:00"
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts="2024-01-01T15:00:00", server_ts=server_ts)


def trade(symbol: str, side: str, qty: int, price: float, ts: str = "2024-01-01T13:30:00Z") -> dict:
    return {"sym": symbol, "side": side, "qty": qty, "price": price, "ts": ts}


def make_ctx(
    *,
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    now_et: datetime | None = None,
    underlying_price: float = 400.0,
    rows: list[dict] | None = None,
) -> StrategyContext:
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=make_snapshot(underlying_price, rows),
        account_state={},
        book=Book(trades or []),
        now_et=now_et or datetime(2024, 1, 1, 9, 30, tzinfo=ET),
        session_phase="open",
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params if params is not None else DEFAULT_PARAMS,
    )


def scenario_waits_before_entry_time() -> None:
    print("\n1. Flat book, before the entry time -- waits, does not enter")
    ctx = make_ctx(now_et=datetime(2024, 1, 1, 9, 15, tzinfo=ET))
    decision = _decide(ctx)
    check("no trade before entry time", not decision.is_trade)


def scenario_enters_leg_one_at_entry_time() -> None:
    print("\n2. Flat book, at the entry time, tight two-sided quotes on both legs -- buys N calls (leg 1/2)")
    ctx = make_ctx(
        quote_map={CALL_SYM: quote(CALL_SYM, 1.0, 1.05), PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 9, 30, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("a decision is proposed", decision.is_trade)
    check("buys calls", decision.action == "buy" and decision.symbol == CALL_SYM)
    check("buys N=4 contracts", decision.quantity == 4)
    check("attributed to looking_glass_straddle", decision.strategy_id == STRATEGY_ID)


def scenario_wide_spread_defers_entry() -> None:
    print("\n3. Flat book, at entry time, but one leg's spread exceeds the limit -- waits rather than entering")
    ctx = make_ctx(
        quote_map={CALL_SYM: quote(CALL_SYM, 0.50, 2.00), PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 9, 30, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("no trade -- spread too wide", not decision.is_trade)


def scenario_missing_quote_defers_entry() -> None:
    print("\n4. Flat book, at entry time, missing a live quote on one leg -- waits rather than entering")
    ctx = make_ctx(
        quote_map={CALL_SYM: quote(CALL_SYM, 1.0, 1.05)},  # no put quote
        now_et=datetime(2024, 1, 1, 9, 30, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("no trade -- missing put quote", not decision.is_trade)


def scenario_completes_leg_two() -> None:
    print("\n5. N calls held, no puts yet -- buys N puts (leg 2/2)")
    # Call leg filled 1 minute ago -- well inside max_leg_completion_wait_minutes.
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:31:00-05:00")],
        quote_map={PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 9, 32, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("buys puts", decision.is_trade and decision.action == "buy" and decision.symbol == PUT_SYM)
    check("buys N=4 contracts", decision.quantity == 4)


def scenario_holds_below_core_threshold() -> None:
    print("\n6. Full straddle held, live value below the core (eta) threshold -- holds")
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0), trade(PUT_SYM, "buy", 4, 1.0)],
        # C0 = 4*(1.0+1.0) = 8.0; threshold = 8*(1.20) = 9.6; V_t below that.
        quote_map={CALL_SYM: quote(CALL_SYM, 1.0, 1.1), PUT_SYM: quote(PUT_SYM, 1.0, 1.1)},
        now_et=datetime(2024, 1, 1, 10, 0, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("no trade -- below core threshold", not decision.is_trade)


def scenario_core_target_reached_sells_calls() -> None:
    print("\n7. Full straddle held, live value at/above the core threshold -- sells k calls (core, leg 1/2)")
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0), trade(PUT_SYM, "buy", 4, 1.0)],
        # C0 = 8.0; threshold = 9.6; V_t = 4*(1.30+1.20) = 10.0 >= threshold.
        quote_map={CALL_SYM: quote(CALL_SYM, 1.30, 1.35), PUT_SYM: quote(PUT_SYM, 1.20, 1.25)},
        now_et=datetime(2024, 1, 1, 10, 30, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("sells calls", decision.is_trade and decision.action == "sell" and decision.symbol == CALL_SYM)
    check("sells k=3 contracts", decision.quantity == 3)


def scenario_core_sale_completes_with_puts() -> None:
    print("\n8. Calls already reduced to the runner size, puts still at N -- sells k puts to match")
    ctx = make_ctx(
        trades=[
            trade(CALL_SYM, "buy", 4, 1.0), trade(PUT_SYM, "buy", 4, 1.0),
            trade(CALL_SYM, "sell", 3, 1.30),
        ],
        quote_map={PUT_SYM: quote(PUT_SYM, 1.20, 1.25)},
        now_et=datetime(2024, 1, 1, 10, 31, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("sells puts", decision.is_trade and decision.action == "sell" and decision.symbol == PUT_SYM)
    check("sells k=3 contracts", decision.quantity == 3)


def scenario_runner_holds_until_terminal() -> None:
    print("\n9. Core already sold, N-k runner held in both legs -- holds until the terminal exit time")
    ctx = make_ctx(
        trades=[
            trade(CALL_SYM, "buy", 4, 1.0), trade(PUT_SYM, "buy", 4, 1.0),
            trade(CALL_SYM, "sell", 3, 1.30), trade(PUT_SYM, "sell", 3, 1.20),
        ],
        now_et=datetime(2024, 1, 1, 11, 0, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("no trade -- runner held, not yet terminal time", not decision.is_trade)


def scenario_terminal_exit_sells_calls_then_puts() -> None:
    print("\n10. Terminal exit time reached with a runner held -- sells calls first, then puts next cycle")
    trades = [
        trade(CALL_SYM, "buy", 4, 1.0), trade(PUT_SYM, "buy", 4, 1.0),
        trade(CALL_SYM, "sell", 3, 1.30), trade(PUT_SYM, "sell", 3, 1.20),
    ]
    ctx_calls = make_ctx(
        trades=trades,
        quote_map={CALL_SYM: quote(CALL_SYM, 0.5, 0.55)},
        now_et=datetime(2024, 1, 1, 15, 55, tzinfo=ET),
    )
    decision = _decide(ctx_calls)
    check("sells the runner call", decision.is_trade and decision.action == "sell" and decision.symbol == CALL_SYM)
    check("sells the full runner quantity (1)", decision.quantity == 1)

    ctx_puts = make_ctx(
        trades=trades + [trade(CALL_SYM, "sell", 1, 0.5)],
        quote_map={PUT_SYM: quote(PUT_SYM, 0.6, 0.65)},
        now_et=datetime(2024, 1, 1, 15, 56, tzinfo=ET),
    )
    decision2 = _decide(ctx_puts)
    check("then sells the runner put", decision2.is_trade and decision2.action == "sell" and decision2.symbol == PUT_SYM)


def scenario_no_touch_liquidates_full_straddle_at_terminal() -> None:
    print("\n11. No-touch day: terminal exit time reached, core never sold -- liquidates the full straddle")
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0), trade(PUT_SYM, "buy", 4, 1.0)],
        quote_map={CALL_SYM: quote(CALL_SYM, 0.2, 0.25)},
        now_et=datetime(2024, 1, 1, 15, 55, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("sells all 4 held calls", decision.is_trade and decision.action == "sell" and decision.quantity == 4)


def scenario_no_reentry_after_flattening_today() -> None:
    print("\n12. Book is flat again after a completed sequence today -- refuses to re-enter")
    ctx = make_ctx(
        trades=[
            trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T13:30:00Z"),
            trade(PUT_SYM, "buy", 4, 1.0, ts="2024-01-01T13:32:00Z"),
            trade(CALL_SYM, "sell", 4, 0.2, ts="2024-01-01T20:55:00Z"),
            trade(PUT_SYM, "sell", 4, 0.2, ts="2024-01-01T20:56:00Z"),
        ],
        quote_map={CALL_SYM: quote(CALL_SYM, 1.0, 1.05), PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 21, 0, tzinfo=ET),  # still "open" in this synthetic ctx
    )
    decision = _decide(ctx)
    check("no re-entry after a completed sequence today", not decision.is_trade)


def scenario_stands_down_on_multiple_call_symbols() -> None:
    print("\n13. Two distinct call symbols held -- stands down rather than guessing which is this strategy's")
    other_call = "QQQ240101C00405000"
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0), trade(other_call, "buy", 1, 1.0), trade(PUT_SYM, "buy", 4, 1.0)],
        now_et=datetime(2024, 1, 1, 10, 0, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("stands down on ambiguous call holdings", not decision.is_trade)


def scenario_stands_down_on_unexpected_combination() -> None:
    print("\n14. An unexpected call/put quantity combination -- stands down rather than guessing the next step")
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 2, 1.0), trade(PUT_SYM, "buy", 4, 1.0)],  # neither entry nor core-sale shape
        now_et=datetime(2024, 1, 1, 10, 0, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("stands down on an unrecognized state", not decision.is_trade)


def scenario_market_not_open_is_a_no_op() -> None:
    print("\n15. session_phase != open -- never acts")
    ctx = make_ctx(now_et=datetime(2024, 1, 1, 9, 30, tzinfo=ET))
    ctx.session_phase = "afterhours"
    decision = _decide(ctx)
    check("no trade outside the open session", not decision.is_trade)


def scenario_leg_two_wide_spread_waits() -> None:
    print("\n16. Regression: leg 2 (put) also respects the spread guard, not just leg 1")
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:31:00-05:00")],
        quote_map={PUT_SYM: quote(PUT_SYM, 1.0, 1.30)},  # (1.30-1.0)/1.15 = 0.26, over the 0.15 limit
        now_et=datetime(2024, 1, 1, 9, 32, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("no trade -- leg 2 spread too wide to complete the straddle", not decision.is_trade)


def scenario_leg_two_rollback_after_timeout() -> None:
    print("\n17. Regression: put leg never completes -- calls are rolled back after max_leg_completion_wait_minutes")
    # Call leg filled 15 minutes before this decision, past the default 10m rollback limit.
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:20:00-05:00")],
        quote_map={CALL_SYM: quote(CALL_SYM, 0.90, 0.95), PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 9, 35, tzinfo=ET),
    )
    decision = _decide(ctx)
    check(
        "sells the calls back instead of waiting further or buying puts",
        decision.is_trade and decision.action == "sell" and decision.symbol == CALL_SYM,
        decision.reason,
    )
    check("rolls back the full call quantity", decision.quantity == 4)
    check("metadata marks this as a rollback", decision.metadata is not None and decision.metadata.get("rollback") is True)


def scenario_leg_two_within_wait_window_still_completes() -> None:
    print("\n18. Regression guard: within the wait window, leg 2 still completes normally (no premature rollback)")
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:25:00-05:00")],  # 9 minutes ago, under the 10m limit
        quote_map={PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 9, 34, tzinfo=ET),
    )
    decision = _decide(ctx)
    check("still completes leg 2 normally, not a rollback", decision.is_trade and decision.action == "buy" and decision.symbol == PUT_SYM)


def scenario_leg_two_rollback_fires_exactly_at_the_cap_boundary() -> None:
    print(
        "\n19. Regression: rollback fires the cycle it *reaches* the cap, not only once strictly past it -- "
        "flagged in review: a strict `>` would let the cycle landing exactly on the cap slip through to the next "
        "one, silently widening the advertised 10m cap by up to a full 300s cadence interval"
    )
    ctx = make_ctx(
        # Call filled at 09:30:00 exactly; evaluated again at 09:40:00 exactly --
        # precisely the deployed runner's second 300s cycle after the fill, and
        # exactly `max_leg_completion_wait_minutes` (10) later, not past it.
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:30:00-05:00")],
        quote_map={CALL_SYM: quote(CALL_SYM, 0.90, 0.95), PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
        now_et=datetime(2024, 1, 1, 9, 40, tzinfo=ET),
    )
    decision = _decide(ctx)
    check(
        "rolls back at exactly the cap, not one cycle later",
        decision.is_trade and decision.action == "sell" and decision.symbol == CALL_SYM,
        decision.reason,
    )
    check("metadata marks this as a rollback", decision.metadata is not None and decision.metadata.get("rollback") is True)


def scenario_default_terminal_time_has_margin_before_flatten() -> None:
    print("\n19. Regression: the module's own default terminal exit time leaves margin before PR #59's 15:45 flatten")
    from crassus.strategies.looking_glass_straddle import DEFAULT_TERMINAL_EXIT_TIME_ET
    check(
        "default terminal exit time is before 15:45 (PR #59's default mandatory flatten)",
        DEFAULT_TERMINAL_EXIT_TIME_ET < "15:45",
        DEFAULT_TERMINAL_EXIT_TIME_ET,
    )


WRONG_PUT_SYM = "QQQ240101P00404000"  # a different strike -- would be "ATM" if the underlying moved to 404


def scenario_leg_two_buys_the_matching_put_not_current_atm() -> None:
    print(
        "\n20. Regression (review follow-up P1): leg 2 buys the specific put matching the held "
        "call's strike, not whatever's currently ATM if the underlying moved in between"
    )
    # The call leg (400 strike) is already held. The underlying has since
    # moved to 404 -- if leg 2 re-queried atm("put") it would pick the 404
    # put (closer to the new price), a different strike than the held call.
    rows = [
        {"OptionSymbol": CALL_SYM, "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.05},
        {"OptionSymbol": PUT_SYM, "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.05},  # matches the held call
        {"OptionSymbol": WRONG_PUT_SYM, "Strike": 404.0, "Type": "put", "Bid": 0.5, "Ask": 0.55},  # closer to spot=404
    ]
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:31:00-05:00")],
        quote_map={PUT_SYM: quote(PUT_SYM, 1.0, 1.05), WRONG_PUT_SYM: quote(WRONG_PUT_SYM, 0.5, 0.55)},
        now_et=datetime(2024, 1, 1, 9, 32, tzinfo=ET),
        underlying_price=404.0, rows=rows,
    )
    decision = _decide(ctx)
    check(
        "buys the put matching the held call's strike, not the current-ATM one",
        decision.is_trade and decision.action == "buy" and decision.symbol == PUT_SYM,
        decision.symbol if decision.is_trade else decision.reason,
    )


def scenario_leg_two_waits_rather_than_substitute_wrong_strike() -> None:
    print(
        "\n21. Regression guard: if the exact matching put isn't quoted at all, waits -- "
        "never substitutes a different strike even when one is available"
    )
    rows = [
        {"OptionSymbol": CALL_SYM, "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.05},
        {"OptionSymbol": WRONG_PUT_SYM, "Strike": 404.0, "Type": "put", "Bid": 0.5, "Ask": 0.55},  # matching put absent
    ]
    ctx = make_ctx(
        trades=[trade(CALL_SYM, "buy", 4, 1.0, ts="2024-01-01T09:31:00-05:00")],
        quote_map={WRONG_PUT_SYM: quote(WRONG_PUT_SYM, 0.5, 0.55)},
        now_et=datetime(2024, 1, 1, 9, 32, tzinfo=ET),
        underlying_price=404.0, rows=rows,
    )
    decision = _decide(ctx)
    check("no trade -- waits rather than buying the wrong-strike put", not decision.is_trade)


def main() -> int:
    for scenario in (
        scenario_waits_before_entry_time,
        scenario_enters_leg_one_at_entry_time,
        scenario_wide_spread_defers_entry,
        scenario_missing_quote_defers_entry,
        scenario_completes_leg_two,
        scenario_holds_below_core_threshold,
        scenario_core_target_reached_sells_calls,
        scenario_core_sale_completes_with_puts,
        scenario_runner_holds_until_terminal,
        scenario_terminal_exit_sells_calls_then_puts,
        scenario_no_touch_liquidates_full_straddle_at_terminal,
        scenario_no_reentry_after_flattening_today,
        scenario_stands_down_on_multiple_call_symbols,
        scenario_stands_down_on_unexpected_combination,
        scenario_market_not_open_is_a_no_op,
        scenario_leg_two_wide_spread_waits,
        scenario_leg_two_rollback_after_timeout,
        scenario_leg_two_within_wait_window_still_completes,
        scenario_leg_two_rollback_fires_exactly_at_the_cap_boundary,
        scenario_default_terminal_time_has_margin_before_flatten,
        scenario_leg_two_buys_the_matching_put_not_current_atm,
        scenario_leg_two_waits_rather_than_substitute_wrong_strike,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
