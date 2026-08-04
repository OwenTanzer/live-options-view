"""Near-the-money open-interest skew: a directional order-flow tell for QQQ.

Where the sibling `put_call_ratio` strategy sums OpenInterest across the
*entire* chain into one aggregate put/call ratio and reads it *contrarian*
(a crowd overwhelmingly long puts is priced for a move that, if it doesn't
happen, unwinds violently the other way), this strategy narrows its view to
strikes within a `near_money_pct` band of the current underlying price --
the handful of strikes actually in play for pinning, dealer hedging flows,
and same-day directional bets -- and reads the resulting imbalance
*directionally* instead. Far-dated, far-OTM open interest tends to be
stale hedges, spread legs, and structural positioning with no near-term
directional content; it dilutes a chain-wide ratio without saying much
about where price is likely to go *today*. Concentrated near-money OI is a
different animal: a large, fresh position built right around the current
price is a more plausible informed/large-account bet on where the
underlying gets pulled or pinned, so this strategy leans *with* it --
heavy near-money call OI buildup relative to puts is read as bullish
(buy a call), heavy near-money put OI buildup as bearish (buy a put) --
rather than fading it. It is also mechanistically distinct from the
`gex` sibling strategy, which gamma-weights OI into a dealer-exposure
estimate; this strategy never touches Gamma at all, only raw OpenInterest
counts within the band.

A single snapshot's near-money imbalance is a weak signal on its own,
though, because QQQ's 0DTE/near-dated OI is *naturally* skewed at the
open most days -- yesterday's expiring positions, overnight hedges, and
routine call-heavy retail flow can leave the book looking directionally
loaded before a single share trades that day. A strategy that only checks
"is the imbalance beyond some absolute threshold right now" would see that
baseline skew and treat it as a signal on every session, which is a false
positive by construction. So this strategy also tracks the imbalance
ratio's own history across the session (`SessionImbalanceTracker`, the
same rolling-observation shape as `momentum_qqq`'s `PriceHistoryTracker`:
one observation per distinct, newer `snapshot.timestamp`, reset when the
session's calendar date changes) and requires the *current* imbalance to
both clear the absolute threshold (`imbalance_threshold`) and have moved
by at least `min_change_from_session_start` from the session's earliest
recorded reading. A book that opened skewed and has stayed exactly that
skewed all day is telling us less than one that started closer to flat
and skewed sharply intraday -- the latter looks more like something
actually happening right now, the former looks like furniture.

Be honest about what this is: raw OpenInterest does not distinguish
opening trades from closing trades, nor who initiated a trade (buyer vs.
seller), nor whether size came from one large account or many small ones
converging by coincidence. This strategy cannot see any of that -- it is
a heuristic proxy built entirely from public, lagged OI snapshots, not a
read of real order flow. The near-money band and the session-drift
requirement are both attempts to filter out the noisiest false positives
given that limitation, not a claim that what remains is verified informed
trading.

Position management -- one contract at a time, closed the moment the
signal stops supporting it, book-derived holding state rather than
re-derived ATM matching -- mirrors `reddit_sentiment_qqq` /
`trump_whisperer_qqq` / `momentum_qqq` exactly; see those modules for the
fuller case for that shape. `_decide_core` takes an already-computed
`SessionImbalanceTracker` and has no network dependency; it is what
`scripts/verify_oi_skew.py` exercises directly. `_decide` is the thin
per-account wrapper the registry calls, holding one tracker per process.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "oi_skew_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_NEAR_MONEY_PCT = 0.02
DEFAULT_IMBALANCE_THRESHOLD = 0.3
DEFAULT_MIN_CHANGE_FROM_SESSION_START = 0.1
DEFAULT_MIN_BAND_STRIKES = 4

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself rather than looked up in the current snapshot, same
# reasoning as reddit_sentiment._OCC_TYPE_RE: a held position can roll off
# the front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


class SessionImbalanceTracker:
    """Tracks the near-money imbalance ratio's own history across one session.

    Anchored to `snapshot.timestamp` (the collector's own clock), not local
    wall time, so a skewed laptop clock can't distort staleness detection --
    the same discipline `market.Quote.age_seconds` uses against the server's
    own timestamps rather than the caller's. One observation is kept per
    distinct, strictly-newer timestamp; a re-fetch of a snapshot the
    collector hasn't yet republished (same or an out-of-order timestamp) is
    treated as no new information, not as a fresh reading of an unchanged
    ratio -- the same duplicate/stale-snapshot dedup discipline as
    `momentum_qqq`'s `PriceHistoryTracker`. The session resets when the
    snapshot's calendar date (the first 10 characters of an ISO timestamp)
    changes, so a prior day's session-start anchor never leaks into today's
    drift calculation.
    """

    def __init__(self) -> None:
        self._session_key: str | None = None
        self._observations: list[tuple[str, float]] = []  # (timestamp, imbalance), timestamp-ascending

    @staticmethod
    def _session_key_for(timestamp: str) -> str:
        return timestamp[:10]

    def observe(self, timestamp: str, imbalance: float) -> bool:
        """Record one (timestamp, imbalance) reading if it is new information.

        Returns False -- and records nothing -- for a falsy timestamp, or a
        timestamp that is not strictly newer than the most recently recorded
        one. Returns True once the reading is recorded.
        """
        if not timestamp:
            return False

        session_key = self._session_key_for(timestamp)
        if session_key != self._session_key:
            self._session_key = session_key
            self._observations = []

        if self._observations and timestamp <= self._observations[-1][0]:
            return False

        self._observations.append((timestamp, imbalance))
        return True

    @property
    def session_start_imbalance(self) -> float | None:
        if not self._observations:
            return None
        return self._observations[0][1]

    @property
    def has_history(self) -> bool:
        return bool(self._observations)


def _near_money_oi(snapshot: Any, near_money_pct: float) -> tuple[float, float, int]:
    """Sum call/put OpenInterest within `near_money_pct` of the underlying.

    Returns (call_oi, put_oi, band_strikes), where band_strikes counts the
    distinct strikes in the band that carry any OpenInterest at all (either
    side) -- the coverage check that guards against reading a "signal" out
    of one or two thinly-populated strikes.
    """
    underlying = snapshot.underlying_price
    call_oi = 0.0
    put_oi = 0.0
    strikes_with_oi: set[float] = set()
    if not underlying:
        return call_oi, put_oi, 0

    for row in snapshot.rows:
        strike = row.get("Strike")
        if strike is None:
            continue
        if abs(strike - underlying) / underlying > near_money_pct:
            continue
        oi = row.get("OpenInterest") or 0
        if oi <= 0:
            continue
        strikes_with_oi.add(strike)
        option_type = row.get("Type")
        if option_type == "call":
            call_oi += oi
        elif option_type == "put":
            put_oi += oi

    return call_oi, put_oi, len(strikes_with_oi)


def _decide_core(ctx: StrategyContext, tracker: SessionImbalanceTracker) -> Decision:
    def no(reason: str, **meta: Any) -> Decision:
        return Decision.no_trade(
            reason=reason,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata=meta or None,
        )

    params = ctx.params or {}
    near_money_pct = params.get("near_money_pct", DEFAULT_NEAR_MONEY_PCT)
    imbalance_threshold = params.get("imbalance_threshold", DEFAULT_IMBALANCE_THRESHOLD)
    min_change = params.get("min_change_from_session_start", DEFAULT_MIN_CHANGE_FROM_SESSION_START)
    min_band_strikes = params.get("min_band_strikes", DEFAULT_MIN_BAND_STRIKES)

    snapshot = ctx.snapshot
    call_oi, put_oi, band_strikes = _near_money_oi(snapshot, near_money_pct)
    total_oi = call_oi + put_oi
    imbalance = (call_oi - put_oi) / total_oi if total_oi > 0 else None

    timestamp = getattr(snapshot, "timestamp", None)
    recorded = False
    if imbalance is not None and timestamp:
        recorded = tracker.observe(timestamp, imbalance)

    session_start = tracker.session_start_imbalance
    delta = (imbalance - session_start) if (imbalance is not None and session_start is not None) else None

    meta_base = dict(
        near_money_call_oi=call_oi,
        near_money_put_oi=put_oi,
        imbalance_ratio=imbalance,
        session_start_imbalance=session_start,
        delta_from_session_start=delta,
        band_strikes=band_strikes,
    )

    if ctx.session_phase != "open":
        return no(
            f"Market is {ctx.session_phase}; execution quotes will be stale.",
            session_phase=ctx.session_phase,
            **meta_base,
        )

    open_positions = _open_positions(ctx)
    if len(open_positions) > 1:
        return no(
            "Holding more than one open option position; standing down "
            "rather than guessing which one this strategy owns.",
            open_positions={s: p.quantity for s, p in open_positions.items()},
            **meta_base,
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
                **meta_base,
            )
        held_type = _option_type_from_symbol(held_symbol)
        if held_type is None:
            return no(
                f"Held symbol {held_symbol} has an unrecognized option "
                f"type; standing down rather than guessing whether to "
                f"close it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )

    # A computable-but-unrecorded reading (missing timestamp, or a
    # duplicate/out-of-order one the collector hasn't yet refreshed) is an
    # absence of a *new* observation, not evidence the signal has changed --
    # same treatment as trump_whisperer's fetch-error branch. Retain
    # whatever is held rather than closing on a stale/missing snapshot.
    if imbalance is not None and not recorded:
        stale_reason = (
            "Snapshot has no usable timestamp; cannot confirm this is a new "
            "observation."
            if not timestamp
            else
            f"Snapshot timestamp {timestamp!r} is not newer than the last "
            f"recorded reading; treating as a stale/duplicate snapshot, not "
            f"a new observation."
        )
        if held_symbol is not None:
            return no(
                f"{stale_reason} Retaining the held {held_type} position "
                f"rather than closing on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return no(stale_reason, **meta_base)

    # Genuinely insufficient band coverage: no OI in the band at all, or too
    # few strikes carrying any. This is a read of the current book, not a
    # missing observation, so it closes an unsupported position the same
    # way an insufficient-sample or neutral sentiment reading does in the
    # sibling sentiment strategies.
    if imbalance is None or band_strikes < min_band_strikes:
        reason = (
            f"No open interest in the near-money band "
            f"(+/-{near_money_pct:.1%} of underlying)."
            if imbalance is None
            else
            f"Only {band_strikes} near-money strike(s) carry open interest "
            f"(need {min_band_strikes}); insufficient band coverage for a "
            f"signal."
        )
        return _maybe_close_unsupported(ctx, held_symbol, held_quantity, held_type, no, reason, meta_base)

    if abs(imbalance) < imbalance_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Near-money imbalance ({imbalance:.3f}) is below the "
            f"{imbalance_threshold} threshold.",
            meta_base,
        )

    if delta is None or abs(delta) < min_change:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Near-money imbalance ({imbalance:.3f}) hasn't moved enough "
            f"from the session-start reading ({session_start}) -- "
            f"delta={delta}, need >= {min_change}; likely stale/structural "
            f"skew rather than fresh positioning.",
            meta_base,
        )

    supported_type = "call" if imbalance > 0 else "put"
    tell = "bullish" if supported_type == "call" else "bearish"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; near-money "
                f"skew still supports it (imbalance={imbalance:.3f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Near-money skew now favors {supported_type}s "
                f"(imbalance={imbalance:.3f}); closing stale {held_type} "
                f"position before considering a new one."
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
            f"Near-money OI skew is {tell} (imbalance={imbalance:.3f}, "
            f"call_oi={call_oi:.0f}, put_oi={put_oi:.0f}, moved "
            f"{delta:.3f} from session start); opening one {supported_type}."
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


_session_tracker = SessionImbalanceTracker()


def _decide(ctx: StrategyContext) -> Decision:
    return _decide_core(ctx, _session_tracker)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
oi_skew_qqq = register(_decide)
