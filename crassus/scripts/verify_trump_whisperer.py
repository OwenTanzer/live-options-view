#!/usr/bin/env python3
"""Prove the trump_whisperer_qqq strategy's decision logic and aggregation math.

Hermetic like verify_reddit_sentiment.py, which this mirrors scenario-for-
scenario on purpose (see trump_whisperer.py's docstring for why the two
strategies share a decision shape): no network access, no real feed fetch
or vaderSentiment call. `trump_sentiment.aggregate()` is exercised with a
fake analyzer and hand-built posts; the strategy's `_decide_core()` is
exercised directly with a hand-built `TrumpSentimentSnapshot` -- it takes no
reader and makes no I/O.

    python scripts/verify_trump_whisperer.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.trump_sentiment import TrumpSentimentSnapshot, aggregate  # noqa: E402
from crassus.strategies import trump_whisperer as tw  # noqa: E402
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


class FakeAnalyzer:
    """Stands in for vaderSentiment's SentimentIntensityAnalyzer."""

    def __init__(self, scores: dict[str, float]):
        self.scores = scores

    def polarity_scores(self, text: str) -> dict[str, float]:
        return {"compound": self.scores.get(text, 0.0)}


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


def _snap(mean: float | None, n: int = 5) -> TrumpSentimentSnapshot:
    return TrumpSentimentSnapshot(
        fetched_at="2024-01-01T15:00:00+00:00",
        sample_size=n,
        mean_compound=mean,
        bullish_count=n if (mean or 0) > 0 else 0,
        bearish_count=n if (mean or 0) < 0 else 0,
        neutral_count=0,
        latest_post_at="2024-01-01T14:55:00+00:00",
    )


# ---------------------------------------------------------------------------
# aggregate() -- Truth Social post scoring and freshness filtering
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("trump_whisperer_qqq is registered", "trump_whisperer_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["trump_whisperer_qqq"], "strategy_id", None) == tw.STRATEGY_ID
        and getattr(REGISTRY["trump_whisperer_qqq"], "strategy_version", None) == tw.STRATEGY_VERSION,
    )


def scenario_aggregate_math() -> None:
    print("\n2. aggregate(): pure sentiment math over fresh posts")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    posts = [
        {"text": "the market is booming", "link": "a", "published_at": now - timedelta(minutes=5)},
        {"text": "total disaster unfolding", "link": "b", "published_at": now - timedelta(minutes=10)},
        {"text": "flat, nothing to see", "link": "c", "published_at": now - timedelta(minutes=15)},
    ]
    analyzer = FakeAnalyzer({
        "the market is booming": 0.8,
        "total disaster unfolding": -0.7,
        "flat, nothing to see": 0.0,
    })
    snap = aggregate(posts, analyzer, now=now, max_post_age_minutes=180.0)
    check("sample_size counts every fresh post", snap.sample_size == 3, snap.sample_size)
    check(
        "mean_compound is the plain average",
        abs(snap.mean_compound - (0.8 - 0.7 + 0.0) / 3) < 1e-9,
        snap.mean_compound,
    )
    check("bullish_count uses the >=0.05 cutoff", snap.bullish_count == 1, snap.bullish_count)
    check("bearish_count uses the <=-0.05 cutoff", snap.bearish_count == 1, snap.bearish_count)
    check("neutral_count is the remainder", snap.neutral_count == 1, snap.neutral_count)
    check(
        "latest_post_at is the most recent fresh post's timestamp",
        snap.latest_post_at == "2026-01-01T14:55:00+00:00",
        snap.latest_post_at,
    )


def scenario_aggregate_filters_stale_posts() -> None:
    print("\n3. aggregate(): posts older than max_post_age_minutes are excluded")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    posts = [
        {"text": "fresh take", "link": "a", "published_at": now - timedelta(minutes=30)},
        {"text": "ancient archive post", "link": "b", "published_at": now - timedelta(days=400)},
    ]
    analyzer = FakeAnalyzer({"fresh take": 0.5, "ancient archive post": -0.9})
    snap = aggregate(posts, analyzer, now=now, max_post_age_minutes=60.0)
    check("only the fresh post is scored", snap.sample_size == 1, snap.sample_size)
    check("mean_compound reflects only the fresh post", snap.mean_compound == 0.5, snap.mean_compound)


def scenario_aggregate_skips_undated_posts() -> None:
    print("\n4. aggregate(): a post with no parseable published_at is skipped, not errored")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    posts = [
        {"text": "dated post", "link": "a", "published_at": now - timedelta(minutes=1)},
        {"text": "undated post", "link": "b", "published_at": None},
    ]
    analyzer = FakeAnalyzer({"dated post": 0.5, "undated post": 0.9})
    snap = aggregate(posts, analyzer, now=now, max_post_age_minutes=60.0)
    check("only the dated post is scored", snap.sample_size == 1, snap.sample_size)


