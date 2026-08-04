"""End-of-day flatten: force-close a held position as the close approaches.

Every strategy in this repo only closes a position *reactively* -- when its
own signal disagrees with the side it's holding, while `session_phase ==
"open"`. None of them, and nothing in `runner.py` either, ever revisits a
held position purely because the clock is running out. A position that never
gets a reversing signal on a given day rides untouched through `"afterhours"`
and into next session's settlement sweep (`worker.js`'s `settleAllBots`),
which prices it against that day's spot -- for a same-day (0DTE) contract,
that's expiration, not an actual sell-to-close trade.

This is opt-in per account (`params["flatten_minutes_before_close"]`, unset
by default) rather than a global behavior change, since not every strategy or
account necessarily wants it. When set, `maybe_flatten` is checked by the
runner *before* invoking the account's own strategy for the cycle: inside the
window, it takes priority over whatever the strategy would have decided,
proposing a closing trade for the (single) held position at the current live
quote -- same executability gate (`Quote.is_executable`) every strategy's own
`_close` already uses, so this doesn't force a trade against a stale or
missing quote either.
"""

from __future__ import annotations

from typing import Any

from .strategy import Decision, StrategyContext

STRATEGY_ID = "eod_flatten"
STRATEGY_VERSION = "1.0.0"


def maybe_flatten(ctx: StrategyContext, params: dict[str, Any]) -> Decision | None:
    """A closing `Decision` for the held position if the close is within
    `params["flatten_minutes_before_close"]` minutes, else `None` (meaning
    the caller should fall through to the account's own strategy).

    Only ever fires during `"open"` -- there is nothing to flatten toward in
    premarket/afterhours/weekend, and the position is already flat is a no-op
    regardless of the window. Picks the first held position; every current
    strategy already enforces "at most one open position" as its own
    invariant, so in practice there is only ever one to consider.
    """
    minutes_before_close = params.get("flatten_minutes_before_close")
    if not minutes_before_close:
        return None
    if ctx.session_phase != "open":
        return None
    if ctx.book.is_flat:
        return None

    market_close = ctx.now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    minutes_to_close = (market_close - ctx.now_et).total_seconds() / 60.0
    if minutes_to_close > minutes_before_close:
        return None

    symbol, position = next(iter(ctx.book.positions.items()))

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None or not quote.is_executable:
        # Can't safely close this cycle at a live/fresh quote; the next
        # cycle (still inside the window) gets another chance.
        return None

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
