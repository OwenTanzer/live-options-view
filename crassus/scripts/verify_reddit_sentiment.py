#!/usr/bin/env python3
"""Prove the reddit_sentiment_qqq strategy's decision logic and aggregation math.

Hermetic like verify_invariants.py: no network access, no Reddit credentials,
no real PRAW or vaderSentiment call. `sentiment.aggregate()` is exercised with
a fake analyzer that returns fixed scores per input string, and the strategy's
`_decide_core()` is exercised directly with a hand-built `SentimentSnapshot`
-- it takes no reader and makes no I/O, so this is the same style of pure
scenario check as scenario_strategy_contract() in verify_invariants.py.

    python scripts/verify_reddit_sentiment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.sentiment import SentimentSnapshot, aggregate  # noqa: E402
from crassus.strategies import reddit_sentiment as rs  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [✓] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [✗] {name}" + (f" -- {detail}" if detail else ""))


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


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
) -> StrategyContext:
    snapshot = make_snapshot("QQQ", 400.0, [CALL_ROW, PUT_ROW])
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


# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("reddit_sentiment_qqq is registered", "reddit_sentiment_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["reddit_sentiment_qqq"], "strategy_id", None) == rs.STRATEGY_ID
        and getattr(REGISTRY["reddit_sentiment_qqq"], "strategy_version", None) == rs.STRATEGY_VERSION,
    )


def scenario_aggregate_math() -> None:
    print("\n2. aggregate(): pure sentiment math")
    texts = ["to the moon", "flat chop", "puts printing", "bagholders unite"]
    analyzer = FakeAnalyzer({
        "to the moon": 0.8,
        "flat chop": 0.0,
        "puts printing": -0.6,
        "bagholders unite": -0.9,
    })
    snap = aggregate(texts, analyzer, symbol="QQQ", subreddits=("wallstreetbets",))
    check("sample_size counts every matched item", snap.sample_size == 4, snap.sample_size)
    check(
        "mean_compound is the plain average",
        abs(snap.mean_compound - (0.8 + 0.0 - 0.6 - 0.9) / 4) < 1e-9,
        snap.mean_compound,
    )
    check("bullish_count uses the >=0.05 cutoff", snap.bullish_count == 1, snap.bullish_count)
    check("bearish_count uses the <=-0.05 cutoff", snap.bearish_count == 2, snap.bearish_count)
    check("neutral_count is the remainder", snap.neutral_count == 1, snap.neutral_count)
    check(
        "bullish_share/bearish_share are fractions of sample_size",
        abs(snap.bullish_share - 0.25) < 1e-9 and abs(snap.bearish_share - 0.5) < 1e-9,
    )


def scenario_aggregate_empty() -> None:
    print("\n3. aggregate(): no matches")
    snap = aggregate([], FakeAnalyzer({}), symbol="QQQ", subreddits=("stocks",))
    check("sample_size is zero", snap.sample_size == 0)
    check("mean_compound is None, not 0.0 -- 'no signal' must not look like 'neutral'", snap.mean_compound is None)
    check("bullish_share/bearish_share are None with no sample", snap.bullish_share is None and snap.bearish_share is None)


def _snap(mean: float | None, n: int = 10) -> SentimentSnapshot:
    return SentimentSnapshot(
        fetched_at="2024-01-01T15:00:00+00:00",
        symbol="QQQ",
        subreddits=("wallstreetbets",),
        sample_size=n,
        mean_compound=mean,
        bullish_count=n if (mean or 0) > 0 else 0,
        bearish_count=n if (mean or 0) < 0 else 0,
        neutral_count=0,
    )


def scenario_market_closed() -> None:
    print("\n4. Market not open declines without touching the snapshot/error")
    ctx = make_ctx(session_phase="premarket")
    decision = rs._decide_core(ctx, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)


def scenario_fetch_error() -> None:
    print("\n5. Reddit fetch failure declines instead of crashing the account")
    ctx = make_ctx(session_phase="open")
    decision = rs._decide_core(ctx, None, "REDDIT_CLIENT_ID not set")
    check("no_trade on fetch error", not decision.is_trade)
    check("reason surfaces the underlying error", "REDDIT_CLIENT_ID" in decision.reason)


def scenario_insufficient_sample() -> None:
    print("\n6. Sample too small to trust")
    ctx = make_ctx(session_phase="open")
    decision = rs._decide_core(ctx, _snap(0.9, n=2), None)
    check("no_trade below min_sample_size", not decision.is_trade)
    check("metadata carries the sample size for the audit record", decision.metadata["sample_size"] == 2)


def scenario_neutral() -> None:
    print("\n7. Neutral sentiment trades nothing")
    ctx = make_ctx(session_phase="open")
    decision = rs._decide_core(ctx, _snap(0.0), None)
    check("no_trade in the neutral band", not decision.is_trade)


def scenario_bullish_opens_call() -> None:
    print("\n8. Bullish + flat + executable quote -> buy one call")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = rs._decide_core(ctx, _snap(0.5), None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("quantity is exactly one contract", decision.quantity == 1)


def scenario_bearish_opens_put() -> None:
    print("\n9. Bearish + flat + executable quote -> buy one put")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = rs._decide_core(ctx, _snap(-0.5), None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the ATM put", decision.symbol == "QQQ240101P00400000", decision.symbol)


def scenario_stale_quote_declines() -> None:
    print("\n10. Bullish signal but stale quote declines rather than risking a 409")
    ctx = make_ctx(session_phase="open", quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = rs._decide_core(ctx, _snap(0.5), None)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_already_positioned_holds() -> None:
    print("\n11. Already holding the supported side -- no pyramiding")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = rs._decide_core(ctx, _snap(0.5), None)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_sentiment_flip_closes_opposite() -> None:
    print("\n12. Sentiment flips direction while holding the other side -- close first")
    trades = [{"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = rs._decide_core(ctx, _snap(0.5), None)  # now bullish, but holding a put
    check("action is sell", decision.action == "sell", decision.action)
    check("closes the stale put position", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("closes the full held quantity", decision.quantity == 1)


def scenario_unexpected_short_stands_down() -> None:
    print("\n13. Unexpected short position -- stand down, don't compound it")
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades)
    decision = rs._decide_core(ctx, _snap(0.5), None)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_aggregate_math,
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
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
