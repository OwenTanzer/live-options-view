#!/usr/bin/env python3
"""Prove the vix_term_structure_qqq strategy's decision logic and caching.

Hermetic like verify_trump_whisperer.py, which this mirrors on purpose (see
vix_term_structure.py's docstring): no network access, no real yfinance call.
`VixTermStructureReader` is exercised with a fake `fetch_fn` counting its own
calls; the strategy's `_decide_core()` is exercised directly with hand-built
`VixTermStructureSnapshot` objects -- it takes no reader and makes no I/O.

    python scripts/verify_vix_term_structure.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import vix_term_structure as vts  # noqa: E402
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


def make_snapshot(symbol: str, underlying_price: float, rows: list[dict]) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
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
) -> StrategyContext:
    snapshot = make_snapshot("QQQ", underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW])
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={},
        book=book,
        now_et=None,
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


def _snap(vix9d: float | None, vix3m: float | None) -> vts.VixTermStructureSnapshot:
    return vts.VixTermStructureSnapshot(fetched_at=0.0, vix9d=vix9d, vix3m=vix3m)


# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("vix_term_structure_qqq is registered", "vix_term_structure_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["vix_term_structure_qqq"], "strategy_id", None) == vts.STRATEGY_ID
        and getattr(REGISTRY["vix_term_structure_qqq"], "strategy_version", None) == vts.STRATEGY_VERSION,
    )


def scenario_market_closed() -> None:
    print("\n2. Market not open declines without touching the snapshot/error")
    ctx = make_ctx(session_phase="premarket")
    decision = vts._decide_core(ctx, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_ratio_computed_correctly() -> None:
    print("\n3. Ratio math: VIX9D/VIX3M drives the regime read")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = vts._decide_core(ctx, _snap(15.0, 20.0), None)
    check("ratio computed as vix9d/vix3m", abs(decision.metadata["ratio"] - 0.75) < 1e-9, decision.metadata["ratio"])
    check("regime read as contango", decision.metadata["regime"] == "contango", decision.metadata["regime"])


def scenario_clear_contango_buys_call() -> None:
    print("\n4. Clear contango (ratio well below threshold) + flat -> buy one call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = vts._decide_core(ctx, _snap(12.0, 20.0), None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_clear_backwardation_buys_put() -> None:
    print("\n5. Clear backwardation (ratio well above threshold) + flat -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = vts._decide_core(ctx, _snap(26.0, 20.0), None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_ambiguous_band_no_trade() -> None:
    print("\n6. Ambiguous band (ratio near 1.0) while flat -> no_trade")
    ctx = make_ctx(session_phase="open")
    decision = vts._decide_core(ctx, _snap(20.0, 20.0), None)
    check("no_trade in the ambiguous band", not decision.is_trade)
    check("regime reported as ambiguous", decision.metadata["regime"] == "ambiguous", decision.metadata)


def scenario_fetch_error_no_trade() -> None:
    print("\n7. Fetch failure declines instead of crashing the account")
    ctx = make_ctx(session_phase="open")
    decision = vts._decide_core(ctx, None, "VixFetchError: HTTP 500")
    check("no_trade on fetch error", not decision.is_trade)
    check("reason surfaces the underlying error", "VixFetchError" in decision.reason)


def scenario_missing_ticker_price_no_trade() -> None:
    print("\n8. Fetched-but-missing price for a ticker -> no_trade, treated as ambiguous")
    ctx = make_ctx(session_phase="open")
    decision = vts._decide_core(ctx, _snap(None, 20.0), None)
    check("no_trade when a price is missing", not decision.is_trade)
    check("ratio is None", decision.metadata["ratio"] is None, decision.metadata)


def scenario_fetch_error_while_positioned_retains() -> None:
    print("\n9. Fetch failure while holding a position retains it -- absence of evidence isn't evidence against")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vts._decide_core(ctx, None, "VixFetchError: HTTP 503")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason cites the missing observation", "unavailable" in decision.reason.lower(), decision.reason)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def scenario_missing_ticker_while_positioned_closes() -> None:
    print("\n10. Missing ticker price (a genuine, successful ambiguous read) while holding -- closes")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vts._decide_core(ctx, _snap(None, 20.0), None)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_regime_flip_closes_held_position() -> None:
    print("\n11. Regime flips to unsupported (ambiguous) while holding -- close it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vts._decide_core(ctx, _snap(20.0, 20.0), None)
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("reason cites no longer supporting", "no longer supports" in decision.reason.lower(), decision.reason)


def scenario_regime_flip_opposite_direction_closes_first() -> None:
    print("\n12. Backwardation while holding a call -- close the stale call before considering a put")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vts._decide_core(ctx, _snap(26.0, 20.0), None)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale call position", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_already_positioned_holds() -> None:
    print("\n13. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = vts._decide_core(ctx, _snap(12.0, 20.0), None)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_stale_quote_declines() -> None:
    print("\n14. Contango signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = vts._decide_core(ctx, _snap(12.0, 20.0), None)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n15. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = vts._decide_core(ctx, _snap(12.0, 20.0), None)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n16. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = vts._decide_core(ctx, _snap(12.0, 20.0), None)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n17. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = vts._decide_core(ctx, _snap(12.0, 20.0), None)
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


# ---------------------------------------------------------------------------
# VixTermStructureReader -- caching behavior, with a fake fetch_fn (no
# yfinance import, no network access)
# ---------------------------------------------------------------------------


class FakeFetcher:
    """Stands in for the module's `_default_fetch_fn`, counting calls."""

    def __init__(self, values: list[tuple[float | None, float | None]]):
        self.values = values
        self.calls = 0

    def __call__(self) -> tuple[float | None, float | None]:
        self.calls += 1
        idx = min(self.calls - 1, len(self.values) - 1)
        return self.values[idx]


