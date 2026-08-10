""""Through the Looking-Glass" -- QQQ opening zero-DTE straddle, core + runner.

Implements the strategy brief at large: buy `num_straddles` (`N`) matched ATM
QQQ 0DTE calls and puts at the open, sell `core_sell_count` (`k`) of each leg
the first time the position's live executable value reaches
`C0 * (1 + core_profit_threshold_pct)` (the core), and let the remaining
`N - k` "runner" straddles ride uncapped until `terminal_exit_time_et`. If the
core never triggers, everything is liquidated at the terminal time instead (a
"no-touch day"). No directional filter, no averaging down, no re-entry once a
day's sequence has traded -- see the strategy brief ("Through the
Looking-Glass -- QQQ Opening Straddle Strategy Brief") for the full
rationale.

**Single-symbol-per-cycle is the one place this strategy diverges from the
brief's "one position" framing.** `Decision`/`ExecutionClient.submit` only
ever place one order for one symbol per account per runner cycle (see
`strategy.py`'s `Decision` and `runner.py`'s `_run_account`) -- there is no
multi-leg atomic order anywhere in this codebase, paper-trading or otherwise.
A straddle is unavoidably two legs, so this strategy is a small state machine
across consecutive cycles instead of one atomic action:

    flat --(buy N calls)--> call leg open --(buy N puts)--> full straddle
      --(core threshold reached: sell k calls)--> core call sold
      --(sell k puts)--> runner (N-k each leg) --(terminal time: sell calls,
      then puts)--> flat

Each arrow is a separate cycle's `Decision`, inferred from `ctx.book`'s
current call/put quantities rather than any state this module keeps itself --
the same "book is the only durable state" discipline every other strategy
here follows, and the only way a restart mid-sequence resumes correctly
instead of forgetting where it was. The practical consequence, on the
deployed runner's 5-minute cadence: the two entry legs (and the two exit
legs) can land a full cycle apart, not near-simultaneously, so the ATM put
strike leg 2 completes at can differ from the strike implied at leg 1's
fill if QQQ moved in between, and the between-legs market can widen. Two
things review flagged and this module now guards against, both only
relevant to the call-leg-held/put-leg-pending window between entry legs 1
and 2:

  * **Leg 2 re-queried "whatever's currently ATM" instead of the specific
    contract matching the held call.** `ctx.snapshot.atm("put")` re-evaluates
    against the *current* underlying price -- if QQQ moved between leg 1's
    fill and leg 2's cycle, this could silently buy a different strike than
    the held call, turning the position into a non-matching strangle
    instead of the intended same-strike straddle. Flagged in review (a P1:
    "the live runner cannot currently execute the strategy semantics this
    PR advertises"). Fixed: `_matching_put_symbol` derives the exact
    expiration+strike put from the held call's own OCC symbol; leg 2 now
    looks that specific symbol up via `ctx.snapshot.by_symbol` and waits
    (rather than substituting a different strike) if it isn't quoted yet.
  * **No second-leg spread guard.** Leg 2 used to buy the put the moment
    *any* executable quote existed, without checking its spread against
    `max_entry_leg_spread_pct` the way leg 1's combined check does. Fixed:
    leg 2 now applies the same spread check before completing the straddle.
  * **No rollback if leg 2 never completes.** A held call leg with no
    matching put is unintended single-leg directional exposure, not the
    straddle the strategy is supposed to hold -- previously nothing bounded
    how long it could sit that way. Fixed: if more than
    `max_leg_completion_wait_minutes` (default 10) has elapsed since the
    call leg's own recorded fill time (read from `ctx.book.trades`' `ts`,
    same source `_traded_today` below already reads) without completing leg
    2, the calls are sold back to flatten rather than left open indefinitely.
    This also now covers "the exact matching put never becomes quoted at
    all" -- that case waits every cycle just like a missing live quote does,
    so it converges to the same timeout-driven rollback instead of a
    separate code path.

A true multi-leg combo order would remove the gap between legs entirely, but
that is a server/execution-layer change, not a strategy one.

**Terminal exit is deliberately earlier than a bare "15 minutes before
close" reading of the brief would suggest**, and this is a cross-PR
consideration, not an oversight: this repo also has a mandatory end-of-day
flatten (`flatten.maybe_flatten`, see PR introducing `crassus/flatten.py`)
that by default force-closes every account's positions
`flatten_minutes_before_close` (15 by default) before the real close. This
strategy's own two-cycle terminal unwind (sell calls, then puts one cycle
later) needs to *finish*, not just *start*, before that blanket flatten
would otherwise preempt it and hand the exit to a control that isn't aware
of the straddle's two-leg shape. `DEFAULT_TERMINAL_EXIT_TIME_ET` is set to
15:30, not 15:55, specifically to leave that margin -- flagged in review as
a real conflict at the old default (a two-cycle unwind starting at 15:55
could still be mid-sequence, holding a naked put, when 16:00 arrives).

**Construction is ATM straddle only.** The brief requires symmetric OTM
strangles to be tested as a *separate, separately labeled* construction --
this module does not implement one; running a strangle test means writing
`looking_glass_strangle.py` alongside this, not a params flag here, so the
two are never silently conflated in a fills log.

**Validation-grid params are still open questions, not tuned defaults.** The
values below are one representative point in the brief's own sweep (entry
09:30, terminal 15:55, eta 20%, N=4/k=3 -- one of the brief's own worked
capital-recovery rows), not a result of backtesting anything. Treat every
one of them as provisional until the brief's validation protocol has
actually run.
"""

