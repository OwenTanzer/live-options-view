"""VWAP mean-reversion fade strategy for QQQ options.

Thesis: an underlying that has stretched unusually far from its own
session VWAP, on volume that is not itself elevated, is a reasonable
candidate to snap back toward VWAP rather than keep extending -- the
classic mean-reversion read of price-vs-VWAP dislocation. Price well
ABOVE VWAP -> expect reversion DOWN -> buy a put. Price well BELOW VWAP
-> expect reversion UP -> buy a call. This is the opposite trade
direction from a breakout strategy reading the same dislocation.

Contrast with the sibling `vwap_breakout` strategy (same underlying data,
opposite thesis): `vwap_breakout` trades WITH a VWAP break, treating it as
the start of a move, and wants that move CONFIRMED by elevated relative
volume (an RVOL *floor*) -- a breakout with real participation behind it
is more likely to continue. This strategy does the reverse on both axes:

  * A wider deviation band (`fade_threshold_pct`, default 0.5%) than a
    breakout strategy would use for its own trigger -- a fade needs a
    genuine overextension to have edge, not an ordinary/early breakout
    level. Fading every minor VWAP cross would just be relabeling
    breakout's own entries in the opposite direction.
  * An RVOL *ceiling* (`rvol_ceiling`, default 1.5) rather than a floor --
    this strategy only fades when relative volume is BELOW the ceiling.
    An overextension on ordinary or below-average volume looks more like
    drift or thin-book noise than a real move, and is a better candidate
    for reversion. An overextension accompanied by a volume surge looks
    like a confirmed, participation-backed breakout -- exactly the
    setup `vwap_breakout` wants to trade WITH, not the setup this
    strategy should fight. Requiring RVOL below the ceiling is what
    keeps this strategy from simply re-trading `vwap_breakout`'s own
    entries in reverse: the two are deliberately selecting on opposite,
    largely non-overlapping RVOL regimes for the same deviation event.

Being honest about the risk: mean reversion against price extended away
from VWAP is a heuristic, not a law. A genuine trend can begin on
ordinary volume before participation catches up (RVOL is a lagging
confirmation signal, not a leading one), and this strategy will
cheerfully fade the first leg of that trend, taking a loss on a real
directional move rather than a spurious overextension. No overshoot
limit, hysteresis, or stop-based invalidation is implemented here beyond
"the deviation gate is no longer satisfied" -- see the module docstring
of `momentum_qqq.py` for why that's judged an acceptable P3 scope for a
first cut, with sizing/hysteresis left to `ctx.params` or a later
iteration.

Position management mirrors `momentum_qqq.py` exactly: at most one
contract at a time, OCC-symbol parsing to recognize a held position's
type, closing a held position the moment the fade setup no longer
supports it (RVOL climbs to/above the ceiling, deviation collapses back
inside the band, or the underlying market read stops being trustworthy)
-- except when the underlying market observation is simply *missing* or
untrustworthy (no `underlying_market`, `freshness != "live"`, or
`rvol_status != "ok"`), in which case a held position is *retained*
rather than closed: absence of a fresh, trustworthy read is not evidence
the fade thesis has failed, just evidence there is nothing to say this
cycle. That distinction -- "gate hasn't looked" vs. "gate looked and
disagrees" -- is the same one `vwap_rvol.evaluate_gate` and
`momentum_qqq.py` already draw; this strategy reads `underlying_market`
directly (rather than through `evaluate_gate`) because its fade logic
needs the raw deviation magnitude and an RVOL *ceiling* comparison that
`evaluate_gate`'s floor-oriented, direction-agreement-oriented API
doesn't express.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "vwap_fade_qqq"
STRATEGY_VERSION = "1.0.0"

# Deliberately wider than a breakout strategy's own trigger band -- see the
# module docstring above for why an ordinary VWAP cross shouldn't be faded.
DEFAULT_FADE_THRESHOLD_PCT = 0.5

# RVOL *ceiling*: this strategy only fades when relative volume is BELOW this
# multiple. At/above it, the move looks confirmed rather than an
# overextension worth reverting -- see the module docstring.
DEFAULT_RVOL_CEILING = 1.5

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike, matching
# momentum_qqq._OCC_TYPE_RE -- a held position can roll off the front of the
# chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide(ctx: StrategyContext) -> Decision:
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
    fade_threshold_pct = params.get("fade_threshold_pct", DEFAULT_FADE_THRESHOLD_PCT)
    rvol_ceiling = params.get("rvol_ceiling", DEFAULT_RVOL_CEILING)

    underlying_market = ctx.snapshot.underlying_market

    def gate_meta(**extra: Any) -> dict[str, Any]:
        base = dict(
            fade_threshold_pct=fade_threshold_pct,
            rvol_ceiling=rvol_ceiling,
            vwap=underlying_market.vwap if underlying_market else None,
            price_vs_vwap_pct=underlying_market.price_vs_vwap_pct if underlying_market else None,
            rvol_multiple=underlying_market.rvol_multiple if underlying_market else None,
            rvol_status=underlying_market.rvol_status if underlying_market else "no_data",
            freshness=underlying_market.freshness if underlying_market else "stale",
        )
        base.update(extra)
        return base

    # Absence of a trustworthy read -- missing record, not live, or RVOL
    # baseline not yet trustworthy -- is not evidence the fade setup has
    # failed. Retain a held position rather than close on nothing.
    if underlying_market is None:
        reason = "No underlying market data available."
        if held_symbol is not None:
            return no(
                f"{reason} Retaining the held {held_type} position rather "
                f"than closing on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **gate_meta(),
            )
        return no(reason, **gate_meta())

    if underlying_market.freshness != "live":
        reason = f"Underlying market data is not live (freshness={underlying_market.freshness})."
        if held_symbol is not None:
            return no(
                f"{reason} Retaining the held {held_type} position rather "
                f"than closing on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **gate_meta(),
            )
        return no(reason, **gate_meta())

    if underlying_market.rvol_status != "ok":
        reason = f"RVOL baseline not yet trustworthy (rvol_status={underlying_market.rvol_status})."
        if held_symbol is not None:
            return no(
                f"{reason} Retaining the held {held_type} position rather "
                f"than closing on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **gate_meta(),
            )
        return no(reason, **gate_meta())

    meta = gate_meta()
    price_vs_vwap_pct = underlying_market.price_vs_vwap_pct
    rvol_multiple = underlying_market.rvol_multiple

    if price_vs_vwap_pct is None or rvol_multiple is None:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "VWAP deviation or RVOL multiple missing from an otherwise-live "
            "underlying market read.",
            meta,
        )

    if not (price_vs_vwap_pct > fade_threshold_pct or price_vs_vwap_pct < -fade_threshold_pct):
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"price_vs_vwap_pct={price_vs_vwap_pct:.3f} is within the "
            f"{fade_threshold_pct}% fade band -- no genuine overextension "
            f"to fade.",
            meta,
        )

    if rvol_multiple >= rvol_ceiling:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"RVOL {rvol_multiple} is at/above the ceiling {rvol_ceiling} -- "
            f"the move looks confirmed by volume rather than an "
            f"overextension worth reverting; declining to fight it.",
            meta,
        )

    # Both conditions hold: a genuine overextension, unconfirmed by volume.
    # Fade the direction: price above VWAP -> expect reversion down -> put;
    # price below VWAP -> expect reversion up -> call.
    supported_type = "put" if price_vs_vwap_pct > 0 else "call"
    direction = "down" if supported_type == "put" else "up"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; fade setup "
                f"still supports it (price_vs_vwap_pct={price_vs_vwap_pct:.3f}, "
                f"rvol_multiple={rvol_multiple}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Fade setup now points {direction} "
                f"(price_vs_vwap_pct={price_vs_vwap_pct:.3f}); closing stale "
                f"{held_type} position before considering a new one."
            ),
            meta=meta,
            no=no,
        )

    row = ctx.snapshot.atm(supported_type)
    if not row:
        return no(f"No quoted {supported_type} in the snapshot to trade.", **meta)
    symbol = row["OptionSymbol"]

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}.", symbol=symbol, **meta)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
            **meta,
        )

    return Decision(
        action="buy",
        symbol=symbol,
        quantity=1,
        reason=(
            f"Price is {abs(price_vs_vwap_pct):.3f}% away from VWAP "
            f"(vwap={underlying_market.vwap}) on RVOL {rvol_multiple} below "
            f"the {rvol_ceiling} ceiling; fading back toward VWAP with one "
            f"{supported_type}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            **meta,
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


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
vwap_fade_qqq = register(_decide)
