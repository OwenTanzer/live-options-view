"""Dealer gamma exposure (GEX) / zero-gamma flip regime strategy.

**The GEX formula and its provenance.** For each option row with a usable
Gamma and OpenInterest, this module computes a per-strike, per-side
contribution to "dealer gamma exposure":

    contribution = Gamma * OpenInterest * 100 * UnderlyingPrice**2 * 0.01 * sign

with `sign = +1` for calls and `sign = -1` for puts. This is the well-known
public/retail GEX heuristic popularized by SqueezeMetrics and reproduced by
countless retail GEX calculators and dashboards -- it is **not** a
measurement of any real dealer's book. Actual dealer positioning (who is
long/short which contracts, and how they are hedged) is not observable data;
this formula is a market-wide convention that simply assumes "customers are
net long the calls they buy and net long the puts they buy, so market makers
who sold those contracts to them are, in aggregate, short gamma from the puts
and long gamma from the calls they warehoused" -- hence the sign flip between
calls and puts. Open interest is used as a (noisy, lagging) proxy for the
size of that warehoused position because no venue publishes actual dealer
inventory. Every strategy that uses this formula, this one included, is
trading a *popular heuristic about aggregate positioning*, not a fact.

**Net GEX and the zero-gamma flip.** Summing every strike's contribution
gives a net GEX figure. Sorting strikes and walking the *cumulative* GEX from
the lowest strike upward, the strike at which that cumulative sum crosses
zero is the approximate "zero-gamma flip" -- found here by linear
interpolation between the two straddling strikes (see `compute_gex`). Retail
GEX lore treats the flip as a regime boundary: when the underlying trades
*above* the flip, aggregate dealer gamma is positive, and dealers are
theorized to hedge in a way that dampens realized volatility (buying dips /
selling rallies, i.e. a pinning/mean-reverting tendency). When the underlying
trades *below* the flip, aggregate dealer gamma is negative, and dealers are
theorized to hedge in the *same* direction as the move (selling into
weakness / buying into strength), amplifying realized volatility.

**Why this implementation only trades one branch.** A positive-gamma regime
telling you "moves are likely to be dampened" is not, by itself, a
directional edge -- it is a statement about expected *volatility*, not
expected *direction*. Manufacturing a directional call out of it (e.g. "fade
the move back toward some reference level") would require a second, separate
signal (a VWAP anchor, a range definition, an overshoot measure) that this
strategy does not have and should not invent just to keep both branches
symmetric. So the positive-gamma branch is a deliberate, honest scope-down:
`no_trade`, always -- this strategy uses GEX regime here only to tell it *not*
to expect a big move, not to pick a side.

The negative-gamma (short-gamma) branch is where GEX heuristics most commonly
claim genuine predictive value, because the amplification story *is*
directional once you know which way the underlying is already leaning: a
short-gamma regime that is getting *more* short (net GEX trending more
negative reading-over-reading) is read here as escalating downside
acceleration risk, worth a small long-put bet. A short-gamma regime that is
recovering (net GEX trending back toward zero/the flip) removes that specific
signal without asserting a new bullish one, so it is treated as no-signal
(`no_trade`), not as a reason to buy a call. This keeps the strategy
one-directional and easy to reason about rather than overclaiming a
symmetric two-sided edge from a heuristic that, empirically, mostly gets
cited for its volatility-regime implications, not its directional ones.

**Position management, staleness, and dedup** mirror `momentum_qqq` exactly
(see that module's docstring for the full rationale): at most one contract
held at a time; a stale or duplicate source snapshot retains a held position
rather than closing it (absence of a fresh read isn't evidence against);
`net_gex` readings are recorded into a shared, module-level trailing history
keyed off `snapshot.timestamp` (never `ctx.now_et`), deduped by
`(timestamp, sha256)` so a repeated poll of an unrepublished snapshot can't
be double-counted as a second, independent reading. The trend requires at
least two recorded readings before the negative-gamma branch can trade at
all (`net_gex_trend_status == "warming_up"` otherwise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "gamma_exposure_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_MIN_STRIKES_WITH_GAMMA = 5

# Mirrors momentum_qqq's DEFAULT_MAX_SNAPSHOT_AGE_MINUTES -- same board, same
# ~60s republish cadence, same reasoning for the cutoff.
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# How long a net-GEX reading stays in the trailing trend history. Generous
# relative to the trend calc (which only ever looks at the last two
# readings) so an operator inspecting the audit trail still has a full
# session's worth of readings to look back over.
DEFAULT_RETAIN_MINUTES = 1440.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE: a held position can roll off the
# front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


@dataclass(frozen=True)
class GexResult:
    """Output of one `compute_gex` call against a single snapshot's rows."""

    status: str  # "ok" | "insufficient_data"
    net_gex: float | None
    flip_strike: float | None
    regime: str  # "positive" | "negative" | "undetermined"
    usable_strikes: int
    per_strike: dict[float, float]