from __future__ import annotations

import re
from datetime import datetime, time as dt_time
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "looking_glass_straddle"
STRATEGY_VERSION = "1.0.0"

DEFAULT_ENTRY_TIME_ET = "09:30"
DEFAULT_TERMINAL_EXIT_TIME_ET = "15:30"  # see module docstring: margin before PR #59's 15:45 mandatory flatten
DEFAULT_CORE_PROFIT_THRESHOLD_PCT = 0.20  # eta
DEFAULT_NUM_STRADDLES = 4  # N
DEFAULT_CORE_SELL_COUNT = 3  # k -- the brief's own "+33.3%" row, 1 runner left
DEFAULT_MAX_LEG_SPREAD_PCT = 0.15  # each leg's own (ask-bid)/mid, not combined
DEFAULT_MAX_LEG_COMPLETION_WAIT_MINUTES = 10.0  # rollback the call leg if the put leg never completes

# Same OCC-suffix parse as momentum_qqq._OCC_TYPE_RE / trump_whisperer's --
# duplicated deliberately, not shared, so a held position is still
# recognized after its contract rolls off the front of the current chain.
# See momentum_qqq.py's module docstring for the fuller rationale.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


# Same expiration+strike, opposite side, derived directly from the OCC
# symbol rather than re-querying "whatever's currently ATM" for leg 2 --
# flagged in review: re-querying ATM independently at leg 2 can drift to a
# different strike than the held call implies if the underlying moved
# between the two entry legs' fills (a real risk on the deployed runner's
# 5-minute cadence), silently turning the position into a non-matching
# strangle instead of the intended same-strike straddle.
_CALL_TO_PUT_RE = re.compile(r"^(.*\d{6})C(\d{8})$")


def _matching_put_symbol(call_symbol: str) -> str | None:
    match = _CALL_TO_PUT_RE.match(call_symbol)
    return f"{match.group(1)}P{match.group(2)}" if match else None


def _parse_hhmm(value: Any, default: str) -> dt_time:
    """`"HH:MM"` -> `time`, falling back to parsing `default` on anything
    else -- same "bad params must not silently defeat a mandatory control"
    posture as `flatten._resolve_window_minutes`, not a raise."""
    for candidate in (value, default):
        if isinstance(candidate, str):
            try:
                return datetime.strptime(candidate, "%H:%M").time()
            except ValueError:
                continue
    return datetime.strptime(default, "%H:%M").time()


