"""Gamma scalping's entry signal, honestly reduced to what this platform can implement.

**This is not gamma scalping's hedge mechanics.** True gamma scalping means
holding a delta-neutral straddle (long a call and a long put, or an
equivalent combination) and continuously re-hedging with the UNDERLYING
SHARES as the price moves -- selling shares as the underlying rises, buying
them back as it falls -- to capture the difference between realized
volatility and the implied volatility paid for the straddle. This repo's
`Decision` contract (`strategy.py`) can only `buy`/`sell` exactly ONE OPTION
SYMBOL per decision: there is no way to trade the underlying shares through
it, and no way to hold or manage a multi-leg position (a call + a put)
through it either. Re-hedging -- the entire mechanism that actually
harvests realized-vs-implied vol -- is therefore structurally unavailable
here.

What *is* available, and what this strategy implements, is gamma scalping's
classic **entry condition**: "buy gamma when realized volatility is running
hotter than the volatility implied by option prices." That's expressed here
as a single-leg long option (one call or one put), opened when the signal
fires and closed when it stops firing, with no ongoing re-hedge of any
kind. Read this strategy as "a long-vol entry filter dressed as gamma
scalping," not as gamma scalping itself.

Mechanism:

  * Underlying price observations (`ctx.snapshot.underlying_price`) are
    recorded into a rolling `PriceHistoryTracker` (`crassus/momentum.py`),
    anchored to `snapshot.timestamp` -- not `ctx.now_et` -- with the same
    stale/duplicate-snapshot dedup discipline as `momentum_qqq.py`: a
    snapshot whose timestamp+sha256 hasn't changed since the last recorded
    read isn't re-appended under a new clock reading, and a snapshot that is
    itself too old (`max_snapshot_age_minutes`) is treated as a missing
    observation, not a fresh one. See `momentum_qqq.py`'s docstring for the
    full reasoning; it's reused verbatim here.

  * `compute_realized_vol` (pure, network/clock-free, exercised directly by
    `scripts/verify_gamma_scalping.py`) takes the tracker's recorded points
    within the trailing `realized_vol_lookback_minutes` window (default 30)
    and computes the sample standard deviation of consecutive log returns
    between them, then annualizes it by multiplying by
    `sqrt(periods_per_year)`, where `periods_per_year =
    ANNUALIZATION_TRADING_MINUTES_PER_YEAR / median_spacing_minutes` and
    `median_spacing_minutes` is the median observed gap between consecutive
    in-window timestamps -- *not* an assumed fixed cadence. (An earlier
    version multiplied by a fixed `sqrt(98280)`, implicitly assuming
    one-minute spacing; the runner is deployed at a 300s interval, which
    overstated realized vol by sqrt(5) =~ 2.24x and made the entry
    threshold trivially easy to clear.) At least `min_samples` observations
    (default 5) must fall inside the window or the result is
    `"warming_up"` -- the same "insufficient data yet, not neutral data"
    status vocabulary as `momentum.MomentumSignal`, and it is handled the
    same way: a held position gets closed (an internal, self-computed
    absence of signal is still a *current read*, just one that doesn't
    support a position), exactly as `momentum_qqq._decide_core` treats its
    own `"warming_up"`. Note the lookback window is also thin at the
    deployed cadence -- 30 minutes at 300s sampling is only ~6 observations
    (5 log returns), right at `min_samples`; fixing the annualization
    doesn't fix that a 5-point stdev is a noisy estimate on its own.

  * Implied volatility is the average of the nearest-ATM call's IV and
    nearest-ATM put's IV (`ctx.snapshot.atm("call")` / `.atm("put")`). If
    either side is missing a quote or an IV value, that is treated as a
    *fetch/data-absence* case -- like `momentum_qqq`'s `stale_source_reason`
    branch -- not a neutral reading: a held position is retained rather than
    closed, because "we couldn't read implied vol this cycle" is not
    evidence that the entry condition has stopped holding.

  * Entry rule: `vol_ratio = realized_vol / implied_vol`. If it clears
    `vol_ratio_threshold` (default 1.15 -- realized running noticeably
    hotter than what's priced in), that's the "buy gamma" signal. Direction
    is a pragmatic simplification of an otherwise direction-agnostic
    strategy: since there is no way to actually hold both legs of a
    straddle, this leans toward whichever side the recent move already
    favors, using the same window's trailing simple return
    (`trailing_return_over_window`, from the same tracked prices) -- buy a
    call if that return is non-negative, a put if it's negative. If the
    ratio doesn't clear the threshold, that's a genuine (not
    absent) read that doesn't support a position, so it's handled like
    `momentum_qqq`'s neutral band: no_trade while flat, close if held.

`realized_vol`, `implied_vol`, `vol_ratio`, `vol_ratio_threshold`, and
`trailing_return_over_window` are carried in `Decision.metadata` on every
signal-dependent branch (populated with `None` where not yet computable)
so the audit trail always shows what the strategy saw.

Position management (at most one contract, opened in the supported
direction, closed the moment that support goes away, no pyramiding, stand
down on more than one open position or an unrecognized/short held symbol)
is deliberately identical in shape to `momentum_qqq.py` -- see that module's
docstring for why this is the right level of complexity for now.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import DEFAULT_RETAIN_MINUTES, PriceHistoryTracker, PricePoint
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "gamma_scalping_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_REALIZED_VOL_LOOKBACK_MINUTES = 30.0
DEFAULT_MIN_SAMPLES = 5
DEFAULT_VOL_RATIO_THRESHOLD = 1.15

# 252 trading days/year x 390 trading minutes/day (9:30-16:00 ET). Used to
# annualize a standard deviation computed on consecutive per-observation log
# returns. This is trading-*minutes* per year; `compute_realized_vol`
# divides it by the median observed spacing (in minutes) between in-window
# samples to get periods-per-year for the actual sampling cadence, rather
# than assuming one-minute spacing -- the runner is deployed at a 300s
# interval (`crassus/railway.toml`), not the collector's ~60s republish
# cadence, so a fixed sqrt(98280) factor overstated realized vol by
# sqrt(5) =~ 2.24x at the deployed cadence.
ANNUALIZATION_TRADING_MINUTES_PER_YEAR = 252 * 390  # 98,280

# Same rationale as momentum_qqq.DEFAULT_MAX_SNAPSHOT_AGE_MINUTES: the board
# republishes roughly once a minute, so a snapshot whose own timestamp is
# older than this by the runner's clock means the collector has stalled.
DEFAULT_MAX_SNAPSHOT_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself so a held position that has rolled off the front of the
# chain can still be recognized and closed -- same reasoning as
# momentum_qqq._OCC_TYPE_RE.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy -- one QQQ underlying
# price, not one per account -- exactly like momentum_qqq's module-level
# `_tracker`. Kept as a separate instance (not momentum_qqq's shared
# tracker) because this strategy's default lookback (30m) differs from
# momentum_qqq's (60m) and the two should be free to be configured
# independently without one strategy's params silently affecting the other.
_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)

# (timestamp, sha256) of the most recently *recorded* snapshot, so a board
# read that hasn't actually changed isn't re-appended to the tracker as if
# it were new information under a new timestamp.
_last_recorded_snapshot: tuple[str, str] | None = None


@dataclass(frozen=True)
class RealizedVolSignal:
    """The result of `compute_realized_vol`.

    `status` mirrors `momentum.MomentumSignal.status`'s "absence of
    evidence" vocabulary:

      * "no_data" -- no observations recorded at all yet.
      * "warming_up" -- fewer than `min_samples` observations fall inside
        the trailing `lookback_minutes` window (either too few points have
        been recorded yet, or not enough of them are recent enough).
      * "ok" -- `realized_vol` and `trailing_return_over_window` are
        trustworthy.
    """

    lookback_minutes: float
    sample_count: int
    realized_vol: float | None
    trailing_return_over_window: float | None
    window_start_price: float | None
    window_end_price: float | None
    status: str


def compute_realized_vol(
    history: list[PricePoint],
    now: datetime,
    lookback_minutes: float = DEFAULT_REALIZED_VOL_LOOKBACK_MINUTES,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> RealizedVolSignal:
    """Pure function: annualized realized vol of `history` over the trailing
    `lookback_minutes` window, plus the window's trailing simple return.

    `history` need not be pre-sorted. Only points within
    `[now - lookback_minutes, now]` are used; at least `min_samples` of them
    must fall in that window or the result is `"warming_up"`. Realized vol is
    the sample standard deviation (ddof=1) of consecutive log returns
    between the in-window points, annualized by
    `sqrt(ANNUALIZATION_TRADING_MINUTES_PER_YEAR)` -- see the module
    docstring for that constant's derivation and its per-minute-spacing
    assumption.
    """
    if not history:
        return RealizedVolSignal(
            lookback_minutes=lookback_minutes,
            sample_count=0,
            realized_vol=None,
            trailing_return_over_window=None,
            window_start_price=None,
            window_end_price=None,
            status="no_data",
        )

    ordered = sorted(history, key=lambda p: p.observed_at)
    cutoff = now - timedelta(minutes=lookback_minutes)
    window = [p for p in ordered if p.observed_at >= cutoff and p.observed_at <= now]
    sample_count = len(window)

    if sample_count < max(min_samples, 2):
        return RealizedVolSignal(
            lookback_minutes=lookback_minutes,
            sample_count=sample_count,
            realized_vol=None,
            trailing_return_over_window=None,
            window_start_price=None,
            window_end_price=None,
            status="warming_up",
        )

    prices = [p.price for p in window]
    log_returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i - 1] > 0 and prices[i] > 0
    ]

    if len(log_returns) < 2:
        return RealizedVolSignal(
            lookback_minutes=lookback_minutes,
            sample_count=sample_count,
            realized_vol=None,
            trailing_return_over_window=None,
            window_start_price=None,
            window_end_price=None,
            status="warming_up",
        )

    mean_return = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_return) ** 2 for r in log_returns) / (len(log_returns) - 1)

    # Annualize against the *observed* sampling cadence, not an assumed
    # one-minute spacing -- the runner may be deployed at any interval (300s
    # in production), and a fixed per-minute assumption would misstate the
    # annualized figure by the square root of however far off that
    # assumption is.
    gaps_minutes = [
        (window[i].observed_at - window[i - 1].observed_at).total_seconds() / 60.0
        for i in range(1, len(window))
    ]
    median_spacing_minutes = statistics.median(gaps_minutes)
    periods_per_year = (
        ANNUALIZATION_TRADING_MINUTES_PER_YEAR / median_spacing_minutes if median_spacing_minutes > 0 else 0.0
    )
    realized_vol = math.sqrt(variance) * math.sqrt(periods_per_year)
    trailing_return = (prices[-1] / prices[0]) - 1.0 if prices[0] else None

    return RealizedVolSignal(
        lookback_minutes=lookback_minutes,
        sample_count=sample_count,
        realized_vol=realized_vol,
        trailing_return_over_window=trailing_return,
        window_start_price=prices[0],
        window_end_price=prices[-1],
        status="ok",
    )


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
    vol_signal: RealizedVolSignal | None,
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

    params = ctx.params or {}
    threshold = params.get("vol_ratio_threshold", DEFAULT_VOL_RATIO_THRESHOLD)

    if stale_source_reason is not None:
        # A stale/unparseable/duplicate source snapshot is not evidence the
        # entry condition has changed -- it's an absence of a fresh
        # observation. Same reasoning as momentum_qqq's stale_source_reason
        # branch: retain a held position rather than closing on a missing
        # read.
        meta = dict(
            realized_vol=None,
            implied_vol=None,
            vol_ratio=None,
            vol_ratio_threshold=threshold,
            trailing_return_over_window=None,
        )
        if held_symbol is not None:
            return no(
                f"Market snapshot unavailable or stale ({stale_source_reason}); "
                f"retaining the held {held_type} position rather than closing "
                f"on a missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return no(f"Market snapshot unavailable or stale: {stale_source_reason}", **meta)

    if vol_signal is None or vol_signal.status in ("no_data", "warming_up"):
        sample_count = vol_signal.sample_count if vol_signal else 0
        meta = dict(
            realized_vol=None,
            implied_vol=None,
            vol_ratio=None,
            vol_ratio_threshold=threshold,
            trailing_return_over_window=None,
            realized_vol_sample_count=sample_count,
            realized_vol_status=vol_signal.status if vol_signal else "no_data",
        )
        lookback = vol_signal.lookback_minutes if vol_signal else DEFAULT_REALIZED_VOL_LOOKBACK_MINUTES
        reason = (
            f"Only {sample_count} price observation(s) in the trailing "
            f"{lookback:.0f}-minute window; still warming up to a realized-vol "
            f"read."
        )
        return _maybe_close_unsupported(ctx, held_symbol, held_quantity, held_type, no, reason, meta)

    call_row = ctx.snapshot.atm("call")
    put_row = ctx.snapshot.atm("put")
    call_iv = call_row.get("IV") if call_row else None
    put_iv = put_row.get("IV") if put_row else None

    if call_row is None or put_row is None or call_iv is None or put_iv is None:
        # Missing/unusable ATM IV is a fetch/data-absence case, not a
        # neutral reading -- retain a held position rather than closing on
        # missing data, same treatment as stale_source_reason above.
        meta = dict(
            realized_vol=vol_signal.realized_vol,
            implied_vol=None,
            vol_ratio=None,
            vol_ratio_threshold=threshold,
            trailing_return_over_window=vol_signal.trailing_return_over_window,
        )
        if held_symbol is not None:
            return no(
                "No usable ATM implied vol this cycle (missing call/put IV); "
                f"retaining the held {held_type} position rather than closing "
                f"on missing data.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return no("No usable ATM implied vol this cycle (missing call/put IV).", **meta)

    implied_vol = (call_iv + put_iv) / 2.0

    if implied_vol <= 0:
        meta = dict(
            realized_vol=vol_signal.realized_vol,
            implied_vol=implied_vol,
            vol_ratio=None,
            vol_ratio_threshold=threshold,
            trailing_return_over_window=vol_signal.trailing_return_over_window,
        )
        if held_symbol is not None:
            return no(
                f"ATM implied vol reads as non-positive ({implied_vol}); "
                f"retaining the held {held_type} position rather than closing "
                f"on unusable data.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return no(f"ATM implied vol reads as non-positive ({implied_vol}).", **meta)

    vol_ratio = vol_signal.realized_vol / implied_vol
    meta_base = dict(
        realized_vol=vol_signal.realized_vol,
        implied_vol=implied_vol,
        vol_ratio=vol_ratio,
        vol_ratio_threshold=threshold,
        trailing_return_over_window=vol_signal.trailing_return_over_window,
        realized_vol_sample_count=vol_signal.sample_count,
    )

    if vol_ratio < threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Realized/implied vol ratio {vol_ratio:.3f} does not clear the "
            f"{threshold} threshold; realized vol isn't running hot enough "
            f"over implied to justify buying gamma.",
            meta_base,
        )

    trailing_return = vol_signal.trailing_return_over_window
    supported_type = "call" if (trailing_return is not None and trailing_return >= 0) else "put"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; the entry "
                f"condition still supports it (vol_ratio={vol_ratio:.3f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Trailing return over the window flipped direction "
                f"(trailing_return_over_window={trailing_return}); closing "
                f"stale {held_type} position before considering a new one."
            ),
            meta=meta_base,
            no=no,
        )

    row = call_row if supported_type == "call" else put_row
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
            f"Realized/implied vol ratio {vol_ratio:.3f} clears the "
            f"{threshold} threshold (buy-gamma entry condition); trailing "
            f"return over the window ({trailing_return}) favors the "
            f"{supported_type} side; opening one {supported_type}."
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
    lookback_minutes = params.get("realized_vol_lookback_minutes", DEFAULT_REALIZED_VOL_LOOKBACK_MINUTES)
    min_samples = params.get("min_samples", DEFAULT_MIN_SAMPLES)
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

    vol_signal = compute_realized_vol(
        _tracker.snapshot(),
        now=ctx.now_et,
        lookback_minutes=lookback_minutes,
        min_samples=min_samples,
    )
    return _decide_core(ctx, vol_signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
gamma_scalping_qqq = register(_decide)
