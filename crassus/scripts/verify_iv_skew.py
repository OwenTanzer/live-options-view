#!/usr/bin/env python3
"""Prove the iv_skew_qqq strategy's 25-delta selection, skew math, and
decision logic.

Hermetic like verify_momentum_qqq.py, which this mirrors on purpose (see
iv_skew.py's docstring for why the position-management shape is identical):
no network access, no real snapshot fetch. `skew.compute_skew_signal()` is
exercised with hand-built `PricePoint` lists; `iv_skew._decide_core()` is
exercised directly with a hand-built `SkewSignal` -- it takes no tracker and
makes no I/O; `iv_skew.select_25_delta_rows()` is exercised against
hand-built chains; `iv_skew._decide()` is exercised end-to-end for the
snapshot-timestamp recording/dedup/staleness behavior.

    python scripts/verify_iv_skew.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import PriceHistoryTracker, PricePoint  # noqa: E402
from crassus.skew import SkewSignal, compute_skew_signal  # noqa: E402
from crassus.strategies import iv_skew as ivs  # noqa: E402
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


UNDERLYING = 400.0

# ATM rows -- used as the execution leg once a direction is supported.
ATM_CALL = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "Delta": 0.50, "IV": 0.22}
ATM_PUT = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "Delta": -0.50, "IV": 0.24}

# 25-delta OTM candidates.
PUT_25D = {"OptionSymbol": "QQQ240101P00380000", "Strike": 380.0, "Type": "put", "Bid": 0.5, "Ask": 0.6, "Delta": -0.25, "IV": 0.30}
CALL_25D = {"OptionSymbol": "QQQ240101C00420000", "Strike": 420.0, "Type": "call", "Bid": 0.5, "Ask": 0.6, "Delta": 0.25, "IV": 0.20}

# Further-out decoys -- present to prove selection picks the *closest* to
# 0.25, not just any OTM candidate.
PUT_10D = {"OptionSymbol": "QQQ240101P00360000", "Strike": 360.0, "Type": "put", "Bid": 0.2, "Ask": 0.3, "Delta": -0.10, "IV": 0.35}
CALL_10D = {"OptionSymbol": "QQQ240101C00440000", "Strike": 440.0, "Type": "call", "Bid": 0.2, "Ask": 0.3, "Delta": 0.10, "IV": 0.18}

FULL_CHAIN = [ATM_CALL, ATM_PUT, PUT_25D, CALL_25D, PUT_10D, CALL_10D]


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = UNDERLYING,
    now_et: datetime | None = None,
    snapshot_timestamp: str = "2024-01-01T15:00:00+00:00",
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else FULL_CHAIN, timestamp=snapshot_timestamp)
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


def _signal(z_score: float | None, *, status: str = "ok", sample_count: int = 12, current_skew: float = 0.10) -> SkewSignal:
    mean = current_skew - (z_score * 0.02) if z_score is not None else None
    stdev = 0.02 if z_score is not None else None
    return SkewSignal(
        current_skew=current_skew,
        baseline_mean=mean,
        baseline_stdev=stdev,
        sample_count=sample_count,
        z_score=z_score,
        status=status,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("iv_skew_qqq is registered", "iv_skew_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["iv_skew_qqq"], "strategy_id", None) == ivs.STRATEGY_ID
        and getattr(REGISTRY["iv_skew_qqq"], "strategy_version", None) == ivs.STRATEGY_VERSION,
    )


# ---------------------------------------------------------------------------
# select_25_delta_rows() -- row selection
# ---------------------------------------------------------------------------


def scenario_selects_closest_to_25_delta() -> None:
    print("\n2. select_25_delta_rows(): picks the OTM put/call closest to |delta|=0.25 out of several candidates")
    put_row, call_row = ivs.select_25_delta_rows(FULL_CHAIN, UNDERLYING)
    check("put candidate is the -0.25 delta row, not the -0.10 decoy", put_row is not None and put_row["OptionSymbol"] == PUT_25D["OptionSymbol"], put_row)
    check("call candidate is the 0.25 delta row, not the 0.10 decoy", call_row is not None and call_row["OptionSymbol"] == CALL_25D["OptionSymbol"], call_row)


def scenario_selection_excludes_missing_iv_or_delta() -> None:
    print("\n3. select_25_delta_rows(): rows missing Delta or IV are excluded from candidacy")
    broken_put = {"OptionSymbol": "QQQ240101P00380000", "Strike": 380.0, "Type": "put", "Bid": 0.5, "Ask": 0.6, "Delta": -0.25, "IV": None}
    rows = [ATM_CALL, ATM_PUT, broken_put, CALL_25D]
    put_row, call_row = ivs.select_25_delta_rows(rows, UNDERLYING)
    check("put with missing IV is not selected", put_row is None, put_row)
    check("call candidate still found", call_row is not None and call_row["OptionSymbol"] == CALL_25D["OptionSymbol"])


def scenario_selection_excludes_itm_side() -> None:
    print("4. select_25_delta_rows(): a put with positive delta / strike above spot is not a valid OTM candidate")
    mislabeled_put = {"OptionSymbol": "QQQ240101P00420000", "Strike": 420.0, "Type": "put", "Bid": 0.5, "Ask": 0.6, "Delta": 0.25, "IV": 0.30}
    rows = [ATM_CALL, ATM_PUT, mislabeled_put, CALL_25D]
    put_row, call_row = ivs.select_25_delta_rows(rows, UNDERLYING)
    check("mislabeled ITM-shaped put is excluded", put_row is None, put_row)


def scenario_skew_computed_correctly() -> None:
    print("5. _decide(): skew = put_iv - call_iv is computed correctly from the selected 25-delta rows")
    ivs._tracker = PriceHistoryTracker(retain_minutes=1440.0)
    ivs._last_recorded_snapshot = None
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(now_et=now, snapshot_timestamp="2024-01-01T15:00:00+00:00")
    decision = ivs._decide(ctx)
    expected_skew = PUT_25D["IV"] - CALL_25D["IV"]
    check(
        "metadata.skew matches put_iv - call_iv",
        decision.metadata is not None and abs(decision.metadata["skew"] - expected_skew) < 1e-9,
        decision.metadata,
    )
    check("metadata carries put_iv/call_iv/put_delta/call_delta", decision.metadata is not None and decision.metadata["put_iv"] == PUT_25D["IV"] and decision.metadata["call_delta"] == CALL_25D["Delta"])


# ---------------------------------------------------------------------------
# compute_skew_signal() -- pure baseline math
# ---------------------------------------------------------------------------


def scenario_compute_no_data() -> None:
    print("\n6. compute_skew_signal(): empty history")
    signal = compute_skew_signal([])
    check("status is no_data", signal.status == "no_data", signal.status)


def scenario_compute_warming_up() -> None:
    print("7. compute_skew_signal(): fewer than min_samples points -> warming_up")
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [PricePoint(now - timedelta(minutes=i), 0.05) for i in range(3)]
    signal = compute_skew_signal(history, min_samples=10)
    check("status is warming_up", signal.status == "warming_up", signal.status)
    check("z_score is None", signal.z_score is None)
    check("current_skew is still surfaced", signal.current_skew == 0.05)


def scenario_compute_ok_z_score() -> None:
    print("8. compute_skew_signal(): z-score reflects distance from the running mean/stdev")
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [PricePoint(now - timedelta(minutes=i), 0.05) for i in range(1, 10)]
    history.append(PricePoint(now, 0.20))  # a clear outlier as the newest point
    signal = compute_skew_signal(history, min_samples=5)
    check("status is ok", signal.status == "ok", signal.status)
    check("current_skew is the newest point", signal.current_skew == 0.20)
    check("z_score is strongly positive for the outlier", signal.z_score is not None and signal.z_score > 1.5, signal.z_score)


def scenario_compute_zero_variance() -> None:
    print("9. compute_skew_signal(): zero-variance baseline reports z_score=0.0, not a division error")
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [PricePoint(now - timedelta(minutes=i), 0.05) for i in range(10)]
    signal = compute_skew_signal(history, min_samples=5)
    check("status is ok", signal.status == "ok", signal.status)
    check("z_score is 0.0 on a flat history", signal.z_score == 0.0, signal.z_score)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n10. Market not open declines without touching the signal")
    ctx = make_ctx(session_phase="premarket")
    decision = ivs._decide_core(ctx, None)
    check("no_trade when market isn't open", not decision.is_trade)


def scenario_no_data() -> None:
    print("11. No skew history recorded yet declines instead of crashing")
    ctx = make_ctx(session_phase="open")
    decision = ivs._decide_core(ctx, None)
    check("no_trade with no signal", not decision.is_trade)
    check("reason cites missing history", "No skew history" in decision.reason)


def scenario_warming_up_while_flat() -> None:
    print("12. Warming up (not enough samples yet) while flat -- stand down")
    ctx = make_ctx(session_phase="open")
    decision = ivs._decide_core(ctx, _signal(None, status="warming_up", sample_count=3))
    check("no_trade while warming up", not decision.is_trade)
    check("reason cites warming up", "warming up" in decision.reason.lower())
    check("metadata still carries sample_count", decision.metadata is not None and decision.metadata["sample_count"] == 3)


def scenario_warming_up_while_positioned_closes() -> None:
    print("13. Warming up while holding a position -- close it, don't freeze holding it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = ivs._decide_core(ctx, _signal(None, status="warming_up", sample_count=1))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_normal_range_no_trade() -> None:
    print("14. Normal-range skew (small z-score) while flat trades nothing")
    ctx = make_ctx(session_phase="open")
    decision = ivs._decide_core(ctx, _signal(0.4))
    check("no_trade -- z within default threshold 1.5", not decision.is_trade, decision.to_dict())


def scenario_normal_range_while_positioned_closes() -> None:
    print("15. Skew settles back to normal range while holding a call -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = ivs._decide_core(ctx, _signal(0.2))
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_extreme_rich_buys_call() -> None:
    print("16. Extremely rich skew (high positive z) + flat + executable quote -> contrarian buy call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("reason names the contrarian read", "contrarian" in decision.reason.lower() or "overdone" in decision.reason.lower(), decision.reason)


def scenario_extreme_compressed_buys_put() -> None:
    print("17. Extremely compressed/inverted skew (very negative z) + flat + executable quote -> buy put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = ivs._decide_core(ctx, _signal(-2.5))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("18. Extreme signal but stale execution quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("19. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_signal_flip_closes_opposite() -> None:
    print("20. Signal flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_unexpected_short_stands_down() -> None:
    print("21. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)


def scenario_multiple_open_positions_stand_down() -> None:
    print("22. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("no_trade with more than one open position", not decision.is_trade)


def scenario_custom_threshold() -> None:
    print("23. Custom min_skew_z_threshold is honored")
    ctx = make_ctx(
        session_phase="open",
        params={"min_skew_z_threshold": 3.0},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = ivs._decide_core(ctx, _signal(2.5))
    check("no_trade -- z=2.5 doesn't clear the widened threshold of 3.0", not decision.is_trade, decision.to_dict())


# ---------------------------------------------------------------------------
# stale_source_reason / candidates_reason branches
# ---------------------------------------------------------------------------


def scenario_stale_source_declines_while_flat() -> None:
    print("\n24. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = ivs._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)
    check("reason cites the stale source", "stale" in decision.reason.lower())


def scenario_stale_source_while_positioned_retains_not_sells() -> None:
    print("25. Stale source while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = ivs._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_missing_candidates_declines_while_flat() -> None:
    print("26. Missing 25-delta put/call candidates declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = ivs._decide_core(ctx, None, candidates_reason="No usable 25-delta OTM put found in this snapshot.")
    check("no_trade on missing candidates", not decision.is_trade)
    check("reason cites the missing candidate", "25-delta" in decision.reason)


def scenario_missing_candidates_while_positioned_retains() -> None:
    print("27. Missing 25-delta candidates while holding a position retains it rather than closing")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = ivs._decide_core(ctx, None, candidates_reason="No usable 25-delta OTM call found in this snapshot.")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_decide_missing_candidates_end_to_end() -> None:
    print("28. _decide(): a chain with no valid 25-delta candidates declines end-to-end without recording")
    ivs._tracker = PriceHistoryTracker(retain_minutes=1440.0)
    ivs._last_recorded_snapshot = None
    now = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(now_et=now, rows=[ATM_CALL, ATM_PUT], snapshot_timestamp="2024-01-01T15:00:00+00:00")
    decision = ivs._decide(ctx)
    check("no_trade end-to-end when the chain has no 25-delta wings", not decision.is_trade, decision.to_dict())
    check("nothing recorded into the tracker", len(ivs._tracker.snapshot()) == 0)


# ---------------------------------------------------------------------------
# _decide() -- snapshot-timestamp recording, dedup, staleness gate
# ---------------------------------------------------------------------------


def _reset_tracker() -> None:
    ivs._tracker = PriceHistoryTracker(retain_minutes=1440.0)
    ivs._last_recorded_snapshot = None


def scenario_decide_records_using_snapshot_timestamp_not_now_et() -> None:
    print("\n29. _decide(): records observed_at from snapshot.timestamp, not ctx.now_et")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")
    ivs._decide(ctx)
    points = ivs._tracker.snapshot()
    check("exactly one point recorded", len(points) == 1, len(points))
    check(
        "recorded observed_at matches the snapshot's own timestamp, not the runner's now_et",
        points[0].observed_at == datetime(2026, 1, 1, 14, 58, tzinfo=timezone.utc),
        points[0].observed_at,
    )


def scenario_decide_dedupes_identical_snapshot() -> None:
    print("30. _decide(): repeated reads of the same unchanged snapshot are not double-recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx1 = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T15:00:00+00:00")
    ctx2 = make_ctx(session_phase="open", now_et=now + timedelta(minutes=1), snapshot_timestamp="2026-01-01T15:00:00+00:00")
    ivs._decide(ctx1)
    ivs._decide(ctx2)
    points = ivs._tracker.snapshot()
    check(
        "only one point recorded despite two decide() calls against the same (unrepublished) snapshot",
        len(points) == 1,
        len(points),
    )


def scenario_decide_rejects_stale_snapshot_while_flat() -> None:
    print("31. _decide(): a snapshot far older than the runner's clock is rejected as a stale source, not recorded")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00")  # 6 hours old
    decision = ivs._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", len(ivs._tracker.snapshot()) == 0, len(ivs._tracker.snapshot()))


def scenario_decide_rejects_stale_snapshot_while_positioned() -> None:
    print("32. _decide(): a stale source snapshot while holding a position retains it rather than closing on a fabricated read")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T09:00:00+00:00",
        trades=trades, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = ivs._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)


def scenario_decide_accepts_fresh_snapshot_within_age_limit() -> None:
    print("33. _decide(): a snapshot within the age limit is recorded normally")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")  # 2 minutes old
    ivs._decide(ctx)
    check("one point recorded from a snapshot well within max_snapshot_age_minutes", len(ivs._tracker.snapshot()) == 1)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_selects_closest_to_25_delta,
        scenario_selection_excludes_missing_iv_or_delta,
        scenario_selection_excludes_itm_side,
        scenario_skew_computed_correctly,
        scenario_compute_no_data,
        scenario_compute_warming_up,
        scenario_compute_ok_z_score,
        scenario_compute_zero_variance,
        scenario_market_closed,
        scenario_no_data,
        scenario_warming_up_while_flat,
        scenario_warming_up_while_positioned_closes,
        scenario_normal_range_no_trade,
        scenario_normal_range_while_positioned_closes,
        scenario_extreme_rich_buys_call,
        scenario_extreme_compressed_buys_put,
        scenario_stale_quote_declines,
        scenario_already_positioned_holds,
        scenario_signal_flip_closes_opposite,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_custom_threshold,
        scenario_stale_source_declines_while_flat,
        scenario_stale_source_while_positioned_retains_not_sells,
        scenario_missing_candidates_declines_while_flat,
        scenario_missing_candidates_while_positioned_retains,
        scenario_decide_missing_candidates_end_to_end,
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
