#!/usr/bin/env python3
"""Prove `maybe_flatten`'s end-of-day close-out logic.

Hermetic like verify_momentum_qqq.py: no network access, no real snapshot
fetch. `maybe_flatten` takes a `StrategyContext` and a params dict and makes
no I/O beyond `ctx.quotes()`, which every scenario here stubs directly.

    python scripts/verify_flatten.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.flatten import DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE, STRATEGY_ID, maybe_flatten  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
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


def make_snapshot(underlying_price: float = 400.0) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": [],
        },
        raw=b"{}",
    )


LONG_CALL = {
    "sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0,
    "strike": 400.0, "type": "call", "exp": "2024-01-01",
    "instrument_type": "option", "multiplier": 100, "ts": "2024-01-01T10:00:00Z",
}
SHORT_PUT = {
    "sym": "QQQ240101P00400000", "side": "sell", "qty": 1, "price": 1.0,
    "strike": 400.0, "type": "put", "exp": "2024-01-01",
    "instrument_type": "option", "multiplier": 100, "ts": "2024-01-01T10:00:00Z",
}


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    now_et: datetime | None = None,
) -> StrategyContext:
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=make_snapshot(),
        account_state={},
        book=Book(trades or []),
        now_et=now_et or datetime(2024, 1, 1, 15, 50, tzinfo=ET),
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


def scenario_fires_without_param_using_default() -> None:
    print("\n1. No flatten_minutes_before_close set -- still mandatory, falls back to the default window")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),  # 10 min to close, inside the 15-min default
    )
    decision = maybe_flatten(ctx, {})
    check("a decision is proposed even with no params at all", decision is not None)
    check("uses the default window", decision.metadata["flatten_minutes_before_close"] == 15.0)


def scenario_no_params_value_disables_it() -> None:
    print("\n1b. Explicitly setting flatten_minutes_before_close to 0/None still falls back to the default -- there is no opt-out")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision_zero = maybe_flatten(ctx, {"flatten_minutes_before_close": 0})
    decision_none = maybe_flatten(ctx, {"flatten_minutes_before_close": None})
    check("0 doesn't disable it", decision_zero is not None)
    check("None doesn't disable it", decision_none is not None)


def scenario_outside_window() -> None:
    print("\n2. Outside the flatten window -- falls through to the strategy")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 30, tzinfo=ET),  # 30 min to close
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision 30 minutes out with a 15-minute window", decision is None)


def scenario_flat_book_blocks_entry() -> None:
    print("\n3. No open position but inside the window -- explicit no_trade, not None, so the strategy can't open one")
    ctx = make_ctx(
        trades=[],
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 55, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("a decision is still returned (not None)", decision is not None)
    check("it's a no_trade", decision is not None and not decision.is_trade)
    check("attributed to the flatten module", decision is not None and decision.strategy_id == STRATEGY_ID)


def scenario_not_open_is_a_no_op() -> None:
    print("\n4. session_phase != open -- never fires (nothing to flatten toward)")
    ctx = make_ctx(
        session_phase="afterhours",
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 16, 5, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("no decision outside the open session", decision is None)


def scenario_closes_a_held_long() -> None:
    print("\n5. Inside the window with a held long -- sells to close")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),  # 10 min to close
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("a decision is proposed", decision is not None)
    check("sells the held long", decision.action == "sell" and decision.symbol == "QQQ240101C00400000")
    check("quantity matches the held size", decision.quantity == 1)
    check("strategy_id identifies this as a flatten, not the account's own strategy", decision.strategy_id == STRATEGY_ID)


def scenario_closes_a_held_short() -> None:
    print("\n6. Inside the window with a held short -- buys to close")
    ctx = make_ctx(
        trades=[SHORT_PUT],
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("buys to close the held short", decision is not None and decision.action == "buy" and decision.symbol == "QQQ240101P00400000")


def scenario_missing_quote_defers() -> None:
    print("\n7. No live quote for the held symbol -- explicit no_trade (retry next cycle), not None (which would let the strategy run)")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("a decision is still returned (not None)", decision is not None)
    check("it's a no_trade, not a close", decision is not None and not decision.is_trade)


def scenario_stale_quote_defers() -> None:
    print("\n8. A stale quote for the held symbol -- explicit no_trade rather than executing against it or falling through")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 50, tzinfo=ET),
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("a decision is still returned (not None)", decision is not None)
    check("it's a no_trade, not a close", decision is not None and not decision.is_trade)


def scenario_exactly_at_threshold_fires() -> None:
    print("\n9. Exactly at the threshold boundary -- fires (inclusive)")
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 1, 1, 15, 45, tzinfo=ET),  # exactly 15 min to close
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("fires at the boundary itself", decision is not None)


def scenario_no_reentry_after_close_same_window() -> None:
    print("\n10. Regression: close on one cycle, still-supported entry signal on the next -- flatten blocks it, not the runner falling through")
    now = datetime(2024, 1, 1, 15, 50, tzinfo=ET)  # 10 min to close

    # Cycle 1: held long gets closed.
    ctx_holding = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": 15},
        now_et=now,
    )
    closing_decision = maybe_flatten(ctx_holding, ctx_holding.params)
    check("cycle 1 closes the held long", closing_decision is not None and closing_decision.action == "sell")

    # Cycle 2, a few minutes later, still inside the window: the close filled
    # so the book is now flat -- if the account's own strategy still supports
    # entering (a persistent signal), maybe_flatten must still return a
    # decision of its own (a no_trade) so the runner never calls the
    # strategy this cycle, rather than returning None and letting a fresh
    # entry through.
    ctx_after_fill = make_ctx(
        trades=[],  # book is flat again after the closing fill
        params={"flatten_minutes_before_close": 15},
        now_et=now.replace(minute=53),
    )
    reentry_guard = maybe_flatten(ctx_after_fill, ctx_after_fill.params)
    check("cycle 2 still returns a decision (never None) inside the window", reentry_guard is not None)
    check("cycle 2's decision is a no_trade, blocking any new entry", reentry_guard is not None and not reentry_guard.is_trade)
    check(
        "cycle 2's decision is attributed to the flatten module, not the account's strategy",
        reentry_guard is not None and reentry_guard.strategy_id == STRATEGY_ID,
    )


def scenario_half_day_uses_the_real_close() -> None:
    print("\n12. Regression: a half-day session uses its actual 13:00 close, not an unconditional 16:00")
    # 2024-11-29 (day after Thanksgiving) is an NYSE early-close day: 13:00 ET.
    half_day = datetime(2024, 11, 29, tzinfo=ET)
    quote_map = {"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")}

    # 12:50 ET: 190 minutes to a normal 16:00 close, but only 10 to the real
    # 13:00 half-day close -- must fire.
    ctx_inside_half_day_window = make_ctx(
        trades=[LONG_CALL],
        quote_map=quote_map,
        params={"flatten_minutes_before_close": 15},
        now_et=half_day.replace(hour=12, minute=50),
    )
    decision = maybe_flatten(ctx_inside_half_day_window, ctx_inside_half_day_window.params)
    check(
        "fires at 12:50 on a half day (10 minutes to the real 13:00 close)",
        decision is not None,
    )

    # 12:30 ET on the same half day: still 30 minutes out even under the
    # 13:00 close -- must not fire yet.
    ctx_before_half_day_window = make_ctx(
        trades=[LONG_CALL],
        quote_map=quote_map,
        params={"flatten_minutes_before_close": 15},
        now_et=half_day.replace(hour=12, minute=30),
    )
    decision_early = maybe_flatten(ctx_before_half_day_window, ctx_before_half_day_window.params)
    check(
        "does not fire at 12:30 on a half day (30 minutes to the real 13:00 close)",
        decision_early is None,
    )

    # A normal trading day at the same wall-clock time (12:50) must not fire
    # -- confirms the half-day close is date-specific, not a blanket change.
    ctx_normal_day = make_ctx(
        trades=[LONG_CALL],
        quote_map=quote_map,
        params={"flatten_minutes_before_close": 15},
        now_et=datetime(2024, 11, 27, 12, 50, tzinfo=ET),
    )
    decision_normal = maybe_flatten(ctx_normal_day, ctx_normal_day.params)
    check(
        "does not fire at 12:50 on an ordinary day (still 190 minutes to the real 16:00 close)",
        decision_normal is None,
    )


def scenario_invalid_params_fall_back_to_default() -> None:
    print("\n11. Invalid flatten_minutes_before_close values (negative, wrong type, NaN, inf) fall back to the default rather than silently disabling the window")
    now = datetime(2024, 1, 1, 15, 50, tzinfo=ET)  # 10 min to close -- inside the 15-min default

    for label, bad_value in (
        ("negative", -5),
        ("non-numeric string", "fifteen"),
        ("NaN", math.nan),
        ("+inf", math.inf),
        ("-inf", -math.inf),
    ):
        ctx = make_ctx(
            trades=[LONG_CALL],
            quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
            params={"flatten_minutes_before_close": bad_value},
            now_et=now,
        )
        decision = maybe_flatten(ctx, ctx.params)
        check(f"{label} value still fires (falls back to the default)", decision is not None, bad_value)
        if decision is not None:
            check(
                f"{label} value's metadata reports the default, not the bad input",
                decision.metadata["flatten_minutes_before_close"] == DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE,
            )

    # A numeric string is a legitimate coercion, not an error case.
    ctx = make_ctx(
        trades=[LONG_CALL],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        params={"flatten_minutes_before_close": "20"},
        now_et=now,
    )
    decision = maybe_flatten(ctx, ctx.params)
    check("a numeric string is coerced rather than rejected", decision is not None and decision.metadata["flatten_minutes_before_close"] == 20.0)


def scenario_multi_position_book_converges_to_flat() -> None:
    print(
        "\n12. Regression: a book holding two positions at once (e.g. a straddle's call+put) "
        "gets both closed, one per cycle, not just the first"
    )
    now = datetime(2024, 1, 1, 15, 50, tzinfo=ET)  # 10 min to close
    quote_map = {
        "QQQ240101C00400000": fresh_quote("QQQ240101C00400000"),
        "QQQ240101P00400000": fresh_quote("QQQ240101P00400000"),
    }

    # Cycle 1: both legs still held.
    ctx1 = make_ctx(trades=[LONG_CALL, SHORT_PUT], quote_map=quote_map, now_et=now)
    d1 = maybe_flatten(ctx1, ctx1.params)
    check("cycle 1 closes one of the two held positions", d1 is not None and d1.is_trade)
    check(
        "cycle 1 picks deterministically (sorted symbol), not arbitrarily",
        d1.symbol == "QQQ240101C00400000",
        d1.symbol,
    )

    # Cycle 2: the book is rebuilt from server state each cycle, and the
    # closed leg's own closing trade is now part of that history -- so the
    # call position nets to flat and only the put remains held.
    ctx2 = make_ctx(
        trades=[LONG_CALL, SHORT_PUT, {**LONG_CALL, "side": "sell", "ts": "2024-01-01T15:50:00Z"}],
        quote_map=quote_map, now_et=now + timedelta(minutes=1),
    )
    d2 = maybe_flatten(ctx2, ctx2.params)
    check("cycle 2 closes the remaining put leg", d2 is not None and d2.is_trade and d2.symbol == "QQQ240101P00400000")

    # Cycle 3: fully flat now -- back to the ordinary "block new entries" no_trade.
    ctx3 = make_ctx(
        trades=[
            LONG_CALL, SHORT_PUT,
            {**LONG_CALL, "side": "sell", "ts": "2024-01-01T15:50:00Z"},
            {**SHORT_PUT, "side": "buy", "ts": "2024-01-01T15:51:00Z"},
        ],
        quote_map=quote_map, now_et=now + timedelta(minutes=2),
    )
    d3 = maybe_flatten(ctx3, ctx3.params)
    check("cycle 3: book is fully flat, blocks new entries rather than trying to close anything else", d3 is not None and not d3.is_trade)


def scenario_starved_leg_does_not_block_a_closeable_one() -> None:
    print(
        "\n13. Regression (review follow-up): the first-sorted leg having a missing/stale quote "
        "must not starve a second held leg that has a perfectly good one"
    )
    now = datetime(2024, 1, 1, 15, 50, tzinfo=ET)  # 10 min to close
    # QQQ240101C... sorts before QQQ240101P... -- give the call (sorted
    # first) a stale quote and the put a fresh one. An earlier version
    # would retry only the call, forever, and never even look at the put.
    quote_map = {
        "QQQ240101C00400000": stale_quote("QQQ240101C00400000"),
        "QQQ240101P00400000": fresh_quote("QQQ240101P00400000"),
    }
    ctx = make_ctx(trades=[LONG_CALL, SHORT_PUT], quote_map=quote_map, now_et=now)
    decision = maybe_flatten(ctx, ctx.params)
    check(
        "closes the put (good quote) instead of getting stuck waiting on the call (stale quote)",
        decision is not None and decision.is_trade and decision.symbol == "QQQ240101P00400000",
        decision.symbol if decision and decision.is_trade else (decision.reason if decision else None),
    )

    # And when *neither* leg has a usable quote, it still correctly retries
    # rather than forcing a bad fill -- naming both stuck symbols now, not just one.
    ctx_both_stale = make_ctx(
        trades=[LONG_CALL, SHORT_PUT],
        quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000"), "QQQ240101P00400000": stale_quote("QQQ240101P00400000")},
        now_et=now,
    )
    decision_both_stale = maybe_flatten(ctx_both_stale, ctx_both_stale.params)
    check("no trade when every held leg's quote is stale", decision_both_stale is not None and not decision_both_stale.is_trade)
    check(
        "the retry reason names every stuck symbol, not just one",
        decision_both_stale.metadata is not None
        and set(decision_both_stale.metadata.get("symbols", [])) == {"QQQ240101C00400000", "QQQ240101P00400000"},
        decision_both_stale.metadata,
    )


def main() -> int:
    for scenario in (
        scenario_fires_without_param_using_default,
        scenario_no_params_value_disables_it,
        scenario_outside_window,
        scenario_flat_book_blocks_entry,
        scenario_not_open_is_a_no_op,
        scenario_closes_a_held_long,
        scenario_closes_a_held_short,
        scenario_missing_quote_defers,
        scenario_stale_quote_defers,
        scenario_exactly_at_threshold_fires,
        scenario_no_reentry_after_close_same_window,
        scenario_half_day_uses_the_real_close,
        scenario_invalid_params_fall_back_to_default,
        scenario_multi_position_book_converges_to_flat,
        scenario_starved_leg_does_not_block_a_closeable_one,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
