#!/usr/bin/env python3
"""Prove the put_call_ratio strategy's decision logic and PCR-extremity math.

Hermetic like verify_momentum_qqq.py, which this mirrors scenario-for-
scenario on purpose (see put_call_ratio.py's docstring for why the
position-management shape is identical): no network access, no real
snapshot fetch. `pcr.compute_pcr_extremity()` is exercised with hand-built
`PCRPoint` lists; the strategy's `_decide_core()` is exercised directly with
a hand-built `PCRExtremitySignal` -- it takes no tracker and makes no I/O.

    python scripts/verify_put_call_ratio.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.pcr import PCRExtremitySignal, PCRHistoryTracker, PCRPoint, compute_pcr_extremity  # noqa: E402
from crassus.strategies import put_call_ratio as pcr_strategy  # noqa: E402
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


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 1000}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 1000}
CALL_ROW_DRIFTED = {"OptionSymbol": "QQQ240101C00402000", "Strike": 402.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 1000}
PUT_ROW_DRIFTED = {"OptionSymbol": "QQQ240101P00402000", "Strike": 402.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 1000}


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


def _signal(
    z_score: float | None,
    *,
    status: str = "ok",
    current_pcr: float = 1.0,
    baseline_mean: float | None = 0.9,
    baseline_stdev: float | None = 0.1,
    baseline_sample_count: int = 10,
    sample_count: int = 11,
) -> PCRExtremitySignal:
    return PCRExtremitySignal(
        current_pcr=current_pcr,
        baseline_mean=baseline_mean,
        baseline_stdev=baseline_stdev,
        baseline_sample_count=baseline_sample_count,
        sample_count=sample_count,
        z_score=z_score,
        status=status,
    )


# ---------------------------------------------------------------------------
# compute_pcr() -- pure OI-table math
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("put_call_ratio_qqq is registered", "put_call_ratio_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["put_call_ratio_qqq"], "strategy_id", None) == pcr_strategy.STRATEGY_ID
        and getattr(REGISTRY["put_call_ratio_qqq"], "strategy_version", None) == pcr_strategy.STRATEGY_VERSION,
    )


def scenario_compute_pcr_basic() -> None:
    print("\n2. compute_pcr(): summed OI across multiple strikes")
    rows = [
        {"Type": "put", "OpenInterest": 300},
        {"Type": "put", "OpenInterest": 200},
        {"Type": "call", "OpenInterest": 100},
        {"Type": "call", "OpenInterest": 150},
    ]
    ratio = pcr_strategy.compute_pcr(rows)
    check("PCR is sum(put OI) / sum(call OI)", abs(ratio - (500 / 250)) < 1e-9, ratio)


def scenario_compute_pcr_zero_call_oi() -> None:
    print("\n3. compute_pcr(): zero call OI is undefined, not zero/infinite")
    rows = [{"Type": "put", "OpenInterest": 300}, {"Type": "call", "OpenInterest": 0}]
    check("returns None rather than dividing by zero", pcr_strategy.compute_pcr(rows) is None)


def scenario_compute_pcr_empty_rows() -> None:
    print("\n4. compute_pcr(): no rows at all is undefined")
    check("returns None for an empty chain", pcr_strategy.compute_pcr([]) is None)


def scenario_compute_pcr_missing_oi_field() -> None:
    print("\n5. compute_pcr(): missing OpenInterest treated as zero, not a crash")
    rows = [{"Type": "put"}, {"Type": "call", "OpenInterest": 100}]
    ratio = pcr_strategy.compute_pcr(rows)
    check("missing put OI counted as 0", ratio == 0.0, ratio)


# ---------------------------------------------------------------------------
# compute_pcr_extremity() -- pure z-score math
# ---------------------------------------------------------------------------


def scenario_extremity_no_data() -> None:
    print("\n6. compute_pcr_extremity(): empty history")
    signal = compute_pcr_extremity([])
    check("status is no_data", signal.status == "no_data", signal.status)
    check("z_score is None", signal.z_score is None)
    check("sample_count is zero", signal.sample_count == 0)


def scenario_extremity_warming_up() -> None:
    print("\n7. compute_pcr_extremity(): fewer than min_samples prior readings")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PCRPoint(now - timedelta(minutes=3), 0.9),
        PCRPoint(now - timedelta(minutes=2), 0.95),
        PCRPoint(now - timedelta(minutes=1), 1.0),
    ]
    signal = compute_pcr_extremity(history, min_samples=10)
    check("status is warming_up", signal.status == "warming_up", signal.status)
    check("z_score is None", signal.z_score is None)
    check("current_pcr is the newest reading", signal.current_pcr == 1.0, signal.current_pcr)
    check("baseline_sample_count counts only the prior points", signal.baseline_sample_count == 2, signal.baseline_sample_count)


def scenario_extremity_ok_high() -> None:
    print("\n8. compute_pcr_extremity(): an outlier-high reading vs. a tight baseline")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    # Baseline oscillates 0.9/1.1 (mean 1.0, stdev 0.1) so there's real
    # variance to score the outlier current reading against.
    baseline = [PCRPoint(now - timedelta(minutes=i), 0.9 if i % 2 else 1.1) for i in range(1, 11)]
    history = baseline + [PCRPoint(now, 2.0)]
    signal = compute_pcr_extremity(history, min_samples=10)
    check("status is ok", signal.status == "ok", signal.status)
    check("baseline excludes the current point", abs(signal.baseline_mean - 1.0) < 1e-9, signal.baseline_mean)
    check("z_score is strongly positive", signal.z_score is not None and signal.z_score > 5, signal.z_score)


def scenario_extremity_flat_baseline_zero_z() -> None:
    print("\n9. compute_pcr_extremity(): perfectly flat baseline scores z=0.0, not a division error")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    baseline = [PCRPoint(now - timedelta(minutes=i), 1.0) for i in range(1, 11)]
    history = baseline + [PCRPoint(now, 1.0)]
    signal = compute_pcr_extremity(history, min_samples=10)
    check("z_score is exactly 0.0 on a zero-stdev baseline", signal.z_score == 0.0, signal.z_score)


def scenario_extremity_unsorted_input() -> None:
    print("\n10. compute_pcr_extremity(): history need not be pre-sorted by the caller")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [PCRPoint(now, 2.0)] + [PCRPoint(now - timedelta(minutes=i), 1.0) for i in range(1, 11)]
    signal = compute_pcr_extremity(history, min_samples=10)
    check("current_pcr is still the chronologically newest point", signal.current_pcr == 2.0, signal.current_pcr)


def scenario_tracker_prunes_old_points() -> None:
    print("\n11. PCRHistoryTracker: points older than retain_minutes are dropped")
    tracker = PCRHistoryTracker(retain_minutes=60.0)
    t0 = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    tracker.observe(t0, 1.0)
    tracker.observe(t0 + timedelta(minutes=30), 1.1)
    tracker.observe(t0 + timedelta(minutes=61), 1.2)  # drops the t0 point (61m old at observe time)
    remaining = tracker.snapshot()
    check("oldest point pruned once past retain_minutes", all(p.pcr != 1.0 for p in remaining), [p.pcr for p in remaining])
    check("newer points retained", {p.pcr for p in remaining} == {1.1, 1.2}, {p.pcr for p in remaining})


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic (mirrors momentum_qqq's scenarios)
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n12. Market not open declines without touching the signal")
    ctx = make_ctx(session_phase="premarket")
    decision = pcr_strategy._decide_core(ctx, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_no_data() -> None:
    print("\n13. No PCR history recorded yet declines instead of crashing")
    ctx = make_ctx(session_phase="open")
    decision = pcr_strategy._decide_core(ctx, None)
    check("no_trade with no signal", not decision.is_trade)
    check("reason cites missing history", "No PCR history" in decision.reason)


def scenario_warming_up_while_flat() -> None:
    print("\n14. Warming up (not enough baseline yet) while flat -- stand down")
    ctx = make_ctx(session_phase="open")
    decision = pcr_strategy._decide_core(ctx, _signal(None, status="warming_up", baseline_sample_count=2))
    check("no_trade while warming up", not decision.is_trade)
    check("reason cites warming up", "warming up" in decision.reason.lower())


def scenario_warming_up_while_positioned_closes() -> None:
    print("\n15. Warming up (e.g. after a restart) while holding a position -- close it, don't freeze holding it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide_core(ctx, _signal(None, status="warming_up", baseline_sample_count=1))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_normal_range_while_flat() -> None:
    print("\n16. Normal-range PCR (|z| under threshold) while flat trades nothing")
    ctx = make_ctx(session_phase="open")
    decision = pcr_strategy._decide_core(ctx, _signal(0.5))
    check("no_trade in the normal band", not decision.is_trade)


def scenario_normal_range_while_positioned_closes() -> None:
    print("\n17. PCR settles back to normal while holding a call -- close it, don't just decline")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide_core(ctx, _signal(0.2))
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_extreme_high_pcr_opens_call() -> None:
    print("\n18. Extremely high PCR (put-heavy crowd) -> contrarian bullish -> buy one call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = pcr_strategy._decide_core(ctx, _signal(2.0, current_pcr=1.5))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("metadata carries current_pcr and z_score", decision.metadata.get("current_pcr") == 1.5 and decision.metadata.get("z_score") == 2.0)


def scenario_extreme_low_pcr_opens_put() -> None:
    print("\n19. Extremely low PCR (call-heavy crowd) -> contrarian bearish -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = pcr_strategy._decide_core(ctx, _signal(-2.0, current_pcr=0.5))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n20. Extreme signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("\n21. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_extremity_flip_closes_opposite() -> None:
    print("\n22. PCR extremity flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_unexpected_short_stands_down() -> None:
    print("\n23. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_atm_drift_no_pyramiding() -> None:
    print("\n24. ATM drifts to a new strike while holding the old one -- no duplicate open")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        rows=[CALL_ROW_DRIFTED, PUT_ROW_DRIFTED], underlying_price=402.0,
        quote_map={"QQQ240101C00402000": fresh_quote("QQQ240101C00402000")},
    )
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check(
        "no_trade rather than opening a second call at the new ATM strike",
        not decision.is_trade,
        decision.to_dict(),
    )
    check("reason references the originally held symbol, not the new ATM one",
          "QQQ240101C00400000" in decision.reason, decision.reason)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n25. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n26. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def scenario_custom_threshold() -> None:
    print("\n27. Custom extreme_z_threshold is honored")
    ctx = make_ctx(
        session_phase="open",
        params={"extreme_z_threshold": 3.0},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide_core(ctx, _signal(2.0))
    check("no_trade -- z=2.0 doesn't clear the widened 3.0 threshold", not decision.is_trade, decision.to_dict())


# ---------------------------------------------------------------------------
# _decide_core()'s stale_source_reason branch -- mirrors momentum_qqq's
# handling of an unusable source read
# ---------------------------------------------------------------------------


def scenario_stale_source_declines_while_flat() -> None:
    print("\n28. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = pcr_strategy._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)
    check("reason cites the stale source", "stale" in decision.reason.lower())


def scenario_stale_source_while_positioned_retains_not_sells() -> None:
    print("\n29. Stale source while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        # A live, executable quote is available -- if the bug were still
        # present, the position could still get closed on some other basis,
        # so an executable quote can't be why this doesn't sell; only the
        # stale_source_reason-vs-computed-signal distinction can be.
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_undefined_pcr_retains_position() -> None:
    print("\n30. Undefined PCR (zero call OI) while holding a position retains it, mirroring a stale source")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide_core(ctx, None, stale_source_reason="no call open interest in snapshot; PCR is undefined")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason cites the undefined ratio", "undefined" in decision.reason.lower())


# ---------------------------------------------------------------------------
# _decide() -- snapshot-timestamp recording, dedup, and staleness gate
# Unlike the sentiment strategies, _decide has no network dependency here,
# so it's exercised directly rather than only through _decide_core -- but it
# does mutate the shared module-level tracker, so each scenario resets it
# first for isolation.
# ---------------------------------------------------------------------------


def _reset_tracker() -> None:
    pcr_strategy._tracker = PCRHistoryTracker(retain_minutes=1440.0)
    pcr_strategy._last_recorded_snapshot = None


def scenario_decide_records_using_snapshot_timestamp_not_now_et() -> None:
    print("\n31. _decide(): records observed_at from snapshot.timestamp, not ctx.now_et")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")
    pcr_strategy._decide(ctx)
    points = pcr_strategy._tracker.snapshot()
    check("exactly one point recorded", len(points) == 1, len(points))
    check(
        "recorded observed_at matches the snapshot's own timestamp, not the runner's now_et",
        points[0].observed_at == datetime(2026, 1, 1, 14, 58, tzinfo=timezone.utc),
        points[0].observed_at,
    )
    check("recorded PCR matches compute_pcr(rows)", points[0].pcr == pcr_strategy.compute_pcr([CALL_ROW, PUT_ROW]), points[0].pcr)


def scenario_decide_dedupes_identical_snapshot() -> None:
    print("\n32. _decide(): repeated reads of the same unchanged snapshot are not double-recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx1 = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T15:00:00+00:00")
    ctx2 = make_ctx(session_phase="open", now_et=now + timedelta(minutes=1), snapshot_timestamp="2026-01-01T15:00:00+00:00")
    pcr_strategy._decide(ctx1)
    pcr_strategy._decide(ctx2)
    points = pcr_strategy._tracker.snapshot()
    check(
        "only one point recorded despite two decide() calls against the same (unrepublished) snapshot",
        len(points) == 1,
        len(points),
    )


def scenario_decide_rejects_stale_snapshot_while_flat() -> None:
    print("\n33. _decide(): a snapshot far older than the runner's clock is rejected as a stale source, not recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00")  # 6 hours old
    decision = pcr_strategy._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", len(pcr_strategy._tracker.snapshot()) == 0, len(pcr_strategy._tracker.snapshot()))
    check("reason cites the stale source", "stale" in decision.reason.lower() or "stalled" in decision.reason.lower(), decision.reason)


def scenario_decide_rejects_stale_snapshot_while_positioned() -> None:
    print("\n34. _decide(): a stale source snapshot while holding a position retains it rather than closing on a fabricated read")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00",
        trades=trades, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)


def scenario_decide_accepts_fresh_snapshot_within_age_limit() -> None:
    print("\n35. _decide(): a snapshot within the age limit is recorded normally")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")  # 2 minutes old
    pcr_strategy._decide(ctx)
    check("one point recorded from a snapshot well within max_snapshot_age_minutes", len(pcr_strategy._tracker.snapshot()) == 1)


def scenario_decide_zero_call_oi_not_recorded() -> None:
    print("\n36. _decide(): a snapshot with zero call OI is not recorded as a data point")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    zero_call_rows = [
        {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 500},
        {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 0},
    ]
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00", rows=zero_call_rows)
    decision = pcr_strategy._decide(ctx)
    check("no_trade on an undefined PCR", not decision.is_trade, decision.to_dict())
    check("nothing recorded", len(pcr_strategy._tracker.snapshot()) == 0, len(pcr_strategy._tracker.snapshot()))
    check("reason cites the undefined ratio", "undefined" in decision.reason.lower(), decision.reason)


def scenario_decide_end_to_end_extreme_reading_trades() -> None:
    print("\n37. _decide(): a genuinely extreme reading built up over many cycles produces a trade")
    _reset_tracker()
    base_time = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    # 10 baseline cycles oscillating PCR 0.9/1.1 (mean 1.0, stdev 0.1, so the
    # baseline has real variance to score against), then one cycle with a
    # sharp put-heavy skew -- should score as extremely high and open a
    # contrarian call.
    for i in range(10):
        put_oi = 1100 if i % 2 == 0 else 900
        rows = [
            {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 1000},
            {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": put_oi},
        ]
        ts = (base_time + timedelta(minutes=i)).isoformat()
        ctx = make_ctx(session_phase="open", now_et=base_time + timedelta(minutes=i), snapshot_timestamp=ts, rows=rows)
        pcr_strategy._decide(ctx)

    skewed_rows = [
        {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 1000},
        {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "OpenInterest": 5000},
    ]
    now = base_time + timedelta(minutes=10)
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp=now.isoformat(),
        rows=skewed_rows,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = pcr_strategy._decide(ctx)
    check("action is buy", decision.action == "buy", decision.to_dict())
    check("opens the contrarian call, not a put", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("metadata reports the extreme z_score", decision.metadata.get("z_score") is not None and decision.metadata["z_score"] > 1.5, decision.metadata)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_compute_pcr_basic,
        scenario_compute_pcr_zero_call_oi,
        scenario_compute_pcr_empty_rows,
        scenario_compute_pcr_missing_oi_field,
        scenario_extremity_no_data,
        scenario_extremity_warming_up,
        scenario_extremity_ok_high,
        scenario_extremity_flat_baseline_zero_z,
        scenario_extremity_unsorted_input,
        scenario_tracker_prunes_old_points,
        scenario_market_closed,
        scenario_no_data,
        scenario_warming_up_while_flat,
        scenario_warming_up_while_positioned_closes,
        scenario_normal_range_while_flat,
        scenario_normal_range_while_positioned_closes,
        scenario_extreme_high_pcr_opens_call,
        scenario_extreme_low_pcr_opens_put,
        scenario_stale_quote_declines,
        scenario_already_positioned_holds,
        scenario_extremity_flip_closes_opposite,
        scenario_unexpected_short_stands_down,
        scenario_atm_drift_no_pyramiding,
        scenario_multiple_open_positions_stand_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_custom_threshold,
        scenario_stale_source_declines_while_flat,
        scenario_stale_source_while_positioned_retains_not_sells,
        scenario_undefined_pcr_retains_position,
        scenario_decide_records_using_snapshot_timestamp_not_now_et,
        scenario_decide_dedupes_identical_snapshot,
        scenario_decide_rejects_stale_snapshot_while_flat,
        scenario_decide_rejects_stale_snapshot_while_positioned,
        scenario_decide_accepts_fresh_snapshot_within_age_limit,
        scenario_decide_zero_call_oi_not_recorded,
        scenario_decide_end_to_end_extreme_reading_trades,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
