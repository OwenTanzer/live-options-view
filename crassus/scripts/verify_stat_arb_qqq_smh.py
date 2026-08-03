#!/usr/bin/env python3
"""Prove the stat_arb_qqq_smh strategy's decision logic and ratio z-score math.

Hermetic like verify_momentum_qqq.py, which this mirrors scenario-for-
scenario where the shape matches (see stat_arb_qqq_smh.py's docstring for
why position management is deliberately identical to momentum_qqq/
trump_whisperer_qqq): no network access, no real tickers-board fetch.
`compute_ratio_signal()` is exercised with hand-built `PricePoint` lists;
the strategy's `_decide_core()` is exercised directly with a hand-built
`RatioSignal` (or a `fetch_error` string) -- it takes no tracker and no
`TickerBoardReader`, so it has no I/O of its own.

    python scripts/verify_stat_arb_qqq_smh.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import PriceHistoryTracker, PricePoint  # noqa: E402
from crassus.strategies import stat_arb_qqq_smh as sa  # noqa: E402
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


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = 400.0,
    now_et: datetime | None = None,
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW])
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


def _signal(
    z_score: float | None,
    *,
    status: str = "ok",
    sample_count: int = 10,
    log_ratio: float | None = 0.05,
    mean: float | None = 0.04,
    stdev: float | None = 0.01,
) -> sa.RatioSignal:
    return sa.RatioSignal(
        log_ratio=log_ratio,
        sample_count=sample_count,
        mean=mean,
        stdev=stdev,
        z_score=z_score,
        status=status,
    )


# ---------------------------------------------------------------------------
# compute_ratio_signal() -- pure z-score math
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("stat_arb_qqq_smh is registered", "stat_arb_qqq_smh" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["stat_arb_qqq_smh"], "strategy_id", None) == sa.STRATEGY_ID
        and getattr(REGISTRY["stat_arb_qqq_smh"], "strategy_version", None) == sa.STRATEGY_VERSION,
    )


def scenario_compute_no_data() -> None:
    print("\n2. compute_ratio_signal(): empty history")
    signal = sa.compute_ratio_signal([], min_samples=10)
    check("status is no_data", signal.status == "no_data", signal.status)
    check("z_score is None", signal.z_score is None)
    check("sample_count is zero", signal.sample_count == 0)


def scenario_compute_warming_up() -> None:
    print("\n3. compute_ratio_signal(): fewer than min_samples observations")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now - timedelta(minutes=2), 0.04),
        PricePoint(now - timedelta(minutes=1), 0.045),
        PricePoint(now, 0.05),
    ]
    signal = sa.compute_ratio_signal(history, min_samples=10)
    check("status is warming_up", signal.status == "warming_up", signal.status)
    check("z_score is None", signal.z_score is None)
    check("log_ratio is the newest point", signal.log_ratio == 0.05, signal.log_ratio)
    check("sample_count counts every point", signal.sample_count == 3)


def scenario_compute_ok() -> None:
    print("\n4. compute_ratio_signal(): min_samples reached -> real z-score")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    # Nine identical points at 0.04, plus a new one at 0.05 -- newest is the
    # only outlier, so mean/stdev are easy to hand-verify.
    history = [PricePoint(now - timedelta(minutes=9 - i), 0.04) for i in range(9)]
    history.append(PricePoint(now, 0.05))
    signal = sa.compute_ratio_signal(history, min_samples=10)
    check("status is ok", signal.status == "ok", signal.status)
    check("sample_count is 10", signal.sample_count == 10)
    values = [0.04] * 9 + [0.05]
    import statistics as _stats
    expected_mean = _stats.mean(values)
    expected_stdev = _stats.pstdev(values)
    expected_z = (0.05 - expected_mean) / expected_stdev
    check("mean matches hand computation", abs(signal.mean - expected_mean) < 1e-9, signal.mean)
    check("z_score matches hand computation", abs(signal.z_score - expected_z) < 1e-9, signal.z_score)


def scenario_compute_zero_stdev() -> None:
    print("\n5. compute_ratio_signal(): zero stdev reports z_score 0.0, not a crash")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [PricePoint(now - timedelta(minutes=9 - i), 0.04) for i in range(10)]
    signal = sa.compute_ratio_signal(history, min_samples=10)
    check("status is ok", signal.status == "ok", signal.status)
    check("stdev is zero", signal.stdev == 0.0, signal.stdev)
    check("z_score is 0.0, not None/NaN", signal.z_score == 0.0, signal.z_score)


def scenario_compute_unsorted_input() -> None:
    print("\n6. compute_ratio_signal(): history need not be pre-sorted")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    history = [
        PricePoint(now, 0.05),
        PricePoint(now - timedelta(minutes=1), 0.045),
        PricePoint(now - timedelta(minutes=2), 0.04),
    ]
    signal = sa.compute_ratio_signal(history, min_samples=2)
    check("log_ratio is still the chronologically newest point", signal.log_ratio == 0.05, signal.log_ratio)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n7. Market not open declines without touching the signal")
    ctx = make_ctx(session_phase="premarket")
    decision = sa._decide_core(ctx, None, None, qqq_price=None, smh_price=None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_no_data_while_flat() -> None:
    print("\n8. No ratio history recorded yet declines instead of crashing")
    ctx = make_ctx(session_phase="open")
    decision = sa._decide_core(ctx, sa.compute_ratio_signal([], min_samples=10), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade with no signal history", not decision.is_trade)
    check("reason cites missing history", "No QQQ/SMH ratio history" in decision.reason)


def scenario_warming_up_while_flat() -> None:
    print("\n9. Warming up (not enough samples yet) while flat -- stand down")
    ctx = make_ctx(session_phase="open")
    decision = sa._decide_core(ctx, _signal(None, status="warming_up", sample_count=3), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade while warming up", not decision.is_trade)
    check("reason cites the sample count", "3" in decision.reason)


def scenario_warming_up_while_positioned_closes() -> None:
    print("\n10. Warming up while holding a position -- close it, don't freeze")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = sa._decide_core(ctx, _signal(None, status="warming_up", sample_count=1), None, qqq_price=400.0, smh_price=250.0)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_neutral_band_no_trade() -> None:
    print("\n11. Normal range (|z| within band) trades nothing while flat")
    ctx = make_ctx(session_phase="open")
    decision = sa._decide_core(ctx, _signal(0.5), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade in the neutral band", not decision.is_trade, decision.to_dict())


def scenario_neutral_band_while_positioned_closes() -> None:
    print("\n12. z-score settles back into the neutral band while holding a put -- close it")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = sa._decide_core(ctx, _signal(0.2), None, qqq_price=400.0, smh_price=250.0)
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held put", decision.symbol == "QQQ240101P00400000")


def scenario_extreme_high_z_buys_put() -> None:
    print("\n13. Extreme-high z-score (QQQ ran up relative to SMH) -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_extreme_low_z_buys_call() -> None:
    print("\n14. Extreme-low z-score (QQQ fell relative to SMH) -> buy one call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = sa._decide_core(ctx, _signal(-2.0), None, qqq_price=400.0, smh_price=250.0)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_custom_entry_threshold() -> None:
    print("\n15. Custom entry_z_threshold param is honored")
    ctx = make_ctx(
        session_phase="open",
        params={"entry_z_threshold": 3.0},
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade -- z=2.0 doesn't clear the widened 3.0 threshold", not decision.is_trade, decision.to_dict())


def scenario_already_positioned_holds() -> None:
    print("\n16. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_signal_flip_closes_opposite() -> None:
    print("\n17. Signal flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale call position", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_stale_quote_declines() -> None:
    print("\n18. Supported signal but no quote returned declines rather than guessing")
    ctx = make_ctx(session_phase="open", quote_map={})
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade with no live quote", not decision.is_trade)
    check("reason cites no live quote", "No live quote" in decision.reason)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n19. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unexpected_short_stands_down() -> None:
    print("\n20. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = sa._decide_core(ctx, _signal(2.0), None, qqq_price=400.0, smh_price=250.0)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


# ---------------------------------------------------------------------------
# fetch_error handling -- mirrors trump_whisperer's stale-source scenarios
# ---------------------------------------------------------------------------


def scenario_fetch_error_declines_while_flat() -> None:
    print("\n21. Fetch failure (tickers board unreachable/malformed) declines while flat")
    ctx = make_ctx(session_phase="open")
    decision = sa._decide_core(ctx, None, "request failed: timeout", qqq_price=400.0, smh_price=None)
    check("no_trade on a fetch error", not decision.is_trade)
    check("reason cites the fetch error", "unavailable" in decision.reason.lower())


def scenario_fetch_error_while_positioned_retains() -> None:
    print("\n22. Fetch failure while holding a position retains it -- absence of a fresh read isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = sa._decide_core(ctx, None, "request failed: timeout", qqq_price=400.0, smh_price=None)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_missing_smh_price_is_fetch_error() -> None:
    print("\n23. _decide(): missing/null SMH price on the tickers board is treated as a fetch error")
    _reset_tracker()

    class FakeBoard:
        def price(self, symbol: str) -> float | None:
            return None

    class FakeReader:
        def read(self, force: bool = False) -> FakeBoard:
            return FakeBoard()

    sa._tickers_reader = FakeReader()
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = sa._decide(ctx)
    check("held position retained rather than closed on a missing SMH price", decision.action == "no_trade", decision.action)
    check("reason cites the missing/invalid SMH price", "smh price" in decision.reason.lower(), decision.reason)
    check("nothing recorded into the tracker from a failed read", len(sa._tracker.snapshot()) == 0, len(sa._tracker.snapshot()))


def scenario_fetch_exception_is_fetch_error() -> None:
    print("\n24. _decide(): an exception from the reader is treated as a fetch error, not a crash")
    _reset_tracker()

    class RaisingReader:
        def read(self, force: bool = False) -> None:
            raise RuntimeError("network is down")

    sa._tickers_reader = RaisingReader()
    ctx = make_ctx(session_phase="open")
    decision = sa._decide(ctx)
    check("no_trade rather than raising", not decision.is_trade)
    check("reason surfaces the underlying error", "network is down" in decision.reason, decision.reason)


# ---------------------------------------------------------------------------
# _decide() -- ratio computation and tracker recording (end-to-end, fake reader)
# ---------------------------------------------------------------------------


def _reset_tracker() -> None:
    sa._tracker = PriceHistoryTracker(retain_minutes=1440.0)


class _FakeBoardOK:
    def __init__(self, smh_price: float):
        self._smh_price = smh_price

    def price(self, symbol: str) -> float | None:
        return self._smh_price if symbol == sa.SMH_SYMBOL else None


class _FakeReaderOK:
    def __init__(self, smh_price: float):
        self._board = _FakeBoardOK(smh_price)

    def read(self, force: bool = False) -> _FakeBoardOK:
        return self._board


def scenario_decide_records_correct_log_ratio() -> None:
    print("\n25. _decide(): records log(qqq/smh) into the tracker, anchored to ctx.now_et")
    _reset_tracker()
    sa._tickers_reader = _FakeReaderOK(smh_price=250.0)
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", underlying_price=400.0, now_et=now)
    sa._decide(ctx)
    points = sa._tracker.snapshot()
    check("exactly one point recorded", len(points) == 1, len(points))
    expected = math.log(400.0 / 250.0)
    check("recorded value is log(qqq/smh)", abs(points[0].price - expected) < 1e-9, points[0].price)
    check("recorded observed_at is ctx.now_et", points[0].observed_at == now, points[0].observed_at)


def scenario_decide_end_to_end_extreme_ratio_buys() -> None:
    print("\n26. _decide(): end-to-end -- enough warmed-up history plus an extreme new ratio opens a position")
    _reset_tracker()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    # Prime 9 stable observations at a SMH price that keeps log_ratio ~ constant,
    # then let the 10th _decide() call (with a jumped QQQ price) be the outlier.
    for i in range(9):
        sa._tracker.observe(now - timedelta(minutes=9 - i), math.log(400.0 / 250.0))
    sa._tickers_reader = _FakeReaderOK(smh_price=250.0)
    ctx = make_ctx(
        session_phase="open", underlying_price=460.0, now_et=now,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = sa._decide(ctx)
    check("action is buy", decision.action == "buy", decision.to_dict())
    check("targets the ATM put (QQQ ran up relative to SMH)", decision.symbol == "QQQ240101P00400000", decision.symbol)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_compute_no_data,
        scenario_compute_warming_up,
        scenario_compute_ok,
        scenario_compute_zero_stdev,
        scenario_compute_unsorted_input,
        scenario_market_closed,
        scenario_no_data_while_flat,
        scenario_warming_up_while_flat,
        scenario_warming_up_while_positioned_closes,
        scenario_neutral_band_no_trade,
        scenario_neutral_band_while_positioned_closes,
        scenario_extreme_high_z_buys_put,
        scenario_extreme_low_z_buys_call,
        scenario_custom_entry_threshold,
        scenario_already_positioned_holds,
        scenario_signal_flip_closes_opposite,
        scenario_stale_quote_declines,
        scenario_multiple_open_positions_stand_down,
        scenario_unexpected_short_stands_down,
        scenario_fetch_error_declines_while_flat,
        scenario_fetch_error_while_positioned_retains,
        scenario_missing_smh_price_is_fetch_error,
        scenario_fetch_exception_is_fetch_error,
        scenario_decide_records_correct_log_ratio,
        scenario_decide_end_to_end_extreme_ratio_buys,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
