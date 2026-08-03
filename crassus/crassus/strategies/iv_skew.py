"""25-delta risk-reversal IV skew, expressed as a contrarian single-leg bet.

**The signal.** A "25-delta risk reversal" is the standard options-desk skew
metric: take the OTM put whose delta is closest to -0.25 and the OTM call
whose delta is closest to +0.25 (roughly equidistant from the money on either
side), and compare their implied vols. `skew = put_iv - call_iv`. Positive
skew is the normal equity/index shape -- puts trade rich to calls because
demand for downside protection (crash insurance) structurally exceeds demand
for upside calls. The risk-reversal's *level* moves with that structural
skew; what this strategy trades is not the level itself but how far the
*current* level has moved from its own recent norm -- see "Session-relative
baseline" below. This repo's collector already publishes `Delta` and `IV`
per row, so no Black-Scholes solver or delta-from-strike approximation is
needed -- selection is a direct nearest-|delta|-to-0.25 search over the
snapshot's rows.

**The single-leg simplification (name it plainly).** A risk reversal is a
*relative-value* signal about two legs at once, and the textbook way to
trade a skew view is a two-leg structure -- a risk-reversal or vertical
spread that is long the cheap side and short the rich side, so the trade's
P&L tracks the skew converging rather than the underlying's outright
direction. `Decision` (see `strategy.py`) carries exactly one `symbol` per
trade -- there is no two-leg primitive in this contract yet. So this
strategy does what `momentum_qqq`, `reddit_sentiment`, and `trump_whisperer`
all do with their own single-signal reads: it takes the volatility read and
expresses it as a single-leg directional options bet instead of the
structurally-correct spread. That is a real simplification, not a detail --
a single long call or put has outright delta and vega exposure to the
underlying and to the *overall* level of IV that a risk reversal would have
hedged away. It is accepted here for the same reason the other P3 strategies
accept their own single-signal-to-single-leg mappings: the point of this
batch is demonstrating one complete strategy-ecology loop per signal, not
building the multi-leg execution primitive first.

**Session-relative baseline, not a fixed skew threshold.** "Positive skew"
is normal and structural, so a fixed absolute threshold (e.g. "trade
whenever put_iv - call_iv > 0.05") would fire on the market's ordinary
resting shape rather than on something unusual happening *today*. Instead,
skew readings are accumulated across the session into a
`skew.PriceHistoryTracker`-backed history (see `crassus/skew.py`; the
tracker class itself is reused unmodified from `momentum.py` -- it is
generic on "a scalar observed at a timestamp," nothing about it is
price-specific), and the current reading is judged by its z-score against
that session's own running mean/stdev (`skew.compute_skew_signal`). This is
the same "relative to its own recent history, not a universal constant"
reasoning `momentum_qqq` applies to trailing returns and `reddit_sentiment`/
`trump_whisperer` apply to their own signals -- a level of skew that would be
unremarkable on one underlying, or on a high-vol day, can be genuinely
extreme relative to what this session itself has been showing.

**The contrarian mapping.** When skew is extremely *rich* relative to the
session baseline (high positive z-score -- puts have gotten unusually
expensive relative to calls, beyond the session's own norm), the read here
is contrarian: that much incremental fear priced into puts, beyond what the
session has otherwise shown, is treated as more likely to mean-revert than
to keep extending, so the trade is a long call (betting the fear was
overdone). When skew is extremely *compressed or inverted* relative to
baseline (low or negative z-score -- put fear premium has richened for calls
instead, or collapsed outright), the mapping flips: a long put. Both
directions require `abs(z_score) >= min_skew_z_threshold` (default 1.5) --
inside that band, the reading is treated as unremarkable and no position is
opened; a warm-up period (`skew.DEFAULT_MIN_SAMPLES` observations) is
required before *any* baseline is trusted at all, same "absence of evidence"
treatment `momentum_qqq` gives its own warming-up state.

**Position management, snapshot freshness, and dedup** all mirror
`momentum_qqq.py` step-for-step (single held contract at most; OCC-symbol
parsing to recognize what's held; close-if-no-longer-supported rather than
just declining; a stale/duplicate source snapshot is treated as an absence
of a fresh read -- retaining a held position rather than closing on missing
data, not the same as a fresh read that genuinely disagrees). See that
module's docstring for the full reasoning; it is not repeated here. What
`iv_skew_qqq` adds on top is a *third* "can't read anything new this cycle"
case that momentum doesn't have: even with a perfectly fresh, non-duplicate
snapshot, the OTM 25-delta put and/or call candidates might not both be
findable (thin/missing IV or Delta on the wings, or a chain too narrow to
have real 25-delta strikes). That is handled the same way as a stale source
snapshot -- an absence of a usable observation, not evidence of anything --
so a held position is retained rather than closed, and nothing is recorded
into the tracker for that cycle.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import PriceHistoryTracker
from ..skew import DEFAULT_MIN_SAMPLES, DEFAULT_RETAIN_MINUTES, SkewSignal, compute_skew_signal
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "iv_skew_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_MIN_SKEW_Z_THRESHOLD = 1.5
DEFAULT_TARGET_DELTA = 0.25

# Same reasoning as momentum_qqq.DEFAULT_MAX_SNAPSHOT_AGE_MINUTES: the board
# republishes roughly once a minute; a snapshot older than this by the
# runner's own clock means the collector itself has stalled.
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# held symbol itself, not looked up in the current chain -- same reasoning as
# momentum_qqq._OCC_TYPE_RE: a held position can roll off the front of the
# chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy -- one QQQ skew reading
# per cycle, not one per account, same as momentum_qqq's module-level tracker.
_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)

# (timestamp, sha256) of the most recently *recorded* snapshot, so an
# unchanged board read isn't re-appended to the tracker under a new timestamp.
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


def _usable(value: Any) -> bool:
    return isinstance(value, (int, float)) and value == value  # excludes None and NaN


def select_25_delta_rows(
    rows: list[dict[str, Any]],
    underlying_price: float,
    target_delta: float = DEFAULT_TARGET_DELTA,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the OTM put and OTM call whose |Delta| is closest to `target_delta`.

    Restricted to rows carrying a usable numeric `Delta` and `IV` (excludes
    `None`, missing keys, and NaN) and to the OTM side for each type (puts
    below the underlying, calls above it) -- a put with a *positive* delta or
    a call with a negative one is not a real OTM candidate and would only
    indicate a bad row. Returns `(put_row, call_row)`; either may be `None`
    if no such row exists in this snapshot.
    """
    puts = [
        r
        for r in rows
        if r.get("Type") == "put"
        and _usable(r.get("Delta"))
        and _usable(r.get("IV"))
        and r["Delta"] < 0
        and r.get("Strike") is not None
        and r["Strike"] < underlying_price
        and r["IV"] > 0
    ]
    calls = [
        r
        for r in rows
        if r.get("Type") == "call"
        and _usable(r.get("Delta"))
        and _usable(r.get("IV"))
        and r["Delta"] > 0
        and r.get("Strike") is not None
        and r["Strike"] > underlying_price
        and r["IV"] > 0
    ]
    put_row = min(puts, key=lambda r: abs(abs(r["Delta"]) - target_delta)) if puts else None
    call_row = min(calls, key=lambda r: abs(r["Delta"] - target_delta)) if calls else None
    return put_row, call_row


