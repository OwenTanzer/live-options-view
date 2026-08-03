"""Max-pain / expiration-pinning QQQ options strategy.

"Max pain" is options-theory shorthand for the strike at which total
expiration payout to *option holders* (equivalently, aggregate loss to
option *writers*) is minimized, across every strike currently carrying open
interest. For a candidate settlement level K, this strategy sums, over every
row in the current chain, `max(0, Strike - K) * OpenInterest` for calls plus
`max(0, K - Strike) * OpenInterest` for puts -- i.e. what every strike's
outstanding open interest would be worth if the underlying settled at K --
then repeats that sum for every K present in the chain and takes the K that
minimizes it. That K is the max-pain strike.

The trading heuristic layered on top -- "price tends to drift toward the
max-pain strike into expiration" -- is a theory about market-maker/dealer
hedging pressure, not a law. It is a real, widely-cited pattern (large OI
concentrations can create delta-hedging flows that nudge price toward the
strike that hurts the most holders), but it is also a simplification this
module owns honestly: it ignores gamma/vanna exposure by strike, new OI
added intraday, and the fact that plenty of expirations simply don't pin.
Treat the signal as a mild positional bias, not a prediction.

Because of that, two guards keep this strategy from acting on noise:

  * `pin_threshold_pct` (default 0.15% of the underlying price) -- the
    underlying must be meaningfully away from the max-pain strike, not just
    on the wrong side of it by a rounding error, before a direction is
    called.
  * `min_strikes_with_oi` (default 5) -- there must be at least this many
    distinct strikes carrying nonzero open interest on *both* the call and
    put side before the computed max-pain strike is trusted at all. A chain
    with sparse OI produces a max-pain strike that is essentially noise.

Position management mirrors `reddit_sentiment.reddit_sentiment_qqq` and
`trump_whisperer.trump_whisperer_qqq`: at most one contract at a time,
opened in the direction the max-pain bias supports, closed the moment that
bias stops supporting it (including when the signal merely goes quiet --
inside the threshold band or short on OI data -- not only when it flips to
the other side). `ctx.book.positions` is the source of truth for what's
held, never a freshly re-derived ATM strike, for the same reason given in
those strategies' docstrings: the underlying can drift after entry.

Unlike the sentiment strategies, there is no external I/O here -- the whole
signal comes from `ctx.snapshot.rows`, which the runner has already fetched.
`_decide_core` still exists as a separate, no-I/O entry point (identical to
`_decide` here) purely so `scripts/verify_max_pain.py` has a stable, direct
target to exercise, matching the shape of the other strategies' verify
scripts.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "max_pain_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_PIN_THRESHOLD_PCT = 0.15  # percent of underlying price
DEFAULT_MIN_STRIKES_WITH_OI = 5  # distinct strikes needing OI on both sides

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself rather than looked up in the current market snapshot, same
# reasoning as the sentiment strategies: a held position can roll off the
# front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _compute_max_pain(rows: list[dict[str, Any]]) -> tuple[float | None, int]:
    """Compute the max-pain strike from a chain's rows.

    Returns `(max_pain_strike, strikes_with_oi_both_sides)`. The strike is
    `None` when the chain carries no strike data at all; the count is
    computed independently and used by the caller as the data-sufficiency
    guard regardless of whether a strike could be computed.
    """
    calls = [r for r in rows if r.get("Type") == "call" and r.get("OpenInterest")]
    puts = [r for r in rows if r.get("Type") == "put" and r.get("OpenInterest")]

    call_strikes = {r["Strike"] for r in calls}
    put_strikes = {r["Strike"] for r in puts}
    strikes_with_oi_both_sides = len(call_strikes & put_strikes)

    all_strikes = sorted({r["Strike"] for r in rows if r.get("Strike") is not None})
    if not all_strikes:
        return None, strikes_with_oi_both_sides

    def payout_at(k: float) -> float:
        total = 0.0
        for r in calls:
            total += max(0.0, r["Strike"] - k) * r["OpenInterest"]
        for r in puts:
            total += max(0.0, k - r["Strike"]) * r["OpenInterest"]
        return total

    max_pain_strike = min(all_strikes, key=lambda k: (payout_at(k), k))
    return max_pain_strike, strikes_with_oi_both_sides


def _decide_core(ctx: StrategyContext) -> Decision:
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

    params = ctx.params or {}
    pin_threshold_pct = params.get("pin_threshold_pct", DEFAULT_PIN_THRESHOLD_PCT)
    min_strikes_with_oi = params.get("min_strikes_with_oi", DEFAULT_MIN_STRIKES_WITH_OI)

    underlying_price = ctx.snapshot.underlying_price
    max_pain_strike, strikes_with_oi_both_sides = _compute_max_pain(ctx.snapshot.rows)

    deviation_pct: float | None = None
    if max_pain_strike is not None and underlying_price:
        deviation_pct = (underlying_price - max_pain_strike) / underlying_price * 100.0

    meta_base = dict(
        max_pain_strike=max_pain_strike,
        underlying_price=underlying_price,
        deviation_pct=deviation_pct,
        strikes_with_oi_both_sides=strikes_with_oi_both_sides,
        pin_threshold_pct=pin_threshold_pct,
        min_strikes_with_oi=min_strikes_with_oi,
    )

    if max_pain_strike is None or strikes_with_oi_both_sides < min_strikes_with_oi:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {strikes_with_oi_both_sides} strike(s) with open interest "
            f"on both sides; need {min_strikes_with_oi} to trust a max-pain "
            f"strike.",
            meta_base,
        )

    if abs(deviation_pct) <= pin_threshold_pct:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Underlying ({underlying_price}) is within {pin_threshold_pct}% "
            f"of the max-pain strike ({max_pain_strike}); no directional "
            f"pin bias.",
            meta_base,
        )

    # Underlying above max pain -> bias is a drift down toward it -> put.
    # Underlying below max pain -> bias is a drift up toward it -> call.
    supported_type = "put" if deviation_pct > 0 else "call"
    direction = "down toward" if supported_type == "put" else "up toward"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; max-pain "
                f"bias still supports it (max_pain_strike={max_pain_strike}, "
                f"deviation_pct={deviation_pct:.3f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Max-pain bias now favors {supported_type}s "
                f"(max_pain_strike={max_pain_strike}, "
                f"deviation_pct={deviation_pct:.3f}); closing stale "
                f"{held_type} position before considering a new one."
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
            f"Underlying ({underlying_price}) is pinned {direction} "
            f"max-pain strike {max_pain_strike} (deviation_pct="
            f"{deviation_pct:.3f}%); opening one {supported_type}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            **meta_base,
            "strike": row["Strike"],
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
    return _decide_core(ctx)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
max_pain_qqq = register(_decide)
