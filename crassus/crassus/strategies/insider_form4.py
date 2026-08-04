"""Insider Form 4 filing-activity confirmation strategy for QQQ options.

Sources its signal from `crassus.insider_flow.InsiderFlowReader`, which polls
SEC EDGAR's free, public, unauthenticated submissions API
(`https://data.sec.gov/submissions/CIK##########.json`) for a small basket of
QQQ mega-cap components and counts each company's recent Form 4 filings
within a lookback window -- see `insider_flow.py`'s module docstring for the
full ingestion shape, the EDGAR User-Agent requirement, and courtesy caching.

Scope limitation (read this before touching thresholds below): the count
this strategy consumes is an ACTIVITY count, not a directional one. Parsing
whether a given Form 4 reflects a purchase or a sale -- the transaction code,
share count, and price per share -- requires fetching and parsing that
filing's own XML, a heavier per-filing fetch this v1 deliberately does not
do (see `insider_flow.py`'s docstring for what a v2 would add). That means
"elevated Form 4 activity" here can mean a cluster of insiders buying,
selling, or simply exercising and holding options-derived shares near a
vesting date -- there is no way to tell which from this data alone.

Because of that, elevated activity is used only as a **confirming lean**,
never a standalone directional call: it is only acted on when it agrees with
a direction the underlying's own intraday move already shows. The strategy
tracks each session's own opening print (`_SessionOpenTracker`, a small
in-memory map keyed by date -- the first `ctx.snapshot.underlying_price`
observed once the market opens for a given day *is* that day's opening
print; there is no separate historical source for it) and compares the
current underlying price against it. If Form 4 activity across the basket
clears `activity_threshold` (default 3) *and* the resulting intraday move
clears a small noise floor, `direction_threshold_pct` (default 0.1%), in
either direction, that combination is read as "insiders are unusually active
at the same time the tape is already moving this way" and a same-direction
call or put is opened. Elevated activity alone, or a clear intraday move
alone, is not enough -- both must agree.

Every other combination -- activity not elevated, activity elevated but the
session hasn't moved enough to read a direction yet, or the EDGAR fetch
failing outright -- is treated as an absence of evidence, not evidence of
"neutral": a held position is retained rather than closed, unlike
`reddit_sentiment_qqq`/`trump_whisperer_qqq`, where a neutral read of an
always-available sentiment source does close a held position. The
distinction matters here because this signal is one-sided and low-frequency
by construction (Form 4s trickle in over business days) -- a quiet day is
not this strategy re-observing "no signal" the way a fresh sentiment poll
would, it is simply the common case. A held position is only ever closed
when the confirming signal has actually fired for the *other* side.

`_decide_core` takes an already-fetched activity snapshot (or an error
string) plus the session's resolved opening price, and contains all the
actual decision logic; it has no network or session-tracking dependency of
its own and is what `scripts/verify_insider_form4.py` exercises directly.
`_decide` is the thin I/O wrapper the registry calls: it resolves the
session's opening price via `_session_tracker`, fetches the activity
snapshot, and calls `_decide_core`.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..insider_flow import DEFAULT_LOOKBACK_HOURS, InsiderActivitySnapshot, InsiderFlowReader
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "insider_form4_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_ACTIVITY_THRESHOLD = 3
DEFAULT_DIRECTION_THRESHOLD_PCT = 0.001  # 0.10% intraday move, as a fraction

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as reddit_sentiment._OCC_TYPE_RE: a held position can roll off
# the front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

_reader = InsiderFlowReader(lookback_hours=DEFAULT_LOOKBACK_HOURS)


class _SessionOpenTracker:
    """Remembers each session's opening print exactly once per date.

    Deliberately simpler than `momentum.PriceHistoryTracker`: there is no
    rolling series to prune here, just one value per calendar date -- the
    first underlying price observed for that date is that date's opening
    print, and every later observation the same day compares against it,
    never overwrites it.
    """

    def __init__(self) -> None:
        self._by_date: dict[str, float] = {}

    def observe(self, date_key: str, price: float) -> float:
        return self._by_date.setdefault(date_key, price)


_session_tracker = _SessionOpenTracker()


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(
    ctx: StrategyContext,
    activity: InsiderActivitySnapshot | None,
    fetch_error: str | None,
    session_start_price: float | None,
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

    params = ctx.params or {}
    activity_threshold = params.get("activity_threshold", DEFAULT_ACTIVITY_THRESHOLD)
    direction_threshold_pct = params.get("direction_threshold_pct", DEFAULT_DIRECTION_THRESHOLD_PCT)

    current_price = ctx.snapshot.underlying_price if ctx.snapshot is not None else None
    intraday_move_pct = None
    if session_start_price is not None and current_price is not None and session_start_price:
        intraday_move_pct = (current_price - session_start_price) / session_start_price

    def retain(reason: str, **extra: Any) -> Decision:
        # Absence of evidence (fetch failure, activity not elevated, no
        # clear intraday direction yet) is not evidence *against* a held
        # position -- retain it rather than closing on a missing or
        # unsupportive-but-not-disagreeing observation. See module docstring
        # for why this differs from reddit_sentiment/trump_whisperer's
        # "neutral closes" behavior.
        meta = dict(
            total_recent_form4_count=(activity.total_recent_form4_count if activity else None),
            activity_threshold=activity_threshold,
            session_start_price=session_start_price,
            current_price=current_price,
            intraday_move_pct=intraday_move_pct,
            **extra,
        )
        if held_symbol is not None:
            return no(
                f"{reason} Retaining the held {held_type} position rather "
                f"than closing on an unconfirmed or missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return no(reason, **meta)

    if fetch_error is not None:
        return retain(f"Insider filing activity unavailable: {fetch_error}.")

    if activity is None:
        return retain("No insider filing activity snapshot available.")

    total_count = activity.total_recent_form4_count
    elevated = total_count > activity_threshold

    if not elevated:
        return retain(
            f"Recent Form 4 activity across the basket ({total_count}) does "
            f"not clear the activity threshold ({activity_threshold})."
        )

    if intraday_move_pct is None:
        return retain(
            f"Form 4 activity is elevated ({total_count} > {activity_threshold}) "
            f"but no session opening price is available yet to read a direction."
        )

    if abs(intraday_move_pct) <= direction_threshold_pct:
        return retain(
            f"Form 4 activity is elevated ({total_count} > {activity_threshold}) "
            f"but the session hasn't moved enough to read a direction "
            f"(intraday_move_pct={intraday_move_pct:.4f}, "
            f"floor={direction_threshold_pct})."
        )

    supported_type = "call" if intraday_move_pct > 0 else "put"
    direction = "up" if supported_type == "call" else "down"

    meta_base = dict(
        total_recent_form4_count=total_count,
        activity_threshold=activity_threshold,
        session_start_price=session_start_price,
        current_price=current_price,
        intraday_move_pct=intraday_move_pct,
    )

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; elevated "
                f"Form 4 activity ({total_count}) still confirms the "
                f"session's {direction} move (intraday_move_pct="
                f"{intraday_move_pct:.4f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Elevated Form 4 activity ({total_count}) now confirms a "
                f"{direction} session move (intraday_move_pct="
                f"{intraday_move_pct:.4f}), which disagrees with the held "
                f"{held_type} position; closing it before considering a "
                f"new one."
            ),
            meta=meta_base,
            no=no,
        )

    row = ctx.snapshot.atm(supported_type)
    if not row:
        return no(f"No quoted {supported_type} in the snapshot to trade.", **meta_base)
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
            f"Elevated Form 4 activity ({total_count} > {activity_threshold}) "
            f"confirms the session's {direction} move (intraday_move_pct="
            f"{intraday_move_pct:.4f}); opening one {supported_type} as a "
            f"confirming lean, not a standalone call."
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
    if ctx.session_phase != "open":
        return _decide_core(ctx, None, None, None)

    date_key = ctx.now_et.date().isoformat()
    session_start_price = _session_tracker.observe(date_key, ctx.snapshot.underlying_price)

    try:
        activity = _reader.read()
    except Exception as exc:  # InsiderFetchError, network failure, etc.
        return _decide_core(ctx, None, str(exc), session_start_price)
    return _decide_core(ctx, activity, None, session_start_price)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
insider_form4_qqq = register(_decide)
