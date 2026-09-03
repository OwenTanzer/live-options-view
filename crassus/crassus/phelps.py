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
"""

from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Any

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
        open_positions = {
            symbol: position
            for symbol, position in ctx.book.positions.items()
            if position.quantity != 0
        }
        if len(open_positions) > 1:
            return Decision.no_trade(
                reason=(
                    "Guideline Phelps: holding more than one open option "
                    "position; standing down rather than tracking an arbitrary "
                    "symbol or compounding the contaminated book."
                ),
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                metadata={
                    "open_positions": {
                        symbol: position.quantity
                        for symbol, position in open_positions.items()
                    },
                    "phelps_multiple_positions": True,
                },
            )
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
