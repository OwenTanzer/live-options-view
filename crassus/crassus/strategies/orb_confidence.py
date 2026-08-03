"""Opening Range Breakout (ORB) with a two-factor confidence score, on QQQ.

Opening Range Breakout is one of the oldest intraday setups there is: mark
the high/low the underlying trades in during the first N minutes of the
session (the "opening range"), then treat a subsequent print outside that
band as a breakout candidate -- the idea being that a market that can't hold
inside its own first few minutes of price discovery is more likely to
continue in the direction it broke than mean-revert. Like `momentum_qqq`,
the only market data consumed is `ctx.snapshot.underlying_price`, the same
durable QQQ mark every strategy in this repo already reads off the R2
snapshot; no new data source is introduced.

Per-day range, not a rolling window
------------------------------------
Unlike `momentum.PriceHistoryTracker` (a fixed-retention rolling window
shared across the whole trading day, built for a lookback that slides
forward continuously), the opening range is a fixed quantity defined once
per session and then held constant for the rest of the day: "the high/low
observed in the first 15 minutes of *today's* regular session." There is no
sliding here -- once the range is built it does not change again until the
next session. That's a different enough shape from `PriceHistoryTracker`
that this module has its own tiny tracker (`_OpeningRangeTracker` below)
rather than repurposing it: reusing a time-windowed accumulator to represent
a value that's supposed to freeze after 15 minutes would require bolting on
a "stop updating" flag anyway, at which point it's no longer the same
abstraction.

The tracker keys its state to `ctx.now_et.date()` (Eastern-Time-aware, per
the `StrategyContext` contract) -- the first observation recorded on a new
date discards any range being built for a prior date and starts a fresh one.
This is a read-time inference, exactly like `session_phase` itself: nothing
tells the tracker "a new day started," it just notices the date on the next
observation changed. Only observations taken while `ctx.session_phase ==
"open"` are recorded, so pre-market prints (which aren't the regular
session's opening range at all) never pollute it.

The range is considered "established" once *both* (a) at least
`opening_range_minutes` (default 15) have elapsed since the first recorded
observation of the day, and (b) at least `min_range_samples` (default 3)
observations were recorded during that window -- guarding against a
collector outage during the first few minutes producing a "range" built
from a single lucky print. Elapsed time is measured against the first
*recorded* observation's own timestamp (anchored to `snapshot.timestamp`,
never `ctx.now_et`, for the same reason `momentum_qqq._decide` anchors
there -- see that module's docstring), not against session open, so a late
process start doesn't retroactively fabricate range history that was never
observed.

The same stale/duplicate-snapshot dedup discipline as `momentum_qqq` is
used: a snapshot is only recorded into the tracker when its own
`(timestamp, sha256)` differs from the last one recorded, and a
`max_snapshot_age_minutes`-old snapshot is treated as a stale source (no
recording, and a held position is retained rather than closed, "absence of
evidence" the same as `momentum_qqq`'s `stale_source_reason` branch) rather
than a fresh read of "no breakout."

Confidence scoring: an honest, deliberately small stand-in
------------------------------------------------------------
"Confidence scoring" here is two cheap, already-available confirming
factors combined into a 0.0-1.0 score -- not a model, not an ensemble, and
not meant to be mistaken for one. It exists so a breakout candidate can be
ranked/gated by more than "did price cross a line," using data this
strategy already has in hand:

  1. RVOL confirmation (+0.5): if `ctx.snapshot.underlying_market` is
     present, its `freshness == "live"`, `rvol_status == "ok"`, and
     `rvol_multiple >= rvol_floor` (default 1.1), add 0.5. A breakout on
     unusually light volume is a much weaker signal than one accompanied by
     real participation. Missing or stale RVOL data does *not* block the
     trade or count against it -- it simply can't add confidence it hasn't
     earned, the same "absence of evidence isn't evidence against"
     treatment `momentum_qqq`'s VWAP/RVOL gate gives a not-yet-ready gate.
  2. Breakout magnitude (+0.0 to +0.5): how far price has moved past the
     range, expressed as a fraction of the range's own width --
     `(price - range_high) / (range_high - range_low)` for a bullish
     breakout, mirrored for bearish -- clipped to [0, 1] and scaled by 0.5.
     A print that has barely cleared the range by a hair contributes little;
     one that has moved a full range-width past it contributes the max.

A real system built for this would likely fold in more independent,
higher-cost confirming signals (volume profile, multi-timeframe agreement,
a trained classifier) as a proper ensemble; this is deliberately scoped down
to two cheap, already-in-hand factors so the mechanism is auditable in one
reading, matching the rest of this repo's "rules-based first, compare
richer models later without touching the execution path" philosophy (see
`strategy.py`'s module docstring).

Position management mirrors `momentum_qqq._decide_core` in shape: at most
one contract at a time, opened in the direction a confirmed breakout
supports and closed the moment that support goes away (range not yet
established, price back inside the range, or confidence too low) -- same
"absence of evidence" and OCC-symbol/`_close`/`no()` plumbing, intentionally
duplicated rather than shared, for the reasons `trump_whisperer.py`'s
docstring gives.

`_decide_core` takes an already-computed `_BreakoutReading` (see below) and
contains all the decision logic; it has no network or clock dependency and
is what `scripts/verify_orb_confidence.py` exercises directly with
hand-built readings. `_decide` is the wrapper that validates snapshot
freshness/dedup, feeds the per-day tracker, computes the reading, and calls
`_decide_core`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "orb_confidence_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_OPENING_RANGE_MINUTES = 15.0
DEFAULT_MIN_RANGE_SAMPLES = 3
DEFAULT_BREAKOUT_BUFFER_PCT = 0.0005  # 0.05%
DEFAULT_RVOL_FLOOR = 1.1
DEFAULT_MIN_CONFIDENCE = 0.3

# Same rationale as momentum_qqq.DEFAULT_MAX_SNAPSHOT_AGE_MINUTES: the board
# republishes roughly once a minute, so a snapshot timestamp older than this
# by the runner's clock means the collector itself has stalled.
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# held symbol itself rather than looked up in the current snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE: a held position can roll off the
# front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


@dataclass
class _BreakoutReading:
    """What `_decide_core` needs to know about today's opening range and the
    current print, computed by `_decide` from the tracker plus the snapshot.
    """

    range_high: float | None
    range_low: float | None
    opening_range_minutes: float
    elapsed_minutes_since_range_start: float | None
    sample_count: int
    range_established: bool
    current_price: float


class _OpeningRangeTracker:
    """Per-day opening-range high/low, reset on the first observation of a
    new `ctx.now_et.date()` -- see this module's docstring for why this is a
    distinct, deliberately non-reused shape from `PriceHistoryTracker`.
    """

    def __init__(self) -> None:
        self._day: date | None = None
        self._range_start: datetime | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._count: int = 0

    def observe(self, observed_at: datetime, price: float) -> None:
        day = observed_at.date()
        if day != self._day:
            # New trading day (or first observation ever) -- discard
            # whatever range was being built for the prior date and start
            # fresh. Deliberately not "close enough to yesterday" logic:
            # any date change is a new session.
            self._day = day
            self._range_start = observed_at
            self._high = price
            self._low = price
            self._count = 1
            return
        self._high = price if self._high is None else max(self._high, price)
        self._low = price if self._low is None else min(self._low, price)
        self._count += 1

    @property
    def range_start(self) -> datetime | None:
        return self._range_start

    @property
    def high(self) -> float | None:
        return self._high

    @property
    def low(self) -> float | None:
        return self._low

    @property
    def sample_count(self) -> int:
        return self._count


# Shared across every account running this strategy, deliberately -- there is
# one QQQ opening range per day, not one per account, exactly like
# momentum_qqq's module-level `_tracker`.
_tracker = _OpeningRangeTracker()

# (timestamp, sha256) of the most recently *recorded* snapshot, so a board
# read that hasn't actually changed isn't re-appended as if it were new
# information under a new timestamp -- same dedup discipline as
# momentum_qqq._last_recorded_snapshot.
_last_recorded_snapshot: tuple[str, str] | None = None


def _snapshot_observed_at(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _confidence_and_direction(
    reading: _BreakoutReading,
    ctx: StrategyContext,
    *,
    breakout_buffer_pct: float,
    rvol_floor: float,
) -> tuple[str | None, float, dict[str, Any]]:
    """Returns (direction, confidence, meta) where direction is "up", "down",
    or None (price is within the buffer of the range, i.e. no breakout).
    """
    price = reading.current_price
    high = reading.range_high
    low = reading.range_low
    width = high - low

    meta: dict[str, Any] = {}

    direction: str | None = None
    magnitude = 0.0
    if width > 0 and price > high * (1.0 + breakout_buffer_pct):
        direction = "up"
        magnitude = max(0.0, min(1.0, (price - high) / width))
    elif width > 0 and price < low * (1.0 - breakout_buffer_pct):
        direction = "down"
        magnitude = max(0.0, min(1.0, (low - price) / width))

    if direction is None:
        return None, 0.0, meta

    confidence = 0.0

    underlying_market = ctx.snapshot.underlying_market
    rvol_confirmed = (
        underlying_market is not None
        and underlying_market.freshness == "live"
        and underlying_market.rvol_status == "ok"
        and underlying_market.rvol_multiple is not None
        and underlying_market.rvol_multiple >= rvol_floor
    )
    if rvol_confirmed:
        confidence += 0.5
    meta["rvol_confirmed"] = rvol_confirmed
    meta["rvol_multiple"] = underlying_market.rvol_multiple if underlying_market else None
    meta["rvol_status"] = underlying_market.rvol_status if underlying_market else None
    meta["rvol_freshness"] = underlying_market.freshness if underlying_market else None

    confidence += 0.5 * magnitude
    meta["breakout_magnitude"] = magnitude

    return direction, confidence, meta


def _decide_core(
    ctx: StrategyContext,
    reading: _BreakoutReading | None,
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

    if stale_source_reason is not None:
        # Same "absence of evidence" treatment as momentum_qqq's
        # stale_source_reason branch: a stale/unparseable/duplicate source
        # snapshot doesn't tell us the breakout thesis has failed, so a held
        # position is retained, not closed.
        if held_symbol is not None:
            return no(
                f"Market snapshot unavailable or stale ({stale_source_reason}); "
                f"retaining the held {held_type} position rather than closing "
                f"on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}")

    if reading is None:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No opening-range observations recorded yet today.", {},
        )

    meta_base: dict[str, Any] = dict(
        range_high=reading.range_high,
        range_low=reading.range_low,
        opening_range_minutes=reading.opening_range_minutes,
        elapsed_minutes_since_range_start=reading.elapsed_minutes_since_range_start,
        range_sample_count=reading.sample_count,
    )

    params = ctx.params or {}
    min_range_samples = params.get("min_range_samples", DEFAULT_MIN_RANGE_SAMPLES)

    if not reading.range_established:
        elapsed = reading.elapsed_minutes_since_range_start
        elapsed_desc = f"{elapsed:.1f}m" if elapsed is not None else "0.0m"
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Opening range not yet established ({elapsed_desc} elapsed of "
            f"{reading.opening_range_minutes:.0f}m window, "
            f"{reading.sample_count} sample(s) so far).",
            meta_base,
        )

    if reading.sample_count < min_range_samples:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {reading.sample_count} observation(s) recorded during the "
            f"opening range window; need at least {min_range_samples} to "
            f"trust the range.",
            meta_base,
        )

    breakout_buffer_pct = params.get("breakout_buffer_pct", DEFAULT_BREAKOUT_BUFFER_PCT)
    rvol_floor = params.get("rvol_floor", DEFAULT_RVOL_FLOOR)
    min_confidence = params.get("min_confidence", DEFAULT_MIN_CONFIDENCE)

    direction, confidence, factor_meta = _confidence_and_direction(
        reading, ctx, breakout_buffer_pct=breakout_buffer_pct, rvol_floor=rvol_floor,
    )
    meta_base = {
        **meta_base,
        "breakout_direction": direction,
        "confidence": confidence,
        **factor_meta,
    }

    if direction is None:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Price {reading.current_price} is within the opening range "
            f"[{reading.range_low}, {reading.range_high}] (buffer "
            f"{breakout_buffer_pct:.4%}); no breakout.",
            meta_base,
        )

    if confidence < min_confidence:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Breakout {direction} detected but confidence {confidence:.2f} "
            f"is below the required {min_confidence:.2f}.",
            meta_base,
        )

    supported_type = "call" if direction == "up" else "put"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; breakout "
                f"{direction} still supports it (confidence={confidence:.2f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Breakout now points {direction} (confidence={confidence:.2f}); "
                f"closing stale {held_type} position before considering a "
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
            f"Opening-range breakout {direction} (range=[{reading.range_low}, "
            f"{reading.range_high}], confidence={confidence:.2f}); opening "
            f"one {supported_type}."
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


def _build_reading(
    ctx: StrategyContext,
    *,
    opening_range_minutes: float,
) -> _BreakoutReading | None:
    if _tracker.range_start is None:
        return None
    elapsed_minutes = (ctx.now_et - _tracker.range_start).total_seconds() / 60.0
    range_established = elapsed_minutes >= opening_range_minutes
    return _BreakoutReading(
        range_high=_tracker.high,
        range_low=_tracker.low,
        opening_range_minutes=opening_range_minutes,
        elapsed_minutes_since_range_start=elapsed_minutes,
        sample_count=_tracker.sample_count,
        range_established=range_established,
        current_price=ctx.snapshot.underlying_price,
    )


def _decide(ctx: StrategyContext) -> Decision:
    global _last_recorded_snapshot

    if ctx.session_phase != "open":
        return _decide_core(ctx, None)

    params = ctx.params or {}
    opening_range_minutes = params.get("opening_range_minutes", DEFAULT_OPENING_RANGE_MINUTES)
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

    reading = _build_reading(ctx, opening_range_minutes=opening_range_minutes)
    return _decide_core(ctx, reading)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
orb_confidence_qqq = register(_decide)
