"""Put/Call-ratio-driven QQQ options strategy -- a contrarian sentiment read.

The put/call ratio (PCR) is `sum(put OpenInterest) / sum(call OpenInterest)`
across every row in the current 0DTE chain snapshot (`ctx.snapshot.rows`).
It is a standard crowd-positioning gauge: a high PCR means the resting open
interest skews heavily toward puts (the crowd has been buying/writing more
puts than calls); a low PCR means the opposite.

This strategy reads PCR *contrarian*, not directionally, which is the whole
point of it as a signal distinct from a raw directional open-interest skew
strategy. The reasoning is standard crowd-positioning theory: an unusually
put-heavy positioning means most of the crowd that wanted to express a
bearish view already has -- there are fewer bears left to keep pushing the
market down, and comparatively more capacity for a squeeze/short-covering
move higher. Conversely an unusually call-heavy positioning suggests the
bulls are largely already in, leaving relatively more room for disappointment
to the downside. So an *extremely high* PCR is read as contrarian
**bullish** (buy a call) and an *extremely low* PCR is read as contrarian
**bearish** (buy a put) -- the opposite mapping a naive "follow the OI skew"
strategy would use. This is a heuristic with real practitioner following
(PCR is a textbook sentiment indicator) but no guaranteed edge: crowd
positioning can also just be right, and 0DTE dealer hedging flows can
dominate whatever retail sentiment this ratio is trying to capture. Nothing
here should be read as validated alpha.

"Extreme" is judged relative to *a 24h trailing window of prior* PCR
readings, not a fixed absolute PCR level -- see `crassus/pcr.py`'s module
docstring for the full reasoning (same idea `momentum_qqq.py` applies to
price: a 0DTE QQQ PCR baseline is not 0DTE SPX's, and isn't necessarily
stable across sessions either, so a hand-picked universal cutoff would be
mis-calibrated more often than not). Concretely, extremity is a z-score of
the current reading against the mean/stdev of every *prior* reading still
inside the retention window (`pcr.compute_pcr_extremity`) -- 24h by default,
so the baseline is a trailing window that can span more than one session
rather than something that resets at the open; a single session's worth of
readings at the runner's cadence is a thin sample to z-score against.
`extreme_z_threshold` (default 1.5)
is the number of standard deviations required before a reading counts as
extreme enough to trade.

Position management, snapshot-freshness handling and the shared-tracker
dedup discipline are deliberately identical in shape to
`momentum_qqq.momentum_qqq`: at most one contract at a time, opened in the
direction the extremity signal supports and closed the moment that support
goes away (including settling back into the normal/non-extreme band, not
only a flip to the opposite extreme). See `momentum_qqq.py`'s docstring for
why this is the right level of complexity for now, and for why every
recorded PCR observation is anchored to `snapshot.timestamp` (the source's
own clock) rather than `ctx.now_et`, with a duplicate/stale-snapshot guard
so a stalled collector serving the same snapshot repeatedly cannot get
re-recorded as fresh baseline history under an ever-advancing timestamp.

The one PCR-specific wrinkle: a snapshot can have zero call open interest
(e.g. a very early/degenerate chain), which would make the ratio undefined.
That is treated the same as any other unusable-source read -- a
`stale_source_reason`, not a recordable data point -- so a held position is
retained rather than closed on it, and nothing poisons the trailing baseline.

`_decide_core` takes an already-computed `PCRExtremitySignal` (see
`crassus/pcr.py`) plus an optional `stale_source_reason`, and contains all
the decision logic; it has no network or clock dependency and is what
`scripts/verify_put_call_ratio.py` exercises directly with hand-built
signals. `_decide` is the wrapper that computes PCR from the snapshot,
validates the snapshot's own freshness, records a new observation into the
shared tracker when it's a genuinely new and current read, computes the
signal, and calls `_decide_core`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..pcr import (
    DEFAULT_MIN_BASELINE_SAMPLES,
    DEFAULT_RETAIN_MINUTES,
    PCRExtremitySignal,
    PCRHistoryTracker,
    compute_pcr_extremity,
)
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "put_call_ratio_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_EXTREME_Z_THRESHOLD = 1.5

# The board is republished roughly once a minute (market.py); a snapshot
# whose own timestamp is older than this by the runner's clock means the
# collector itself has stalled, not just "hasn't repolled since last cycle."
# Same value and reasoning as momentum_qqq.py.
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE: a held position can roll off the
# front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy, deliberately -- there is
# one trailing window of PCR history, not one per account, exactly like
# momentum_qqq's module-level `_tracker`.
_tracker = PCRHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)

# (timestamp, sha256) of the most recently *recorded* snapshot, so a board
# read that hasn't actually changed -- the collector hasn't republished yet,
# or is stalled -- isn't re-appended to the tracker as if it were new
# information under a new timestamp.
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


def compute_pcr(rows: list[dict[str, Any]]) -> float | None:
    """Put/call ratio for one chain snapshot: sum(put OI) / sum(call OI).

    Returns `None` when call open interest is zero (or the chain has no
    rows at all) -- the ratio is undefined, not zero or infinite, and callers
    must treat that as an unusable read rather than a data point.
    """
    put_oi = sum(r.get("OpenInterest") or 0 for r in rows if r.get("Type") == "put")
    call_oi = sum(r.get("OpenInterest") or 0 for r in rows if r.get("Type") == "call")
    if call_oi <= 0:
        return None
    return put_oi / call_oi


def _decide_core(
    ctx: StrategyContext,
    signal: PCRExtremitySignal | None,
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
        # A stale/unparseable/duplicate/undefined-ratio source read is not
        # evidence sentiment has changed -- it's an absence of a fresh
        # observation, not an observation of "normal" or "unsupported."
        # Same reasoning as momentum_qqq's stale_source_reason branch:
        # retain a held position rather than closing on a missing read.
        if held_symbol is not None:
            return no(
                f"Market snapshot unavailable or stale ({stale_source_reason}); "
                f"retaining the held {held_type} position rather than closing "
                f"on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}")

    if signal is None or signal.status == "no_data":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No PCR history recorded yet.", {},
        )

    meta_base = dict(
        current_pcr=signal.current_pcr,
        baseline_mean=signal.baseline_mean,
        baseline_stdev=signal.baseline_stdev,
        baseline_sample_count=signal.baseline_sample_count,
        sample_count=signal.sample_count,
        z_score=signal.z_score,
        signal_status=signal.status,
    )

    if signal.status == "warming_up":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {signal.baseline_sample_count} prior PCR reading(s) so "
            f"far; still warming up to a {DEFAULT_MIN_BASELINE_SAMPLES}-"
            f"reading 24h trailing baseline.",
            meta_base,
        )

    params = ctx.params or {}
    extreme_z_threshold = params.get("extreme_z_threshold", DEFAULT_EXTREME_Z_THRESHOLD)

    z = signal.z_score
    if z is None or abs(z) < extreme_z_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Current PCR {signal.current_pcr} is within the 24h trailing "
            f"baseline's normal range (z={z}, threshold={extreme_z_threshold}).",
            meta_base,
        )

    # Contrarian mapping: extremely HIGH PCR (excessive put buying, crowd
    # bearish) -> bullish call; extremely LOW PCR (crowd bullish) -> bearish
    # put. See module docstring for the crowd-positioning reasoning.
    supported_type = "call" if z >= extreme_z_threshold else "put"
    direction = "up" if supported_type == "call" else "down"
    crowd_bias = "bearish (put-heavy)" if supported_type == "call" else "bullish (call-heavy)"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; extreme "
                f"PCR still supports it (z={z:.2f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"PCR extremity now points {direction} (z={z:.2f}); closing "
                f"stale {held_type} position before considering a new one."
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
            f"PCR is extremely {'high' if supported_type == 'call' else 'low'} "
            f"(z={z:.2f}, current_pcr={signal.current_pcr}, "
            f"threshold={extreme_z_threshold}) -- crowd positioning looks "
            f"{crowd_bias}, read contrarian as {direction}; opening one "
            f"{supported_type}."
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


def _decide(ctx: StrategyContext) -> Decision:
    global _last_recorded_snapshot

    if ctx.session_phase != "open":
        return _decide_core(ctx, None)

    params = ctx.params or {}
    min_samples = params.get("min_baseline_samples", DEFAULT_MIN_BASELINE_SAMPLES)
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

    pcr = compute_pcr(ctx.snapshot.rows)
    if pcr is None:
        return _decide_core(
            ctx, None,
            stale_source_reason="no call open interest in snapshot; PCR is undefined",
        )

    snapshot_key = (ctx.snapshot.timestamp, ctx.snapshot.sha256)
    if snapshot_key != _last_recorded_snapshot:
        _tracker.observe(observed_at, pcr)
        _last_recorded_snapshot = snapshot_key

    signal = compute_pcr_extremity(_tracker.snapshot(), min_samples=min_samples)
    return _decide_core(ctx, signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
put_call_ratio = register(_decide)
