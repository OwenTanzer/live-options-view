"""A QQQ options strategy whose entry and exit are both Phelps, not just its
exit timing.

`phelps_variants.py` retrofits Guideline Phelps' hold-time floor onto
strategies whose *entry* thesis comes from somewhere else (sentiment,
momentum, OI skew). This module instead builds the entry directly out of
the guideline's own premise: convexity is priced too coarsely in time, so
the trade worth taking is a bet on a specific narrow interval, not a
directional view that happens to be expressed with an option.

Entry heuristic
----------------
The guideline itself doesn't specify what a "structurally privileged
moment" is -- that's explicitly left to whatever thesis is being expressed
(see the sibling strategies, or the frozen Modulo rule, for worked
examples). Absent a specific structural setup, the most direct thing this
strategy can look for that's actually *about* temporal concentration -- not
borrowed from a different signal family -- is the beginning of a
displacement: a trailing return over a short window (`displacement_window_minutes`,
default 5.0, chosen because the guideline's own "five or six bars on a
five-minute chart" framing is exactly this scale) that has just cleared
`displacement_threshold` (default 0.15%, i.e. 15bps). That is read as "a
move started," not "a move will continue" -- deliberately a much shorter,
noisier lookback than `momentum_qqq`'s (default 60 minutes), because this
strategy is trying to catch the onset of the tear the guideline describes,
not confirm an established trend. It is a heuristic proxy, not a validated
structural-privilege detector, and should be read with the same honesty
`oi_skew_qqq` and `max_pain_qqq` apply to their own proxies.

Exit: only Phelps
------------------
Once entered, this strategy applies exactly one exit rule, matching the
guideline's own operational rule as literally as this codebase's shape
allows:

  1. **Invalidation** -- if the underlying fully retraces the displacement
     that triggered entry (crosses back through the pre-displacement anchor
     price against the held direction), the thesis that a move was
     underway is invalidated outright and the position is closed
     immediately, at any elapsed time. This is the guideline's "exit
     immediately if the actual thesis is invalidated ... not permission to
     ignore price structure."
  2. **Time** -- absent invalidation, the position is held regardless of
     option P&L or intermediate chop until `phelps_minutes`
     (`ctx.params["phelps_minutes"]`, default `phelps.PHELPS_MINUTES_DEFAULT`)
     has elapsed since entry, then closed unconditionally via the next
     executable quote -- mirroring the frozen Modulo rule's own "liquidate
     at the terminal time using a marketable order" fallback.

There is deliberately no third exit path (no profit target, no stop-loss,
no re-entry). Layering a target on top would make this a Modulo clone with
a generic trigger instead of a strategy that isolates what Phelps alone
contributes; that comparison is the point of running this strategy
alongside the Phelps-wrapped variants in `phelps_variants.py`.

Position/state tracking, mirroring `momentum_qqq.py`
------------------------------------------------------
Like `momentum_qqq`, the signal here is computed from `ctx.snapshot.underlying_price`
accumulated into a rolling, module-level `PriceHistoryTracker` -- no new
data source. Also like `momentum_qqq`, every recorded observation is
anchored to `snapshot.timestamp` (the source's own clock), and a snapshot
whose timestamp hasn't advanced, or is more than `max_snapshot_age_minutes`
old, is treated as "no usable signal" (retain a held position; don't open a
new one) rather than re-recorded or read as neutral. See that module's
docstring for the full reasoning; it applies here without modification.

Per-account entry state (entry time, anchor price, held direction) is
tracked in an in-process dict keyed by account username, the same
constraint and the same restart caveat documented in `crassus/phelps.py`
(no server-side trade timestamps to recover it from). A restart that finds
an already-open position with no recorded entry starts both the Phelps
clock and the invalidation anchor fresh, at the current price -- the same
"fail toward granting the sanctuary" choice `phelps.phelps_wrap` makes, and
in this strategy's case also the only self-consistent choice, since the
real anchor price is simply not recoverable from the server's trade
history.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import (
    DEFAULT_RETAIN_MINUTES,
    MomentumSignal,
    PriceHistoryTracker,
    compute_momentum,
)
from ..phelps import PHELPS_MINUTES_DEFAULT
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "phelps_pure_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_DISPLACEMENT_WINDOW_MINUTES = 5.0
DEFAULT_DISPLACEMENT_THRESHOLD = 0.0015  # 15 basis points over the window
DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES = 2.0  # tight: a short window needs a fresh anchor
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# Same OCC-suffix parse as reddit_sentiment/trump_whisperer/momentum_qqq --
# a held position can roll off the front of the current chain while still
# needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)
_last_recorded_snapshot: tuple[str, str] | None = None


@dataclass
class _Watch:
    symbol: str
    entry_time: datetime
    anchor_price: float  # underlying price immediately before the displacement
    direction: str  # "up" or "down"


_watches: dict[str, _Watch] = {}
_lock = threading.Lock()


def _account_key(ctx: StrategyContext) -> str:
    return str((ctx.account_state or {}).get("username", "unknown"))


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _snapshot_observed_at(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None


def _close(ctx: StrategyContext, symbol: str, quantity: int, *, reason: str, meta: dict[str, Any], no: Any) -> Decision:
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

    key_prefix = _account_key(ctx)
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
                f"Held symbol {held_symbol} has an unrecognized option type; "
                f"standing down rather than guessing whether to close it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )

    with _lock:
        watch = _watches.get(key_prefix)
        if held_symbol is None:
            if watch is not None:
                del _watches[key_prefix]
            watch = None
        elif watch is None or watch.symbol != held_symbol:
            # Holding a position with no (or a stale) recorded watch -- a
            # fresh process, or the prior watch referred to a symbol that's
            # since closed. Start both clocks now, at the current price;
            # see the module docstring for why this is the only
            # self-consistent default.
            watch = _Watch(
                symbol=held_symbol,
                entry_time=ctx.now_et,
                anchor_price=ctx.snapshot.underlying_price,
                direction="up" if held_type == "call" else "down",
            )
            _watches[key_prefix] = watch

    if held_symbol is not None and watch is not None:
        current_price = ctx.snapshot.underlying_price
        retraced = (
            (watch.direction == "up" and current_price <= watch.anchor_price)
            or (watch.direction == "down" and current_price >= watch.anchor_price)
        )
        if retraced:
            return _close(
                ctx, held_symbol, held_quantity,
                reason=(
                    f"Guideline Phelps: underlying ({current_price}) has fully "
                    f"retraced through the pre-entry anchor "
                    f"({watch.anchor_price}) against the held {watch.direction} "
                    f"displacement -- thesis invalidated, closing immediately "
                    f"regardless of elapsed time."
                ),
                meta={"anchor_price": watch.anchor_price, "current_price": current_price, "direction": watch.direction},
                no=no,
            )

        params = ctx.params or {}
        phelps_minutes = params.get("phelps_minutes", PHELPS_MINUTES_DEFAULT)
        elapsed_minutes = (ctx.now_et - watch.entry_time).total_seconds() / 60.0
        if elapsed_minutes < phelps_minutes:
            return no(
                f"Holding {held_quantity} {held_symbol}; {elapsed_minutes:.1f}m "
                f"of {phelps_minutes:.1f}m Phelps window elapsed with the "
                f"thesis still intact -- discomfort alone is not exit evidence.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                phelps_elapsed_minutes=round(elapsed_minutes, 2),
                phelps_window_minutes=phelps_minutes,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Guideline Phelps: {elapsed_minutes:.1f}m elapsed "
                f"(>= {phelps_minutes:.1f}m window) -- releasing the position "
                f"regardless of resolution, per the frozen terminal-time rule."
            ),
            meta={"phelps_elapsed_minutes": round(elapsed_minutes, 2), "phelps_window_minutes": phelps_minutes},
            no=no,
        )

    # Flat: only an entry decision remains.
    if stale_source_reason is not None:
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}")

    if signal is None or signal.status in ("no_data", "warming_up", "stale_anchor"):
        detail = {
            "no_data": "No price history recorded yet.",
            "warming_up": "Still warming up to the displacement lookback window.",
            "stale_anchor": "Nearest usable price anchor is too stale to trust.",
        }[signal.status if signal else "no_data"]
        return no(detail)

    params = ctx.params or {}
    threshold = params.get("displacement_threshold", DEFAULT_DISPLACEMENT_THRESHOLD)
    ret = signal.return_pct
    if ret is None or abs(ret) < threshold:
        return no(
            f"No displacement: trailing {signal.lookback_minutes:.0f}m return "
            f"is {ret} (threshold={threshold}).",
            return_pct=ret,
            threshold=threshold,
        )

    direction = "up" if ret > 0 else "down"
    option_type = "call" if direction == "up" else "put"

    row = ctx.snapshot.atm(option_type)
    if not row:
        return no(f"No quoted {option_type} in the snapshot to trade.")
    symbol = row["OptionSymbol"]

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}.", symbol=symbol)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
        )

    with _lock:
        _watches[key_prefix] = _Watch(
            symbol=symbol,
            entry_time=ctx.now_et,
            anchor_price=signal.anchor_price if signal.anchor_price is not None else ctx.snapshot.underlying_price,
            direction=direction,
        )

    return Decision(
        action="buy",
        symbol=symbol,
        quantity=1,
        reason=(
            f"Displacement detected: {signal.lookback_minutes:.0f}m return "
            f"{ret:.4f} clears threshold {threshold}; opening one {option_type} "
            f"to hold for one Phelps window."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            "strike": row["Strike"],
            "underlying_price": ctx.snapshot.underlying_price,
            "anchor_price": signal.anchor_price,
            "return_pct": ret,
            "bid": quote.bid,
            "ask": quote.ask,
            "quote_age_seconds": quote.age_seconds,
        },
    )


def _decide(ctx: StrategyContext) -> Decision:
    global _last_recorded_snapshot

    if ctx.session_phase != "open":
        return _decide_core(ctx, None)

    params = ctx.params or {}
    window = params.get("displacement_window_minutes", DEFAULT_DISPLACEMENT_WINDOW_MINUTES)
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
        lookback_minutes=window,
        max_anchor_overshoot_minutes=max_overshoot,
    )
    return _decide_core(ctx, signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
phelps_pure_qqq = register(_decide)
