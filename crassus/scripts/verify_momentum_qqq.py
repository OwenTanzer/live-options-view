#!/usr/bin/env python3
"""Prove the momentum_qqq strategy's decision logic and momentum math.

Hermetic like verify_trump_whisperer.py, which this mirrors scenario-for-
scenario on purpose (see momentum_qqq.py's docstring for why the position-
management shape is identical): no network access, no real snapshot fetch.
`momentum.compute_momentum()` is exercised with hand-built `PricePoint`
lists; the strategy's `_decide_core()` is exercised directly with a
hand-built `MomentumSignal` -- it takes no tracker and makes no I/O.

    python scripts/verify_momentum_qqq.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import MomentumSignal, PricePoint, PriceHistoryTracker, compute_momentum  # noqa: E402
from crassus.strategies import momentum_qqq as mq  # noqa: E402
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
CALL_ROW_DRIFTED = {"OptionSymbol": "QQQ240101C00402000", "Strike": 402.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW_DRIFTED = {"OptionSymbol": "QQQ240101P00402000", "Strike": 402.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


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


def _signal(return_pct: float | None, *, status: str = "ok", sample_count: int = 10, anchor_age_minutes: float = 60.0) -> MomentumSignal:
    current_price = 400.0
    anchor_price = current_price / (1.0 + return_pct) if return_pct is not None else None
    return MomentumSignal(
        lookback_minutes=60.0,
        current_price=current_price,
        anchor_price=anchor_price,
        return_pct=return_pct,
        sample_count=sample_count,
        anchor_age_minutes=anchor_age_minutes,
        status=status,
    )


# ---------------------------------------------------------------------------
# compute_momentum() -- pure trailing-return math
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("momentum_qqq is registered", "momentum_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["momentum_qqq"], "strategy_id", None) == mq.STRATEGY_ID
        and getattr(REGISTRY["momentum_qqq"], "strategy_version", None) == mq.STRATEGY_VERSION,
    )


def scenario_compute_no_data() -> None:
    print("\n2. compute_momentum(): empty history")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    signal = compute_momentum([], now=now, lookback_minutes=60.0)
    check("status is no_data", signal.status == "no_data", signal.status)
    check("return_pct is None", signal.return_pct is None)
    check("sample_count is zero", signal.sample_count == 0)


def scenario_compute_warming_up() -> None:
    print("\n3. compute_momentum(): no point old enough yet")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=10), 400.0),
        PricePoint(now - timedelta(minutes=5), 401.0),
        PricePoint(now, 402.0),
    ]
    signal = compute_momentum(history, now=now, lookback_minutes=60.0)
    check("status is warming_up", signal.status == "warming_up", signal.status)
    check("return_pct is None", signal.return_pct is None)
    check("current_price is the newest point", signal.current_price == 402.0)
    check("sample_count counts every retained point", signal.sample_count == 3)


def scenario_compute_ok() -> None:
    print("\n4. compute_momentum(): a valid anchor produces a trailing return")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=90), 390.0),  # too old to be picked over the 65m point
        PricePoint(now - timedelta(minutes=65), 400.0),  # newest point >= 60m old -> chosen anchor
        PricePoint(now - timedelta(minutes=30), 404.0),
        PricePoint(now, 404.0),
    ]
    signal = compute_momentum(history, now=now, lookback_minutes=60.0)
    check("status is ok", signal.status == "ok", signal.status)
    check("anchor is the freshest point >= lookback old", signal.anchor_price == 400.0, signal.anchor_price)
    check("return_pct is (current/anchor - 1)", abs(signal.return_pct - (404.0 / 400.0 - 1.0)) < 1e-9, signal.return_pct)
    check("anchor_age_minutes reflects the 65-minute-old anchor", abs(signal.anchor_age_minutes - 65.0) < 1e-9, signal.anchor_age_minutes)


def scenario_compute_stale_anchor() -> None:
    print("\n5. compute_momentum(): a polling gap makes the only anchor too stale")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=400), 390.0),  # only candidate >= 60m old, way past 60+10m
        PricePoint(now, 404.0),
    ]
    signal = compute_momentum(history, now=now, lookback_minutes=60.0, max_anchor_overshoot_minutes=10.0)
    check("status is stale_anchor", signal.status == "stale_anchor", signal.status)
    check("return_pct is None despite having an anchor price", signal.return_pct is None)
    check("anchor_price is still surfaced for the audit record", signal.anchor_price == 390.0)


def scenario_compute_overshoot_bound_is_additive_not_multiplicative() -> None:
    print("\n5b. compute_momentum(): overshoot bound doesn't scale with the lookback -- a 100-minute-old anchor on a 60-minute lookback is stale, not 'ok'")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=100), 390.0),  # would have passed under the old 3x=180m multiplicative bound
        PricePoint(now, 404.0),
    ]
    signal = compute_momentum(history, now=now, lookback_minutes=60.0, max_anchor_overshoot_minutes=10.0)
    check(
        "a 100-minute-old anchor is rejected under a 60m lookback + 10m overshoot bound (70m max), "
        "not silently accepted and mislabeled as a 60-minute return",
        signal.status == "stale_anchor",
        signal.status,
    )


def scenario_compute_unsorted_input() -> None:
    print("\n6. compute_momentum(): history need not be pre-sorted by the caller")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now, 404.0),
        PricePoint(now - timedelta(minutes=65), 400.0),
        PricePoint(now - timedelta(minutes=90), 390.0),
    ]
    signal = compute_momentum(history, now=now, lookback_minutes=60.0)
    check("current_price is still the chronologically newest point", signal.current_price == 404.0, signal.current_price)
    check("anchor is still the chronologically correct one", signal.anchor_price == 400.0, signal.anchor_price)


def scenario_tracker_prunes_old_points() -> None:
    print("\n7. PriceHistoryTracker: points older than retain_minutes are dropped")
    tracker = PriceHistoryTracker(retain_minutes=60.0)
    t0 = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    tracker.observe(t0, 400.0)
    tracker.observe(t0 + timedelta(minutes=30), 401.0)
    tracker.observe(t0 + timedelta(minutes=61), 402.0)  # drops the t0 point (61m old at observe time)
    remaining = tracker.snapshot()
    check("oldest point pruned once past retain_minutes", all(p.price != 400.0 for p in remaining), [p.price for p in remaining])
    check("newer points retained", {p.price for p in remaining} == {401.0, 402.0}, {p.price for p in remaining})


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic (mirrors trump_whisperer's scenarios)
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n8. Market not open declines without touching the signal")
    ctx = make_ctx(session_phase="premarket")
    decision = mq._decide_core(ctx, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_no_data() -> None:
    print("\n9. No price history recorded yet declines instead of crashing")
    ctx = make_ctx(session_phase="open")
    decision = mq._decide_core(ctx, None)
    check("no_trade with no signal", not decision.is_trade)
    check("reason cites missing history", "No price history" in decision.reason)


def scenario_warming_up_while_flat() -> None:
    print("\n10. Warming up (not enough history yet) while flat -- stand down")
    ctx = make_ctx(session_phase="open")
    decision = mq._decide_core(ctx, _signal(None, status="warming_up", sample_count=2))
    check("no_trade while warming up", not decision.is_trade)
    check("reason cites warming up", "warming up" in decision.reason.lower())


def scenario_warming_up_while_positioned_closes() -> None:
    print("\n11. Warming up (e.g. after a restart) while holding a position -- close it, don't freeze holding it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mq._decide_core(ctx, _signal(None, status="warming_up", sample_count=1))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_stale_anchor_while_positioned_closes() -> None:
    print("\n12. Stale anchor (polling gap) while holding a position -- close it rather than trust a bad read")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mq._decide_core(ctx, _signal(None, status="stale_anchor", anchor_age_minutes=200.0))
    check("action is sell", decision.action == "sell", decision.action)
    check("reason cites the gap", "stale" in decision.reason.lower() or "gap" in decision.reason.lower(), decision.reason)


def scenario_neutral() -> None:
    print("\n13. Neutral trailing return while flat trades nothing")
    ctx = make_ctx(session_phase="open")
    decision = mq._decide_core(ctx, _signal(0.0))
    check("no_trade in the neutral band", not decision.is_trade)


def scenario_bullish_opens_call() -> None:
    print("\n14. Positive momentum + flat + executable quote -> buy one call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = mq._decide_core(ctx, _signal(0.01))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_bearish_opens_put() -> None:
    print("\n15. Negative momentum + flat + executable quote -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = mq._decide_core(ctx, _signal(-0.01))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n16. Bullish signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = mq._decide_core(ctx, _signal(0.01))
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("\n17. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mq._decide_core(ctx, _signal(0.01))
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_momentum_flip_closes_opposite() -> None:
    print("\n18. Momentum flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mq._decide_core(ctx, _signal(0.01))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_unexpected_short_stands_down() -> None:
    print("\n19. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mq._decide_core(ctx, _signal(0.01))
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_neutral_while_positioned_closes() -> None:
    print("\n20. Return settles to neutral while holding a call -- close it, don't just decline")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mq._decide_core(ctx, _signal(0.0))
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_atm_drift_no_pyramiding() -> None:
    print("\n21. ATM drifts to a new strike while holding the old one -- no duplicate open")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        rows=[CALL_ROW_DRIFTED, PUT_ROW_DRIFTED], underlying_price=402.0,
        quote_map={"QQQ240101C00402000": fresh_quote("QQQ240101C00402000")},
    )
    decision = mq._decide_core(ctx, _signal(0.01))
    check(
        "no_trade rather than opening a second call at the new ATM strike",
        not decision.is_trade,
        decision.to_dict(),
    )
    check("reason references the originally held symbol, not the new ATM one",
          "QQQ240101C00400000" in decision.reason, decision.reason)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n22. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mq._decide_core(ctx, _signal(0.01))
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n23. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mq._decide_core(ctx, _signal(0.01))
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def scenario_custom_thresholds() -> None:
    print("\n24. Custom bullish/bearish thresholds are honored")
    ctx = make_ctx(
        session_phase="open",
        params={"bullish_threshold": 0.02, "bearish_threshold": -0.02},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mq._decide_core(ctx, _signal(0.01))
    check("no_trade -- 0.01 return doesn't clear the widened 0.02 threshold", not decision.is_trade, decision.to_dict())


# ---------------------------------------------------------------------------
# _decide_core()'s stale_source_reason branch -- mirrors trump_whisperer's
# fetch_error handling (P1 fix)
# ---------------------------------------------------------------------------


def scenario_stale_source_declines_while_flat() -> None:
    print("\n25. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = mq._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)
    check("reason cites the stale source", "stale" in decision.reason.lower())


def scenario_stale_source_while_positioned_retains_not_sells() -> None:
    print("\n26. Stale source while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        # A live, executable quote is available -- if the bug were still
        # present, the position could still get closed on some other basis,
        # so an executable quote can't be why this doesn't sell; only the
        # stale_source_reason-vs-computed-signal distinction can be.
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mq._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


# ---------------------------------------------------------------------------
# _decide() -- snapshot-timestamp recording, dedup, and staleness gate
# (P1 fix). Unlike the sentiment strategies, _decide has no network
# dependency here, so it's exercised directly rather than only through
# _decide_core -- but it does mutate the shared module-level tracker, so
# each scenario resets it first for isolation.
# ---------------------------------------------------------------------------


def _reset_tracker() -> None:
    mq._tracker = PriceHistoryTracker(retain_minutes=1440.0)
    mq._last_recorded_snapshot = None


def scenario_decide_records_using_snapshot_timestamp_not_now_et() -> None:
    print("\n27. _decide(): records observed_at from snapshot.timestamp, not ctx.now_et")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")
    mq._decide(ctx)
    points = mq._tracker.snapshot()
    check("exactly one point recorded", len(points) == 1, len(points))
    check(
        "recorded observed_at matches the snapshot's own timestamp, not the runner's now_et",
        points[0].observed_at == datetime(2026, 1, 1, 14, 58, tzinfo=timezone.utc),
        points[0].observed_at,
    )


def scenario_decide_dedupes_identical_snapshot() -> None:
    print("\n28. _decide(): repeated reads of the same unchanged snapshot are not double-recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx1 = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T15:00:00+00:00")
    ctx2 = make_ctx(session_phase="open", now_et=now + timedelta(minutes=1), snapshot_timestamp="2026-01-01T15:00:00+00:00")
    mq._decide(ctx1)
    mq._decide(ctx2)
    points = mq._tracker.snapshot()
    check(
        "only one point recorded despite two decide() calls against the same (unrepublished) snapshot",
        len(points) == 1,
        len(points),
    )


def scenario_decide_rejects_stale_snapshot_while_flat() -> None:
    print("\n29. _decide(): a snapshot far older than the runner's clock is rejected as a stale source, not recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00")  # 6 hours old
    decision = mq._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", len(mq._tracker.snapshot()) == 0, len(mq._tracker.snapshot()))
    check("reason cites the stale source", "stale" in decision.reason.lower() or "stalled" in decision.reason.lower(), decision.reason)


def scenario_decide_rejects_stale_snapshot_while_positioned() -> None:
    print("\n30. _decide(): a stale source snapshot while holding a position retains it rather than closing on a fabricated read")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00",
        trades=trades, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mq._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)


def scenario_decide_accepts_fresh_snapshot_within_age_limit() -> None:
    print("\n31. _decide(): a snapshot within the age limit is recorded normally")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")  # 2 minutes old
    mq._decide(ctx)
    check("one point recorded from a snapshot well within max_snapshot_age_minutes", len(mq._tracker.snapshot()) == 1)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_compute_no_data,
        scenario_compute_warming_up,
        scenario_compute_ok,
        scenario_compute_stale_anchor,
        scenario_compute_overshoot_bound_is_additive_not_multiplicative,
        scenario_compute_unsorted_input,
        scenario_tracker_prunes_old_points,
        scenario_market_closed,
        scenario_no_data,
        scenario_warming_up_while_flat,
        scenario_warming_up_while_positioned_closes,
        scenario_stale_anchor_while_positioned_closes,
        scenario_neutral,
        scenario_bullish_opens_call,
        scenario_bearish_opens_put,
        scenario_stale_quote_declines,
        scenario_already_positioned_holds,
        scenario_momentum_flip_closes_opposite,
        scenario_unexpected_short_stands_down,
        scenario_neutral_while_positioned_closes,
        scenario_atm_drift_no_pyramiding,
        scenario_multiple_open_positions_stand_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_custom_thresholds,
        scenario_stale_source_declines_while_flat,
        scenario_stale_source_while_positioned_retains_not_sells,
        scenario_decide_records_using_snapshot_timestamp_not_now_et,
        scenario_decide_dedupes_identical_snapshot,
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