@dataclass(frozen=True)
class GexReading:
    observed_at: datetime
    net_gex: float


def _row_contribution(row: dict[str, Any], underlying_price: float) -> tuple[float, float] | None:
    """`(strike, signed contribution)` for one row, or None if unusable."""
    gamma = row.get("Gamma")
    oi = row.get("OpenInterest")
    option_type = row.get("Type")
    strike = row.get("Strike")
    if gamma is None or oi is None or strike is None or option_type not in ("call", "put"):
        return None
    try:
        gamma = float(gamma)
        oi = float(oi)
        strike = float(strike)
    except (TypeError, ValueError):
        return None
    sign = 1.0 if option_type == "call" else -1.0
    contribution = gamma * oi * 100.0 * (underlying_price ** 2) * 0.01 * sign
    return strike, contribution


def compute_gex(
    rows: list[dict[str, Any]],
    underlying_price: float,
    min_strikes_with_gamma: int = DEFAULT_MIN_STRIKES_WITH_GAMMA,
) -> GexResult:
    """Net GEX and the approximate zero-gamma flip strike for one snapshot.

    See this module's docstring for the formula and its sign convention.
    Rows are aggregated per strike (call + put contributions at the same
    strike net together) before the cumulative walk that locates the flip,
    since the flip is a property of the combined book at each strike, not of
    an individual call or put row.
    """
    per_strike: dict[float, float] = {}
    for row in rows:
        result = _row_contribution(row, underlying_price)
        if result is None:
            continue
        strike, contribution = result
        per_strike[strike] = per_strike.get(strike, 0.0) + contribution

    usable_strikes = len(per_strike)
    if usable_strikes < min_strikes_with_gamma:
        return GexResult(
            status="insufficient_data",
            net_gex=None,
            flip_strike=None,
            regime="undetermined",
            usable_strikes=usable_strikes,
            per_strike=per_strike,
        )

    net_gex = sum(per_strike.values())

    flip_strike: float | None = None
    cumulative = 0.0
    prev_strike: float | None = None
    prev_cumulative: float | None = None
    for strike in sorted(per_strike):
        cumulative += per_strike[strike]
        if cumulative == 0.0:
            flip_strike = strike
            break
        if prev_cumulative is not None and (prev_cumulative < 0.0) != (cumulative < 0.0):
            flip_strike = prev_strike + (0.0 - prev_cumulative) / (cumulative - prev_cumulative) * (
                strike - prev_strike
            )
            break
        prev_strike, prev_cumulative = strike, cumulative

    if flip_strike is None:
        regime = "undetermined"
    elif underlying_price >= flip_strike:
        regime = "positive"
    else:
        regime = "negative"

    return GexResult(
        status="ok",
        net_gex=net_gex,
        flip_strike=flip_strike,
        regime=regime,
        usable_strikes=usable_strikes,
        per_strike=per_strike,
    )


# Shared across every account running this strategy, deliberately -- there is
# one QQQ GEX reading per snapshot, not one per account, same reasoning as
# momentum_qqq's module-level `_tracker`.
_history: list[GexReading] = []

# (timestamp, sha256) of the most recently *recorded* snapshot, so a board
# read that hasn't actually changed isn't re-appended to the history as if it
# were a new, independent reading.
_last_recorded_snapshot: tuple[str, str] | None = None


def _prune_history(now: datetime, retain_minutes: float) -> None:
    global _history
    cutoff = now - timedelta(minutes=retain_minutes)
    _history = [r for r in _history if r.observed_at >= cutoff]


