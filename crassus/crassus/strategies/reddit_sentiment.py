"""Reddit-sentiment-driven QQQ options strategy.

Sources its signal from `crassus.sentiment.RedditSentimentReader`, which
polls a fixed subreddit set and scores each match with VADER -- the same
ingestion-and-scoring shape as
[nama1arpit/reddit-streaming-pipeline](https://github.com/nama1arpit/reddit-streaming-pipeline),
reimplemented as an in-process poller instead of that project's Kafka/Spark/
Cassandra stack (see `sentiment.py` for why).

Position management is deliberately as simple as `smoke.smoke_atm_roundtrip`:
at most one contract at a time, opened in the direction of the aggregate
mood and closed the moment that mood stops supporting it. Sizing beyond one
contract, hysteresis, and cool-downs belong in `ctx.params` on top of this
contract or in a later strategy, not baked into this one -- per the README,
strategy-level rules are configuration, not platform invariants.

`_decide_core` takes an already-fetched snapshot (or an error string) and
contains all the actual decision logic; it has no network dependency and is
what `scripts/verify_reddit_sentiment.py` exercises. `_decide` is the thin
I/O wrapper the registry calls.
"""

from __future__ import annotations

from typing import Any

from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..sentiment import RedditSentimentReader, SentimentSnapshot
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "reddit_sentiment_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_MIN_SAMPLE_SIZE = 5
DEFAULT_BULLISH_THRESHOLD = 0.15
DEFAULT_BEARISH_THRESHOLD = -0.15

_reader = RedditSentimentReader(symbol="QQQ")


def _decide_core(
    ctx: StrategyContext,
    snapshot: SentimentSnapshot | None,
    fetch_error: str | None,
) -> Decision:
    def no(reason: str, **meta: Any) -> Decision:
        return Decision.no_trade(
            reason=reason,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata=meta or None,
        )

    if ctx.session_phase != "open":
        # Same reasoning as the smoke strategy: outside regular hours the
        # collector's quotes age out and the Worker rejects anything older
        # than ~15s, so there is nothing useful an open position check could
        # do here anyway.
        return no(
            f"Market is {ctx.session_phase}; execution quotes will be stale.",
            session_phase=ctx.session_phase,
        )

    if fetch_error is not None:
        return no(f"Reddit sentiment unavailable: {fetch_error}")
    if snapshot is None:
        return no("Reddit sentiment unavailable: no snapshot and no error reported.")

    params = ctx.params or {}
    min_sample = params.get("min_sample_size", DEFAULT_MIN_SAMPLE_SIZE)
    bullish_threshold = params.get("bullish_threshold", DEFAULT_BULLISH_THRESHOLD)
    bearish_threshold = params.get("bearish_threshold", DEFAULT_BEARISH_THRESHOLD)

    meta_base = dict(
        sample_size=snapshot.sample_size,
        mean_compound=snapshot.mean_compound,
        bullish_share=snapshot.bullish_share,
        bearish_share=snapshot.bearish_share,
        subreddits=list(snapshot.subreddits),
    )

    if snapshot.sample_size < min_sample:
        return no(
            f"Only {snapshot.sample_size} matching Reddit item(s) for "
            f"{snapshot.symbol}; need {min_sample} for a signal.",
            **meta_base,
        )

    mean = snapshot.mean_compound
    if mean is None or bearish_threshold < mean < bullish_threshold:
        return no(
            f"Reddit sentiment on {snapshot.symbol} is neutral "
            f"(mean_compound={mean}).",
            **meta_base,
        )

    option_type = "call" if mean >= bullish_threshold else "put"
    opposite_type = "put" if option_type == "call" else "call"

    row = ctx.snapshot.atm(option_type)
    if not row:
        return no(f"No quoted {option_type} in the snapshot to trade.", **meta_base)
    symbol = row["OptionSymbol"]
    position = ctx.book.position(symbol)

    opposite_row = ctx.snapshot.atm(opposite_type)
    opposite_symbol = opposite_row["OptionSymbol"] if opposite_row else None
    opposite_position = ctx.book.position(opposite_symbol) if opposite_symbol else None

    if opposite_position is not None and opposite_position.quantity > 0:
        # Sentiment flipped direction while we were holding the other side.
        # Close it now; a fresh entry in the new direction waits for the
        # next cycle, same one-action-per-cycle discipline as the smoke
        # strategy's round trip.
        return Decision(
            action="sell",
            symbol=opposite_symbol,
            quantity=opposite_position.quantity,
            reason=(
                f"Sentiment on {snapshot.symbol} now favors {option_type}s "
                f"(mean_compound={mean:.3f}); closing stale {opposite_type} "
                f"position before considering a new one."
            ),
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata={**meta_base, "closing_symbol": opposite_symbol},
        )

    if position.quantity > 0:
        return no(
            f"Already holding {position.quantity} {symbol}; sentiment "
            f"still supports it (mean_compound={mean:.3f}).",
            symbol=symbol,
            held_quantity=position.quantity,
            **meta_base,
        )

    if position.quantity < 0:
        # Not reachable from this strategy's own actions, but the book is
        # rebuilt from server history that may include anything.
        return no(
            f"Unexpected short position in {symbol}; standing down rather "
            f"than compounding it.",
            symbol=symbol,
            held_quantity=position.quantity,
            **meta_base,
        )

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}.", symbol=symbol, **meta_base)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
            **meta_base,
        )

    mood = "bullish" if option_type == "call" else "bearish"
    return Decision(
        action="buy",
        symbol=symbol,
        quantity=1,
        reason=(
            f"Reddit sentiment on {snapshot.symbol} is {mood} "
            f"(mean_compound={mean:.3f}, n={snapshot.sample_size}); "
            f"opening one {option_type}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            **meta_base,
            "strike": row["Strike"],
            "underlying_price": ctx.snapshot.underlying_price,
            "bid": quote.bid,
            "ask": quote.ask,
            "quote_age_seconds": quote.age_seconds,
        },
    )


def _decide(ctx: StrategyContext) -> Decision:
    if ctx.session_phase != "open":
        # Decline before spending a Reddit API call on a decision that's
        # going to be a no_trade regardless of what sentiment says.
        return _decide_core(ctx, None, None)

    try:
        snapshot = _reader.read()
    except Exception as exc:  # missing credentials, PRAW/network failure, etc.
        return _decide_core(ctx, None, str(exc))
    return _decide_core(ctx, snapshot, None)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
reddit_sentiment_qqq = register(_decide)
