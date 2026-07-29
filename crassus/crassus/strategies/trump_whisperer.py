"""Trump-Truth-Social-sentiment-driven QQQ options strategy.

Sources its signal from `crassus.trump_sentiment.TrumpSentimentReader`,
which polls `https://trumpstruth.org/feed` -- an unauthenticated RSS mirror
of Trump's actual Truth Social posts, no app registration or credentials
needed -- and scores each fresh post with VADER. See
`crassus/trump_sentiment.py`'s module docstring for the prior art
considered (`trump2cash`, `TrumpTruthsMarketAnalysis`) and why neither is
ported as-is.

Position management is deliberately identical in shape to
`reddit_sentiment.reddit_sentiment_qqq`: at most one contract at a time,
opened in the direction of the aggregate mood and closed the moment that
mood stops supporting it -- including when sentiment merely goes neutral,
not only when it flips to the other side. That strategy's own docstring
already makes the case for this shape (simple as `smoke.smoke_atm_roundtrip`,
sizing/hysteresis/cool-downs belong in `ctx.params` or a later strategy, not
platform invariants) and it applies here without modification -- this is
the same decision shape pointed at a different sentiment source, not a
different strategy design. `_decide_core` is deliberately structured to
mirror `reddit_sentiment._decide_core` step-for-step (including the
book-based holding detection and ATM-drift handling described there) so the
two stay easy to compare; the duplication is intentional isolation, not an
oversight -- see `crassus/trump_sentiment.py`'s docstring for why the
ingestion/scoring layer underneath is likewise a separate, independently
testable module rather than a shared one.

`_decide_core` takes an already-fetched snapshot (or an error string) and
contains all the actual decision logic; it has no network dependency and is
what `scripts/verify_trump_whisperer.py` exercises. `_decide` is the thin
I/O wrapper the registry calls.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..trump_sentiment import TrumpSentimentReader, TrumpSentimentSnapshot
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "trump_whisperer_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_MIN_SAMPLE_SIZE = 2
DEFAULT_BULLISH_THRESHOLD = 0.2
DEFAULT_BEARISH_THRESHOLD = -0.2

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as reddit_sentiment._OCC_TYPE_RE: a held position can roll off
# the front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

_reader = TrumpSentimentReader()


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(
    ctx: StrategyContext,
    snapshot: TrumpSentimentSnapshot | None,
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
            no, f"Trump sentiment unavailable: {reason}", {},
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
        latest_post_at=snapshot.latest_post_at,
        duplicate_count=snapshot.duplicate_count,
    )

    if snapshot.sample_size < min_sample:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {snapshot.sample_size} fresh Trump post(s); need "
            f"{min_sample} for a signal.",
            meta_base,
        )

    mean = snapshot.mean_compound
    if mean is None or bearish_threshold < mean < bullish_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Trump sentiment is neutral (mean_compound={mean}).",
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
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Trump sentiment now favors {supported_type}s "
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
            f"Trump sentiment is {mood} (mean_compound={mean:.3f}, "
            f"n={snapshot.sample_size}); opening one {supported_type}."
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
        return _decide_core(ctx, None, None)

    try:
        snapshot = _reader.read()
    except Exception as exc:  # TrumpFetchError, network failure, etc.
        return _decide_core(ctx, None, str(exc))
    return _decide_core(ctx, snapshot, None)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
trump_whisperer_qqq = register(_decide)