def _decide_core(
    ctx: StrategyContext,
    skew_signal: SkewSignal | None,
    *,
    put_iv: float | None = None,
    call_iv: float | None = None,
    put_delta: float | None = None,
    call_delta: float | None = None,
    stale_source_reason: str | None = None,
    candidates_reason: str | None = None,
) -> Decision:
    def no(reason: str, **meta: Any) -> Decision:
        return Decision.no_trade(
            reason=reason,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata=meta or None,
        )

    def meta_for(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        base = dict(
            put_iv=put_iv,
            call_iv=call_iv,
            put_delta=put_delta,
            call_delta=call_delta,
            skew=(put_iv - call_iv) if _usable(put_iv) and _usable(call_iv) else None,
            skew_z_score=skew_signal.z_score if skew_signal else None,
            baseline_mean=skew_signal.baseline_mean if skew_signal else None,
            baseline_stdev=skew_signal.baseline_stdev if skew_signal else None,
            sample_count=skew_signal.sample_count if skew_signal else 0,
            signal_status=skew_signal.status if skew_signal else "no_data",
        )
        if extra:
            base.update(extra)
        return base

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
        # Absence of a fresh observation, not an observation of "neutral" --
        # same reasoning as momentum_qqq's stale_source_reason branch.
        if held_symbol is not None:
            return no(
                f"Market snapshot unavailable or stale ({stale_source_reason}); "
                f"retaining the held {held_type} position rather than closing "
                f"on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}")

    if candidates_reason is not None:
        # The snapshot itself is fresh, but the chain didn't yield both a
        # usable 25-delta put and call this cycle -- also an absence of a
        # usable reading, not a neutral reading, so treated the same way.
        if held_symbol is not None:
            return no(
                f"{candidates_reason} Retaining the held {held_type} "
                f"position rather than closing on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        return no(candidates_reason)

    if skew_signal is None or skew_signal.status == "no_data":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No skew history recorded yet.", meta_for(),
        )

    if skew_signal.status == "warming_up":
        min_samples = (ctx.params or {}).get("min_baseline_samples", DEFAULT_MIN_SAMPLES)
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {skew_signal.sample_count} skew observation(s) so far; "
            f"still warming up to a {min_samples}-sample session baseline.",
            meta_for(),
        )

    params = ctx.params or {}
    min_z = params.get("min_skew_z_threshold", DEFAULT_MIN_SKEW_Z_THRESHOLD)
    z = skew_signal.z_score

    if z is None or abs(z) < min_z:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Skew z-score ({z}) is within the normal session range "
            f"(threshold={min_z}).",
            meta_for(),
        )

    if z >= min_z:
        supported_type = "call"
        rationale = (
            f"Skew is extremely rich vs. its session baseline "
            f"(z={z:.2f} >= {min_z}); reading this as fear priced into puts "
            f"beyond the session norm, likely to mean-revert -- contrarian "
            f"long call."
        )
    else:
        supported_type = "put"
        rationale = (
            f"Skew is extremely compressed/inverted vs. its session "
            f"baseline (z={z:.2f} <= -{min_z}); reading this as put fear "
            f"premium having richened for calls or collapsed outright -- "
            f"long put."
        )

    meta_base = meta_for()

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; skew "
                f"still supports it (z={z:.2f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Skew signal now supports {supported_type} (z={z:.2f}); "
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
        reason=f"{rationale} Opening one ATM {supported_type}.",
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
    max_snapshot_age = params.get("max_snapshot_age_minutes", DEFAULT_MAX_SNAPSHOT_AGE_MINUTES)
    min_samples = params.get("min_baseline_samples", DEFAULT_MIN_SAMPLES)
    target_delta = params.get("target_delta", DEFAULT_TARGET_DELTA)

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

    put_row, call_row = select_25_delta_rows(ctx.snapshot.rows, ctx.snapshot.underlying_price, target_delta)
    if put_row is None or call_row is None:
        missing = []
        if put_row is None:
            missing.append("put")
        if call_row is None:
            missing.append("call")
        return _decide_core(
            ctx, None,
            candidates_reason=(
                f"No usable 25-delta OTM {' or '.join(missing)} found in this "
                f"snapshot (missing Delta/IV, or too narrow a chain)."
            ),
        )

    put_iv = float(put_row["IV"])
    call_iv = float(call_row["IV"])
    put_delta = float(put_row["Delta"])
    call_delta = float(call_row["Delta"])
    skew = put_iv - call_iv

    snapshot_key = (ctx.snapshot.timestamp, ctx.snapshot.sha256)
    if snapshot_key != _last_recorded_snapshot:
        _tracker.observe(observed_at, skew)
        _last_recorded_snapshot = snapshot_key

    skew_signal = compute_skew_signal(_tracker.snapshot(), min_samples=min_samples)
    return _decide_core(
        ctx, skew_signal,
        put_iv=put_iv, call_iv=call_iv, put_delta=put_delta, call_delta=call_delta,
    )


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
iv_skew_qqq = register(_decide)
