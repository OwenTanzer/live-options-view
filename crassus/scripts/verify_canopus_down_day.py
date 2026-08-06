#!/usr/bin/env python3
"""Prove the canopus_down_day_14 strategy's frozen Down-Day 14 rule (see
crassus/strategies/canopus_down_day.py): reference recording, entry-window
qualification, target-vs-fallback exit, and no-re-entry.

Hermetic like verify_flatten.py / verify_looking_glass_straddle.py: no
network access. `ctx.quotes()` is stubbed directly for every scenario. Each
scenario uses its own session date so the module-level reference-price cache
never leaks state between scenarios.

    python scripts/verify_canopus_down_day.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies.canopus_down_day import (  # noqa: E402
    STRATEGY_ID,
    _reference_price_by_date,
    _decide,
)
from crassus.strategy import StrategyContext  # noqa: E402

ET = ZoneInfo("America/New_York")

passed, failed = 0, 0
_day_counter = 1


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def next_day() -> int:
    global _day_counter
    _day_counter += 1
    return _day_counter


PUT_SYM = "QQQ240101P00396000"
DEFAULT_PARAMS = {
    "reference_time_et": "09:30",
    "entry_window_start_et": "14:45",
    "entry_window_end_et": "14:51",
    "fallback_exit_time_et": "15:45",
    "down_threshold_pct": 0.0025,
    "target_multiplier": 1.14,
}


def make_snapshot(underlying_price: float = 396.0) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T19:45:00+00:00",
            "snapshot_time": "2024-01-01T19:45:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": [
                {"OptionSymbol": PUT_SYM, "Strike": 396.0, "Type": "put", "Bid": 1.0, "Ask": 1.05},
            ],
        },
        raw=b"{}",
    )


def quote(symbol: str, bid: float, ask: float, fresh: bool = True) -> Quote:
    server_ts = "2024-01-01T19:45:05" if fresh else "2024-01-01T19:50:00"
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts="2024-01-01T19:45:00", server_ts=server_ts)


def trade(symbol: str, side: str, qty: int, price: float, ts: str) -> dict:
    return {"sym": symbol, "side": side, "qty": qty, "price": price, "ts": ts}


def make_ctx(
    *,
    day: int,
    hour: int,
    minute: int,
    underlying_price: float = 396.0,
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
) -> StrategyContext:
    quote_map = quote_map or {}
    now_et = datetime(2024, 1, day, hour, minute, tzinfo=ET)
    return StrategyContext(
        snapshot=make_snapshot(underlying_price),
        account_state={},
        book=Book(trades or []),
        now_et=now_et,
        session_phase="open",
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params if params is not None else DEFAULT_PARAMS,
    )


def ts(day: int, hour: int, minute: int) -> str:
    return f"2024-01-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def scenario_records_reference_and_waits_for_window() -> None:
    print("\n1. Reference price recorded at/after 9:30; flat book waits before the entry window opens")
    day = next_day()
    ctx = make_ctx(day=day, hour=9, minute=30, underlying_price=400.0)
    _decide(ctx)  # records the reference
    check("reference price recorded", _reference_price_by_date.get(ctx.now_et.date()) == 400.0)

    ctx_wait = make_ctx(day=day, hour=10, minute=0, underlying_price=398.0)
    decision = _decide(ctx_wait)
    check("no trade before the entry window", not decision.is_trade)


def scenario_qualifies_and_buys_put() -> None:
    print("\n2. Reference recorded, QQQ down >= 0.25% in the entry window -- buys 1 ATM put")
    day = next_day()
    ctx_ref = make_ctx(day=day, hour=9, minute=30, underlying_price=400.0)
    _decide(ctx_ref)

    # 400 * (1 - 0.003) = 398.8 -- down 0.30%, qualifies (>= 0.25%).
    ctx_entry = make_ctx(
        day=day, hour=14, minute=45, underlying_price=398.8,
        quote_map={PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
    )
    decision = _decide(ctx_entry)
    check("a decision is proposed", decision.is_trade)
    check("buys the put", decision.action == "buy" and decision.symbol == PUT_SYM)
    check("buys exactly 1 contract", decision.quantity == 1)
    check("attributed to canopus_down_day_14", decision.strategy_id == STRATEGY_ID)


def scenario_does_not_qualify_when_not_down_enough() -> None:
    print("\n3. Reference recorded, QQQ down less than the threshold -- does not enter")
    day = next_day()
    ctx_ref = make_ctx(day=day, hour=9, minute=30, underlying_price=400.0)
    _decide(ctx_ref)

    # 400 * (1 - 0.001) = 399.6 -- only down 0.10%, does not qualify.
    ctx_entry = make_ctx(
        day=day, hour=14, minute=45, underlying_price=399.6,
        quote_map={PUT_SYM: quote(PUT_SYM, 1.0, 1.05)},
    )
    decision = _decide(ctx_entry)
    check("no trade -- displacement below threshold", not decision.is_trade)


def scenario_entry_window_passes_without_qualifying() -> None:
    print("\n4. Entry window closes without a qualifying print -- stands down for the day")
    day = next_day()
    ctx_ref = make_ctx(day=day, hour=9, minute=30, underlying_price=400.0)
    _decide(ctx_ref)

    ctx_after = make_ctx(day=day, hour=14, minute=52, underlying_price=395.0)
    decision = _decide(ctx_after)
    check("no trade -- entry window has passed", not decision.is_trade)


def scenario_missing_reference_blocks_entry() -> None:
    print("\n5. No reference price recorded yet this session -- cannot evaluate displacement, no trade")
    day = next_day()
    ctx = make_ctx(day=day, hour=14, minute=45, underlying_price=390.0)
    decision = _decide(ctx)
    check("no trade -- no reference recorded", not decision.is_trade)


def scenario_holds_below_target() -> None:
    print("\n6. Held put, live bid below the rounded-up 114% target -- holds")
    day = next_day()
    trades = [trade(PUT_SYM, "buy", 1, 1.00, ts(day, 14, 45))]
    # target = ceil(1.00 * 1.14 * 100)/100 = 1.14
    ctx = make_ctx(day=day, hour=15, minute=0, trades=trades, quote_map={PUT_SYM: quote(PUT_SYM, 1.10, 1.15)})
    decision = _decide(ctx)
    check("no trade -- bid below target", not decision.is_trade)


def scenario_sells_at_target() -> None:
    print("\n7. Held put, live bid at/above the rounded-up target -- sells")
    day = next_day()
    trades = [trade(PUT_SYM, "buy", 1, 1.00, ts(day, 14, 45))]
    ctx = make_ctx(day=day, hour=15, minute=0, trades=trades, quote_map={PUT_SYM: quote(PUT_SYM, 1.14, 1.19)})
    decision = _decide(ctx)
    check("sells the put", decision.is_trade and decision.action == "sell" and decision.symbol == PUT_SYM)
    check("sells the 1 held contract", decision.quantity == 1)


def scenario_target_rounds_up_to_next_cent() -> None:
    print("\n8. 114% of a fill price that lands mid-cent rounds UP, not down, before comparing to the bid")
    day = next_day()
    # fill=1.11 -> raw target = 1.2654 -> rounds up to 1.27, not down to 1.26.
    trades = [trade(PUT_SYM, "buy", 1, 1.11, ts(day, 14, 45))]
    ctx_below = make_ctx(day=day, hour=15, minute=0, trades=trades, quote_map={PUT_SYM: quote(PUT_SYM, 1.26, 1.30)})
    decision_below = _decide(ctx_below)
    check("does not sell at 1.26 (below the rounded-up 1.27 target)", not decision_below.is_trade)

    ctx_at = make_ctx(day=day, hour=15, minute=1, trades=trades, quote_map={PUT_SYM: quote(PUT_SYM, 1.27, 1.31)})
    decision_at = _decide(ctx_at)
    check("sells at 1.27 (the rounded-up target)", decision_at.is_trade and decision_at.action == "sell")


def scenario_fallback_liquidates_when_target_never_fills() -> None:
    print("\n9. Fallback exit time reached, target never hit -- liquidates at the market")
    day = next_day()
    trades = [trade(PUT_SYM, "buy", 1, 1.00, ts(day, 14, 45))]
    ctx = make_ctx(day=day, hour=15, minute=45, trades=trades, quote_map={PUT_SYM: quote(PUT_SYM, 0.60, 0.65)})
    decision = _decide(ctx)
    check("sells at fallback", decision.is_trade and decision.action == "sell" and decision.symbol == PUT_SYM)


def scenario_fallback_without_a_quote_retries() -> None:
    print("\n10. Fallback exit time reached but no live/fresh quote -- retries next cycle rather than forcing a fill")
    day = next_day()
    trades = [trade(PUT_SYM, "buy", 1, 1.00, ts(day, 14, 45))]
    ctx = make_ctx(day=day, hour=15, minute=45, trades=trades, quote_map={})
    decision = _decide(ctx)
    check("no trade -- no live quote to liquidate against yet", not decision.is_trade)


def scenario_no_reentry_after_completed_trade() -> None:
    print("\n11. Book is flat again after a completed trade today -- refuses to re-enter")
    day = next_day()
    ctx_ref = make_ctx(day=day, hour=9, minute=30, underlying_price=400.0)
    _decide(ctx_ref)

    trades = [
        trade(PUT_SYM, "buy", 1, 1.00, ts(day, 14, 45)),
        trade(PUT_SYM, "sell", 1, 1.20, ts(day, 15, 30)),
    ]
    ctx = make_ctx(day=day, hour=15, minute=40, underlying_price=395.0, trades=trades)
    decision = _decide(ctx)
    check("no re-entry after a completed trade today", not decision.is_trade)


def scenario_stands_down_on_held_call() -> None:
    print("\n12. Holding an unexpected call position -- stands down (this strategy is put-only)")
    day = next_day()
    call_sym = "QQQ240101C00404000"
    trades = [trade(call_sym, "buy", 1, 1.00, ts(day, 14, 45))]
    ctx = make_ctx(day=day, hour=15, minute=0, trades=trades)
    decision = _decide(ctx)
    check("stands down on an unexpected call", not decision.is_trade)


def scenario_market_not_open_is_a_no_op() -> None:
    print("\n13. session_phase != open -- never acts")
    day = next_day()
    ctx = make_ctx(day=day, hour=14, minute=45, underlying_price=390.0)
    ctx.session_phase = "afterhours"
    decision = _decide(ctx)
    check("no trade outside the open session", not decision.is_trade)


def main() -> int:
    for scenario in (
        scenario_records_reference_and_waits_for_window,
        scenario_qualifies_and_buys_put,
        scenario_does_not_qualify_when_not_down_enough,
        scenario_entry_window_passes_without_qualifying,
        scenario_missing_reference_blocks_entry,
        scenario_holds_below_target,
        scenario_sells_at_target,
        scenario_target_rounds_up_to_next_cent,
        scenario_fallback_liquidates_when_target_never_fills,
        scenario_fallback_without_a_quote_retries,
        scenario_no_reentry_after_completed_trade,
        scenario_stands_down_on_held_call,
        scenario_market_not_open_is_a_no_op,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
