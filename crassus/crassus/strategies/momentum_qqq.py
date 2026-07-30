"""Time-series-momentum-driven QQQ options strategy.

Trailing return over a configurable lookback window is the textbook
time-series momentum signal (Moskowitz, Ooi & Pedersen (2012), "Time Series
Momentum": regress next-period return on trailing-period return; here the
trailing return's sign/magnitude directly gates a long/short/flat decision
rather than feeding a regression, the same simplification the sentiment
strategies already make). The underlying price used for the trailing return
is `ctx.snapshot.underlying_price` -- the same durable QQQ mark every other
strategy in this repo already reads -- observed once per polling cycle into
a shared, in-memory `PriceHistoryTracker` (see `crassus/momentum.py`); no new
data source or external API is introduced. Cross-sectional momentum (ranking
QQQ's return against a basket of other tickers) isn't used here because this
repo only ever quotes one underlying -- there's no universe to rank against.

Position management is deliberately identical in shape to
`reddit_sentiment.reddit_sentiment_qqq` and `trump_whisperer.trump_whisperer_qqq`:
at most one contract at a time, opened in the direction the trailing return
supports and closed the moment that support goes away -- including when the
return merely settles back into the neutral band, not only when it flips
sign. See those modules' docstrings for why this is the right level of
complexity for now (sizing/hysteresis/cool-downs belong in `ctx.params` or a
later strategy, not platform invariants). `_decide_core` mirrors their
`_decide_core` step-for-step so the three stay easy to compare; the
duplication (OCC-symbol parsing, close/no-trade plumbing) is intentional
isolation, not an oversight -- see `trump_whisperer.py`'s docstring for why.

Unlike the sentiment strategies, there is no network call of its own here --
the price observation comes from `ctx.snapshot`, which the runner has already
fetched. But that snapshot is itself a poll of a durable board the collector
republishes on its own ~60s cadence (see `market.py`'s module docstring), so
it can go stale independently of whether the runner's own poll succeeded: a
collector outage means the runner keeps getting *a* snapshot every cycle,
just the same one, over and over. `_decide` therefore anchors every recorded
observation to `snapshot.timestamp` -- the source's own clock -- never to
`ctx.now_et`, and skips recording entirely when either the timestamp hasn't
advanced since the last recorded read (`sha256` used as a tie-breaker in case
a timestamp ever repeats across genuinely different content) or it's already
more than `max_snapshot_age_minutes` old by the runner's clock. Using
`ctx.now_et` instead would have let a single frozen price get re-recorded at
an ever-advancing timestamp every cycle, letting the strategy compute and
trade on a fabricated lookback return from data that never actually moved.

What this strategy has instead of a fetch-error path is a warm-up/staleness
problem the sentiment strategies don't: on the first calls of a session there
isn't yet a price point `lookback_minutes` old to compare against
("warming_up"), and if polling was interrupted for a stretch -- an outage, an
overnight or weekend gap -- the oldest available anchor can be far older than
the lookback window actually calls for ("stale_anchor"). Both, like a stale
source snapshot, are treated as "no usable signal": closed positions get
closed, flat stays flat, and neither is treated as evidence of anything. A
stale *source* snapshot is the one case treated like the sentiment
strategies' fetch_error instead -- a held position is retained rather than
closed, because a collector outage is an absence of a fresh observation, not
a fresh observation that happens to be neutral.

`_decide_core` takes an already-computed `MomentumSignal` (see
`crassus/momentum.py`) plus an optional `stale_source_reason`, and contains
all the decision logic; it has no network or clock dependency and is what
`scripts/verify_momentum_qqq.py` exercises directly with hand-built signals.
`_decide` is the wrapper that validates the snapshot's own freshness, records
its price into the shared tracker when it's a genuinely new and current read,
computes the signal, and calls `_decide_core`.

Two optional params, both default off so no deployed account's behavior
changes until an operator opts in: `vwap_confirmation_required` (bool) vetoes
an otherwise-supported direction unless the underlying is on the agreeing
side of its own VWAP; `rvol_floor` (float) vetoes it unless relative volume
clears the floor. Both read from `ctx.snapshot.underlying_market` (see
`crassus/crassus/market.py`) via `crassus/crassus/vwap_rvol.py`'s
`evaluate_gate` -- see that module's docstring for why VWAP/RVOL evaluation
is a separate module from momentum's own math. The gate can only veto an
already-supported direction, never manufacture one, and a gate that hasn't
looked yet (RVOL still building its baseline, or a missing snapshot field)
retains a held position rather than closing it, same "absence of evidence
isn't evidence against" treatment as `stale_source_reason` above -- not the
same as a gate that has looked and genuinely disagrees, which does close.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..vwap_rvol import evaluate_gate
from ..momentum import (
    DEFAULT_LOOKBACK_MINUTES,
    DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES,
    DEFAULT_RETAIN_MINUTES,
    MomentumSignal,
    PriceHistoryTracker,
    compute_momentum,
)
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "momentum_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_BULLISH_THRESHOLD = 0.003  # +0.30% trailing return
DEFAULT_BEARISH_THRESHOLD = -0.003  # -0.30% trailing return

# The board is republished roughly once a minute (market.py); a snapshot
# whose own timestamp is older than this by the runner's clock means the
# collector itself has stalled, not just "hasn't repolled since last cycle."
# Generous relative to the ~60s cadence so ordinary jitter doesn't trip it,
# but tight enough to catch a real outage well before it could distort an
# hour-scale lookback.
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as reddit_sentiment._OCC_TYPE_RE and trump_whisperer._OCC_TYPE_RE:
# a held position can roll off the front of the chain while still needing to
# be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy, deliberately -- there is
# one QQQ underlying price, not one per account, exactly like trump_whisperer's
# module-level `_reader`.
_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)

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


def _decide_core(
    ctx: StrategyContext,
    signal: MomentumSignal | None,
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
        # A stale/unparseable/duplicate source snapshot is not evidence
        # momentum has changed -- it's an absence of a fresh observation, not
        # an observation of "neutral" or "unsupported." Same reasoning as
        # trump_whisperer's fetch_error branch: retain a held position rather
        # than closing on a missing read, unlike the valid-empty-result cases
        # below (warming up, stale anchor, neutral), which do still close
        # because those genuinely are a read of current momentum, just one
        # that doesn't support a position.
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
            "No price history recorded yet.", {},
        )

    meta_base = dict(
        lookback_minutes=signal.lookback_minutes,
        current_price=signal.current_price,
        anchor_price=signal.anchor_price,
        return_pct=signal.return_pct,
        sample_count=signal.sample_count,
        anchor_age_minutes=signal.anchor_age_minutes,
        signal_status=signal.status,
    )

    if signal.status == "warming_up":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {signal.sample_count} price observation(s) so far; still "
            f"warming up to a {signal.lookback_minutes:.0f}-minute lookback.",
            meta_base,
        )

    if signal.status == "stale_anchor":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Nearest usable price anchor is "
            f"{signal.anchor_age_minutes:.1f} minutes old -- a gap in "
            f"observations makes it too stale to trust for a "
            f"{signal.lookback_minutes:.0f}-minute lookback.",
            meta_base,
        )

    params = ctx.params or {}
    bullish_threshold = params.get("bullish_threshold", DEFAULT_BULLISH_THRESHOLD)
    bearish_threshold = params.get("bearish_threshold", DEFAULT_BEARISH_THRESHOLD)

    ret = signal.return_pct
    if ret is None or bearish_threshold < ret < bullish_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Trailing return over the last {signal.anchor_age_minutes:.0f}m "
            f"(target lookback {signal.lookback_minutes:.0f}m) is neutral "
            f"(return_pct={ret}).",
            meta_base,
        )

    supported_type = "call" if ret >= bullish_threshold else "put"
    direction = "up" if supported_type == "call" else "down"

    vwap_confirmation_required = params.get("vwap_confirmation_required", False)
    rvol_floor = params.get("rvol_floor", None)
    if vwap_confirmation_required or rvol_floor is not None:
        gate = evaluate_gate(
            ctx.snapshot.underlying_market, direction,
            rvol_floor=rvol_floor, require_vwap_agreement=vwap_confirmation_required,
        )
        gate_meta = dict(
            meta_base,
            vwap_gate_status=gate.status,
            vwap=gate.vwap,
            price_vs_vwap_pct=gate.price_vs_vwap_pct,
            vwap_agrees=gate.vwap_agrees,
            rvol_multiple=gate.rvol_multiple,
            rvol_floor=rvol_floor,
            rvol_participation_ok=gate.rvol_participation_ok,
        )
        if gate.status != "ok":
            # No trustworthy VWAP/RVOL reading yet (RVOL still building its
            # baseline, or the underlying market data itself is missing) --
            # an absence of a fresh gate observation, not a gate that has
            # actually looked and disagreed. Same reasoning as
            # stale_source_reason above: retain a held position, don't act
            # on nothing.
            if held_symbol is not None:
                return no(
                    f"VWAP/RVOL confirmation data unavailable "
                    f"(status={gate.status}); retaining the held "
                    f"{held_type} position rather than closing on a "
                    f"missing observation.",
                    symbol=held_symbol,
                    held_quantity=held_quantity,
                    **gate_meta,
                )
            return no(
                f"VWAP/RVOL confirmation data unavailable (status={gate.status}).",
                **gate_meta,
            )
        if vwap_confirmation_required and not gate.vwap_agrees:
            return _maybe_close_unsupported(
                ctx, held_symbol, held_quantity, held_type, no,
                f"VWAP does not confirm {direction} momentum "
                f"(price_vs_vwap_pct={gate.price_vs_vwap_pct}).",
                gate_meta,
            )
        if rvol_floor is not None and not gate.rvol_participation_ok:
            return _maybe_close_unsupported(
                ctx, held_symbol, held_quantity, held_type, no,
                f"RVOL {gate.rvol_multiple} is below the required floor "
                f"{rvol_floor}.",
                gate_meta,
            )
        meta_base = gate_meta

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; momentum "
                f"still supports it (return_pct={ret:.4f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Momentum now points {direction} (return_pct={ret:.4f}); "
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
            f"Momentum points {direction} (return_pct={ret:.4f} over the "
            f"last {signal.anchor_age_minutes:.0f}m, target lookback "
            f"{signal.lookback_minutes:.0f}m, n={signal.sample_count}); "
            f"opening one {supported_type}."
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
    lookback_minutes = params.get("lookback_minutes", DEFAULT_LOOKBACK_MINUTES)
    max_overshoot = params.get("max_anchor_overshoot_minutes", DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES)
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

    signal = compute_momentum(
        _tracker.snapshot(),
        now=ctx.now_et,
        lookback_minutes=lookback_minutes,
        max_anchor_overshoot_minutes=max_overshoot,
    )
    return _decide_core(ctx, signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
momentum_qqq = register(_decide)
