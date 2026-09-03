"""Guideline Phelps: temporal concentration of convexity.

See the Notion page "Preliminary Guideline Phelps" (Trading Method /
Guideline Phelps) for the full argument. Compressed to the part this module
implements: a 0DTE option position is a short-horizon convexity purchase,
and the market may overprice convexity across a whole session while
underpricing it inside the particular 25-30 minute window the day's move
actually happens in. Guideline Phelps says a position should be granted one
such window -- P_Phelps -- before ordinary option-price discomfort or a
temporarily unsupportive signal is treated as a reason to exit. Within the
window, only a genuine invalidation of the entry thesis should close the
position; past the window, failure to resolve becomes affirmative evidence
against it.

This module provides `phelps_wrap`, which retrofits that hold-time floor
onto any existing strategy in this repo without touching its entry or signal
logic at all. Every strategy here shares one exit shape -- close the
position the moment its own signal stops supporting it (see
`reddit_sentiment.py`, `trump_whisperer.py`, `momentum_qqq.py`, `oi_skew.py`,
`max_pain.py`, `put_call_ratio.py`) -- and none of them currently distinguish
"the signal quietly reversed" from "the thesis is structurally broken."
Given that, `phelps_wrap` treats every proposed close of a held position as
a *deferrable* one: it lets the position stand until either the base
strategy stops proposing to close it, or the Phelps window elapses with the
close still standing, at which point it lets the close through unmodified
in spirit (same symbol, same reason lineage) but attributed to the wrapped
strategy_id.

Phelps never manufactures a trade the base strategy didn't propose, and it
never blocks an opening `buy` or a `no_trade` while flat -- it only ever
delays a `sell` of a position it is watching.

See `crassus/strategies/phelps_variants.py` for the wrapped bots and
`crassus/strategies/phelps_pure.py` for a strategy whose entry signal, not
just its exit timing, is built from Phelps directly.

`phelps_wrap` above is a permanent experimental control (MOO-161) and must
not change. This module also provides `fixed_window_wrap`, a second,
independent wrapper for the canonical MOO-161 fixed-window rule: instead of
merely deferring a sell the base strategy already proposed, it imposes a
true terminal boundary -- suppressing every base-strategy sell while held,
then forcing the entire position closed unconditionally once one Phelps
window has elapsed, regardless of what the base strategy currently wants.
Deliberately no structural-invalidation distinction is made anywhere in
`fixed_window_wrap` -- see its own docstring.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Any

from .market import EXECUTION_QUOTE_MAX_AGE_S
from .strategy import Decision, Strategy, StrategyContext


def _fill_time_for_symbol(ctx: StrategyContext, symbol: str) -> datetime | None:
    """The most recent `buy` trade's own timestamp for `symbol`, if the raw
    trade record carries one -- `Book.trades` (client.py) keeps the raw
    dicts, and `ts` is present on them (see `canopus_down_day._traded_today`
    for the same read against the same shape). Returns `None` if there's no
    matching trade or its `ts` can't be parsed, so the caller can fall back
    to "first observed" rather than crash on an unexpected trade shape.
    """
    best: datetime | None = None
    for trade in ctx.book.trades:
        if not isinstance(trade, dict):
            continue
        trade_symbol = trade.get("sym") or trade.get("symbol") or trade.get("option_symbol") or trade.get("OptionSymbol")
        side = str(trade.get("side") or trade.get("action") or trade.get("direction") or "").lower()
        ts = trade.get("ts")
        if trade_symbol != symbol or side != "buy" or not ts:
            continue
        try:
            fired_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if best is None or fired_at > best:
            best = fired_at
    return best


def resolve_phelps_minutes(params: dict[str, Any]) -> float:
    """`params.get("phelps_minutes")`, validated -- shared by `phelps_wrap`
    and `phelps_pure_qqq` so both apply the same guard, rather than each
    reading the raw params value directly. Flagged in review: neither did
    any validation before this -- a non-numeric string raised inside the
    `:.1f}m` reason-string formatting (a strategy exception, not a clean
    no_trade), NaN made every `elapsed_minutes < phelps_minutes` comparison
    false so the window "elapsed" immediately, and +inf made it always true
    so the window never elapsed (holds forever). Same "bad params must not
    silently defeat the guideline" posture as `flatten._resolve_window_minutes`
    and `canopus_down_day._resolve_params`'s numeric fields -- falls back to
    the default rather than raising or propagating a nonsensical value.
    """
    value = params.get("phelps_minutes")
    if value is None:
        return PHELPS_MINUTES_DEFAULT
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return PHELPS_MINUTES_DEFAULT
    if not math.isfinite(minutes) or minutes <= 0:
        return PHELPS_MINUTES_DEFAULT
    return minutes

# The guideline's working value is a 25-30 minute band with no established
# point estimate yet ("Parameters not yet established"). The midpoint is
# used as the default rather than either edge, and is a knob
# (`ctx.params["phelps_minutes"]`) precisely because the source guideline
# says this number isn't settled.
PHELPS_MINUTES_DEFAULT = 27.5

# (account username, symbol) -> entry time. Module-level and shared across
# every account/strategy pairing that goes through phelps_wrap, mirroring
# momentum_qqq's module-level `_tracker`: one process, one clock table, not
# one per call. Guarded by `_lock` because the runner may evaluate multiple
# accounts in the same process.
_entry_times: dict[tuple[str, str], datetime] = {}
_lock = threading.Lock()


def _account_key(ctx: StrategyContext) -> str:
    # `AccountState.summary()["username"]` is the one stable per-account
    # identifier `StrategyContext` actually carries -- there is no `alias`
    # field on it, and the server has no `account_id` distinct from a login
    # (see client.py's `AccountSession` docstring).
    return str((ctx.account_state or {}).get("username", "unknown"))


def _held(ctx: StrategyContext) -> tuple[str, int] | None:
    open_positions = {s: p for s, p in ctx.book.positions.items() if p.quantity != 0}
    if not open_positions:
        return None
    symbol, position = next(iter(open_positions.items()))
    return symbol, position.quantity


def phelps_wrap(base: Strategy, *, strategy_id: str, strategy_version: str) -> Strategy:
    """Wrap `base` so a proposed close of a held position waits one Phelps.

    Entry time is tracked in-process, keyed by (account username, symbol),
    seeded from the real fill timestamp when one is recoverable
    (`_fill_time_for_symbol` reads it off `ctx.book.trades`' own `ts` field
    -- `Position` itself carries no `opened_at`, but the raw trade dicts
    `Book` is built from do, the same source `canopus_down_day._traded_today`
    already reads). This matters under the deployed runner's 5-minute
    cadence: `Position`/`Book` only reflect a fill on the *next* cycle after
    it happened (the position reads as flat on the cycle the buy is
    proposed), so seeding the clock from "when this cycle first observed
    the position held" -- rather than the trade's own timestamp --
    systematically started every Phelps window a full cycle late. Flagged
    in review.

    Two consequences worth being honest about:

    1. If no matching trade record exists or its `ts` can't be parsed (an
       unexpected trade shape, or a position that predates the trade
       history this runtime can see), the clock still falls back to "first
       observed," not the position's real (unknown) entry time -- a
       deliberate choice between two wrong defaults, and "fresh window" was
       chosen because it fails toward Phelps's own stated bias (grant the
       discomfort sanctuary) rather than against it.
    2. The table is per-process and unbounded only in the sense that a
       symbol's entry is cleared the moment the account is next observed
       flat in it -- there is no long-lived leak, but a crash between "sell
       filled" and "next cycle observed the position closed" could leave a
       stale entry a little longer than real life. Harmless: the entry is
       replaced or cleared the next time that (account, symbol) pair is
       actually seen again in either state.
    """

    def _decide(ctx: StrategyContext) -> Decision:
        key_prefix = _account_key(ctx)
        held = _held(ctx)

        with _lock:
            if held is None:
                for key in [k for k in _entry_times if k[0] == key_prefix]:
                    del _entry_times[key]
            else:
                symbol, _qty = held
                key = (key_prefix, symbol)
                if key not in _entry_times:
                    fill_time = _fill_time_for_symbol(ctx, symbol)
                    _entry_times[key] = fill_time if fill_time is not None else ctx.now_et
                # These strategies cap at one open position each, so a
                # tracked entry under any other symbol for this account is
                # stale (the position it referred to is gone) -- drop it
                # rather than let it accumulate.
                for stale_key in [
                    k for k in _entry_times if k[0] == key_prefix and k[1] != symbol
                ]:
                    del _entry_times[stale_key]

        decision = base(ctx)

        if held is None or decision.action != "sell":
            return decision

        symbol, _qty = held
        with _lock:
            entry_time = _entry_times.get((key_prefix, symbol), ctx.now_et)

        params = ctx.params or {}
        phelps_minutes = resolve_phelps_minutes(params)
        elapsed_minutes = (ctx.now_et - entry_time).total_seconds() / 60.0

        if elapsed_minutes < phelps_minutes:
            return Decision.no_trade(
                reason=(
                    f"Guideline Phelps: base strategy proposed to close {symbol} "
                    f"after {elapsed_minutes:.1f}m, short of the "
                    f"{phelps_minutes:.1f}m Phelps window -- holding through the "
                    f"discomfort. Base reason: {decision.reason!r}"
                ),
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                metadata={
                    "phelps_elapsed_minutes": round(elapsed_minutes, 2),
                    "phelps_window_minutes": phelps_minutes,
                    "deferred_action": decision.action,
                    "deferred_reason": decision.reason,
                    "deferred_metadata": decision.metadata,
                    "symbol": symbol,
                },
            )

        # Window elapsed with the close still standing: this is exactly the
        # "demand evidence of resolution by the end of the window" branch --
        # let it through, but as this strategy's own attributed decision.
        return Decision(
            action=decision.action,
            reason=(
                f"Guideline Phelps: {elapsed_minutes:.1f}m elapsed "
                f"(>= {phelps_minutes:.1f}m window) with no resolution -- "
                f"releasing the position. Base reason: {decision.reason!r}"
            ),
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=decision.symbol,
            quantity=decision.quantity,
            confidence=decision.confidence,
            metadata={
                **(decision.metadata or {}),
                "phelps_elapsed_minutes": round(elapsed_minutes, 2),
                "phelps_window_minutes": phelps_minutes,
                "phelps_released": True,
            },
        )

    _decide.strategy_id = strategy_id
    _decide.strategy_version = strategy_version
    return _decide


# Separate tracking table from `phelps_wrap`'s `_entry_times` -- the two
# wrappers are never applied to the same strategy_id, but sharing one dict
# would let a restart/flat-clear cycle for one interfere with the other
# purely by coincidence of (account, symbol) keys. Same shape and same
# locking discipline as `_entry_times` above.
_fixed_window_entry_times: dict[tuple[str, str], datetime] = {}
_fixed_window_lock = threading.Lock()


def fixed_window_wrap(base: Strategy, *, strategy_id: str, strategy_version: str) -> Strategy:
    """Wrap `base` with the canonical MOO-161 fixed-window rule.

    Unlike `phelps_wrap`, which only ever defers a sell the base strategy
    itself proposed and lets it through once the base still wants it gone,
    this wrapper imposes a true terminal boundary: while a position is
    held and less than one Phelps window has elapsed since the real fill
    time, every base-strategy sell is suppressed; once the window has
    elapsed, the entire held position is closed unconditionally on the next
    executable quote, regardless of what the base strategy currently
    proposes (including a `no_trade` that would otherwise keep holding).
    This is the same "recover fill time, hold, force-close at the terminal
    boundary" shape `phelps_pure_qqq` already uses for its own time-based
    exit (`strategies/phelps_pure.py`), factored out here so it can be
    layered onto any of this repo's existing base strategies without
    touching their entry/signal logic -- but deliberately without that
    strategy's retracement-based early-invalidation branch: MOO-161 is
    explicit that this wrapper must not classify reversals, stale data, or
    any other base-strategy signal as a distinct "structural invalidation"
    case. Every proposed close is equally deferrable inside the window, and
    the window's own expiry is the only thing that ever forces an exit.

    Entry-time tracking, fill-time recovery, and per-account state clearing
    all reuse the exact mechanics `phelps_wrap` already established above
    (see that function's docstring for the full reasoning) -- only the
    exit decision differs.
    """

    def _decide(ctx: StrategyContext) -> Decision:
        key_prefix = _account_key(ctx)
        held = _held(ctx)

        with _fixed_window_lock:
            if held is None:
                for key in [k for k in _fixed_window_entry_times if k[0] == key_prefix]:
                    del _fixed_window_entry_times[key]
            else:
                symbol, _qty = held
                key = (key_prefix, symbol)
                if key not in _fixed_window_entry_times:
                    fill_time = _fill_time_for_symbol(ctx, symbol)
                    _fixed_window_entry_times[key] = fill_time if fill_time is not None else ctx.now_et
                for stale_key in [
                    k for k in _fixed_window_entry_times if k[0] == key_prefix and k[1] != symbol
                ]:
                    del _fixed_window_entry_times[stale_key]

        decision = base(ctx)

        if held is None:
            return decision

        symbol, quantity = held
        with _fixed_window_lock:
            entry_time = _fixed_window_entry_times.get((key_prefix, symbol), ctx.now_et)

        params = ctx.params or {}
        phelps_minutes = resolve_phelps_minutes(params)
        elapsed_minutes = (ctx.now_et - entry_time).total_seconds() / 60.0

        if elapsed_minutes < phelps_minutes:
            if decision.action != "sell":
                return decision
            return Decision.no_trade(
                reason=(
                    f"Fixed-window Phelps: base strategy proposed to close {symbol} "
                    f"after {elapsed_minutes:.1f}m, short of the "
                    f"{phelps_minutes:.1f}m window -- suppressing the sell. "
                    f"Base reason: {decision.reason!r}"
                ),
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                metadata={
                    "phelps_elapsed_minutes": round(elapsed_minutes, 2),
                    "phelps_window_minutes": phelps_minutes,
                    "deferred_action": decision.action,
                    "deferred_reason": decision.reason,
                    "deferred_metadata": decision.metadata,
                    "symbol": symbol,
                },
            )

        # Window elapsed: force the close unconditionally, regardless of
        # what the base strategy currently proposes. Mirrors
        # `phelps_pure_qqq`'s own terminal-close quote handling -- a
        # missing or non-executable quote records the failed attempt as a
        # no_trade and retries next cycle rather than fabricating a fill or
        # reinterpreting the strategy.
        quote = ctx.quotes([symbol]).get(symbol)
        if quote is None:
            return Decision.no_trade(
                reason=(
                    f"Fixed-window Phelps: {elapsed_minutes:.1f}m elapsed "
                    f"(>= {phelps_minutes:.1f}m window) but no live quote "
                    f"returned for {symbol}; will retry the forced close next cycle."
                ),
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                metadata={
                    "phelps_elapsed_minutes": round(elapsed_minutes, 2),
                    "phelps_window_minutes": phelps_minutes,
                    "symbol": symbol,
                    "forced_close_pending": True,
                },
            )
        if not quote.is_executable:
            return Decision.no_trade(
                reason=(
                    f"Fixed-window Phelps: {elapsed_minutes:.1f}m elapsed "
                    f"(>= {phelps_minutes:.1f}m window) but the live quote for "
                    f"{symbol} is not executable (age={quote.age_seconds}s, "
                    f"limit={EXECUTION_QUOTE_MAX_AGE_S}s); will retry next cycle."
                ),
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                metadata={
                    "phelps_elapsed_minutes": round(elapsed_minutes, 2),
                    "phelps_window_minutes": phelps_minutes,
                    "symbol": symbol,
                    "forced_close_pending": True,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "age_seconds": quote.age_seconds,
                },
            )

        return Decision(
            action="sell",
            symbol=symbol,
            quantity=quantity,
            reason=(
                f"Fixed-window Phelps: {elapsed_minutes:.1f}m elapsed "
                f"(>= {phelps_minutes:.1f}m window) -- closing the entire "
                f"position unconditionally, regardless of the base strategy's "
                f"current signal."
            ),
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            metadata={
                "phelps_elapsed_minutes": round(elapsed_minutes, 2),
                "phelps_window_minutes": phelps_minutes,
                "symbol": symbol,
                "forced_close": True,
                "base_action_at_boundary": decision.action,
                "base_reason_at_boundary": decision.reason,
            },
        )

    _decide.strategy_id = strategy_id
    _decide.strategy_version = strategy_version
    return _decide