def scenario_aggregate_empty() -> None:
    print("\n5. aggregate(): no fresh posts")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    snap = aggregate([], FakeAnalyzer({}), now=now, max_post_age_minutes=180.0)
    check("sample_size is zero", snap.sample_size == 0)
    check("mean_compound is None, not 0.0 -- 'no signal' must not look like 'neutral'", snap.mean_compound is None)
    check("bullish_share/bearish_share are None with no sample", snap.bullish_share is None and snap.bearish_share is None)
    check("latest_post_at is None with no fresh posts", snap.latest_post_at is None)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic (mirrors reddit_sentiment's scenarios)
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n6. Market not open declines without touching the snapshot/error")
    ctx = make_ctx(session_phase="premarket")
    decision = tw._decide_core(ctx, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_fetch_error() -> None:
    print("\n7. Feed fetch failure declines instead of crashing the account")
    ctx = make_ctx(session_phase="open")
    decision = tw._decide_core(ctx, None, "TrumpFetchError: HTTP 500")
    check("no_trade on fetch error", not decision.is_trade)
    check("reason surfaces the underlying error", "TrumpFetchError" in decision.reason)


def scenario_insufficient_sample() -> None:
    print("\n8. Sample too small to trust")
    ctx = make_ctx(session_phase="open")
    decision = tw._decide_core(ctx, _snap(0.9, n=1), None)
    check("no_trade below min_sample_size", not decision.is_trade)
    check("metadata carries the sample size for the audit record", decision.metadata["sample_size"] == 1)


def scenario_neutral() -> None:
    print("\n9. Neutral sentiment while flat trades nothing")
    ctx = make_ctx(session_phase="open")
    decision = tw._decide_core(ctx, _snap(0.0), None)
    check("no_trade in the neutral band", not decision.is_trade)


def scenario_bullish_opens_call() -> None:
    print("\n10. Bullish + flat + executable quote -> buy one call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_bearish_opens_put() -> None:
    print("\n11. Bearish + flat + executable quote -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = tw._decide_core(ctx, _snap(-0.5), None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n12. Bullish signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("\n13. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_sentiment_flip_closes_opposite() -> None:
    print("\n14. Sentiment flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")},
    )
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_unexpected_short_stands_down() -> None:
    print("\n15. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_neutral_while_positioned_closes() -> None:
    print("\n16. Sentiment goes neutral while holding a call -- close it, don't just decline")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = tw._decide_core(ctx, _snap(0.0), None)
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_insufficient_sample_while_positioned_closes() -> None:
    print("\n17. Sample too small to trust while holding -- close rather than freeze holding it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = tw._decide_core(ctx, _snap(0.9, n=1), None)
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the actual held call", decision.symbol == "QQQ240101C00400000")


def scenario_atm_drift_no_pyramiding() -> None:
    print("\n18. ATM drifts to a new strike while holding the old one -- no duplicate open")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        rows=[CALL_ROW_DRIFTED, PUT_ROW_DRIFTED], underlying_price=402.0,
        quote_map={"QQQ240101C00402000": fresh_quote("QQQ240101C00402000")},
    )
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check(
        "no_trade rather than opening a second call at the new ATM strike",
        not decision.is_trade,
        decision.to_dict(),
    )
    check("reason references the originally held symbol, not the new ATM one",
          "QQQ240101C00400000" in decision.reason, decision.reason)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n19. More than one open position -- stand down rather than guess")
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


def scenario_unrecognized_symbol_stands_down() -> None:
    print("\n20. Held symbol doesn't parse as an OCC option -- stand down rather than guess")
    trades = [{"sym": "NOT-AN-OPTION-SYMBOL", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = tw._decide_core(ctx, _snap(0.5), None)
    check("no_trade on an unparseable held symbol", not decision.is_trade)
    check("reason names the unrecognized symbol", "unrecognized" in decision.reason.lower())


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_aggregate_math,
        scenario_aggregate_filters_stale_posts,
        scenario_aggregate_skips_undated_posts,
        scenario_aggregate_empty,
        scenario_market_closed,
        scenario_fetch_error,
        scenario_insufficient_sample,
        scenario_neutral,
        scenario_bullish_opens_call,
        scenario_bearish_opens_put,
        scenario_stale_quote_declines,
        scenario_already_positioned_holds,
        scenario_sentiment_flip_closes_opposite,
        scenario_unexpected_short_stands_down,
        scenario_neutral_while_positioned_closes,
        scenario_insufficient_sample_while_positioned_closes,
        scenario_atm_drift_no_pyramiding,
        scenario_multiple_open_positions_stand_down,
        scenario_unrecognized_symbol_stands_down,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
