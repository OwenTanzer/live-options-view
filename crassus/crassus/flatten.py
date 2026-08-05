"""End-of-day flatten: force-close a held position as the close approaches,
and refuse any new entry for the rest of the session once inside the window.

Every strategy in this repo only closes a position *reactively* -- when its
own signal disagrees with the side it's holding, while `session_phase ==
"open"`. None of them, and nothing in `runner.py` either, ever revisits a
held position purely because the clock is running out. A position that never
gets a reversing signal on a given day rides untouched through `"afterhours"`
and into next session's settlement sweep (`worker.js`'s `settleAllBots`),
which prices it against that day's spot -- for a same-day (0DTE) contract,
that's expiration, not an actual sell-to-close trade.

This is mandatory, not opt-in: every account gets it regardless of its own
params, since every strategy here is an intraday play and none of them is
meant to carry a position past the close. `params["flatten_minutes_before_close"]`
only tunes *how early* the window opens (default `DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE`)
-- there is no params value that turns it off; a missing, non-numeric,
non-finite, zero, or negative value all fall back to the default rather than
disabling the check (see `_resolve_window_minutes`) or, worse, letting a
negative value make the `minutes_to_close > minutes_before_close` comparison
vacuously true so the window silently never opens.

`maybe_flatten` is checked by the runner *before* invoking the account's own
strategy for the cycle. Once the window is open it **always** returns a
`Decision` -- never `None` -- so the runner never falls through to the
strategy for the rest of the session: a flat book still gets an explicit
`no_trade` (not `None`), specifically to keep the strategy from opening a
fresh position seconds after this closed the last one. Closing itself uses
the same executability gate (`Quote.is_executable`) every strategy's own
`_close` already uses, so this doesn't force a trade against a stale or
missing quote -- it just retries (still blocking entries) next cycle.
"""

from __future__ import annotations

import math
from typing import Any

from . import exchange_calendar
from .strategy import Decision, StrategyContext

STRATEGY_ID = "eod_flatten"
STRATEGY_VERSION = "1.0.0"

DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE = 15.0


def _resolve_window_minutes(params: dict[str, Any]) -> float:
    """`params["flatten_minutes_before_close"]`, coerced and validated, or
    the default for anything that isn't a usable positive number.

    Deliberately falls back rather than raising: a bad value in an account's
    params (missing, wrong type, NaN/inf, zero, negative) must not be able to
    silently defeat the mandatory flatten -- e.g. a negative number would
    otherwise make `minutes_to_close > minutes_before_close` true for every
    normal `minutes_to_close`, so the window would just never open.
    """
    value = params.get("flatten_minutes_before_close")
    if value is None:
        return DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE
    if not math.isfinite(minutes) or minutes <= 0:
        return DEFAULT_FLATTEN_MINUTES_BEFORE_CLOSE
    return minutes


def maybe_flatten(ctx: StrategyContext, params: dict[str, Any]) -> Decision | None:
    """A `Decision` once the flatten window is open, else `None` (meaning the
    caller should fall through to the account's own strategy).

    Only ever fires during `"open"` -- there is nothing to flatten toward in
    premarket/afterhours/weekend. Outside the window, returns `None` so the
    account's own strategy runs as normal. Once inside the window, always
    returns a `Decision`: a close for a held position, an explicit `no_trade`
    while a flat book waits out the rest of the session (never `None`, so the
    strategy can't reopen one), or an explicit `no_trade` while waiting on a
    live quote to close safely. Picks the first held position; every current
    strategy already enforces "at most one open position" as its own
    invariant, so in practice there is only ever one to consider.
    """
    if ctx.session_phase != "open":
        return None

    minutes_before_close = _resolve_window_minutes(params)
    close_time = exchange_calendar.session_close(ctx.now_et.date())
    market_close = ctx.now_et.replace(
        hour=close_time.hour, minute=close_time.minute, second=0, microsecond=0
    )
    minutes_to_close = (market_close - ctx.now_et).total_seconds() / 60.0
    if minutes_to_close > minutes_before_close:
        return None

    if ctx.book.is_flat:
        return Decision.no_trade(
            reason=(
                f"End-of-day flatten window ({minutes_to_close:.1f} minute(s) to close, "
                f"threshold {minutes_before_close}m): no new entries this close to the bell."
            ),
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata={
                "flatten_minutes_before_close": minutes_before_close,
                "minutes_to_close": minutes_to_close,
            },
        )

    symbol, position = next(iter(ctx.book.positions.items()))

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None or not quote.is_executable:
        # Can't safely close this cycle at a live/fresh quote -- still an
        # explicit no_trade, not None, so the strategy doesn't get a chance
        # to open something else while this one waits to be closed.
        return Decision.no_trade(
            reason=(
                f"End-of-day flatten window ({minutes_to_close:.1f} minute(s) to close): "
                f"no live/fresh quote for {symbol} yet to close it against; retrying next "
                f"cycle rather than falling through to the strategy."
            ),
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata={
                "flatten_minutes_before_close": minutes_before_close,
                "minutes_to_close": minutes_to_close,
                "symbol": symbol,
            },
        )

    side = "sell" if position.quantity > 0 else "buy"
    return Decision(
        action=side,
        symbol=symbol,
        quantity=abs(position.quantity),
        reason=(
            f"End-of-day flatten: {minutes_to_close:.1f} minute(s) to close "
            f"(threshold {minutes_before_close}m) -- closing the held position "
            f"rather than letting it ride into expiration/settlement."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            "flatten_minutes_before_close": minutes_before_close,
            "minutes_to_close": minutes_to_close,
            "symbol": symbol,
            "held_quantity": position.quantity,
            "bid": quote.bid,
            "ask": quote.ask,
        },
    )