def _resolve_params(params: dict[str, Any]) -> dict[str, Any]:
    def _positive_int(value: Any, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return n if n > 0 else default

    def _positive_float(value: Any, default: float) -> float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        if f != f or f in (float("inf"), float("-inf")) or f <= 0:  # NaN/inf/non-positive
            return default
        return f

    num_straddles = _positive_int(params.get("num_straddles"), DEFAULT_NUM_STRADDLES)
    if num_straddles < 2:
        # A runner requires at least one straddle left after the core sale;
        # N=1 has nothing left to retain. Fall back rather than run a
        # variant of the strategy the brief doesn't describe.
        num_straddles = DEFAULT_NUM_STRADDLES

    core_sell_count = _positive_int(params.get("core_sell_count"), DEFAULT_CORE_SELL_COUNT)
    core_sell_count = min(max(core_sell_count, 1), num_straddles - 1)

    entry_time = _parse_hhmm(params.get("entry_time_et"), DEFAULT_ENTRY_TIME_ET)
    terminal_time = _parse_hhmm(params.get("terminal_exit_time_et"), DEFAULT_TERMINAL_EXIT_TIME_ET)
    if entry_time >= terminal_time:
        entry_time = _parse_hhmm(None, DEFAULT_ENTRY_TIME_ET)
        terminal_time = _parse_hhmm(None, DEFAULT_TERMINAL_EXIT_TIME_ET)

    return {
        "num_straddles": num_straddles,
        "core_sell_count": core_sell_count,
        "entry_time": entry_time,
        "terminal_time": terminal_time,
        "core_profit_threshold_pct": _positive_float(
            params.get("core_profit_threshold_pct"), DEFAULT_CORE_PROFIT_THRESHOLD_PCT
        ),
        "max_leg_spread_pct": _positive_float(
            params.get("max_entry_leg_spread_pct"), DEFAULT_MAX_LEG_SPREAD_PCT
        ),
        "max_leg_completion_wait_minutes": _positive_float(
            params.get("max_leg_completion_wait_minutes"), DEFAULT_MAX_LEG_COMPLETION_WAIT_MINUTES
        ),
    }


def _traded_today(ctx: StrategyContext) -> bool:
    """Whether this account already has a fill today -- the no-re-entry
    rule's memory. Read from the raw trade records' own `ts` (see
    `worker.js`'s trade shape), not any state this module keeps, so a
    restart mid-day still refuses to re-enter after a completed sequence."""
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


def _leg_fill_time(ctx: StrategyContext, symbol: str) -> datetime | None:
    """The most recent `buy` trade's own timestamp for `symbol`, if the raw
    trade record carries one -- same `ctx.book.trades` shape `_traded_today`
    reads above. Used to bound how long the call leg can sit without a
    matching put before rolling it back. `None` if no matching timed trade
    exists (an unexpected trade shape); callers treat that as "can't
    measure the wait, don't force a rollback on missing data."
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
            fired_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(ctx.now_et.tzinfo)
        except ValueError:
            continue
        if best is None or fired_at > best:
            best = fired_at
    return best


def _leg(ctx: StrategyContext, option_type: str, positions: dict[str, Position]) -> tuple[str | None, int]:
    matches = [(s, p) for s, p in positions.items() if _option_type_from_symbol(s) == option_type]
    if not matches:
        return None, 0
    symbol, position = matches[0]
    return symbol, position.quantity


def _quote_or_none(ctx: StrategyContext, symbol: str):
    quote = ctx.quotes([symbol]).get(symbol)
    return quote if quote is not None and quote.is_executable else None


def _decide(ctx: StrategyContext) -> Decision:
    def no(reason: str, **meta: Any) -> Decision:
        return Decision.no_trade(
            reason=reason, strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION, metadata=meta or None
        )

    if ctx.session_phase != "open":
        return no(f"Market is {ctx.session_phase}; nothing to do.", session_phase=ctx.session_phase)

    cfg = _resolve_params(ctx.params or {})
    n, k = cfg["num_straddles"], cfg["core_sell_count"]
    runner_size = n - k

    positions = {s: p for s, p in ctx.book.positions.items() if p.quantity != 0}
    unrecognized = {s: p.quantity for s, p in positions.items() if _option_type_from_symbol(s) is None}
    if unrecognized:
        return no("Holding position(s) with an unrecognized option type; standing down.", positions=unrecognized)

    calls = {s: p for s, p in positions.items() if _option_type_from_symbol(s) == "call"}
    puts = {s: p for s, p in positions.items() if _option_type_from_symbol(s) == "put"}
    if len(calls) > 1 or len(puts) > 1:
        return no(
            "Holding more than one distinct call or put symbol; standing down rather than guessing which is this strategy's.",
            calls={s: p.quantity for s, p in calls.items()},
            puts={s: p.quantity for s, p in puts.items()},
        )

    call_symbol, call_qty = _leg(ctx, "call", positions)
    put_symbol, put_qty = _leg(ctx, "put", positions)
    if call_qty < 0 or put_qty < 0:
        return no(
            "Unexpected short call/put position; standing down rather than compounding it.",
            call_qty=call_qty, put_qty=put_qty,
        )

    now_time = ctx.now_et.time()
    meta_base = dict(
        num_straddles=n, core_sell_count=k, runner_size=runner_size,
        entry_time_et=cfg["entry_time"].strftime("%H:%M"),
        terminal_exit_time_et=cfg["terminal_time"].strftime("%H:%M"),
        core_profit_threshold_pct=cfg["core_profit_threshold_pct"],
        call_qty=call_qty, put_qty=put_qty,
    )

    # -- terminal / no-touch liquidation: overrides everything else once T is
    # reached, whether or not the core ever sold. -----------------------
    if now_time >= cfg["terminal_time"] and (call_qty > 0 or put_qty > 0):
        if call_qty > 0:
            quote = _quote_or_none(ctx, call_symbol)
            if quote is None:
                return no(
                    f"Terminal exit time reached; no live/fresh quote yet for held calls "
                    f"{call_symbol} to close them -- retrying next cycle.",
                    symbol=call_symbol, **meta_base,
                )
            return Decision(
                action="sell", symbol=call_symbol, quantity=call_qty,
                reason=(
                    f"Terminal exit time {meta_base['terminal_exit_time_et']} reached; "
                    f"liquidating {call_qty} held call(s) {call_symbol} (puts to follow)."
                ),
                strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                metadata={**meta_base, "bid": quote.bid, "ask": quote.ask},
            )
        quote = _quote_or_none(ctx, put_symbol)
        if quote is None:
            return no(
                f"Terminal exit time reached; no live/fresh quote yet for held puts "
                f"{put_symbol} to close them -- retrying next cycle.",
                symbol=put_symbol, **meta_base,
            )
        return Decision(
            action="sell", symbol=put_symbol, quantity=put_qty,
            reason=(
                f"Terminal exit time {meta_base['terminal_exit_time_et']} reached; "
                f"liquidating {put_qty} held put(s) {put_symbol}."
            ),
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata={**meta_base, "bid": quote.bid, "ask": quote.ask},
        )

    # -- flat: either wait for entry time, or refuse to re-enter today. --
    if call_qty == 0 and put_qty == 0:
        if _traded_today(ctx):
            return no("Already traded today's sequence; no re-entry once a day's contracts have flattened.", **meta_base)
        if now_time < cfg["entry_time"]:
            return no(f"Waiting for the entry time ({meta_base['entry_time_et']} ET).", **meta_base)

        call_row = ctx.snapshot.atm("call")
        put_row = ctx.snapshot.atm("put")
        if not call_row or not put_row:
            return no("No quoted ATM call/put in the current snapshot to enter with.", **meta_base)
        call_sym, put_sym = call_row["OptionSymbol"], put_row["OptionSymbol"]

        quotes = ctx.quotes([call_sym, put_sym])
        cq, pq = quotes.get(call_sym), quotes.get(put_sym)
        if cq is None or pq is None or not cq.is_executable or not pq.is_executable:
            return no(
                "Waiting for live/fresh two-sided quotes on both legs before entering.",
                call_symbol=call_sym, put_symbol=put_sym, **meta_base,
            )

        call_mid = (cq.bid + cq.ask) / 2
        put_mid = (pq.bid + pq.ask) / 2
        call_spread_pct = (cq.ask - cq.bid) / call_mid if call_mid > 0 else float("inf")
        put_spread_pct = (pq.ask - pq.bid) / put_mid if put_mid > 0 else float("inf")
        max_spread = cfg["max_leg_spread_pct"]
        if call_spread_pct > max_spread or put_spread_pct > max_spread:
            return no(
                f"Combined market too wide to enter (call spread={call_spread_pct:.3f}, "
                f"put spread={put_spread_pct:.3f}, limit={max_spread}); waiting for a tighter print.",
                call_symbol=call_sym, put_symbol=put_sym,
                call_spread_pct=call_spread_pct, put_spread_pct=put_spread_pct, **meta_base,
            )

        return Decision(
            action="buy", symbol=call_sym, quantity=n,
            reason=(
                f"Opening leg 1/2 of the {n}-straddle opening position: buying {n} calls "
                f"{call_sym}; puts to follow next cycle."
            ),
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata={
                **meta_base, "call_symbol": call_sym, "put_symbol": put_sym,
                "call_bid": cq.bid, "call_ask": cq.ask, "put_bid": pq.bid, "put_ask": pq.ask,
            },
        )

    # -- leg 2 of entry: N calls held, no puts yet. ----------------------
    if call_qty == n and put_qty == 0:
        call_fill_time = _leg_fill_time(ctx, call_symbol)
        if call_fill_time is not None:
            wait_minutes = (ctx.now_et - call_fill_time).total_seconds() / 60.0
            if wait_minutes > cfg["max_leg_completion_wait_minutes"]:
                quote = _quote_or_none(ctx, call_symbol)
                if quote is None:
                    return no(
                        f"Call leg has waited {wait_minutes:.1f}m for a matching put past the "
                        f"{cfg['max_leg_completion_wait_minutes']:g}m rollback limit, but no live/fresh "
                        f"quote yet to sell it back -- retrying next cycle.",
                        symbol=call_symbol, wait_minutes=round(wait_minutes, 2), **meta_base,
                    )
                return Decision(
                    action="sell", symbol=call_symbol, quantity=n,
                    reason=(
                        f"Leg 2 (put) never completed within {cfg['max_leg_completion_wait_minutes']:g}m of the "
                        f"call leg's fill ({wait_minutes:.1f}m elapsed) -- rolling back the unintended "
                        f"naked call exposure rather than leaving it open indefinitely."
                    ),
                    strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                    metadata={**meta_base, "symbol": call_symbol, "wait_minutes": round(wait_minutes, 2), "rollback": True,
                              "bid": quote.bid, "ask": quote.ask},
                )

        put_sym = _matching_put_symbol(call_symbol)
        if put_sym is None:
            return no(
                f"Holding the call leg {call_symbol}, but couldn't derive its matching put symbol "
                f"from an unrecognized OCC format; standing down rather than guessing.",
                symbol=call_symbol, **meta_base,
            )
        put_row = ctx.snapshot.by_symbol(put_sym)
        if not put_row or not put_row.get("Bid") or not put_row.get("Ask"):
            return no(
                f"Holding the call leg; the specific matching put {put_sym} (same strike/expiration "
                f"as the held call) isn't quoted in the current snapshot yet -- waiting rather than "
                f"substituting a different strike.",
                symbol=put_sym, **meta_base,
            )
        pq = _quote_or_none(ctx, put_sym)
        if pq is None:
            return no(
                "Holding the call leg; waiting for a live/fresh put quote to complete leg 2.",
                symbol=put_sym, **meta_base,
            )
        put_mid = (pq.bid + pq.ask) / 2
        put_spread_pct = (pq.ask - pq.bid) / put_mid if put_mid > 0 else float("inf")
        max_spread = cfg["max_leg_spread_pct"]
        if put_spread_pct > max_spread:
            return no(
                f"Holding the call leg; put market too wide to complete leg 2 "
                f"(put spread={put_spread_pct:.3f}, limit={max_spread}); waiting for a tighter print.",
                symbol=put_sym, put_spread_pct=put_spread_pct, **meta_base,
            )
        return Decision(
            action="buy", symbol=put_sym, quantity=n,
            reason=f"Completing leg 2/2 of the opening straddle: buying {n} puts {put_sym} to match {n} held calls.",
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata={**meta_base, "symbol": put_sym, "bid": pq.bid, "ask": pq.ask},
        )

    # -- full straddle held, core not yet sold: watch for the eta target. -
    if call_qty == n and put_qty == n:
        quotes = ctx.quotes([call_symbol, put_symbol])
        cq, pq = quotes.get(call_symbol), quotes.get(put_symbol)
        if cq is None or pq is None or not cq.is_executable or not pq.is_executable:
            return no(
                "Full straddle held; missing a live/fresh quote on one leg, cannot evaluate the core target this cycle.",
                **meta_base,
            )
        call_position, put_position = calls[call_symbol], puts[put_symbol]
        c0 = n * (call_position.average_cost + put_position.average_cost)
        v_t = n * (cq.bid + pq.bid)
        threshold = c0 * (1 + cfg["core_profit_threshold_pct"])
        core_meta = {**meta_base, "c0": c0, "v_t": v_t, "threshold": threshold}
        if v_t < threshold:
            return no(
                f"Core target not yet reached (V_t={v_t:.2f} < threshold={threshold:.2f} "
                f"= C0={c0:.2f} * (1+eta)); holding the full straddle.",
                **core_meta,
            )
        return Decision(
            action="sell", symbol=call_symbol, quantity=k,
            reason=(
                f"Core target reached (V_t={v_t:.2f} >= threshold={threshold:.2f}); "
                f"selling {k} calls (puts to follow), retaining a {runner_size}-straddle runner."
            ),
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata=core_meta,
        )

    # -- core sale in progress: calls already reduced to runner size, puts
    # still at N -- sell k puts to match. -------------------------------
    if call_qty == runner_size and put_qty == n and runner_size < n:
        pq = _quote_or_none(ctx, put_symbol)
        if pq is None:
            return no(
                "Core calls already sold; waiting for a live/fresh put quote to match with the put-side core sale.",
                symbol=put_symbol, **meta_base,
            )
        return Decision(
            action="sell", symbol=put_symbol, quantity=k,
            reason=(
                f"Completing the core sale: selling {k} puts {put_symbol} to match the "
                f"already-sold calls, leaving a {runner_size}-straddle runner in both legs."
            ),
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata={**meta_base, "symbol": put_symbol, "bid": pq.bid, "ask": pq.ask},
        )

    # -- runner-only: core already sold, both legs at N-k. Nothing to do
    # until the terminal-exit check above fires. -------------------------
    if runner_size > 0 and call_qty == runner_size and put_qty == runner_size:
        return no(
            f"Core already sold; holding the {runner_size}-straddle runner until the terminal exit time.",
            **meta_base,
        )

    return no(
        f"Unexpected call/put quantity combination (call_qty={call_qty}, put_qty={put_qty}); "
        f"standing down rather than guessing the next step.",
        **meta_base,
    )


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
looking_glass_straddle = register(_decide)
