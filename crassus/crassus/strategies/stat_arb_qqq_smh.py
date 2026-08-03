"""Cross-asset "statistical arbitrage" between QQQ and SMH.

QQQ (Nasdaq-100) and SMH (VanEck Semiconductor ETF) are historically tightly
correlated: semiconductor names are a large and growing share of the
Nasdaq-100's own weighting, so the two tend to move together on the same
macro and sector news. When the ratio between them drifts unusually far from
its recent norm, the textbook stat-arb bet is that the pair reverts -- the
laggard catches up or the leader gives back its excess move -- rather than
that a durable, permanent repricing of the relationship just happened.

**Honesty about what this is not.** Real statistical arbitrage estimates a
hedge ratio from a proper historical regression, runs a cointegration test
(Engle-Granger / Johansen) to confirm the two series actually share a
mean-reverting long-run relationship rather than merely looking correlated
over some short window, and monitors that relationship over months of daily
data, not a live session. What this module does is a deliberately simplified
proxy: it tracks the *log-price ratio* `log(qqq/smh)` across one trading
session's own observations and reacts to how many standard deviations the
current ratio sits from that session's own running mean -- there is no
hedge-ratio regression (an implicit 1:1 log-ratio is assumed) and no
cointegration test at all. This is the same kind of simplification
`momentum.py`'s docstring already makes for academic time-series momentum
(Moskowitz, Ooi & Pedersen (2012)): a real trading system's fuller
methodology collapsed to the one statistic ("is the recent trailing
number of standard deviations enough") that's cheap to compute from a
single account's own polling loop, with the gap between the two written
down here rather than left implicit.

**Data source.** QQQ's price is `ctx.snapshot.underlying_price`, the same
durable mark every other strategy in this repo reads off the option-chain
snapshot. SMH never appears in that snapshot (it only ever covers QQQ's own
chain), so this strategy makes its own network read each cycle against a
second, independent board the same collector republishes into the same R2
bucket -- see `crassus/tickers.py`'s `TickerBoardReader` and
`config.TICKERS_URL`.

**Fetch-error handling.** A failed or malformed request to the tickers
board, a present-but-null/missing SMH price on an otherwise successful read,
and a present-but-*stale* SMH price (`board.is_stale()` -- the collector had
no fresh DXLink/yfinance read this cycle and republished its last-known
value, per `tickers.TickerBoard.is_stale`) are all treated identically -- as
`fetch_error`, not as a reading of "no correlation" or "neutral." A carried-
forward price is the worse of the two failure modes to miss: it looks like a
perfectly ordinary observation, so skipping this check would feed a frozen
SMH price into the ratio as if it were live, producing an artificially
stable log-ratio and a confident-looking z-score instead of an honest
"no data." Same reasoning as `trump_whisperer_qqq`'s `fetch_error` branch:
an absent (or stale) observation isn't evidence the pair's relationship has
changed, so a held position is retained rather than closed, and no new
position is opened from nothing. This is different from a genuinely
*computed* ratio landing inside the neutral band, or from not yet having
`min_samples` observations to trust a mean/stdev at all ("warming_up") --
both of those are real reads of the current state that do still close a
held position if it disagrees, exactly like momentum_qqq's warming-up/
neutral branches.

**Session tracker.** Each cycle's `log(qqq/smh)` observation is recorded
into a `momentum.PriceHistoryTracker` (imported and reused as-is -- the
tracker only ever stores a `(observed_at, float)` pair, so the "price" it
retains here is a log-ratio rather than a dollar price, same accumulate-
across-cycles shape momentum_qqq already relies on). Observations are
anchored to `ctx.now_et`, not to the option-chain snapshot's own timestamp:
this strategy's price series comes from its own fetch, not from the
snapshot the runner already pulled, so there is no shared clock to defer to
the way momentum_qqq defers to `snapshot.timestamp`.

Position management -- one contract at a time, opened in the direction the
current read supports and closed the moment that support goes away -- is
deliberately identical in shape to `momentum_qqq`/`trump_whisperer_qqq`; see
those modules' docstrings for why this is the right level of complexity for
now. `_decide_core` takes an already-computed `RatioSignal` (or a
`fetch_error` string) plus the raw qqq/smh prices, and has no network
dependency -- what `scripts/verify_stat_arb_qqq_smh.py` exercises directly.
`_decide` is the thin I/O wrapper that fetches SMH's price, records the
ratio, computes the signal, and calls `_decide_core`.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import DEFAULT_RETAIN_MINUTES, PricePoint, PriceHistoryTracker
from ..strategy import Decision, StrategyContext, register
from ..tickers import TickerBoardReader

STRATEGY_ID = "stat_arb_qqq_smh"
STRATEGY_VERSION = "1.0.0"

SMH_SYMBOL = "SMH"

DEFAULT_MIN_SAMPLES = 10
DEFAULT_ENTRY_Z_THRESHOLD = 1.5

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE: a held position can roll off the
# front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy, deliberately -- there is
# one QQQ/SMH ratio, not one per account, exactly like momentum_qqq's
# module-level `_tracker` and trump_whisperer's module-level `_reader`.
_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)
_tickers_reader = TickerBoardReader()


@dataclass(frozen=True)
class RatioSignal:
    """The result of `compute_ratio_signal`.

    `status` mirrors the vocabulary `momentum.MomentumSignal` and
    `trump_sentiment`'s sample-size gate already use:

      * "no_data" -- no observations at all yet.
      * "warming_up" -- fewer than `min_samples` observations, not enough to
        trust a session mean/stdev.
      * "ok" -- `z_score` is a trustworthy read of how far the current ratio
        sits from the session's own running distribution.
    """

    log_ratio: float | None
    sample_count: int
    mean: float | None
    stdev: float | None
    z_score: float | None
    status: str


def compute_ratio_signal(history: list[PricePoint], *, min_samples: int) -> RatioSignal:
    """Pure function: z-score of the newest point in `history` against the
    mean/stdev of every point in `history` (including itself).

    `history` need not be pre-sorted; the newest point (by `observed_at`) is
    taken as the current ratio, same convention as `momentum.compute_momentum`.
    A population stdev of exactly zero (every recorded ratio identical so
    far -- most likely a handful of samples right at the `min_samples`
    boundary) can't be divided by; `z_score` is reported as `0.0` in that
    case rather than raising or returning `None`, since a ratio that hasn't
    moved at all from the session mean has, by construction, zero deviation
    from it.
    """
    if not history:
        return RatioSignal(
            log_ratio=None, sample_count=0, mean=None, stdev=None, z_score=None, status="no_data"
        )

    ordered = sorted(history, key=lambda p: p.observed_at)
    current = ordered[-1]
    sample_count = len(ordered)

    if sample_count < min_samples:
        return RatioSignal(
            log_ratio=current.price, sample_count=sample_count,
            mean=None, stdev=None, z_score=None, status="warming_up",
        )

    values = [p.price for p in ordered]
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values)
    z_score = 0.0 if stdev == 0 else (current.price - mean) / stdev

    return RatioSignal(
        log_ratio=current.price, sample_count=sample_count,
        mean=mean, stdev=stdev, z_score=z_score, status="ok",
    )


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(
    ctx: StrategyContext,
    signal: RatioSignal | None,
    fetch_error: str | None,
    *,
    qqq_price: float | None,
    smh_price: float | None,
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

    meta_base = dict(
        qqq_price=qqq_price,
        smh_price=smh_price,
        log_ratio=signal.log_ratio if signal else None,
        session_mean_log_ratio=signal.mean if signal else None,
        session_stdev_log_ratio=signal.stdev if signal else None,
        ratio_z_score=signal.z_score if signal else None,
        sample_count=signal.sample_count if signal else 0,
        signal_status=signal.status if signal else "fetch_error",
    )

    if fetch_error is not None:
        # A failed request, malformed payload, or a present-but-null/missing
        # SMH price is not evidence the QQQ/SMH relationship has changed --
        # it's an absence of a fresh observation, not an observation of
        # "neutral" or "unsupported." Same reasoning as trump_whisperer's
        # fetch_error branch: retain a held position rather than closing on
        # a missing read.
        if held_symbol is not None:
            return no(
                f"SMH price unavailable ({fetch_error}); retaining the held "
                f"{held_type} position rather than closing on a missing "
                f"observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return no(f"SMH price unavailable: {fetch_error}", **meta_base)

    if signal is None or signal.status == "no_data":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No QQQ/SMH ratio history recorded yet.", meta_base,
        )

    if signal.status == "warming_up":
        params = ctx.params or {}
        min_samples = params.get("min_samples", DEFAULT_MIN_SAMPLES)
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Only {signal.sample_count} ratio observation(s) so far; need "
            f"{min_samples} to trust a session baseline.",
            meta_base,
        )

    params = ctx.params or {}
    entry_z = params.get("entry_z_threshold", DEFAULT_ENTRY_Z_THRESHOLD)
    z = signal.z_score

    if z is None or -entry_z < z < entry_z:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Ratio z-score {z} is within the +/-{entry_z} band around the "
            f"session mean; no reversion signal.",
            meta_base,
        )

    if z >= entry_z:
        # QQQ has run up relative to SMH beyond its normal session
        # correlation -- bet on reversion: QQQ underperforms/reverts.
        supported_type = "put"
        direction = "QQQ reverting down relative to SMH"
    else:
        # QQQ has fallen relative to SMH beyond the threshold -- bet on QQQ
        # catching back up.
        supported_type = "call"
        direction = "QQQ reverting up relative to SMH"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; ratio "
                f"z-score {z:.2f} still supports it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Ratio z-score {z:.2f} now supports {direction} "
                f"({supported_type}); closing stale {held_type} position "
                f"before considering a new one."
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
            f"Ratio z-score {z:.2f} (session mean={signal.mean:.5f}, "
            f"stdev={signal.stdev:.5f}, n={signal.sample_count}) supports "
            f"{direction}; opening one {supported_type}."
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
    if ctx.session_phase != "open":
        return _decide_core(ctx, None, None, qqq_price=None, smh_price=None)

    qqq_price = ctx.snapshot.underlying_price

    try:
        board = _tickers_reader.read()
    except Exception as exc:  # TickerFetchError, network failure, etc.
        return _decide_core(ctx, None, str(exc), qqq_price=qqq_price, smh_price=None)

    smh_price = board.price(SMH_SYMBOL)
    if smh_price is None or smh_price <= 0:
        return _decide_core(
            ctx, None,
            f"SMH price missing or invalid on tickers board (value={smh_price!r})",
            qqq_price=qqq_price, smh_price=smh_price,
        )
    if board.is_stale(SMH_SYMBOL):
        # The collector had no fresh DXLink/yfinance read this cycle and
        # carried the last-known SMH price forward (source="last-known").
        # A frozen price would otherwise read as a live observation and
        # yield an artificially stable ratio -- exactly the wrong direction
        # of wrong for a mean-reversion z-score, which wants "no data" here,
        # not "a confident, unchanging reading."
        return _decide_core(
            ctx, None,
            f"SMH price is stale (carried forward, not a fresh observation this cycle)",
            qqq_price=qqq_price, smh_price=smh_price,
        )

    log_ratio = math.log(qqq_price / smh_price)
    _tracker.observe(ctx.now_et, log_ratio)

    params = ctx.params or {}
    min_samples = params.get("min_samples", DEFAULT_MIN_SAMPLES)
    signal = compute_ratio_signal(_tracker.snapshot(), min_samples=min_samples)
    return _decide_core(ctx, signal, None, qqq_price=qqq_price, smh_price=smh_price)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
stat_arb_qqq_smh = register(_decide)