def scenario_cache_respected() -> None:
    print("\n18. Reader caches: a second read within min_interval_s doesn't re-fetch")
    fetcher = FakeFetcher([(12.0, 20.0), (26.0, 20.0)])
    reader = vts.VixTermStructureReader(fetch_fn=fetcher, min_interval_s=60.0)
    first = reader.read()
    second = reader.read()
    check("fetch_fn called exactly once for two reads within min_interval_s", fetcher.calls == 1, fetcher.calls)
    check("second read returns the same cached snapshot", second.vix9d == first.vix9d and second.vix3m == first.vix3m)


def scenario_cache_expires() -> None:
    print("\n19. Reader re-fetches once the cache is stale (via force=True, no sleep needed)")
    fetcher = FakeFetcher([(12.0, 20.0), (26.0, 20.0)])
    reader = vts.VixTermStructureReader(fetch_fn=fetcher, min_interval_s=60.0)
    reader.read()
    forced = reader.read(force=True)
    check("fetch_fn called again when forced", fetcher.calls == 2, fetcher.calls)
    check("forced read reflects the new values", forced.vix9d == 26.0, forced.vix9d)


def scenario_reader_wraps_exceptions() -> None:
    print("\n20. Reader wraps a raising fetch_fn as VixFetchError")
    def boom() -> tuple[float | None, float | None]:
        raise ValueError("network is down")

    reader = vts.VixTermStructureReader(fetch_fn=boom, min_interval_s=60.0)
    try:
        reader.read()
        check("read() raised VixFetchError", False)
    except vts.VixFetchError as exc:
        check("read() raised VixFetchError", True, str(exc))
        check("underlying error message is preserved", "network is down" in str(exc))


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_market_closed,
        scenario_ratio_computed_correctly,
        scenario_clear_contango_buys_call,
        scenario_clear_backwardation_buys_put,
        scenario_ambiguous_band_no_trade,
        scenario_fetch_error_no_trade,
        scenario_missing_ticker_price_no_trade,
        scenario_fetch_error_while_positioned_retains,
        scenario_missing_ticker_while_positioned_closes,
        scenario_regime_flip_closes_held_position,
        scenario_regime_flip_opposite_direction_closes_first,
        scenario_already_positioned_holds,
        scenario_stale_quote_declines,
        scenario_unexpected_short_stands_down,
        scenario_multiple_open_positions_stand_down,
        scenario_unrecognized_symbol_stands_down,
        scenario_cache_respected,
        scenario_cache_expires,
        scenario_reader_wraps_exceptions,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
