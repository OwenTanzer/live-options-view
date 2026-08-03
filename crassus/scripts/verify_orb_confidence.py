#!/usr/bin/env python3
"""Prove the orb_confidence strategy's decision logic and opening-range tracker.

Hermetic like verify_momentum_qqq.py, which this mirrors scenario-for-scenario
where the shape overlaps (see orb_confidence.py's docstring for why the
position-management shape is intentionally duplicated rather than shared): no
network access, no real snapshot fetch. `_OpeningRangeTracker` is exercised
directly with hand-built observation timestamps/prices; the strategy's
`_decide_core()` is exercised directly with a hand-built `_BreakoutReading` --
it takes no tracker and makes no I/O.

    python scripts/verify_orb_confidence.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import orb_confidence as orb  # noqa: E402
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


def make_snapshot(
    underlying_price: float,
    rows: list[dict],
    *,
    timestamp: str = "2024-01-01T15:00:00+00:00",
    underlying_market: dict | None = None,
) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": timestamp,
            "snapshot_time": timestamp,
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": rows,
            "underlying_market": underlying_market,
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
    underlying_market: dict | None = None,
) -> StrategyContext:
    snapshot = make_snapshot(
        underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW],
        timestamp=snapshot_timestamp, underlying_market=underlying_market,
    )
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


def rvol_confirmed_market(multiple: float = 1.5) -> dict:
    return {
        "symbol": "QQQ",
        "freshness": "live",
        "rvol": {"status": "ok", "multiple": multiple},
    }


def _reading(
    *,
    range_high: float | None = 402.0,
    range_low: float | None = 400.0,
    price: float = 400.0,
    opening_range_minutes: float = 15.0,
    elapsed: float | None = 20.0,
    sample_count: int = 5,
    established: bool = True,
) -> orb._BreakoutReading:
    return orb._BreakoutReading(
        range_high=range_high,
        range_low=range_low,
        opening_range_minutes=opening_range_minutes,
        elapsed_minutes_since_range_start=elapsed,
        sample_count=sample_count,
        range_established=established,
        current_price=price,
    )


def _reset_tracker() -> None:
    orb._tracker = orb._OpeningRangeTracker()
    orb._last_recorded_snapshot = None


# ---------------------------------------------------------------------------
# _OpeningRangeTracker -- pure range-building
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("orb_confidence_qqq is registered", "orb_confidence_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["orb_confidence_qqq"], "strategy_id", None) == orb.STRATEGY_ID
        and getattr(REGISTRY["orb_confidence_qqq"], "strategy_version", None) == orb.STRATEGY_VERSION,
    )


def scenario_tracker_builds_range() -> None:
    print("\n2. _OpeningRangeTracker: high/low/count accumulate from same-day observations")
    tracker = orb._OpeningRangeTracker()
    t0 = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    tracker.observe(t0, 400.0, opening_range_minutes=15.0)
    tracker.observe(t0 + timedelta(minutes=5), 402.0, opening_range_minutes=15.0)
    tracker.observe(t0 + timedelta(minutes=10), 399.0, opening_range_minutes=15.0)
    check("high is the max observed", tracker.high == 402.0, tracker.high)
    check("low is the min observed", tracker.low == 399.0, tracker.low)
    check("sample_count counts every observation", tracker.sample_count == 3, tracker.sample_count)
    check("range_start is the first observation's own timestamp", tracker.range_start == t0, tracker.range_start)


def scenario_tracker_day_rollover_resets() -> None:
    print("\n3. _OpeningRangeTracker: a new ctx.now_et.date() discards yesterday's range")
    tracker = orb._OpeningRangeTracker()
    day1 = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    tracker.observe(day1, 400.0, opening_range_minutes=15.0)
    tracker.observe(day1 + timedelta(minutes=5), 410.0, opening_range_minutes=15.0)
    day2 = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    tracker.observe(day2, 350.0, opening_range_minutes=15.0)
    check("high resets to the new day's single observation", tracker.high == 350.0, tracker.high)
    check("low resets to the new day's single observation", tracker.low == 350.0, tracker.low)
    check("sample_count resets to 1", tracker.sample_count == 1, tracker.sample_count)
    check("range_start moves to the new day's first timestamp", tracker.range_start == day2, tracker.range_start)


def scenario_tracker_freezes_after_window_elapses() -> None:
    print("\n3b. _OpeningRangeTracker: high/low freeze once opening_range_minutes has elapsed")
    tracker = orb._OpeningRangeTracker()
    t0 = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)  # 9:30 ET, i.e. market open
    tracker.observe(t0, 400.0, opening_range_minutes=15.0)
    tracker.observe(t0 + timedelta(minutes=5), 402.0, opening_range_minutes=15.0)
    tracker.observe(t0 + timedelta(minutes=10), 399.0, opening_range_minutes=15.0)
    # This observation lands after the 15-minute window -- without freezing,
    # a breakout print like this would fold straight into high/low and the
    # breakout condition (price > high) could never be satisfied.
    tracker.observe(t0 + timedelta(minutes=20), 420.0, opening_range_minutes=15.0)
    check("high does not include the post-window print", tracker.high == 402.0, tracker.high)
    check("low does not include the post-window print", tracker.low == 399.0, tracker.low)
    check("sample_count stops incrementing once the window has closed", tracker.sample_count == 3, tracker.sample_count)


def scenario_tracker_midsession_restart_does_not_fabricate_range() -> None:
    print("\n3c. _OpeningRangeTracker: a crash-restart mid-session anchors to market open, not the first post-restart print")
    tracker = orb._OpeningRangeTracker()
    # First observation of the day arrives at 11:00 ET (15:00 UTC) -- e.g. a
    # runner restart well after the real opening range already happened.
    restart_at = datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)  # 11:00 ET
    tracker.observe(restart_at, 500.0, opening_range_minutes=15.0)
    check(
        "range_start is anchored to today's market open, not the restart time",
        tracker.range_start == datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc),
        tracker.range_start,
    )
    check("only one sample recorded", tracker.sample_count == 1, tracker.sample_count)
    # The next read is already ~90 minutes past the anchored range_start, so
    # the window is immediately closed with a single sample -- below
    # min_range_samples, so _decide_core stands down rather than trading a
    # fabricated one-tick range.
    tracker.observe(restart_at + timedelta(minutes=1), 500.5, opening_range_minutes=15.0)
    check("sample_count stays at 1 -- the window closed before a second sample landed", tracker.sample_count == 1, tracker.sample_count)


def scenario_decide_end_to_end_breakout_eventually_fires() -> None:
    print("\n3d. _decide(): a real breakout across a full session actually produces a buy (regression for the swapped-payoff-style bug in #1)")
    _reset_tracker()
    start = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)  # 9:30 ET
    price = 500.0
    fired = False
    minute = 0
    # 15-minute opening range near 500, then a steady climb to 535 over the
    # rest of the session -- a clean trend day.
    while minute <= 390 and not fired:
        if minute <= 15:
            price = 500.0 + (0.5 if minute % 2 == 0 else -0.3)
        else:
            price = 500.0 + (minute - 15) * (35.0 / 375.0)
        ts = start + timedelta(minutes=minute)
        ctx = make_ctx(
            session_phase="open",
            now_et=ts,
            snapshot_timestamp=ts.isoformat(),
            underlying_price=price,
            underlying_market=rvol_confirmed_market(2.0),
            quote_map={
                "QQQ240101C00400000": fresh_quote("QQQ240101C00400000"),
                "QQQ240101P00400000": fresh_quote("QQQ240101P00400000"),
            },
        )
        decision = orb._decide(ctx)
        if decision.is_trade:
            fired = True
        minute += 1
    check("a sustained trend day eventually produces a trade", fired, f"stopped at minute={minute}, price={price:.2f}")


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic (mirrors momentum_qqq's scenarios)
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n4. Market not open declines without touching the reading")
    ctx = make_ctx(session_phase="premarket")
    decision = orb._decide_core(ctx, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_no_reading_yet() -> None:
    print("\n5. No opening-range observations recorded yet declines instead of crashing")
    ctx = make_ctx(session_phase="open")
    decision = orb._decide_core(ctx, None)
    check("no_trade with no reading", not decision.is_trade)
    check("reason cites missing observations", "No opening-range observations" in decision.reason)


def scenario_within_range_window_no_trade() -> None:
    print("\n6. Still within the opening-range window (not yet established) while flat -- stand down")
    ctx = make_ctx(session_phase="open")
    reading = _reading(price=401.0, elapsed=5.0, sample_count=2, established=False)
    decision = orb._decide_core(ctx, reading)
    check("no_trade while range not yet established", not decision.is_trade)
    check("reason cites the range not being established", "not yet established" in decision.reason.lower(), decision.reason)


def scenario_within_range_window_while_positioned_closes() -> None:
    print("\n7. Range not yet established (e.g. after a restart) while holding a position -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    reading = _reading(price=401.0, elapsed=5.0, sample_count=2, established=False)
    decision = orb._decide_core(ctx, reading)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_insufficient_samples_no_trade() -> None:
    print("\n8. Window elapsed but too few observations recorded -- stand down rather than trust a thin range")
    ctx = make_ctx(session_phase="open")
    reading = _reading(price=403.0, elapsed=20.0, sample_count=1, established=True)
    decision = orb._decide_core(ctx, reading)
    check("no_trade with too few range samples", not decision.is_trade)
    check("reason cites insufficient samples", "observation(s) recorded" in decision.reason.lower(), decision.reason)


def scenario_insufficient_samples_while_positioned_closes() -> None:
    print("\n9. Too few range samples while holding a position -- close it")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    reading = _reading(price=397.0, elapsed=20.0, sample_count=1, established=True)
    decision = orb._decide_core(ctx, reading)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held put", decision.symbol == "QQQ240101P00400000")


def scenario_inside_range_no_trade() -> None:
    print("\n10. Price still inside the opening range -- no breakout, stand down")
    ctx = make_ctx(session_phase="open")
    reading = _reading(price=401.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade -- price within range", not decision.is_trade)
    check("reason cites no breakout", "no breakout" in decision.reason.lower(), decision.reason)


def scenario_price_back_inside_range_closes_held_position() -> None:
    print("\n11. Price falls back inside the range while holding a breakout position -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    reading = _reading(price=401.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_breakout_up_with_rvol_confirmed_buys_call() -> None:
    print("\n12. Breakout above the range + RVOL confirmed -> buy call, confidence reflects both factors")
    ctx = make_ctx(
        session_phase="open",
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market=rvol_confirmed_market(1.5),
    )
    # magnitude = (403 - 402) / (402 - 400) = 0.5 -> +0.25; rvol +0.5 -> confidence 0.75
    reading = _reading(price=403.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check(
        "confidence combines RVOL confirmation and breakout magnitude",
        abs(decision.metadata["confidence"] - 0.75) < 1e-9,
        decision.metadata["confidence"],
    )
    check("metadata flags RVOL as confirmed", decision.metadata["rvol_confirmed"] is True)


def scenario_breakout_down_without_rvol_but_strong_magnitude_buys_put() -> None:
    print("\n13. Breakout below the range without RVOL confirmation, but strong enough magnitude alone clears min_confidence -> buy put")
    ctx = make_ctx(
        session_phase="open",
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
        # No underlying_market at all -- RVOL simply can't confirm.
    )
    # magnitude = (400 - 396) / (402 - 400) = 2.0 -> clipped to 1.0 -> +0.5 confidence,
    # comfortably above the default min_confidence of 0.3 with zero RVOL contribution.
    reading = _reading(price=396.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("RVOL did not confirm", decision.metadata["rvol_confirmed"] is False)
    check("confidence is magnitude-only and above the default floor", decision.metadata["confidence"] >= 0.3, decision.metadata["confidence"])


def scenario_breakout_below_min_confidence_no_trade() -> None:
    print("\n14. Breakout detected but confidence too low to trust -- stand down")
    ctx = make_ctx(session_phase="open")
    # Clears the 0.05% breakout buffer (402 * 1.0005 = 402.201) so it registers
    # as a genuine breakout, but the magnitude past the range is tiny:
    # (402.3 - 402) / (402 - 400) = 0.15 -> +0.075 confidence; no RVOL -> total
    # 0.075 < the default min_confidence of 0.3.
    reading = _reading(price=402.3, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade -- confidence below the required floor", not decision.is_trade, decision.to_dict())
    check("reason cites confidence", "confidence" in decision.reason.lower(), decision.reason)


def scenario_breakout_below_min_confidence_while_positioned_closes() -> None:
    print("\n15. Confidence drops back below the floor while holding a position -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    reading = _reading(price=402.2, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_stale_quote_declines() -> None:
    print("\n16. Confirmed breakout but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    # magnitude = (405 - 402) / (402 - 400) = 1.5 -> clipped to 1.0 -> +0.5
    # confidence with zero RVOL contribution, safely above min_confidence.
    reading = _reading(price=405.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("\n17. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    # Same well-above-floor magnitude-only confidence as scenario 16.
    reading = _reading(price=405.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_breakout_flip_closes_opposite() -> None:
    print("\n18. Breakout flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    # Confidence must actually clear min_confidence here -- otherwise the
    # close would be routed through the low-confidence branch instead of the
    # direction-flip branch this scenario means to exercise.
    reading = _reading(price=405.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)
    check("reason cites the direction flip, not a confidence/unsupported close", "now points" in decision.reason.lower(), decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n19. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    reading = _reading(price=403.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n20. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    reading = _reading(price=403.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n21. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    reading = _reading(price=403.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def scenario_custom_params_honored() -> None:
    print("\n22. Custom min_confidence/rvol_floor/breakout_buffer_pct params are honored")
    ctx = make_ctx(
        session_phase="open",
        params={"min_confidence": 0.9},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
        underlying_market=rvol_confirmed_market(1.5),
    )
    # Same breakout as scenario 12 (confidence 0.75) but the operator has raised
    # the bar to 0.9 -- must now decline instead of trading.
    reading = _reading(price=403.0, range_high=402.0, range_low=400.0)
    decision = orb._decide_core(ctx, reading)
    check("no_trade -- 0.75 confidence doesn't clear the widened 0.9 floor", not decision.is_trade, decision.to_dict())


# ---------------------------------------------------------------------------
# _decide_core()'s stale_source_reason branch -- mirrors momentum_qqq's
# ---------------------------------------------------------------------------


def scenario_stale_source_declines_while_flat() -> None:
    print("\n23. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = orb._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)
    check("reason cites the stale source", "stale" in decision.reason.lower())


def scenario_stale_source_while_positioned_retains_not_sells() -> None:
    print("\n24. Stale source while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = orb._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


# ---------------------------------------------------------------------------
# _decide() -- snapshot recording, dedup, staleness gate, day rollover
# ---------------------------------------------------------------------------


def scenario_decide_records_using_snapshot_timestamp_not_now_et() -> None:
    print("\n25. _decide(): records the range's first observation from snapshot.timestamp, not ctx.now_et")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")
    orb._decide(ctx)
    check("exactly one observation recorded", orb._tracker.sample_count == 1, orb._tracker.sample_count)
    check(
        "range_start is anchored to the snapshot day's market open (14:30 UTC == 9:30 ET), "
        "not the runner's now_et, and not the snapshot's own (post-open) timestamp",
        orb._tracker.range_start == datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc),
        orb._tracker.range_start,
    )


def scenario_decide_dedupes_identical_snapshot() -> None:
    print("\n26. _decide(): repeated reads of the same unchanged snapshot are not double-recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx1 = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T15:00:00+00:00")
    ctx2 = make_ctx(session_phase="open", now_et=now + timedelta(minutes=1), snapshot_timestamp="2026-01-01T15:00:00+00:00")
    orb._decide(ctx1)
    orb._decide(ctx2)
    check(
        "only one observation recorded despite two decide() calls against the same (unrepublished) snapshot",
        orb._tracker.sample_count == 1,
        orb._tracker.sample_count,
    )


def scenario_decide_day_rollover_resets_range() -> None:
    print("\n27. _decide(): a new day's snapshot resets the opening range")
    _reset_tracker()
    day1 = datetime(2026, 1, 1, 14, 35, tzinfo=timezone.utc)
    ctx1 = make_ctx(session_phase="open", now_et=day1, snapshot_timestamp="2026-01-01T14:30:00+00:00", underlying_price=400.0)
    orb._decide(ctx1)
    day2 = datetime(2026, 1, 2, 14, 35, tzinfo=timezone.utc)
    ctx2 = make_ctx(session_phase="open", now_et=day2, snapshot_timestamp="2026-01-02T14:30:00+00:00", underlying_price=450.0)
    orb._decide(ctx2)
    check("sample_count resets to 1 on the new day", orb._tracker.sample_count == 1, orb._tracker.sample_count)
    check("high/low reset to the new day's single observation", orb._tracker.high == 450.0 and orb._tracker.low == 450.0)
    check(
        "range_start moves to the new day's own snapshot timestamp",
        orb._tracker.range_start == datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc),
        orb._tracker.range_start,
    )


def scenario_decide_rejects_stale_snapshot_while_flat() -> None:
    print("\n28. _decide(): a snapshot far older than the runner's clock is rejected as a stale source, not recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00")  # 6 hours old
    decision = orb._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", orb._tracker.sample_count == 0, orb._tracker.sample_count)
    check("reason cites the stale source", "stale" in decision.reason.lower() or "stalled" in decision.reason.lower(), decision.reason)


def scenario_decide_rejects_stale_snapshot_while_positioned() -> None:
    print("\n29. _decide(): a stale source snapshot while holding a position retains it rather than closing on a fabricated read")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00",
        trades=trades, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = orb._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)


def scenario_decide_accepts_fresh_snapshot_within_age_limit() -> None:
    print("\n30. _decide(): a snapshot within the age limit is recorded normally")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")  # 2 minutes old
    orb._decide(ctx)
    check("one observation recorded from a snapshot well within max_snapshot_age_minutes", orb._tracker.sample_count == 1)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_tracker_builds_range,
        scenario_tracker_day_rollover_resets,
        scenario_tracker_freezes_after_window_elapses,
        scenario_tracker_midsession_restart_does_not_fabricate_range,
        scenario_decide_end_to_end_breakout_eventually_fires,
        scenario_market_closed,
        scenario_no_reading_yet,
        scenario_within_range_window_no_trade,
        scenario_within_range_window_while_positioned_closes,
        scenario_insufficient_samples_no_trade,
        scenario_insufficient_samples_while_positioned_closes,
        scenario_inside_range_no_trade,
        scenario_price_back_inside_range_closes_held_position,
        scenario_breakout_up_with_rvol_confirmed_buys_call,
        scenario_breakout_down_without_rvol_but_strong_magnitude_buys_put,
        scenario_breakout_below_min_confidence_no_trade,
        scenario_breakout_below_min_confidence_while_positioned_closes,
        scenario_stale_quote_declines,
        scenario_already_positioned_holds,
        scenario_breakout_flip_closes_opposite,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_custom_params_honored,
        scenario_stale_source_declines_while_flat,
        scenario_stale_source_while_positioned_retains_not_sells,
        scenario_decide_records_using_snapshot_timestamp_not_now_et,
        scenario_decide_dedupes_identical_snapshot,
        scenario_decide_day_rollover_resets_range,
        scenario_decide_rejects_stale_snapshot_while_flat,
        scenario_decide_rejects_stale_snapshot_while_positioned,
        scenario_decide_accepts_fresh_snapshot_within_age_limit,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