def _compute_trend() -> dict[str, Any]:
    """Compare the two most recent recorded net-GEX readings.

    `status` is "warming_up" (fewer than two readings recorded yet) or "ok".
    When "ok", `trend` is "worsening" (net GEX more negative than the prior
    reading), "improving" (less negative), or "flat" (unchanged).
    """
    if len(_history) < 2:
        return {
            "status": "warming_up",
            "trend": None,
            "reading_count": len(_history),
            "previous_net_gex": None,
            "latest_net_gex": _history[-1].net_gex if _history else None,
        }
    latest = _history[-1].net_gex
    previous = _history[-2].net_gex
    if latest < previous:
        trend = "worsening"
    elif latest > previous:
        trend = "improving"
    else:
        trend = "flat"
    return {
        "status": "ok",
        "trend": trend,
        "reading_count": len(_history),
        "previous_net_gex": previous,
        "latest_net_gex": latest,
    }


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
    gex_result: GexResult | None,
    trend: dict[str, Any] | None,
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

    meta_base: dict[str, Any] = dict(
        net_gex=gex_result.net_gex if gex_result else None,
        flip_strike=gex_result.flip_strike if gex_result else None,
        regime=gex_result.regime if gex_result else "undetermined",
        usable_strikes=gex_result.usable_strikes if gex_result else None,
        underlying_price=ctx.snapshot.underlying_price,
        net_gex_trend=trend.get("trend") if trend else None,
        net_gex_trend_status=trend.get("status") if trend else "no_data",
        net_gex_reading_count=trend.get("reading_count") if trend else 0,
    )

    if stale_source_reason is not None:
        # A stale/unparseable/duplicate source snapshot is not evidence the
        # GEX regime has changed -- it's an absence of a fresh observation,
        # not a fresh observation of "no signal." Same reasoning as
        # momentum_qqq's stale_source_reason branch: retain a held position
        # rather than closing on a missing read.
        if held_symbol is not None:
            return no(
                f"Market snapshot unavailable or stale ({stale_source_reason}); "
                f"retaining the held {held_type} position rather than closing "
                f"on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}", **meta_base)

    if gex_result is None or gex_result.status != "ok":
        usable = gex_result.usable_strikes if gex_result else 0
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {usable} strike(s) with usable Gamma+OpenInterest data; "
            f"need at least the configured minimum to compute GEX.",
            meta_base,
        )

    if gex_result.regime == "undetermined":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No zero-gamma flip strike found within the observed strike "
            "range; regime is undetermined.",
            meta_base,
        )

    if gex_result.regime == "positive":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Positive/long-gamma regime (underlying {ctx.snapshot.underlying_price} "
            f"above flip strike {gex_result.flip_strike:.2f}); dealer hedging is "
            f"expected to dampen moves, so this strategy stands down rather than "
            f"assume a directional edge in a pinned market.",
            meta_base,
        )

    # regime == "negative": short-gamma / amplification regime. Only the
    # worsening-net-GEX branch trades -- see module docstring for why.
    if trend is None or trend.get("status") != "ok":
        reading_count = trend.get("reading_count", 0) if trend else 0
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {reading_count} net GEX reading(s) recorded so far; "
            f"still warming up before evaluating the GEX trend.",
            meta_base,
        )

    if trend["trend"] != "worsening":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Negative-gamma regime but net GEX trend is "
            f"'{trend['trend']}', not worsening; amplification threat "
            f"isn't escalating, no directional edge taken.",
            meta_base,
        )

    supported_type = "put"
    direction = "down"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; "
                f"negative-gamma regime with worsening net GEX still "
                f"supports it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"GEX regime/trend now supports a put "
                f"(net_gex={gex_result.net_gex:.0f}, "
                f"flip_strike={gex_result.flip_strike:.2f}); closing stale "
                f"{held_type} position before considering a new one."
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
            f"Negative-gamma regime (underlying {ctx.snapshot.underlying_price} "
            f"below flip strike {gex_result.flip_strike:.2f}) with worsening "
            f"net GEX (net_gex={gex_result.net_gex:.0f}, "
            f"previous={trend['previous_net_gex']:.0f}); dealer hedging is "
            f"expected to amplify further {direction}side moves, opening one "
            f"{supported_type}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            **meta_base,
            "strike": row["Strike"],
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
        return _decide_core(ctx, None, None)

    params = ctx.params or {}
    max_snapshot_age = params.get("max_snapshot_age_minutes", DEFAULT_MAX_SNAPSHOT_AGE_MINUTES)
    min_strikes = params.get("min_strikes_with_gamma", DEFAULT_MIN_STRIKES_WITH_GAMMA)
    retain_minutes = params.get("retain_minutes", DEFAULT_RETAIN_MINUTES)

    observed_at = _snapshot_observed_at(ctx.snapshot.timestamp)
    if observed_at is None:
        return _decide_core(ctx, None, None, stale_source_reason=f"unparseable snapshot timestamp {ctx.snapshot.timestamp!r}")

    snapshot_age_minutes = (ctx.now_et - observed_at).total_seconds() / 60.0
    if snapshot_age_minutes > max_snapshot_age:
        return _decide_core(
            ctx, None, None,
            stale_source_reason=(
                f"snapshot is {snapshot_age_minutes:.1f} minutes old "
                f"(limit={max_snapshot_age}m) -- the collector looks stalled"
            ),
        )

    gex_result = compute_gex(ctx.snapshot.rows, ctx.snapshot.underlying_price, min_strikes)

    snapshot_key = (ctx.snapshot.timestamp, ctx.snapshot.sha256)
    if snapshot_key != _last_recorded_snapshot:
        if gex_result.status == "ok":
            _history.append(GexReading(observed_at, gex_result.net_gex))
            _prune_history(observed_at, retain_minutes)
        _last_recorded_snapshot = snapshot_key

    trend = _compute_trend()
    return _decide_core(ctx, gex_result, trend)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
gamma_exposure_qqq = register(_decide)
