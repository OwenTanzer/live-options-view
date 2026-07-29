"""Reddit-sentiment-driven QQQ options strategy.

Sources its signal from `crassus.sentiment.RedditSentimentReader`, which
scrapes a fixed subreddit set's public JSON listings and scores each match
with VADER -- the same ingestion-and-scoring shape as
[nama1arpit/reddit-streaming-pipeline](https://github.com/nama1arpit/reddit-streaming-pipeline),
reimplemented as an in-process poller instead of that project's Kafka/Spark/
Cassandra stack (see `sentiment.py` for why).

Position management is deliberately as simple as `smoke.smoke_atm_roundtrip`:
at most one contract at a time, opened in the direction of the aggregate
mood and closed the moment that mood stops supporting it -- including when
sentiment merely goes neutral, not only when it flips to the other side.
Sizing beyond one contract, hysteresis, and cool-downs belong in
`ctx.params` on top of this contract or in a later strategy, not baked into
this one -- per the README, strategy-level rules are configuration, not
platform invariants.

The strategy's notion of "what am I holding" comes from `ctx.book.positions`
directly, never from re-deriving "the current ATM strike" and assuming that's
what was bought. The underlying can drift after entry -- a 400 call bought
last cycle is still the position to manage even once 402 becomes the new ATM
strike -- so re-deriving ATM every cycle and comparing against *that* symbol
would both open a duplicate position (the old 400 call still open, quantity
against 402 reads flat) and fail to find the original position to close on a
sentiment flip.

`_decide_core` takes an already-fetched snapshot (or an error string) and
contains all the actual decision logic; it has no network dependency and is
what `scripts/verify_reddit_sentiment.py` exercises. `_decide` is the thin
I/O wrapper the registry calls.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..sentiment import RedditSentimentReader, SentimentSnapshot
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "reddit_sentiment_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_MIN_SAMPLE_SIZE = 5
DEFAULT_BULLISH_THRESHOLD = 0.15
DEFAULT_BEARISH_THRESHOLD = -0.15

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. The type letter is
# parsed from the symbol itself rather than looked up in the current market
# snapshot, because a held position can roll off the front of the chain (or
# simply not be the ATM row anymore) while still needing to be recognized and
# closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

_reader = RedditSentimentReader(symbol="QQQ")


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


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

    open_positions = _open_positions(ctx)
    if len(open_positions) > 1:
        return no(
            "Holding more than one open option position; standing down "
            "rather than guessing which one this strategy owns.",
            open_positions={s: p.quantity for s, p in open_positions.items()},
        )

    held_symbol: str | None = None
    held_quantity = 0
    held_type: str | None = None
    if open_positions:
        held_symbol, held_position = next(iter(open_positions.items()))
        held_quantity = held_position.quantity
        if held_quantity < 0:
            # Not reachable from this strategy's own actions, but the book
            # is rebuilt from server history that may include anything.
            return no(
                f"Unexpected short position in {held_symbol}; standing down "
                f"rather than compounding it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        held_type = _option_type_from_symbol(held_symbol)
        if held_type is None:
            return no(
                f"Held symbol {held_symbol} has an unrecognized option "
                f"type; standing down rather than guessing whether to "
                f"close it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )

    if fetch_error is not None or snapshot is None:
        reason = fetch_error or "no snapshot and no error reported"
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type,
            no, f"Reddit sentiment unavailable: {reason}", {},
        )

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
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {snapshot.sample_size} matching Reddit item(s) for "
            f"{snapshot.symbol}; need {min_sample} for a signal.",
            meta_base,
        )

    mean = snapshot.mean_compound
    if mean is None or bearish_threshold < mean < bullish_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Reddit sentiment on {snapshot.symbol} is neutral "
            f"(mean_compound={mean}).",
            meta_base,
        )

    supported_type = "call" if mean >= bullish_threshold else "put"
    mood = "bullish" if supported_type == "call" else "bearish"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; sentiment "
                f"still supports it (mean_compound={mean:.3f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        # Sentiment flipped direction while holding the other side. Close it
        # now; a fresh entry in the new direction waits for the next cycle,
        # same one-action-per-cycle discipline as the smoke strategy's round
        # trip. Uses the *actual* held symbol, not a freshly re-derived ATM
        # symbol for the opposite type, so this still finds the position to
        # close even if the underlying has drifted since entry.
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Sentiment on {snapshot.symbol} now favors {supported_type}s "
                f"(mean_compound={mean:.3f}); closing stale {held_type} "
                f"position before considering a new one."
            ),
            meta=meta_base,
            no=no,
        )

    row = ctx.snapshot.atm(supported_type)
    if not row:
        return no(f"No quoted {supported_type} in the snapshot to trade.", **meta_base)
    symbol = row["OptionSymbol"]

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

    return Decision(
        action="buy",
        symbol=symbol,
        quantity=1,
        reason=(
            f"Reddit sentiment on {snapshot.symbol} is {mood} "
            f"(mean_compound={mean:.3f}, n={snapshot.sample_size}); "
            f"opening one {supported_type}."
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


def _maybe_close_unsupported(
    ctx: StrategyContext,
    held_symbol: str | None,
    held_quantity: int,
    held_type: str | None,
    no: Any,
    reason: str,
    meta: dict[str, Any],
) -> Decision:
    """Shared tail for every "sentiment doesn't currently support a position"
    branch (unavailable, insufficient sample, neutral): close whatever is
    held, or just decline if flat. Keeps the close-on-loss-of-support rule
    from this docstring's promise in one place instead of duplicated across
    each caller."""
    if held_symbol is None:
        return no(reason, **meta)
    return _close(
        ctx, held_symbol, held_quantity,
        reason=f"{reason} No longer supports the held {held_type} position; closing it.",
        meta=meta,
        no=no,
    )


def _close(
    ctx: StrategyContext,
    symbol: str,
    quantity: int,
    *,
    reason: str,
    meta: dict[str, Any],
    no: Any,
) -> Decision:
    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}; cannot close it this cycle.", symbol=symbol, **meta)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s); "
            f"cannot close it this cycle.",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
            **meta,
        )
    return Decision(
        action="sell",
        symbol=symbol,
        quantity=quantity,
        reason=reason,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={**meta, "closing_symbol": symbol},
    )


def _decide(ctx: StrategyContext) -> Decision:
    if ctx.session_phase != "open":
        # Decline before spending a Reddit API call on a decision that's
        # going to be a no_trade regardless of what sentiment says.
        return _decide_core(ctx, None, None)

    try:
        snapshot = _reader.read()
    except Exception as exc:  # RedditFetchError, rate limiting, network failure, etc.
        return _decide_core(ctx, None, str(exc))
    return _decide_core(ctx, snapshot, None)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
reddit_sentiment_qqq = register(_decide)
