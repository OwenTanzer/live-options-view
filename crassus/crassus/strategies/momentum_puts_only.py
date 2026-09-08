"""A puts-only mirror of `momentum_qqq`.

Every strategy in this repo that isolates one variable does so by cloning an
existing strategy and changing exactly one thing (see `phelps_variants.py`'s
docstring, or `canopus_down_day.py` vs. `momentum_qqq`'s differing signal
sources). This module clones `momentum_qqq` and changes exactly one thing:
a bullish trailing return is never acted on. The signal, thresholds, ATM
selection, one-contract-at-a-time position management, and OCC-symbol
parsing are otherwise identical -- see `momentum_qqq.py`'s own docstring for
the full reasoning behind all of that, which applies here unmodified.

Why this exists rather than just running `momentum_qqq` at a bearish-only
account: the requirement was two bots that "only do puts" -- one plain, one
run through Guideline Phelps -- so that comparing them isolates the effect
of the hold-time floor specifically on a puts-only book, the same way
Ankit vs. Ankit Phelps isolates it for `smoke_atm_roundtrip`. None of the
four existing Phelps twins (`phelps_variants.py`) are directionally
restricted, so this pair fills that gap rather than duplicating an existing
comparison. `momentum_qqq_phelps` already answers "what does Phelps do to a
two-sided momentum bot"; this pair instead answers "what does Phelps do to a
bot that already only ever expresses a bearish view."

A bullish signal is treated exactly like a neutral one: no entry, and a
close of a held put if one is open (the guideline's own "close only on a
genuine invalidation, not on ordinary discomfort" framing doesn't apply to
this base strategy at all -- it doesn't have a discomfort-vs-invalidation
distinction, same as `momentum_qqq` itself; that distinction is Phelps's own
contribution once this is wrapped, not something the base strategy needs to
approximate first). This is deliberately not "trade a call and immediately
flip it into a put" -- it is "there is no long side to this book at all,"
matching a genuinely puts-only mandate rather than a momentum bot that
merely favors puts.

See `scripts/verify_momentum_puts_only.py` for hermetic coverage, and
`phelps_variants.py` for the Phelps-wrapped twin
(`momentum_puts_only_qqq_phelps`).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import (
    DEFAULT_LOOKBACK_MINUTES,
    DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES,
    DEFAULT_RETAIN_MINUTES,
    MomentumSignal,
    PriceHistoryTracker,
    compute_momentum,
)
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "momentum_puts_only_qqq"
STRATEGY_VERSION = "1.0.0"

# Same magnitude as momentum_qqq's own bearish_threshold -- only the bullish
# side is dropped, not retuned. A bullish_threshold still exists in params
# for parity/documentation purposes but is never consulted: this strategy
# has no call-side branch to gate.
DEFAULT_BEARISH_THRESHOLD = -0.003  # -0.30% trailing return

DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Same as
# momentum_qqq._OCC_TYPE_RE -- a held position can roll off the front of the
# chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Separate tracker from momentum_qqq's -- sharing one would be harmless in
# principle (both read the same underlying price), but keeping them
# independent means a bug or a params change in one strategy's tracker
# lifecycle can never silently affect the other's.
_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)
_last_recorded_snapshot: tuple[str, str] | None = None


def _snapshot_observed_at(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(
    ctx: StrategyContext,
    signal: MomentumSignal | None,
    stale_source_reason: str | None = None,
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
        if held_type != "put":
            # This strategy never opens a call itself, but a book can carry
            # one anyway (e.g. an account re-pointed at this strategy_id
            # while still holding a prior strategy's call). Standing down
            # rather than closing it keeps this strategy from taking an
            # action on a position it never would have opened -- an operator
            # reassigning strategy_id mid-position is a config decision this
            # strategy shouldn't second-guess by force-selling on its behalf.
            return no(
                f"Held symbol {held_symbol} is a {held_type}, not a put; "
                f"standing down rather than acting on a position this "
                f"puts-only strategy would never have opened.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                held_type=held_type,
            )

    if stale_source_reason is not None:
        # Same "absence of a fresh observation isn't evidence against the
        # held thesis" reasoning as momentum_qqq's own stale_source_reason
        # branch -- see that module's docstring.
        if held_symbol is not None:
            return no(
                f"Market snapshot unavailable or stale ({stale_source_reason}); "
                f"retaining the held put position rather than closing on a "
                f"missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}")

    if signal is None or signal.status == "no_data":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, no,
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
            ctx, held_symbol, held_quantity, no,
            f"Only {signal.sample_count} price observation(s) so far; still "
            f"warming up to a {signal.lookback_minutes:.0f}-minute lookback.",
            meta_base,
        )

    if signal.status == "stale_anchor":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, no,
            f"Nearest usable price anchor is "
            f"{signal.anchor_age_minutes:.1f} minutes old -- a gap in "
            f"observations makes it too stale to trust for a "
            f"{signal.lookback_minutes:.0f}-minute lookback.",
            meta_base,
        )

    params = ctx.params or {}
    bearish_threshold = params.get("bearish_threshold", DEFAULT_BEARISH_THRESHOLD)

    ret = signal.return_pct
    bearish = ret is not None and ret <= bearish_threshold

    if not bearish:
        # Bullish or neutral both read the same way here: nothing to open
        # (there is no long side), and any held put loses its support.
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, no,
            f"Trailing return over the last {signal.anchor_age_minutes:.0f}m "
            f"(target lookback {signal.lookback_minutes:.0f}m) is "
            f"{'bullish' if ret is not None and ret > 0 else 'neutral'} "
            f"(return_pct={ret}) -- this strategy only ever holds puts.",
            meta_base,
        )

    if held_symbol is not None:
        return no(
            f"Already holding {held_quantity} {held_symbol}; momentum "
            f"still supports it (return_pct={ret:.4f}).",
            symbol=held_symbol,
            held_quantity=held_quantity,
            **meta_base,
        )

    row = ctx.snapshot.atm("put")
    if not row:
        return no("No quoted put in the snapshot to trade.", **meta_base)
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
            f"Momentum points down (return_pct={ret:.4f} over the last "
            f"{signal.anchor_age_minutes:.0f}m, target lookback "
            f"{signal.lookback_minutes:.0f}m, n={signal.sample_count}); "
            f"opening one put."
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
    no: Any,
    reason: str,
    meta: dict[str, Any],
) -> Decision:
    if held_symbol is None:
        return no(reason, **meta)
    return _close(
        ctx, held_symbol, held_quantity,
        reason=f"{reason} No longer supports the held put position; closing it.",
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
    global _last_recorded_snapshot

    if ctx.session_phase != "open":
        return _decide_core(ctx, None)

    params = ctx.params or {}
    lookback_minutes = params.get("lookback_minutes", DEFAULT_LOOKBACK_MINUTES)
    max_overshoot = params.get("max_anchor_overshoot_minutes", DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES)
    max_snapshot_age = params.get("max_snapshot_age_minutes", DEFAULT_MAX_SNAPSHOT_AGE_MINUTES)

    observed_at = _snapshot_observed_at(ctx.snapshot.timestamp)
    if observed_at is None:
        return _decide_core(ctx, None, stale_source_reason=f"unparseable snapshot timestamp {ctx.snapshot.timestamp!r}")

    snapshot_age_minutes = (ctx.now_et - observed_at).total_seconds() / 60.0
    if snapshot_age_minutes > max_snapshot_age:
        return _decide_core(
            ctx, None,
            stale_source_reason=(
                f"snapshot is {snapshot_age_minutes:.1f} minutes old "
                f"(limit={max_snapshot_age}m) -- the collector looks stalled"
            ),
        )

    snapshot_key = (ctx.snapshot.timestamp, ctx.snapshot.sha256)
    if snapshot_key != _last_recorded_snapshot:
        _tracker.observe(observed_at, ctx.snapshot.underlying_price)
        _last_recorded_snapshot = snapshot_key

    signal = compute_momentum(
        _tracker.snapshot(),
        now=ctx.now_et,
        lookback_minutes=lookback_minutes,
        max_anchor_overshoot_minutes=max_overshoot,
    )
    return _decide_core(ctx, signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
momentum_puts_only_qqq = register(_decide)
