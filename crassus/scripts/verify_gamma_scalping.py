#!/usr/bin/env python3
"""Prove the gamma_scalping_qqq strategy's decision logic and realized-vol math.

Hermetic, like verify_momentum_qqq.py, which this mirrors scenario-for-
scenario where the shapes line up (see gamma_scalping.py's docstring for why
position-management is deliberately identical): no network access, no real
snapshot fetch. `gamma_scalping.compute_realized_vol()` is exercised with
hand-built `PricePoint` lists; the strategy's `_decide_core()` is exercised
directly with a hand-built `RealizedVolSignal` -- it takes no tracker and
makes no I/O.

    python scripts/verify_gamma_scalping.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import PricePoint, PriceHistoryTracker  # noqa: E402
from crassus.strategies import gamma_scalping as gs  # noqa: E402
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


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1, "IV": 0.20}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1, "IV": 0.22}
CALL_ROW_NO_IV = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW_NO_IV = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


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
    realized_vol: float | None,
    trailing_return: float | None,
    *,
    status: str = "ok",
    sample_count: int = 10,
    lookback_minutes: float = 30.0,
) -> gs.RealizedVolSignal:
    return gs.RealizedVolSignal(
        lookback_minutes=lookback_minutes,
        sample_count=sample_count,
        realized_vol=realized_vol,
        trailing_return_over_window=trailing_return,
        window_start_price=400.0,
        window_end_price=400.0 * (1.0 + trailing_return) if trailing_return is not None else None,
        status=status,
    )


# ---------------------------------------------------------------------------
# compute_realized_vol() -- pure realized-vol math
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("gamma_scalping_qqq is registered", "gamma_scalping_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["gamma_scalping_qqq"], "strategy_id", None) == gs.STRATEGY_ID
        and getattr(REGISTRY["gamma_scalping_qqq"], "strategy_version", None) == gs.STRATEGY_VERSION,
    )


def scenario_compute_no_data() -> None:
    print("\n2. compute_realized_vol(): empty history")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    signal = gs.compute_realized_vol([], now=now, lookback_minutes=30.0, min_samples=5)
    check("status is no_data", signal.status == "no_data", signal.status)
    check("realized_vol is None", signal.realized_vol is None)
    check("sample_count is zero", signal.sample_count == 0)


def scenario_compute_warming_up_too_few_samples() -> None:
    print("\n3. compute_realized_vol(): fewer than min_samples in the window")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=20), 400.0),
        PricePoint(now - timedelta(minutes=10), 401.0),
        PricePoint(now, 402.0),
    ]
    signal = gs.compute_realized_vol(history, now=now, lookback_minutes=30.0, min_samples=5)
    check("status is warming_up", signal.status == "warming_up", signal.status)
    check("realized_vol is None", signal.realized_vol is None)
    check("sample_count reflects the 3 in-window points", signal.sample_count == 3, signal.sample_count)


def scenario_compute_realized_vol_hand_built_series() -> None:
    print("\n4. compute_realized_vol(): matches a hand-computed stdev of log returns, annualized")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    prices = [400.0, 401.0, 400.5, 402.0, 401.5, 403.0]
    history = [
        PricePoint(now - timedelta(minutes=(len(prices) - 1 - i) * 5), p)
        for i, p in enumerate(prices)
    ]
    signal = gs.compute_realized_vol(history, now=now, lookback_minutes=30.0, min_samples=5)

    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean_r = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
    # Fixture is spaced 5 minutes apart, not 1 -- annualization must scale
    # against that observed cadence, not a fixed one-minute assumption.
    expected_periods_per_year = gs.ANNUALIZATION_TRADING_MINUTES_PER_YEAR / 5.0
    expected_vol = math.sqrt(variance) * math.sqrt(expected_periods_per_year)
    expected_trailing_return = (prices[-1] / prices[0]) - 1.0

    check("status is ok", signal.status == "ok", signal.status)
    check(
        "realized_vol matches hand-computed annualized stdev of log returns",
        signal.realized_vol is not None and abs(signal.realized_vol - expected_vol) < 1e-9,
        (signal.realized_vol, expected_vol),
    )
    check(
        "trailing_return_over_window matches (last/first - 1)",
        signal.trailing_return_over_window is not None
        and abs(signal.trailing_return_over_window - expected_trailing_return) < 1e-9,
        (signal.trailing_return_over_window, expected_trailing_return),
    )
    check("sample_count counts every in-window point", signal.sample_count == len(prices), signal.sample_count)


def scenario_compute_annualization_constant() -> None:
    print("\n5. compute_realized_vol(): annualization constant is 252*390 trading minutes/year")
    check(
        "ANNUALIZATION_TRADING_MINUTES_PER_YEAR is 252*390",
        gs.ANNUALIZATION_TRADING_MINUTES_PER_YEAR == 252 * 390,
        gs.ANNUALIZATION_TRADING_MINUTES_PER_YEAR,
    )


def scenario_compute_realized_vol_scales_with_deployed_cadence() -> None:
    print("\n5b. compute_realized_vol(): annualization tracks the deployed 300s cadence, not a fixed 1-minute assumption")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    prices = [400.0, 401.0, 400.5, 402.0, 401.5, 403.0]
    # Sampled at the runner's actual deployed interval (300s), not 1 minute.
    history_300s = [
        PricePoint(now - timedelta(seconds=(len(prices) - 1 - i) * 300), p) for i, p in enumerate(prices)
    ]
    signal_300s = gs.compute_realized_vol(history_300s, now=now, lookback_minutes=30.0, min_samples=5)

    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean_r = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
    correctly_annualized = math.sqrt(variance) * math.sqrt(gs.ANNUALIZATION_TRADING_MINUTES_PER_YEAR / 5.0)
    # What the pre-fix implementation returned: a fixed sqrt(98280) applied
    # regardless of the actual 5-minute spacing between samples.
    bugged_fixed_98280 = math.sqrt(variance) * math.sqrt(gs.ANNUALIZATION_TRADING_MINUTES_PER_YEAR)

    check("status is ok", signal_300s.status == "ok", signal_300s.status)
    check(
        "realized_vol is annualized against the observed 300s spacing, not a fixed one-minute assumption",
        signal_300s.realized_vol is not None and abs(signal_300s.realized_vol - correctly_annualized) < 1e-9,
        (signal_300s.realized_vol, correctly_annualized),
    )
    check(
        "the fixed-98280 (pre-fix) computation would have overstated this by sqrt(5) =~ 2.24x",
        signal_300s.realized_vol is not None and abs(bugged_fixed_98280 - signal_300s.realized_vol * math.sqrt(5)) < 1e-9,
        (bugged_fixed_98280, signal_300s.realized_vol * math.sqrt(5) if signal_300s.realized_vol else None),
    )


def scenario_compute_excludes_out_of_window_points() -> None:
    print("\n6. compute_realized_vol(): points outside the lookback window aren't counted")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=200), 350.0),  # far outside a 30m window
        PricePoint(now - timedelta(minutes=25), 400.0),
        PricePoint(now - timedelta(minutes=20), 401.0),
        PricePoint(now - timedelta(minutes=15), 400.5),
        PricePoint(now - timedelta(minutes=10), 402.0),
        PricePoint(now, 403.0),
    ]
    signal = gs.compute_realized_vol(history, now=now, lookback_minutes=30.0, min_samples=5)
    check("sample_count excludes the stale outlier", signal.sample_count == 5, signal.sample_count)
    check("status is ok with 5 in-window points", signal.status == "ok", signal.status)


def scenario_tracker_reused_from_momentum() -> None:
    print("\n7. gamma_scalping reuses momentum.py's PriceHistoryTracker/PricePoint")
    tracker = PriceHistoryTracker(retain_minutes=60.0)
    t0 = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    tracker.observe(t0, 400.0)
    check("tracker accumulates a PricePoint", len(tracker.snapshot()) == 1)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n8. Market not open declines without touching the signal")
    ctx = make_ctx(session_phase="premarket")
    decision = gs._decide_core(ctx, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_no_data_while_flat() -> None:
    print("\n9. No price history recorded yet declines instead of crashing")
    ctx = make_ctx(session_phase="open")
    decision = gs._decide_core(ctx, None)
    check("no_trade with no signal", not decision.is_trade)
    check("metadata carries the 5 required keys with None values", all(
        decision.metadata.get(k) is None
        for k in ("realized_vol", "implied_vol", "vol_ratio", "trailing_return_over_window")
    ) and decision.metadata.get("vol_ratio_threshold") == gs.DEFAULT_VOL_RATIO_THRESHOLD)


def scenario_warming_up_while_flat() -> None:
    print("\n10. Warming up (not enough realized-vol samples yet) while flat -- stand down")
    ctx = make_ctx(session_phase="open")
    decision = gs._decide_core(ctx, _signal(None, None, status="warming_up", sample_count=2))
    check("no_trade while warming up", not decision.is_trade)
    check("reason cites warming up", "warming up" in decision.reason.lower())


def scenario_warming_up_while_positioned_closes() -> None:
    print("\n11. Warming up while holding a position -- close it, don't freeze holding it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = gs._decide_core(ctx, _signal(None, None, status="warming_up", sample_count=1))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_missing_atm_iv_while_flat() -> None:
    print("\n12. Missing ATM IV (call+put both unusable) while flat declines, not neutral")
    ctx = make_ctx(session_phase="open", rows=[CALL_ROW_NO_IV, PUT_ROW_NO_IV])
    decision = gs._decide_core(ctx, _signal(0.5, 0.01, status="ok"))
    check("no_trade on missing IV", not decision.is_trade)
    check("reason cites missing implied vol", "implied vol" in decision.reason.lower())
    check("implied_vol is None in metadata", decision.metadata.get("implied_vol") is None)
    check("realized_vol is still surfaced", decision.metadata.get("realized_vol") == 0.5)


def scenario_missing_atm_iv_while_positioned_retains() -> None:
    print("\n13. Missing ATM IV while holding a position retains it -- fetch/data-absence, not evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades, rows=[CALL_ROW_NO_IV, PUT_ROW_NO_IV],
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = gs._decide_core(ctx, _signal(0.5, 0.01, status="ok"))
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_ratio_above_threshold_nonneg_return_buys_call() -> None:
    print("\n14. vol_ratio above threshold + non-negative trailing return -> buy call")
    # implied_vol = avg(0.20, 0.22) = 0.21; realized_vol=0.30 -> ratio ~1.4286 > 1.15
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("vol_ratio in metadata matches realized/implied", abs(decision.metadata["vol_ratio"] - (0.30 / 0.21)) < 1e-9)


def scenario_ratio_above_threshold_negative_return_buys_put() -> None:
    print("\n15. vol_ratio above threshold + negative trailing return -> buy put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = gs._decide_core(ctx, _signal(0.30, -0.02, status="ok"))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_ratio_below_threshold_while_flat() -> None:
    print("\n16. vol_ratio below threshold while flat -- no_trade")
    # realized_vol=0.21 == implied_vol -> ratio 1.0 < 1.15
    ctx = make_ctx(session_phase="open")
    decision = gs._decide_core(ctx, _signal(0.21, 0.02, status="ok"))
    check("no_trade -- ratio doesn't clear threshold", not decision.is_trade, decision.to_dict())
    check("reason cites the ratio/threshold", "threshold" in decision.reason.lower())


def scenario_ratio_below_threshold_while_positioned_closes() -> None:
    print("\n17. vol_ratio below threshold while holding a position -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = gs._decide_core(ctx, _signal(0.10, 0.02, status="ok"))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000")


def scenario_already_positioned_holds() -> None:
    print("\n18. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_direction_flip_closes_opposite() -> None:
    print("\n19. Trailing return flips sign while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n20. Above-threshold signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n21. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n22. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_custom_vol_ratio_threshold() -> None:
    print("\n23. Custom vol_ratio_threshold param is honored")
    ctx = make_ctx(
        session_phase="open",
        params={"vol_ratio_threshold": 2.0},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    # ratio ~1.4286 clears the default 1.15 but not a widened 2.0
    decision = gs._decide_core(ctx, _signal(0.30, 0.02, status="ok"))
    check("no_trade -- ratio doesn't clear the widened 2.0 threshold", not decision.is_trade, decision.to_dict())


def scenario_metadata_present_on_every_signal_branch() -> None:
    print("\n24. Required metadata keys are present across warming_up / missing-IV / below-threshold / buy branches")
    required = {"realized_vol", "implied_vol", "vol_ratio", "vol_ratio_threshold", "trailing_return_over_window"}

    ctx_warm = make_ctx(session_phase="open")
    d_warm = gs._decide_core(ctx_warm, _signal(None, None, status="warming_up", sample_count=1))
    check("warming_up branch carries all required keys", required.issubset(d_warm.metadata.keys()), d_warm.metadata)

    ctx_missing_iv = make_ctx(session_phase="open", rows=[CALL_ROW_NO_IV, PUT_ROW_NO_IV])
    d_missing = gs._decide_core(ctx_missing_iv, _signal(0.5, 0.01, status="ok"))
    check("missing-IV branch carries all required keys", required.issubset(d_missing.metadata.keys()), d_missing.metadata)

    ctx_below = make_ctx(session_phase="open")
    d_below = gs._decide_core(ctx_below, _signal(0.21, 0.02, status="ok"))
    check("below-threshold branch carries all required keys", required.issubset(d_below.metadata.keys()), d_below.metadata)

    ctx_buy = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    d_buy = gs._decide_core(ctx_buy, _signal(0.30, 0.02, status="ok"))
    check("buy branch carries all required keys", required.issubset(d_buy.metadata.keys()), d_buy.metadata)


# ---------------------------------------------------------------------------
# _decide_core()'s stale_source_reason branch
# ---------------------------------------------------------------------------


def scenario_stale_source_declines_while_flat() -> None:
    print("\n25. Stale/unavailable source snapshot declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = gs._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("no_trade on a stale source", not decision.is_trade)
    check("reason cites the stale source", "stale" in decision.reason.lower())


def scenario_stale_source_while_positioned_retains_not_sells() -> None:
    print("\n26. Stale source while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = gs._decide_core(ctx, None, stale_source_reason="snapshot is 12.0 minutes old (limit=5.0m)")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


# ---------------------------------------------------------------------------
# _decide() -- snapshot-timestamp recording, dedup, and staleness gate
# ---------------------------------------------------------------------------


def _reset_tracker() -> None:
    gs._tracker = PriceHistoryTracker(retain_minutes=1440.0)
    gs._last_recorded_snapshot = None


def scenario_decide_records_using_snapshot_timestamp_not_now_et() -> None:
    print("\n27. _decide(): records observed_at from snapshot.timestamp, not ctx.now_et")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")
    gs._decide(ctx)
    points = gs._tracker.snapshot()
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
    gs._decide(ctx1)
    gs._decide(ctx2)
    points = gs._tracker.snapshot()
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
    decision = gs._decide(ctx)
    check("no_trade on a stale source snapshot", not decision.is_trade, decision.to_dict())
    check("nothing recorded from the stale snapshot", len(gs._tracker.snapshot()) == 0, len(gs._tracker.snapshot()))
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
    decision = gs._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)


def scenario_decide_accepts_fresh_snapshot_within_age_limit() -> None:
    print("\n31. _decide(): a snapshot within the age limit is recorded normally")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now, snapshot_timestamp="2026-01-01T14:58:00+00:00")  # 2 minutes old
    gs._decide(ctx)
    check("one point recorded from a snapshot well within max_snapshot_age_minutes", len(gs._tracker.snapshot()) == 1)


def scenario_decide_end_to_end_builds_up_to_a_buy() -> None:
    print("\n32. _decide(): end-to-end -- enough recorded observations of a volatile series eventually supports a buy")
    _reset_tracker()
    base = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    prices = [400.0, 404.0, 399.0, 405.0, 398.0, 406.0]
    decision = None
    for i, price in enumerate(prices):
        ts = (base + timedelta(minutes=i * 5)).isoformat()
        now_et = base + timedelta(minutes=i * 5, seconds=5)
        ctx = make_ctx(
            session_phase="open", now_et=now_et, snapshot_timestamp=ts, underlying_price=price,
            params={"realized_vol_lookback_minutes": 30.0, "min_samples": 5},
            quote_map={
                "QQQ240101C00400000": fresh_quote("QQQ240101C00400000"),
                "QQQ240101P00400000": fresh_quote("QQQ240101P00400000"),
            },
        )
        decision = gs._decide(ctx)
    check(
        "after enough volatile observations, the strategy reaches a real signal_status (not stuck warming up)",
        decision is not None and decision.metadata is not None
        and (decision.metadata.get("realized_vol_status") != "warming_up"),
        decision.to_dict() if decision else None,
    )


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_compute_no_data,
        scenario_compute_warming_up_too_few_samples,
        scenario_compute_realized_vol_hand_built_series,
        scenario_compute_annualization_constant,
        scenario_compute_realized_vol_scales_with_deployed_cadence,
        scenario_compute_excludes_out_of_window_points,
        scenario_tracker_reused_from_momentum,
        scenario_market_closed,
        scenario_no_data_while_flat,
        scenario_warming_up_while_flat,
        scenario_warming_up_while_positioned_closes,
        scenario_missing_atm_iv_while_flat,
        scenario_missing_atm_iv_while_positioned_retains,
        scenario_ratio_above_threshold_nonneg_return_buys_call,
        scenario_ratio_above_threshold_negative_return_buys_put,
        scenario_ratio_below_threshold_while_flat,
        scenario_ratio_below_threshold_while_positioned_closes,
        scenario_already_positioned_holds,
        scenario_direction_flip_closes_opposite,
        scenario_stale_quote_declines,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_custom_vol_ratio_threshold,
        scenario_metadata_present_on_every_signal_branch,
        scenario_stale_source_declines_while_flat,
        scenario_stale_source_while_positioned_retains_not_sells,
        scenario_decide_records_using_snapshot_timestamp_not_now_et,
        scenario_decide_dedupes_identical_snapshot,
        scenario_decide_rejects_stale_snapshot_while_flat,
        scenario_decide_rejects_stale_snapshot_while_positioned,
        scenario_decide_accepts_fresh_snapshot_within_age_limit,
        scenario_decide_end_to_end_builds_up_to_a_buy,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
