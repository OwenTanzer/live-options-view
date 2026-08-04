#!/usr/bin/env python3
"""Prove the insider_form4_qqq strategy's decision logic and EDGAR-count math.

Hermetic like verify_reddit_sentiment.py: no network access, no real
data.sec.gov request. `insider_flow.count_recent_form4()` is exercised with
hand-built EDGAR submissions payloads (the parallel-array `filings.recent`
shape EDGAR actually returns), and the strategy's `_decide_core()` is
exercised directly with a hand-built `InsiderActivitySnapshot` -- it takes no
reader and makes no I/O, same style as `scenario_strategy_contract()` in
verify_invariants.py.

    python scripts/verify_insider_form4.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.insider_flow import (  # noqa: E402
    CompanyFilingCount,
    InsiderActivitySnapshot,
    InsiderFlowReader,
    count_recent_form4,
)
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import insider_form4 as ifm  # noqa: E402
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


NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def make_snapshot(underlying_price: float, rows: list[dict]) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2026-08-03T15:00:00+00:00",
            "snapshot_time": "2026-08-03T15:00:00+00:00",
            "expiration": "2026-08-03",
            "underlying_price": underlying_price,
            "rows": rows,
        },
        raw=b"{}",
    )


CALL_ROW = {"OptionSymbol": "QQQ260803C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ260803P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = 400.0,
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW])
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={},
        book=book,
        now_et=NOW,
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2026-08-03T15:00:00", server_ts="2026-08-03T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2026-08-03T15:00:00", server_ts="2026-08-03T15:05:00")


def make_activity(total_by_ticker: dict[str, int], *, lookback_hours: float = 48.0) -> InsiderActivitySnapshot:
    basket = tuple(
        CompanyFilingCount(ticker=t, cik=f"000000000{i}", recent_form4_count=n)
        for i, (t, n) in enumerate(total_by_ticker.items())
    )
    return InsiderActivitySnapshot(fetched_at="2026-08-03T15:00:00+00:00", lookback_hours=lookback_hours, basket=basket)


# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("insider_form4_qqq is registered", "insider_form4_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["insider_form4_qqq"], "strategy_id", None) == ifm.STRATEGY_ID
        and getattr(REGISTRY["insider_form4_qqq"], "strategy_version", None) == ifm.STRATEGY_VERSION,
    )


def scenario_count_recent_form4_basic() -> None:
    print("\n2. count_recent_form4(): filters to Form 4 within the lookback window")
    payload = {
        "filings": {
            "recent": {
                "form": ["4", "4", "8-K", "4", "10-Q"],
                "filingDate": [
                    "2026-08-02",  # within 48h of NOW (2026-08-03 15:00 UTC)
                    "2026-07-20",  # a Form 4 but far outside the window
                    "2026-08-02",  # 8-K, not a Form 4 -- excluded regardless of date
                    "2026-08-03",  # within window
                    "2026-08-03",  # 10-Q, excluded
                ],
            }
        }
    }
    n = count_recent_form4(payload, now=NOW, lookback_hours=48.0)
    check("counts only Form 4s within the lookback window", n == 2, n)


def scenario_count_recent_form4_empty() -> None:
    print("\n3. count_recent_form4(): missing/empty filings section")
    check("empty payload counts zero", count_recent_form4({}, now=NOW, lookback_hours=48.0) == 0)
    check(
        "missing 'recent' key counts zero",
        count_recent_form4({"filings": {}}, now=NOW, lookback_hours=48.0) == 0,
    )


def scenario_count_recent_form4_unparseable_date() -> None:
    print("\n4. count_recent_form4(): unparseable date is skipped, not crashed on")
    payload = {"filings": {"recent": {"form": ["4"], "filingDate": ["not-a-date"]}}}
    n = count_recent_form4(payload, now=NOW, lookback_hours=48.0)
    check("unparseable filing date does not count", n == 0, n)


def scenario_basket_aggregation() -> None:
    print("\n5. InsiderActivitySnapshot.total_recent_form4_count sums across the basket")
    snap = make_activity({"AAPL": 2, "MSFT": 1, "NVDA": 0, "AMZN": 3, "GOOGL": 1})
    check("total sums every company in the basket", snap.total_recent_form4_count == 7, snap.total_recent_form4_count)


def scenario_default_basket_shape() -> None:
    print("\n6. Default basket has plausible 4-6 tickers with 10-digit CIKs")
    reader = InsiderFlowReader()
    check("basket has between 4 and 6 tickers", 4 <= len(reader.basket) <= 6, len(reader.basket))
    check(
        "every CIK is a 10-digit zero-padded string",
        all(len(cik) == 10 and cik.isdigit() for _, cik in reader.basket),
        reader.basket,
    )


def scenario_market_closed() -> None:
    print("\n7. Market not open declines without touching activity/fetch error")
    ctx = make_ctx(session_phase="premarket")
    decision = ifm._decide_core(ctx, None, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_fetch_failure_retains_no_position() -> None:
    print("\n8. Fetch failure while flat -- just declines")
    ctx = make_ctx(session_phase="open")
    decision = ifm._decide_core(ctx, None, "CIK 0000320193: HTTP 403", 400.0)
    check("no_trade on fetch error", not decision.is_trade)
    check("reason surfaces the underlying error", "HTTP 403" in decision.reason)


def scenario_fetch_failure_retains_held_position() -> None:
    print("\n9. Fetch failure while holding -- retains the held position, does not close")
    trades = [{"sym": "QQQ260803C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = ifm._decide_core(ctx, None, "network error", 400.0)
    check("no_trade (not a sell) on fetch error while holding", not decision.is_trade, decision.action)
    check("reason says retaining the held position", "Retaining" in decision.reason)
    check("metadata still carries the held symbol", decision.metadata["symbol"] == "QQQ260803C00400000")


def scenario_activity_below_threshold() -> None:
    print("\n10. Activity below threshold -- no_trade even with a clear intraday move")
    activity = make_activity({"AAPL": 1, "MSFT": 1, "NVDA": 0, "AMZN": 0, "GOOGL": 1})  # total 3, threshold 3 (not >)
    ctx = make_ctx(session_phase="open", underlying_price=405.0)
    decision = ifm._decide_core(ctx, activity, None, 400.0)  # +1.25% move, but activity not elevated
    check("no_trade when activity does not clear the threshold", not decision.is_trade)
    check("metadata carries total_recent_form4_count", decision.metadata["total_recent_form4_count"] == 3)
    check("metadata carries activity_threshold", decision.metadata["activity_threshold"] == 3)


def scenario_elevated_but_session_flat() -> None:
    print("\n11. Elevated activity but session flat -- no_trade")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})  # total 7 > 3
    ctx = make_ctx(session_phase="open", underlying_price=400.1)  # +0.025%, under the 0.1% floor
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("no_trade when the session hasn't moved enough to read a direction", not decision.is_trade)
    check("metadata carries intraday_move_pct", abs(decision.metadata["intraday_move_pct"] - 0.00025) < 1e-6)


def scenario_elevated_and_trending_up_opens_call() -> None:
    print("\n12. Elevated activity + trending up -- buy call")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})  # total 7 > 3
    ctx = make_ctx(
        session_phase="open", underlying_price=402.0,  # +0.5%, well past the 0.1% floor
        quote_map={"QQQ260803C00400000": fresh_quote("QQQ260803C00400000")},
    )
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ260803C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)
    check("metadata carries total_recent_form4_count", decision.metadata["total_recent_form4_count"] == 7)
    check("metadata carries session_start_price", decision.metadata["session_start_price"] == 400.0)
    check("metadata carries current_price", decision.metadata["current_price"] == 402.0)


def scenario_elevated_and_trending_down_opens_put() -> None:
    print("\n13. Elevated activity + trending down -- buy put")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})  # total 7 > 3
    ctx = make_ctx(
        session_phase="open", underlying_price=398.0,  # -0.5%
        quote_map={"QQQ260803P00400000": fresh_quote("QQQ260803P00400000")},
    )
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ260803P00400000", decision.symbol)


def scenario_already_positioned_holds() -> None:
    print("\n14. Already holding the confirmed side -- no pyramiding")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})
    trades = [{"sym": "QQQ260803C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades, underlying_price=402.0)
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_signal_disagrees_closes_held() -> None:
    print("\n15. Elevated + confirmed opposite direction while holding -- close the held position")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})
    trades = [{"sym": "QQQ260803P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades, underlying_price=402.0,  # now confirms a call
        quote_map={"QQQ260803P00400000": fresh_quote("QQQ260803P00400000")},
    )
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held put", decision.symbol == "QQQ260803P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_stale_quote_declines() -> None:
    print("\n16. Confirmed direction but stale quote declines rather than risking a 409")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})
    ctx = make_ctx(
        session_phase="open", underlying_price=402.0,
        quote_map={"QQQ260803C00400000": stale_quote("QQQ260803C00400000")},
    )
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n17. Unexpected short position -- stand down, don't compound it")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})
    trades = [{"sym": "QQQ260803C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades, underlying_price=402.0)
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n18. More than one open position -- stand down rather than guess")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})
    trades = [
        {"sym": "QQQ260803C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ260803P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades, underlying_price=402.0)
    decision = ifm._decide_core(ctx, activity, None, 400.0)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_no_session_start_price_yet() -> None:
    print("\n19. No session start price resolved yet -- retains rather than guessing a direction")
    activity = make_activity({"AAPL": 3, "MSFT": 2, "NVDA": 2, "AMZN": 0, "GOOGL": 0})
    ctx = make_ctx(session_phase="open", underlying_price=402.0)
    decision = ifm._decide_core(ctx, activity, None, None)
    check("no_trade when session_start_price is unavailable", not decision.is_trade)
    check("metadata's intraday_move_pct is None", decision.metadata["intraday_move_pct"] is None)


def scenario_cache_respected() -> None:
    print("\n20. InsiderFlowReader cache is respected within min_interval_s")
    calls = {"n": 0}

    def fake_session_factory():
        class FakeResponse:
            status_code = 200

            def json(self_inner):
                calls["n"] += 1
                return {
                    "filings": {
                        "recent": {
                            "form": ["4"],
                            "filingDate": ["2026-08-02"],
                        }
                    }
                }

        class FakeSession:
            def get(self_inner, url, timeout=None):
                return FakeResponse()

        return FakeSession()

    reader = InsiderFlowReader(
        basket=(("AAPL", "0000320193"),),
        lookback_hours=48.0,
        min_interval_s=600.0,
        session_factory=fake_session_factory,
    )
    first = reader.read()
    second = reader.read()
    check("first read fetches once per basket entry", calls["n"] == 1, calls["n"])
    check("second read within min_interval_s reuses the cache", calls["n"] == 1, calls["n"])
    check("cached snapshot is returned unchanged", first is second)
    forced = reader.read(force=True)
    check("force=True bypasses the cache", calls["n"] == 2, calls["n"])
    check("forced read produces a fresh snapshot object", forced is not first)


def scenario_session_open_tracker() -> None:
    print("\n21. Session open tracker records the opening print once per date, not overwritten")
    tracker = ifm._SessionOpenTracker()
    first = tracker.observe("2026-08-03", 400.0)
    second = tracker.observe("2026-08-03", 405.0)  # later same-day observation
    third = tracker.observe("2026-08-04", 410.0)  # a new date gets its own opening print
    check("first observation sets the opening print", first == 400.0, first)
    check("later same-day observation does not overwrite it", second == 400.0, second)
    check("a new date starts its own opening print", third == 410.0, third)


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_count_recent_form4_basic,
        scenario_count_recent_form4_empty,
        scenario_count_recent_form4_unparseable_date,
        scenario_basket_aggregation,
        scenario_default_basket_shape,
        scenario_market_closed,
        scenario_fetch_failure_retains_no_position,
        scenario_fetch_failure_retains_held_position,
        scenario_activity_below_threshold,
        scenario_elevated_but_session_flat,
        scenario_elevated_and_trending_up_opens_call,
        scenario_elevated_and_trending_down_opens_put,
        scenario_already_positioned_holds,
        scenario_signal_disagrees_closes_held,
        scenario_stale_quote_declines,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_no_session_start_price_yet,
        scenario_cache_respected,
        scenario_session_open_tracker,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
