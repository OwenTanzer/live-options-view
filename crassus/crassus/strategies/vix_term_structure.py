"""VIX term-structure (contango/backwardation) regime filter for QQQ options.

The VIX term structure compares the market's priced-in near-term volatility
to its longer-term volatility. This strategy uses two CBOE index tickers:

  * `^VIX9D` -- expected S&P 500 volatility over the next 9 calendar days.
  * `^VIX3M` -- expected S&P 500 volatility over the next 3 months.

`ratio = VIX9D / VIX3M`. When `ratio < 1` the market is pricing near-term
risk *below* longer-term risk -- "contango" -- which is the normal, calm
state of the world: near-dated volatility is a wasting asset absent a
specific catalyst on the horizon, so it usually trades cheap relative to the
longer-dated number. This is the well-known "VIX term structure is in
contango roughly 80% of trading days" stylized fact (see e.g. CBOE/VIX
futures curve literature and the volatility-trading commentary it spawned --
"contango most of the time, backwardation around crises"). When `ratio > 1`
("backwardation") the market is paying up for near-term protection *more*
than for the longer-dated view -- a classic signature of acute, imminent
event risk or an ongoing selloff (2008, March 2020, etc.), not a permanent
regime.

Honest scope of the trade this strategy can actually place: this platform
only ever buys single-leg calls or puts (see `strategy.py`'s docstring and
every other strategy in this package) -- it has no way to sell volatility,
run a calendar spread, or otherwise take a position on the term structure
itself. So this is **not** a real "trade the term structure" strategy in the
vol-trading sense (that would mean selling front-month vol and buying
back-month, or the reverse); it's a regime filter that reads the term
structure as a coarse risk-on/risk-off signal and expresses that view the
only way this bot can -- a directional long option on QQQ. Contango (calm)
leans toward a long call; backwardation (stress) leans toward a long put.
That directional mapping is a simplification, not a rigorous volatility
trade, and this docstring says so explicitly rather than overclaiming.

Data source and caching: `^VIX9D`/`^VIX3M` are not published anywhere else
in this system (unlike `momentum_qqq`, which reuses the durable QQQ snapshot
`market.py` already polls), so this strategy makes its own network call via
`yfinance` -- a free, unauthenticated wrapper around Yahoo Finance's public
quote endpoints, no API key or account needed. `VixTermStructureReader`
caches the last successful read for `min_interval_s` (default 60s, mirroring
`market.SnapshotReader`'s own caching rationale) so a runner polling every
strategy every cycle doesn't hammer Yahoo's endpoint once per account per
cycle -- there is one VIX term structure, not one per account, so (like
`trump_whisperer`'s `_reader` and `momentum_qqq`'s `_tracker`) the reader is
a module-level singleton shared across every account running this strategy.

Error handling follows the same "absence of evidence isn't evidence against"
rule as `trump_whisperer.py` and `momentum_qqq.py`: any fetch failure (yfinance
raises, network error, malformed response) *or* a missing/`None` price for
either ticker is treated identically -- as "no signal this cycle," not as a
neutral or bearish reading. A held position is retained rather than closed;
a flat account stays flat. Only a *successfully fetched* ratio that lands
squarely in the ambiguous band around 1.0 is treated as a genuine "no signal"
read that *does* close a held position -- see `_decide_core` below.

`_decide_core` takes an already-fetched snapshot (or an error string) and
contains all the decision logic; it has no network dependency and is what
`scripts/verify_vix_term_structure.py` exercises directly. `_decide` is the
thin I/O wrapper the registry calls.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "vix_term_structure_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_CONTANGO_THRESHOLD = 0.97  # ratio below this -> clearly calm/contango
DEFAULT_BACKWARDATION_THRESHOLD = 1.03  # ratio above this -> clearly stressed/backwardation

DEFAULT_MIN_INTERVAL_S = 60.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE and trump_whisperer._OCC_TYPE_RE: a
# held position can roll off the front of the chain while still needing to
# be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


class VixFetchError(RuntimeError):
    """VIX9D/VIX3M could not be fetched, or a fetched price was unusable."""


@dataclass(frozen=True)
class VixTermStructureSnapshot:
    """One successful read of both VIX term-structure tickers."""

    fetched_at: float
    vix9d: float | None
    vix3m: float | None


def _default_fetch_fn() -> tuple[float | None, float | None]:
    """Fetch the last price for `^VIX9D` and `^VIX3M` via yfinance.

    Imported lazily (inside the function, not at module scope) so importing
    this module -- and therefore `crassus.strategies` -- never fails for an
    account not configured to use this strategy, same reasoning as
    `trump_sentiment._default_analyzer_factory`'s lazy `vaderSentiment`
    import.
    """
    import yfinance as yf  # noqa: PLC0415 -- optional dependency, only needed if this runs

    vix9d = yf.Ticker("^VIX9D").fast_info.get("last_price")
    vix3m = yf.Ticker("^VIX3M").fast_info.get("last_price")
    return vix9d, vix3m


class VixTermStructureReader:
    """Polls VIX9D/VIX3M and caches the result.

    Cached on the same "don't re-fetch faster than the signal moves"
    principle as `market.SnapshotReader` and `trump_sentiment.TrumpSentimentReader`:
    there is exactly one VIX term structure, so the module-level singleton
    `_reader` below is shared across every account running this strategy,
    exactly like those two.
    """

    def __init__(
        self,
        *,
        fetch_fn: Callable[[], tuple[float | None, float | None]] = _default_fetch_fn,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    ):
        self.fetch_fn = fetch_fn
        self.min_interval_s = min_interval_s
        self._cached: VixTermStructureSnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> VixTermStructureSnapshot:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        try:
            vix9d, vix3m = self.fetch_fn()
        except Exception as exc:  # network errors, yfinance internals, etc.
            raise VixFetchError(f"VIX9D/VIX3M fetch failed: {exc}") from exc

        snapshot = VixTermStructureSnapshot(
            fetched_at=time.monotonic(), vix9d=vix9d, vix3m=vix3m
        )
        self._cached, self._cached_at = snapshot, time.monotonic()
        return snapshot


_reader = VixTermStructureReader()


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(
    ctx: StrategyContext,
    snapshot: VixTermStructureSnapshot | None,
    fetch_error: str | None,
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

    if fetch_error is not None:
        # A fetch failure -- or a fetched-but-missing/None price for either
        # ticker, which `_decide` folds into this same path -- is not
        # evidence the regime has changed; it's an absence of observation,
        # not an observation of "ambiguous." Retain a held position rather
        # than closing on a missing read, same reasoning as
        # trump_whisperer's fetch_error branch and momentum_qqq's
        # stale_source_reason branch.
        if held_symbol is not None:
            return no(
                f"VIX term structure unavailable ({fetch_error}); retaining "
                f"the held {held_type} position rather than closing on a "
                f"missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
            )
        return no(f"VIX term structure unavailable: {fetch_error}")

    if snapshot is None:
        # Not reachable via `_decide` (it only calls `_decide_core` with
        # `snapshot=None` alongside a `fetch_error`, never both `None`), but
        # `_decide_core` is called directly in tests and is not itself
        # responsible for enforcing that invariant.
        return no("No VIX term structure snapshot available.")

    vix9d, vix3m = snapshot.vix9d, snapshot.vix3m
    ratio = (vix9d / vix3m) if (vix9d is not None and vix3m and vix3m != 0) else None

    params = ctx.params or {}
    contango_threshold = params.get("contango_threshold", DEFAULT_CONTANGO_THRESHOLD)
    backwardation_threshold = params.get("backwardation_threshold", DEFAULT_BACKWARDATION_THRESHOLD)

    if ratio is None:
        regime = "ambiguous"
        meta_base = dict(vix9d=vix9d, vix3m=vix3m, ratio=ratio, regime=regime)
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"VIX9D/VIX3M price missing (vix9d={vix9d}, vix3m={vix3m}).",
            meta_base,
        )

    if ratio < contango_threshold:
        regime = "contango"
        supported_type: str | None = "call"
    elif ratio > backwardation_threshold:
        regime = "backwardation"
        supported_type = "put"
    else:
        regime = "ambiguous"
        supported_type = None

    meta_base = dict(vix9d=vix9d, vix3m=vix3m, ratio=ratio, regime=regime)

    if supported_type is None:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"VIX9D/VIX3M ratio ({ratio:.4f}) is within the ambiguous band "
            f"[{contango_threshold}, {backwardation_threshold}]; no clear "
            f"contango or backwardation regime.",
            meta_base,
        )

    direction = "bullish" if supported_type == "call" else "bearish"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; {regime} "
                f"regime still supports it (ratio={ratio:.4f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Regime now reads {regime} ({direction}, ratio={ratio:.4f}); "
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
            f"VIX term structure is in {regime} (VIX9D={vix9d}, VIX3M={vix3m}, "
            f"ratio={ratio:.4f}); leaning {direction} via single-leg option "
            f"since this platform cannot express a direct volatility trade. "
            f"Opening one {supported_type}."
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
        return _decide_core(ctx, None, None)

    try:
        snapshot = _reader.read()
    except VixFetchError as exc:
        return _decide_core(ctx, None, str(exc))
    except Exception as exc:  # unexpected, still not evidence of a regime
        return _decide_core(ctx, None, str(exc))

    return _decide_core(ctx, snapshot, None)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
vix_term_structure_qqq = register(_decide)
