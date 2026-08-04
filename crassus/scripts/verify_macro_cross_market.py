#!/usr/bin/env python3
"""Prove the macro_cross_market_qqq strategy's composite scoring and decision logic.

Hermetic like verify_momentum_qqq.py/verify_trump_whisperer.py, which this
mirrors scenario-for-scenario on purpose (see macro_cross_market.py's module
docstring for why the position-management shape is identical and why
fetch-error handling instead mirrors trump_whisperer.py): no real network
call ever happens. `_decide_core()` is exercised directly with hand-built
`MomentumSignal` pairs (it takes no reader, no tracker, and makes no I/O);
`_decide()` is exercised with a fake in-process `TickerBoardReader` stand-in
(`FakeReader` below) so its fetch-error / staleness / dedup / recording
plumbing is proven without ever hitting the real R2 bucket.

    python scripts/verify_macro_cross_market.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import MomentumSignal, PriceHistoryTracker  # noqa: E402
from crassus.strategies import macro_cross_market as mcm  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402
from crassus.tickers import TickerBoard, _parse_board  # noqa: E402

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


def _sig(return_pct: float | None, *, status: str = "ok", sample_count: int = 10, anchor_age_minutes: float = 15.0) -> MomentumSignal:
    current_price = 100.0
    anchor_price = current_price / (1.0 + return_pct) if return_pct is not None else None
    return MomentumSignal(
        lookback_minutes=15.0,
        current_price=current_price,
        anchor_price=anchor_price,
        return_pct=return_pct,
        sample_count=sample_count,
        anchor_age_minutes=anchor_age_minutes,
        status=status,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def scenario_from_payload_preserves_stale_and_source() -> None:
    print("\n0. tickers._parse_board(): a carried-forward entry's stale/source flags survive parsing")
    payload = {
        "timestamp": "2026-01-01T15:00:00+00:00",
        "snapshot_time": "2026-01-01T15:00:00+00:00",
        "feed_stale": False,
        "prices": {
            "10Y": {"price": 44.85, "source": "dxlink"},
            "USO": {"price": 70.0, "stale": True, "source": "last-known"},
        },
    }
    snapshot = _parse_board(payload, "2026-01-01T15:00:00+00:00")
    check("fresh entry's price parses", snapshot.price("10Y") == 44.85, snapshot.price("10Y"))
    check("fresh entry is not stale", snapshot.is_stale("10Y") is False)
    check("fresh entry's source is preserved", snapshot.source.get("10Y") == "dxlink", snapshot.source.get("10Y"))
    check("carried-forward entry's price still parses", snapshot.price("USO") == 70.0, snapshot.price("USO"))
    check("carried-forward entry is flagged stale", snapshot.is_stale("USO") is True)
    check("carried-forward entry's source is preserved", snapshot.source.get("USO") == "last-known", snapshot.source.get("USO"))


def scenario_registered() -> None:
    print("\n1. Registration")
    check("macro_cross_market_qqq is registered", "macro_cross_market_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["macro_cross_market_qqq"], "strategy_id", None) == mcm.STRATEGY_ID
        and getattr(REGISTRY["macro_cross_market_qqq"], "strategy_version", None) == mcm.STRATEGY_VERSION,
    )


# ---------------------------------------------------------------------------
# Composite score math (hand-built trailing returns, no tracker/network)
# ---------------------------------------------------------------------------


def scenario_composite_score_equal_weight() -> None:
    print("\n2. Composite score: equal-weight combination of two trailing returns")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    # yield +0.5%, crude +0.3% (both a headwind) -> score = -(0.005 + 0.003) = -0.008
    decision = mcm._decide_core(ctx, _sig(0.005), _sig(0.003))
    expected = -(0.005 + 0.003)
    check(
        "composite_score matches -(w_yield*yield_ret + w_crude*crude_ret)",
        abs(decision.metadata["composite_score"] - expected) < 1e-9,
        decision.metadata["composite_score"],
    )
    check("both headwinds -> bearish -> buy put", decision.action == "buy" and decision.symbol == "QQQ240101P00400000")


def scenario_composite_score_custom_weights() -> None:
    print("\n3. Composite score: custom w_yield/w_crude are honored")
    ctx = make_ctx(
        session_phase="open",
        params={"w_yield": 2.0, "w_crude": 0.5},
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    # yield -0.01 (tailwind, weight 2.0), crude +0.002 (headwind, weight 0.5)
    # score = -(2.0*-0.01 + 0.5*0.002) = -(-0.02 + 0.001) = 0.019
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(0.002))
    expected = -((2.0 * -0.01) + (0.5 * 0.002))
    check(
        "composite_score reflects custom weights",
        abs(decision.metadata["composite_score"] - expected) < 1e-9,
        decision.metadata["composite_score"],
    )
    check("net tailwind -> bullish -> buy call", decision.action == "buy" and decision.symbol == "QQQ240101C00400000")


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n4. Market not open declines without touching the signals")
    ctx = make_ctx(session_phase="premarket")
    decision = mcm._decide_core(ctx, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_both_warmed_up_bullish_buys_call() -> None:
    print("\n5. Both trackers warmed up, bullish composite score -> buy call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    # yield -0.01, crude -0.01 -> score = -(-0.01 + -0.01) = 0.02 (bullish)
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_both_warmed_up_bearish_buys_put() -> None:
    print("\n6. Both trackers warmed up, bearish composite score -> buy put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    # yield +0.01, crude +0.01 -> score = -(0.01+0.01) = -0.02 (bearish)
    decision = mcm._decide_core(ctx, _sig(0.01), _sig(0.01))
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_within_threshold_no_trade() -> None:
    print("\n7. Composite score inside the neutral band -> no_trade")
    ctx = make_ctx(session_phase="open")
    # yield +0.0001, crude -0.00005 -> tiny net score, within default 0.001 threshold
    decision = mcm._decide_core(ctx, _sig(0.0001), _sig(-0.00005))
    check("no_trade within the neutral band", not decision.is_trade, decision.to_dict())
    check("reason cites the neutral band", "neutral" in decision.reason.lower())


def scenario_within_threshold_while_positioned_closes() -> None:
    print("\n8. Composite score settles neutral while holding a call -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mcm._decide_core(ctx, _sig(0.0), _sig(0.0))
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_one_tracker_warming_up_no_trade() -> None:
    print("\n9. One tracker still warming up -- withhold the whole composite, no_trade while flat")
    ctx = make_ctx(session_phase="open")
    decision = mcm._decide_core(ctx, _sig(None, status="warming_up", sample_count=2), _sig(0.01))
    check("no_trade while one tracker warms up", not decision.is_trade)
    check("reason cites warming up", "warming up" in decision.reason.lower())
    check("composite_score withheld (None) since not both trackers are ok", decision.metadata["composite_score"] is None)


def scenario_one_tracker_warming_up_while_positioned_closes() -> None:
    print("\n10. One tracker still warming up while holding a position -- close it, don't freeze holding it")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mcm._decide_core(ctx, _sig(0.01), _sig(None, status="warming_up", sample_count=1))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held put", decision.symbol == "QQQ240101P00400000")


def scenario_one_tracker_stale_anchor_no_trade() -> None:
    print("\n11. One tracker has a stale anchor -- withhold the composite")
    ctx = make_ctx(session_phase="open")
    decision = mcm._decide_core(ctx, _sig(0.01), _sig(None, status="stale_anchor", anchor_age_minutes=200.0))
    check("no_trade on a stale anchor", not decision.is_trade)
    check("reason cites the gap/stale anchor", "stale" in decision.reason.lower())


def scenario_no_data_no_trade() -> None:
    print("\n12. One tracker has no history yet -- withhold the composite")
    ctx = make_ctx(session_phase="open")
    decision = mcm._decide_core(ctx, _sig(None, status="no_data", sample_count=0), _sig(0.01))
    check("no_trade with no history for one driver", not decision.is_trade)
    check("reason cites missing history", "history" in decision.reason.lower())


def scenario_fetch_error_while_flat() -> None:
    print("\n13. Fetch failure declines instead of crashing the account, while flat")
    ctx = make_ctx(session_phase="open")
    decision = mcm._decide_core(ctx, None, None, fetch_error="HTTPError: 500")
    check("no_trade on fetch error", not decision.is_trade)
    check("reason surfaces the underlying error", "HTTPError" in decision.reason)


def scenario_fetch_error_while_positioned_retains() -> None:
    print("\n14. Fetch failure while holding a position retains it -- absence of evidence isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        # A live, executable quote is available -- if the bug were still
        # present, everything needed to actually close is present, so an
        # executable quote can't be why this doesn't sell; only the
        # fetch_error-vs-completed-neutral-read distinction can be.
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mcm._decide_core(ctx, None, None, fetch_error="missing price(s) for 10Y")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_already_holding_supported_side_holds() -> None:
    print("\n15. Already holding the composite-supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_composite_flip_closes_opposite() -> None:
    print("\n16. Composite flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n17. Bullish composite but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n18. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n19. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n20. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = mcm._decide_core(ctx, _sig(-0.01), _sig(-0.01))
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


# ---------------------------------------------------------------------------
# _decide() -- fake reader, timestamp recording/dedup, freshness gate
# ---------------------------------------------------------------------------


class FakeReader:
    """Stands in for `TickerBoardReader`: returns pre-built boards or raises."""

    def __init__(self, snapshot: TickerBoard | None = None, exc: Exception | None = None):
        self.snapshot = snapshot
        self.exc = exc
        self.calls = 0

    def read(self, force: bool = False) -> TickerBoard:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.snapshot


def _reset_trackers() -> None:
    mcm._yield_tracker = PriceHistoryTracker(retain_minutes=1440.0)
    mcm._crude_tracker = PriceHistoryTracker(retain_minutes=1440.0)
    mcm._last_recorded_timestamp = None
    mcm._last_seen_source = {mcm.YIELD_TICKER: None, mcm.CRUDE_TICKER: None}


def _make_tickers_snapshot(
    *, yield_price: float | None = 4.5, crude_price: float | None = 70.0,
    timestamp: str = "2026-01-01T14:58:00+00:00", feed_stale: bool = False,
    yield_stale: bool = False, crude_stale: bool = False,
    yield_source: str | None = "dxlink", crude_source: str | None = "dxlink",
) -> TickerBoard:
    prices = {}
    stale = {}
    source = {}
    if yield_price is not None:
        prices["10Y"] = yield_price
        stale["10Y"] = yield_stale
        source["10Y"] = yield_source
    if crude_price is not None:
        prices["USO"] = crude_price
        stale["USO"] = crude_stale
        source["USO"] = crude_source
    return TickerBoard(
        fetched_at=timestamp,
        timestamp=timestamp,
        snapshot_time=timestamp,
        feed_stale=feed_stale,
        prices=prices,
        stale=stale,
        source=source,
    )


def scenario_decide_records_both_series() -> None:
    print("\n21. _decide(): records both yield and crude observations from the fake board")
    _reset_trackers()
    mcm._reader = FakeReader(_make_tickers_snapshot())
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now)
    mcm._decide(ctx)
    check("one yield point recorded", len(mcm._yield_tracker.snapshot()) == 1)
    check("one crude point recorded", len(mcm._crude_tracker.snapshot()) == 1)
    check(
        "recorded observed_at matches the board's own timestamp, not ctx.now_et",
        mcm._yield_tracker.snapshot()[0].observed_at == datetime(2026, 1, 1, 14, 58, tzinfo=timezone.utc),
    )


def scenario_decide_dedupes_identical_timestamp() -> None:
    print("\n22. _decide(): repeated reads of the same unrepublished board are not double-recorded")
    _reset_trackers()
    mcm._reader = FakeReader(_make_tickers_snapshot(timestamp="2026-01-01T15:00:00+00:00"))
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx1 = make_ctx(session_phase="open", now_et=now)
    ctx2 = make_ctx(session_phase="open", now_et=now + timedelta(minutes=1))
    mcm._decide(ctx1)
    mcm._decide(ctx2)
    check("only one point recorded per series despite two decide() calls", len(mcm._yield_tracker.snapshot()) == 1)


def scenario_decide_raise_is_fetch_error_retains() -> None:
    print("\n23. _decide(): a raised exception is treated as a fetch error, retaining a held position")
    _reset_trackers()
    mcm._reader = FakeReader(exc=RuntimeError("connection reset"))
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = mcm._decide(ctx)
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason surfaces the underlying error", "connection reset" in decision.reason, decision.reason)
    check("nothing recorded from a failed fetch", len(mcm._yield_tracker.snapshot()) == 0)


def scenario_decide_missing_ticker_is_fetch_error() -> None:
    print("\n24. _decide(): a missing ticker (e.g. USO absent from the board) is a fetch error, not a neutral read")
    _reset_trackers()
    mcm._reader = FakeReader(_make_tickers_snapshot(crude_price=None))
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now)
    decision = mcm._decide(ctx)
    check("no_trade on a missing ticker", not decision.is_trade)
    check("reason cites the missing ticker", "USO" in decision.reason, decision.reason)
    check("nothing recorded when a required ticker is missing", len(mcm._yield_tracker.snapshot()) == 0)


def scenario_decide_feed_stale_flag_is_fetch_error() -> None:
    print("\n25. _decide(): a feed_stale=true payload is a fetch error, not a neutral read")
    _reset_trackers()
    mcm._reader = FakeReader(_make_tickers_snapshot(feed_stale=True))
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now)
    decision = mcm._decide(ctx)
    check("no_trade on a feed_stale board", not decision.is_trade)
    check("reason cites the stale feed", "stale" in decision.reason.lower(), decision.reason)


def scenario_decide_stalled_timestamp_is_fetch_error() -> None:
    print("\n26. _decide(): a board timestamp far older than the runner's clock is rejected as stalled")
    _reset_trackers()
    mcm._reader = FakeReader(_make_tickers_snapshot(timestamp="2026-01-01T09:00:00+00:00"))  # 6 hours old
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now)
    decision = mcm._decide(ctx)
    check("no_trade on a stalled board", not decision.is_trade)
    check("nothing recorded from a stalled board", len(mcm._yield_tracker.snapshot()) == 0)
    check("reason cites the stall", "stall" in decision.reason.lower() or "old" in decision.reason.lower(), decision.reason)


def scenario_decide_stale_yield_price_is_fetch_error() -> None:
    print("\n26b. _decide(): a carried-forward (stale) 10Y price is a fetch error, not a live observation")
    _reset_trackers()
    mcm._reader = FakeReader(_make_tickers_snapshot(yield_stale=True, yield_source="last-known"))
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    ctx = make_ctx(session_phase="open", now_et=now)
    decision = mcm._decide(ctx)
    check("no_trade on a stale yield price", not decision.is_trade)
    check("reason cites the stale ticker", "10Y" in decision.reason and "stale" in decision.reason.lower(), decision.reason)
    check("nothing recorded from a stale reading", len(mcm._yield_tracker.snapshot()) == 0)


def scenario_decide_yield_source_change_resets_tracker() -> None:
    print("\n26c. _decide(): a 10Y provider failover (DXLink -> yfinance) resets the tracker instead of computing a return across the scale change")
    _reset_trackers()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)

    # Two DXLink-sourced observations at yield~44.85 (the $TNX.X convention:
    # value = rate x10).
    mcm._reader = FakeReader(_make_tickers_snapshot(
        yield_price=44.80, timestamp="2026-01-01T14:50:00+00:00", yield_source="dxlink",
    ))
    mcm._decide(make_ctx(session_phase="open", now_et=now - timedelta(minutes=5)))
    mcm._reader = FakeReader(_make_tickers_snapshot(
        yield_price=44.85, timestamp="2026-01-01T14:55:00+00:00", yield_source="dxlink",
    ))
    mcm._decide(make_ctx(session_phase="open", now_et=now))
    check("two same-source points recorded before failover", len(mcm._yield_tracker.snapshot()) == 2)

    # Failover to yfinance's ^TNX convention: the rate directly (~4.485),
    # not x10. Without a reset, this reads as a ~-90% one-step move.
    mcm._reader = FakeReader(_make_tickers_snapshot(
        yield_price=4.485, timestamp="2026-01-01T15:00:00+00:00", yield_source="yfinance",
    ))
    mcm._decide(make_ctx(session_phase="open", now_et=now + timedelta(minutes=5)))
    check(
        "the tracker was reset on the source change, not fed a cross-scale return",
        len(mcm._yield_tracker.snapshot()) == 1,
        len(mcm._yield_tracker.snapshot()),
    )
    check(
        "the sole remaining point is the new (yfinance-scale) observation",
        mcm._yield_tracker.snapshot()[0].price == 4.485,
        mcm._yield_tracker.snapshot()[0].price,
    )
    # Crude's source didn't change, so its tracker is untouched by the
    # yield-only failover.
    check("crude tracker is unaffected by a yield-only source change", len(mcm._crude_tracker.snapshot()) == 3)


def main() -> int:
    for scenario in (
        scenario_from_payload_preserves_stale_and_source,
        scenario_registered,
        scenario_composite_score_equal_weight,
        scenario_composite_score_custom_weights,
        scenario_market_closed,
        scenario_both_warmed_up_bullish_buys_call,
        scenario_both_warmed_up_bearish_buys_put,
        scenario_within_threshold_no_trade,
        scenario_within_threshold_while_positioned_closes,
        scenario_one_tracker_warming_up_no_trade,
        scenario_one_tracker_warming_up_while_positioned_closes,
        scenario_one_tracker_stale_anchor_no_trade,
        scenario_no_data_no_trade,
        scenario_fetch_error_while_flat,
        scenario_fetch_error_while_positioned_retains,
        scenario_already_holding_supported_side_holds,
        scenario_composite_flip_closes_opposite,
        scenario_stale_quote_declines,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_decide_records_both_series,
        scenario_decide_dedupes_identical_timestamp,
        scenario_decide_raise_is_fetch_error_retains,
        scenario_decide_missing_ticker_is_fetch_error,
        scenario_decide_feed_stale_flag_is_fetch_error,
        scenario_decide_stalled_timestamp_is_fetch_error,
        scenario_decide_stale_yield_price_is_fetch_error,
        scenario_decide_yield_source_change_resets_tracker,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
