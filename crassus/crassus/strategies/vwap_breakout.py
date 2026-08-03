"""VWAP + RVOL breakout QQQ options strategy.

Unlike `momentum_qqq`, which reads `ctx.snapshot.underlying_market` only as
an *optional veto* on a signal computed elsewhere (its own trailing-return
momentum -- see `momentum_qqq.py`'s docstring and `vwap_rvol.evaluate_gate`),
this strategy has no momentum component at all. VWAP and RVOL *are* the
signal here, not a confirmation layered on top of one: price crossing away
from the session VWAP by more than a threshold, while relative volume is
elevated, is a classic intraday breakout heuristic in its own right (see
`docs/plans/2026-07-vwap-rvol.md`), and this strategy trades that heuristic
directly.

Because the direction itself is *derived from* `price_vs_vwap_pct` -- not
supplied by an external signal the way `momentum_qqq` supplies its trailing
return -- this module reads `ctx.snapshot.underlying_market` fields directly
rather than going through `vwap_rvol.evaluate_gate`. `evaluate_gate` takes an
already-decided `direction` and answers "does VWAP/RVOL agree with it,"
which fits a veto-on-a-prior-signal use case; it has no notion of a
*threshold* band around VWAP; that band is the whole of this strategy's
directional test, so building it out of `evaluate_gate`'s boolean
agrees/disagrees primitive would just be an awkward wrapper around fields
this module can read once and reason about directly. The
missing-data/stale-freshness/not-yet-"ok"-RVOL precedence rules `evaluate_gate`
encodes (freshness checked before rvol_status, because a stale record can't
be trusted even if RVOL's own baseline bucket looks fine) are reproduced here
by hand for the same reason, against the same `UnderlyingMarket` fields (see
`market.py`'s `UnderlyingMarket` docstring).

RVOL confirmation is not decorative. A price that has drifted outside the
VWAP band on ordinary, unremarkable volume is not distinguishable from noise
-- VWAP itself is a volume-weighted average, so a small enough volume
excursion barely moves it and "breaking" the band proves little about
participation. Requiring relative volume to clear `rvol_floor` is how this
strategy tries to separate an actual, volume-backed breakout (participants
pushing price away from the session's volume-weighted center) from a light
handful of prints drifting through the band while most of the session's
volume sits elsewhere. It is still just a heuristic gate on a noisy ratio,
not proof of anything.

Be honest about the edge: VWAP/RVOL breakout is a well-worn intraday
heuristic, not a validated edge. It has no theoretical claim to producing
positive expectancy, especially compressed into a single 0DTE session where
whipsaw around the VWAP line is common and a "breakout" can mean-revert
within minutes. This module implements the mechanism faithfully; it makes no
claim the mechanism is profitable.

Position management mirrors `momentum_qqq`/`trump_whisperer`/
`reddit_sentiment`: at most one contract at a time, opened in the direction
the current breakout supports and closed the moment that support goes away
(band re-entered, RVOL falls back under the floor, or the direction flips) --
same "no pyramiding, close on loss of support" shape, same OCC-symbol
parsing and close/no-trade plumbing duplicated intentionally rather than
shared (see `trump_whisperer.py`'s docstring for why). When the underlying
market read itself is missing, stale, or RVOL hasn't reached "ok" yet, that
is treated as an absence of a fresh observation, not a genuine "no signal"
read: a held position is retained, not closed, matching `momentum_qqq`'s
`stale_source_reason`/gate-unavailable handling. A genuine in-band or
volume-unconfirmed read, by contrast, does close a held position -- that is
this strategy actually looking and finding no support, not a missing read.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S, UnderlyingMarket
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "vwap_breakout_qqq"
STRATEGY_VERSION = "1.0.0"

# Band, in the same percent units as UnderlyingMarket.price_vs_vwap_pct (0.5
# means 0.5%, not 0.005). Inside +/- this threshold is "at VWAP," not a
# breakout in either direction.
DEFAULT_VWAP_BREAKOUT_THRESHOLD_PCT = 0.15

# A breakout must be accompanied by at least this multiple of the session's
# baseline relative volume to be trusted as real participation rather than a
# light drift through the band.
DEFAULT_RVOL_FLOOR = 1.2

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike -- same pattern as
# momentum_qqq._OCC_TYPE_RE, parsed from the held symbol itself rather than
# looked up in the current snapshot, so a held contract that has rolled off
# the front of the chain can still be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


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
    threshold_pct = params.get("vwap_breakout_threshold_pct", DEFAULT_VWAP_BREAKOUT_THRESHOLD_PCT)
    rvol_floor = params.get("rvol_floor", DEFAULT_RVOL_FLOOR)

    um: UnderlyingMarket | None = ctx.snapshot.underlying_market

    def um_meta(**extra: Any) -> dict[str, Any]:
        return dict(
            vwap=um.vwap if um else None,
            price_vs_vwap_pct=um.price_vs_vwap_pct if um else None,
            rvol_multiple=um.rvol_multiple if um else None,
            rvol_floor=rvol_floor,
            vwap_breakout_threshold_pct=threshold_pct,
            freshness=um.freshness if um else None,
            rvol_status=um.rvol_status if um else None,
            **extra,
        )

    # Absence of a fresh, trustworthy observation -- missing record, stale
    # freshness, or RVOL not yet "ok" -- is not evidence of anything, so a
    # held position is retained rather than closed. Freshness is checked
    # before rvol_status, mirroring evaluate_gate's precedence: a stale
    # record can't be trusted for the VWAP side of the read just because
    # RVOL's own baseline bucket happens to say "ok" (see vwap_rvol.py's
    # docstring for why they're independent axes).
    if um is None:
        if held_symbol is not None:
            return no(
                "No underlying_market data on the snapshot; retaining the "
                f"held {held_type} position rather than closing on a "
                "missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **um_meta(),
            )
        return no("No underlying_market data on the snapshot.", **um_meta())

    if um.freshness != "live":
        if held_symbol is not None:
            return no(
                f"underlying_market freshness is {um.freshness!r}, not "
                f"'live'; retaining the held {held_type} position rather "
                f"than closing on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **um_meta(),
            )
        return no(f"underlying_market freshness is {um.freshness!r}, not 'live'.", **um_meta())

    if um.rvol_status != "ok":
        if held_symbol is not None:
            return no(
                f"RVOL status is {um.rvol_status!r}, not 'ok'; retaining "
                f"the held {held_type} position rather than closing on a "
                "missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **um_meta(),
            )
        return no(f"RVOL status is {um.rvol_status!r}, not 'ok'.", **um_meta())

    # A trustworthy read now exists -- figure out breakout direction and
    # volume confirmation from it.
    pct = um.price_vs_vwap_pct
    direction: str | None = None
    if pct is not None:
        if pct > threshold_pct:
            direction = "up"
        elif pct < -threshold_pct:
            direction = "down"

    rvol_confirmed = um.rvol_multiple is not None and um.rvol_multiple >= rvol_floor
    meta = um_meta(breakout_direction=direction, rvol_confirmed=rvol_confirmed)

    if direction is None:
        reason = (
            f"price_vs_vwap_pct={pct} is inside the +/-{threshold_pct}% band; "
            "no breakout."
        )
        if held_symbol is not None:
            return _close(
                ctx, held_symbol, held_quantity,
                reason=f"{reason} No longer supports the held {held_type} position; closing it.",
                meta=meta, no=no,
            )
        return no(reason, **meta)

    if not rvol_confirmed:
        reason = (
            f"Price is {'above' if direction == 'up' else 'below'} VWAP by "
            f"{pct}% (threshold {threshold_pct}%), but rvol_multiple="
            f"{um.rvol_multiple} does not clear the required floor "
            f"{rvol_floor} -- a breakout without volume confirmation isn't "
            "trusted."
        )
        if held_symbol is not None:
            return _close(
                ctx, held_symbol, held_quantity,
                reason=f"{reason} No longer supports the held {held_type} position; closing it.",
                meta=meta, no=no,
            )
        return no(reason, **meta)

    supported_type = "call" if direction == "up" else "put"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; breakout "
                f"still supports it (price_vs_vwap_pct={pct}, "
                f"rvol_multiple={um.rvol_multiple}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Breakout now points {direction} (price_vs_vwap_pct={pct}, "
                f"rvol_multiple={um.rvol_multiple}); closing stale "
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
            f"VWAP breakout {direction} confirmed by volume "
            f"(price_vs_vwap_pct={pct}% vs +/-{threshold_pct}% threshold, "
            f"rvol_multiple={um.rvol_multiple} >= floor {rvol_floor}); "
            f"opening one {supported_type}."
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
vwap_breakout_qqq = register(_decide)
