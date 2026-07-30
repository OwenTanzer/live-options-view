"""Time-series-momentum-driven QQQ options strategy.

Trailing return over a configurable lookback window is the textbook
time-series momentum signal (Moskowitz, Ooi & Pedersen (2012), "Time Series
Momentum": regress next-period return on trailing-period return; here the
trailing return's sign/magnitude directly gates a long/short/flat decision
rather than feeding a regression, the same simplification the sentiment
strategies already make). The underlying price used for the trailing return
is `ctx.snapshot.underlying_price` -- the same durable QQQ mark every other
strategy in this repo already reads -- observed once per polling cycle into
a shared, in-memory `PriceHistoryTracker` (see `crassus/momentum.py`); no new
data source or external API is introduced. Cross-sectional momentum (ranking
QQQ's return against a basket of other tickers) isn't used here because this
repo only ever quotes one underlying -- there's no universe to rank against.

Position management is deliberately identical in shape to
`reddit_sentiment.reddit_sentiment_qqq` and `trump_whisperer.trump_whisperer_qqq`:
at most one contract at a time, opened in the direction the trailing return
supports and closed the moment that support goes away -- including when the
return merely settles back into the neutral band, not only when it flips
sign. See those modules' docstrings for why this is the right level of
complexity for now (sizing/hysteresis/cool-downs belong in `ctx.params` or a
later strategy, not platform invariants). `_decide_core` mirrors their
`_decide_core` step-for-step so the three stay easy to compare; the
duplication (OCC-symbol parsing, close/no-trade plumbing) is intentional
isolation, not an oversight -- see `trump_whisperer.py`'s docstring for why.

Unlike the sentiment strategies, there is no fetch/ingestion failure mode
here -- the price observation comes from `ctx.snapshot`, which the runner has
already fetched successfully by the time a strategy is called. What this
strategy has instead is a warm-up/staleness problem the sentiment strategies
don't: on the first calls of a session there isn't yet a price point
`lookback_minutes` old to compare against ("warming_up"), and if polling was
interrupted for a stretch -- an outage, an overnight or weekend gap -- the
oldest available anchor can be far older than the lookback window actually
calls for ("stale_anchor"). Both are treated as "no usable signal," exactly
like an insufficient sentiment sample: closed positions get closed, flat
stays flat, and neither is treated as evidence of anything.

`_decide_core` takes an already-computed `MomentumSignal` (see
`crassus/momentum.py`) and contains all the decision logic; it has no network
or clock dependency and is what `scripts/verify_momentum_qqq.py` exercises
directly with hand-built signals. `_decide` is the thin wrapper that records
the current snapshot's price into the shared tracker and computes the signal
before calling it.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import (
    DEFAULT_LOOKBACK_MINUTES,
    DEFAULT_MAX_ANCHOR_STALENESS_MULTIPLE,
    DEFAULT_RETAIN_MINUTES,
    MomentumSignal,
    PriceHistoryTracker,
    compute_momentum,
)
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "momentum_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_BULLISH_THRESHOLD = 0.003  # +0.30% trailing return
DEFAULT_BEARISH_THRESHOLD = -0.003  # -0.30% trailing return

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as reddit_sentiment._OCC_TYPE_RE and trump_whisperer._OCC_TYPE_RE:
# a held position can roll off the front of the chain while still needing to
# be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy, deliberately -- there is
# one QQQ underlying price, not one per account, exactly like trump_whisperer's
# module-level `_reader`.
_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(ctx: StrategyContext, signal: MomentumSignal | None) -> Decision:
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

    if signal is None or signal.status == "no_data":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No price history recorded yet.", {},
        )

    meta_base = dict(
        lookback_minutes=signal.lookback_minutes,
        current_price=signal.current_price,
        anchor_price=signal.anchor_price,
        return_pct=signal.return_pct,
        sample_count=signal.sample_count,
        anchor_age_minutes=signal.anchor_age_minutes,
        signal_status=signal.status,
    )

    if signal.status == "warming_up":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {signal.sample_count} price observation(s) so far; still "
            f"warming up to a {signal.lookback_minutes:.0f}-minute lookback.",
            meta_base,
        )

    if signal.status == "stale_anchor":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Nearest usable price anchor is "
            f"{signal.anchor_age_minutes:.1f} minutes old -- a gap in "
            f"observations makes it too stale to trust for a "
            f"{signal.lookback_minutes:.0f}-minute lookback.",
            meta_base,
        )

    params = ctx.params or {}
    bullish_threshold = params.get("bullish_threshold", DEFAULT_BULLISH_THRESHOLD)
    bearish_threshold = params.get("bearish_threshold", DEFAULT_BEARISH_THRESHOLD)

    ret = signal.return_pct
    if ret is None or bearish_threshold < ret < bullish_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Trailing {signal.lookback_minutes:.0f}-minute return is "
            f"neutral (return_pct={ret}).",
            meta_base,
        )

    supported_type = "call" if ret >= bullish_threshold else "put"
    direction = "up" if supported_type == "call" else "down"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; momentum "
                f"still supports it (return_pct={ret:.4f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Momentum now points {direction} (return_pct={ret:.4f}); "
                f"closing stale {held_type} position before considering a "
                f"new one."
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
            f"Momentum points {direction} (return_pct={ret:.4f} over "
            f"{signal.lookback_minutes:.0f}m, n={signal.sample_count}); "
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
        return _decide_core(ctx, None)

    params = ctx.params or {}
    lookback_minutes = params.get("lookback_minutes", DEFAULT_LOOKBACK_MINUTES)
    max_staleness = params.get("max_anchor_staleness_multiple", DEFAULT_MAX_ANCHOR_STALENESS_MULTIPLE)

    _tracker.observe(ctx.now_et, ctx.snapshot.underlying_price)
    signal = compute_momentum(
        _tracker.snapshot(),
        now=ctx.now_et,
        lookback_minutes=lookback_minutes,
        max_anchor_staleness_multiple=max_staleness,
    )
    return _decide_core(ctx, signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
momentum_qqq = register(_decide)
