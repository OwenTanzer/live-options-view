"""Canopus Down-Day 14 -- frozen forward-test rule from the "Positive Result
Canopus -- Down-Day 14" research note (child of the Looking-Glass straddle
brief, same Trading Method lineage).

The rule, exactly as frozen for forward observation (do not revise these
parameters in response to individual outcomes -- that's the whole point of
freezing it):

1. Record QQQ's first valid underlying price at or after 9:30 a.m. ET (the
   session reference).
2. At 2:45 p.m., check QQQ's displacement from that reference.
3. Trade only when QQQ is at least 0.25% below the reference.
4. Buy one nearest-ATM QQQ 0DTE put at the displayed ask.
5. Immediately place a day-limit sale at 114% of the actual fill price,
   rounded up to the next cent.
6. If the target hasn't filled by 3:45 p.m., liquidate at the market instead.
7. No averaging down, no adding contracts, no direction flip, no re-entry.

This repo's paper-trading Worker has no resting/day order type (see
`client.ExecutionClient.submit` -- every order is an immediate market order
against the current live quote), so step 5's "day-limit sale" is
approximated the same way `flatten.py`'s profit-taking is: poll the live bid
every cycle and submit a market sell the first time it clears the rounded
target, rather than actually resting an order on the book. Functionally
equivalent for a paper account with no queue position to lose, and the same
approximation the brief's own backtest makes when it prices the target exit
at the observed executable bid rather than assuming perfect limit-order
priority.

Like every other strategy here, there is no shared state beyond `ctx.book` --
except for one exception this rule specifically requires and the book alone
cannot supply: the *morning* 9:30 reference price, needed hours before the
2:45 entry decision and therefore not recoverable from the held position at
decision time. `_reference_price_by_date` is a small **in-process** cache,
same "shared across every account, deliberately -- there is one QQQ
underlying" posture as `momentum_qqq._tracker`. A runner restart after 9:30
but before 2:45 loses the real opening reference and will instead record
whatever price it first observes post-restart -- always a *later*, more
favorable-to-"already-declined" price than the true 9:30 print, so a missed
true reference can only ever suppress a qualifying entry, never manufacture a
false one. Documented here rather than solved with a durable store because
nothing else in this codebase persists an intraday reference price either;
if that becomes a recurring need across strategies it belongs in a shared
module, not bolted onto this one.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time as dt_time
from typing import Any

from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "canopus_down_day_14"
STRATEGY_VERSION = "1.0.0"

DEFAULT_REFERENCE_TIME_ET = "09:30"
DEFAULT_ENTRY_WINDOW_START_ET = "14:45"
DEFAULT_ENTRY_WINDOW_END_ET = "14:51"  # exclusive -- "through 14:50:59" in the brief
DEFAULT_FALLBACK_EXIT_TIME_ET = "15:45"
DEFAULT_DOWN_THRESHOLD_PCT = 0.0025  # 0.25%
DEFAULT_TARGET_MULTIPLIER = 1.14  # eta = 14%, the plateau's empirical upper boundary

# Same OCC-suffix parse as momentum_qqq._OCC_TYPE_RE et al -- duplicated
# deliberately rather than shared; see momentum_qqq.py's module docstring.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# date -> first observed underlying_price at/after the reference time that
# day. Shared across every account running this strategy -- there is one QQQ
# underlying, not one per account.
_reference_price_by_date: dict[date, float] = {}


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _parse_hhmm(value: Any, default: str) -> dt_time:
    for candidate in (value, default):
        if isinstance(candidate, str):
            try:
                return datetime.strptime(candidate, "%H:%M").time()
            except ValueError:
                continue
    return datetime.strptime(default, "%H:%M").time()


def _resolve_params(params: dict[str, Any]) -> dict[str, Any]:
    def _positive_float(value: Any, default: float) -> float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        if f != f or f in (float("inf"), float("-inf")) or f <= 0:
            return default
        return f

    reference_time = _parse_hhmm(params.get("reference_time_et"), DEFAULT_REFERENCE_TIME_ET)
    entry_start = _parse_hhmm(params.get("entry_window_start_et"), DEFAULT_ENTRY_WINDOW_START_ET)
    entry_end = _parse_hhmm(params.get("entry_window_end_et"), DEFAULT_ENTRY_WINDOW_END_ET)
    fallback_exit = _parse_hhmm(params.get("fallback_exit_time_et"), DEFAULT_FALLBACK_EXIT_TIME_ET)
    if not (reference_time < entry_start < entry_end <= fallback_exit):
        # Bad/inconsistent ordering -- fall back to the frozen defaults
        # wholesale rather than run with a nonsensical window.
        reference_time = _parse_hhmm(None, DEFAULT_REFERENCE_TIME_ET)
        entry_start = _parse_hhmm(None, DEFAULT_ENTRY_WINDOW_START_ET)
        entry_end = _parse_hhmm(None, DEFAULT_ENTRY_WINDOW_END_ET)
        fallback_exit = _parse_hhmm(None, DEFAULT_FALLBACK_EXIT_TIME_ET)

    return {
        "reference_time": reference_time,
        "entry_window_start": entry_start,
        "entry_window_end": entry_end,
        "fallback_exit_time": fallback_exit,
        "down_threshold_pct": _positive_float(params.get("down_threshold_pct"), DEFAULT_DOWN_THRESHOLD_PCT),
        "target_multiplier": _positive_float(params.get("target_multiplier"), DEFAULT_TARGET_MULTIPLIER),
    }


def _maybe_record_reference(today: date, price: float, reference_time: dt_time, now_time: dt_time) -> None:
    if now_time < reference_time:
        return
    if today not in _reference_price_by_date:
        _reference_price_by_date[today] = price


def _target_price(fill_price: float, multiplier: float) -> float:
    """114% of the fill price, rounded *up* to the next cent."""
    raw_cents = round(fill_price * multiplier * 100, 6)  # clear float noise first
    return math.ceil(raw_cents) / 100.0


def _traded_today(ctx: StrategyContext) -> bool:
    """No-re-entry rule's memory, read from the raw trade records' own `ts`
    (see `worker.js`'s trade shape) rather than any state this module keeps,
    so a restart mid-day still refuses to re-enter after a completed trade.
    """
    today = ctx.now_et.date()
    for trade in ctx.book.trades:
        ts = trade.get("ts") if isinstance(trade, dict) else None
        if not ts:
            continue
        try:
            fired_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if fired_at.astimezone(ctx.now_et.tzinfo).date() == today:
            return True
    return False


def _decide(ctx: StrategyContext) -> Decision:
    def no(reason: str, **meta: Any) -> Decision:
        return Decision.no_trade(
            reason=reason, strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION, metadata=meta or None
        )

    if ctx.session_phase != "open":
        return no(f"Market is {ctx.session_phase}; nothing to do.", session_phase=ctx.session_phase)

    cfg = _resolve_params(ctx.params or {})
    now_time = ctx.now_et.time()
    today = ctx.now_et.date()

    _maybe_record_reference(today, ctx.snapshot.underlying_price, cfg["reference_time"], now_time)

    positions = {s: p for s, p in ctx.book.positions.items() if p.quantity != 0}
    unrecognized = {s: p.quantity for s, p in positions.items() if _option_type_from_symbol(s) is None}
    if unrecognized:
        return no("Holding position(s) with an unrecognized option type; standing down.", positions=unrecognized)

    calls = {s: p for s, p in positions.items() if _option_type_from_symbol(s) == "call"}
    puts = {s: p for s, p in positions.items() if _option_type_from_symbol(s) == "put"}
    if calls:
        return no(
            "Holding an unexpected call position; this strategy is put-only. Standing down.",
            calls={s: p.quantity for s, p in calls.items()},
        )
    if len(puts) > 1:
        return no(
            "Holding more than one put position; standing down rather than guessing which is this strategy's.",
            puts={s: p.quantity for s, p in puts.items()},
        )

    held_symbol, held_position = next(iter(puts.items())) if puts else (None, None)
    held_qty = held_position.quantity if held_position else 0
    if held_qty not in (0, 1):
        return no(
            f"Unexpected held quantity {held_qty} for {held_symbol} -- this strategy only ever "
            f"holds a single contract; standing down rather than guessing.",
            symbol=held_symbol, held_qty=held_qty,
        )

    meta_base = dict(
        reference_time_et=cfg["reference_time"].strftime("%H:%M"),
        entry_window_start_et=cfg["entry_window_start"].strftime("%H:%M"),
        entry_window_end_et=cfg["entry_window_end"].strftime("%H:%M"),
        fallback_exit_time_et=cfg["fallback_exit_time"].strftime("%H:%M"),
        down_threshold_pct=cfg["down_threshold_pct"],
        target_multiplier=cfg["target_multiplier"],
    )

    # -- held: managing the exit (target sale or fallback liquidation). --
    if held_qty == 1:
        fill_price = held_position.average_cost
        target = _target_price(fill_price, cfg["target_multiplier"])
        quote = ctx.quotes([held_symbol]).get(held_symbol)
        quote_ok = quote is not None and quote.is_executable
        fallback_due = now_time >= cfg["fallback_exit_time"]

        if quote_ok and quote.bid is not None and quote.bid >= target:
            return Decision(
                action="sell", symbol=held_symbol, quantity=1,
                reason=(
                    f"Target reached: live bid {quote.bid:.2f} >= day-limit target {target:.2f} "
                    f"(fill_price={fill_price:.2f} x {cfg['target_multiplier']}, rounded up)."
                ),
                strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                metadata={**meta_base, "fill_price": fill_price, "target": target, "bid": quote.bid, "ask": quote.ask},
            )

        if fallback_due:
            if not quote_ok:
                return no(
                    f"Fallback exit time reached but no live/fresh quote yet for {held_symbol}; "
                    f"retrying next cycle.",
                    symbol=held_symbol, fill_price=fill_price, target=target, **meta_base,
                )
            return Decision(
                action="sell", symbol=held_symbol, quantity=1,
                reason=(
                    f"Fallback exit time {meta_base['fallback_exit_time_et']} reached without the "
                    f"target filling; liquidating at the market (bid={quote.bid:.2f})."
                ),
                strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                metadata={**meta_base, "fill_price": fill_price, "target": target, "bid": quote.bid, "ask": quote.ask},
            )

        return no(
            f"Holding {held_symbol}; target {target:.2f} not yet reached "
            f"(bid={quote.bid if quote_ok else 'n/a'}), before the fallback exit time.",
            symbol=held_symbol, fill_price=fill_price, target=target, **meta_base,
        )

    # -- flat: consider entry, but only once per day. --------------------
    if _traded_today(ctx):
        return no("Already traded today's signal; no re-entry once flat again.", **meta_base)

    if now_time < cfg["entry_window_start"]:
        return no(f"Waiting for the entry window (opens {meta_base['entry_window_start_et']} ET).", **meta_base)

    if now_time >= cfg["entry_window_end"]:
        return no(
            f"Entry window ({meta_base['entry_window_start_et']}-{meta_base['entry_window_end_et']} ET) "
            f"has passed without a qualifying entry; standing down for today.",
            **meta_base,
        )

    reference_price = _reference_price_by_date.get(today)
    if reference_price is None:
        return no("No 9:30 ET reference price recorded yet this session; cannot evaluate displacement.", **meta_base)

    current_price = ctx.snapshot.underlying_price
    displacement_pct = (current_price - reference_price) / reference_price
    down_threshold = cfg["down_threshold_pct"]
    disp_meta = dict(meta_base, reference_price=reference_price, current_price=current_price, displacement_pct=displacement_pct)
    if displacement_pct > -down_threshold:
        return no(
            f"QQQ is not down enough from the 9:30 reference (displacement={displacement_pct:.4%}, "
            f"requires <= -{down_threshold:.2%}); does not qualify.",
            **disp_meta,
        )

    put_row = ctx.snapshot.atm("put")
    if not put_row:
        return no("Qualifying down-day signal, but no quoted ATM put in the current snapshot.", **disp_meta)
    put_sym = put_row["OptionSymbol"]
    quote = ctx.quotes([put_sym]).get(put_sym)
    if quote is None or not quote.is_executable:
        return no(
            "Qualifying down-day signal, but waiting for a live/fresh put quote to enter.",
            symbol=put_sym, **disp_meta,
        )

    return Decision(
        action="buy", symbol=put_sym, quantity=1,
        reason=(
            f"Down-Day 14 signal qualifies (displacement={displacement_pct:.4%} vs 9:30 reference "
            f"{reference_price:.2f}); buying 1 ATM put {put_sym} at the ask."
        ),
        strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
        metadata={**disp_meta, "symbol": put_sym, "bid": quote.bid, "ask": quote.ask, "strike": put_row.get("Strike")},
    )


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
canopus_down_day_14 = register(_decide)
