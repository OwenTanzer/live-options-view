"""Cross-market macro drivers (10Y yield + crude oil) as a directional tell for QQQ.

The intuition, well known but far from infallible: QQQ is a rate-sensitive,
growth/duration-heavy index (long-dated cash flows, richly valued on
forward earnings), so a *rising* 10-year Treasury yield raises the discount
rate applied to those cash flows -- a headwind. Rising crude oil is a
related but distinct headwind: it is a real-economy inflation input (input
costs, consumer spending power) that both pressures the same discount-rate
channel and squeezes margins outside the energy sector QQQ barely holds.
Falling yields and falling crude are the mirror-image tailwind. This is a
textbook macro heuristic, not a fitted or backtested model -- there is no
regression here, no estimated betas, just two independently observed
trailing returns combined with an explicit, deliberately naive equal weight
(`w_yield = w_crude = 1.0` by default). Treat the composite score as a
directional proxy worth a small, hedged options position, not as evidence
of a robust discovered relationship.

Mechanically this is `momentum.py`'s time-series-momentum construction
(see `momentum_qqq.py`'s docstring for the citation) applied twice, to two
different underlyings that are *not* QQQ itself: the 10Y yield and USO (a
crude-oil-tracking ETF used as the crude proxy, since this deployment has
no direct WTI/Brent feed). Both come from the same public, unauthenticated
multi-ticker board already used to publish QQQ's own price
(`crassus/tickers.py`'s `TickerBoardReader`, reading `config.TICKERS_URL` --
same R2 bucket, same ~60s republish cadence as the option-chain snapshot in
`market.py`, just a different artifact). Each series gets its own
independent `PriceHistoryTracker` and its own `compute_momentum()` call
over a shared `lookback_minutes` param (default 15 -- short relative to
momentum_qqq's own default 60m lookback, because macro-driver moves that
matter to an intraday options position tend to show up over minutes, not
hours; still just a param, not a fitted horizon). Each tracker independently
passes through the same `"no_data"` / `"warming_up"` / `"stale_anchor"` /
`"ok"` states as `momentum_qqq`'s own signal (see `momentum.py`'s
`MomentumSignal` docstring) -- both trackers must independently reach "ok"
before the composite is computed at all; a single tracker still warming up
withholds the whole signal rather than half-computing it.

The composite score is:

    score = -(w_yield * yield_trailing_return + w_crude * crude_trailing_return)

The leading minus sign encodes the headwind direction: a *positive*
trailing return in either series (yields or crude rising) subtracts from
the score, and a *negative* trailing return (yields or crude falling) adds
to it -- so a positive score means both drivers are tilted in QQQ's favor
and a negative score means both (or the dominant one) are tilted against
it. `score_threshold` (default 0.001, i.e. a tenth of a percent of combined
weighted trailing return) gates a neutral band the same way
`momentum_qqq`'s own bullish/bearish thresholds do -- small enough to react
to a real move in either driver, large enough that noise in either series
alone doesn't flip a position every cycle.

Fetch-error handling mirrors `trump_whisperer.py`/`reddit_sentiment.py`
rather than `momentum_qqq.py`, because unlike the QQQ price (which the
runner has already fetched into `ctx.snapshot` before this strategy ever
runs), the tickers board is *this strategy's own* network call, made fresh
each cycle via `TickerBoardReader.read()`. A raised exception, a `feed_stale`
flag on the payload, a missing/null price for either `10Y` or `USO`, an
unparseable snapshot timestamp, or a snapshot old enough to suggest the
feed has stalled are all treated identically to `trump_whisperer`'s
`fetch_error`: an absence of a fresh observation, not an observation of
"neutral" -- so a held position is *retained*, never closed, on any of
these paths. That is different from the tracker-level `"warming_up"` /
`"stale_anchor"` / neutral-score paths below, which -- like
`momentum_qqq`'s equivalent states -- genuinely are a completed read that
just doesn't currently support a position, and so *do* close a held
position, the same "absence of evidence during ingestion is not evidence
against; a completed neutral read is" distinction every network-fetching
strategy in this repo already makes.

`_decide_core` takes two already-computed `MomentumSignal`s (or `None`
alongside a `fetch_error`) and contains all the decision logic; it has no
network or clock dependency and is what
`scripts/verify_macro_cross_market.py` exercises directly with hand-built
signals. `_decide` is the I/O wrapper: it fetches the tickers board,
validates its freshness, records new observations into the two shared
module-level trackers keyed off the board's own timestamp (never
`ctx.now_et`, same anti-fabrication reasoning as `momentum_qqq._decide`),
computes both signals, and calls `_decide_core`.

Position management (at most one contract, OCC parsing, ATM lookup,
close-on-disagreement) is deliberately identical in shape to
`momentum_qqq.py` and the sentiment strategies -- see those modules'
docstrings for why this is the right level of complexity for now.
`Decision.metadata` always carries `yield_trailing_return`,
`crude_trailing_return`, `composite_score`, `yield_status`, and
`crude_status`, even on `no_trade`, so the audit trail shows what the
composite was doing on every cycle, not just the cycles it acted on.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..momentum import (
    DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES,
    DEFAULT_RETAIN_MINUTES,
    MomentumSignal,
    PriceHistoryTracker,
    compute_momentum,
)
from ..strategy import Decision, StrategyContext, register
from ..tickers import TickerBoardReader

STRATEGY_ID = "macro_cross_market_qqq"
STRATEGY_VERSION = "1.0.0"

YIELD_TICKER = "10Y"
CRUDE_TICKER = "USO"

# Short relative to momentum_qqq's own default 60-minute lookback -- see
# module docstring for why an intraday macro-driver signal is deliberately
# tuned to a shorter horizon than QQQ's own price momentum.
DEFAULT_LOOKBACK_MINUTES = 15.0

DEFAULT_W_YIELD = 1.0
DEFAULT_W_CRUDE = 1.0

# A tenth of a percent of combined weighted trailing return -- small enough
# to react to a real move in either driver, large enough that ordinary
# noise in one series alone doesn't flip the position every cycle. Not a
# fitted value; see module docstring's "heuristic, not a fitted model"
# framing.
DEFAULT_SCORE_THRESHOLD = 0.001

# The tickers board republishes on roughly the same ~60s cadence as the
# option-chain snapshot (see tickers.py's module docstring); a board whose
# own timestamp is older than this by the runner's clock means the feed
# itself has stalled, not just "hasn't repolled since last cycle" -- same
# reasoning and same default as momentum_qqq's DEFAULT_MAX_SNAPSHOT_AGE_MINUTES.
DEFAULT_MAX_TICKERS_AGE_MINUTES = 5.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE: a held position can roll off the
# front of the chain while still needing to be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")

# Shared across every account running this strategy, deliberately -- there
# is one 10Y yield series and one USO series, not one per account, exactly
# like momentum_qqq's module-level `_tracker`.
_yield_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)
_crude_tracker = PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)

# Last-seen `source` for each ticker (e.g. "dxlink", "yfinance", "last-known"),
# so a provider failover mid-session can be detected and the tracker reset
# rather than silently mixing two providers' scales into one series -- see
# `_reset_tracker_on_source_change` below.
_last_seen_source: dict[str, str | None] = {YIELD_TICKER: None, CRUDE_TICKER: None}

_reader = TickerBoardReader()

# The tickers board's own `timestamp` string for the most recently
# *recorded* read, so a board read that hasn't actually changed (the
# collector hasn't republished yet, or is stalled) isn't re-appended to
# either tracker as if it were new information under a new clock reading.
_last_recorded_timestamp: str | None = None


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


def _base_meta(yield_signal: MomentumSignal | None, crude_signal: MomentumSignal | None) -> dict[str, Any]:
    return dict(
        yield_trailing_return=yield_signal.return_pct if yield_signal else None,
        crude_trailing_return=crude_signal.return_pct if crude_signal else None,
        composite_score=None,
        yield_status=yield_signal.status if yield_signal else "no_data",
        crude_status=crude_signal.status if crude_signal else "no_data",
    )


def _decide_core(
    ctx: StrategyContext,
    yield_signal: MomentumSignal | None,
    crude_signal: MomentumSignal | None,
    fetch_error: str | None = None,
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
        # An ingestion failure -- HTTP error, stale/malformed board, missing
        # ticker, unparseable/stalled timestamp -- is not evidence the macro
        # picture has changed; it's an absence of observation, not an
        # observation of "neutral." See module docstring: retain a held
        # position rather than closing on a missing read, unlike the
        # valid-empty-result cases below (warming up, stale anchor,
        # neutral), which do still close because those genuinely are a
        # completed read that just doesn't support a position.
        meta = _base_meta(yield_signal, crude_signal)
        if held_symbol is not None:
            return no(
                f"Macro tickers unavailable ({fetch_error}); retaining the "
                f"held {held_type} position rather than closing on a "
                f"missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return no(f"Macro tickers unavailable: {fetch_error}", **meta)

    if yield_signal is None or crude_signal is None:
        # Not reachable via `_decide` (it only calls `_decide_core` with
        # both signals `None` alongside a `fetch_error`, never a lone
        # `None`), but `_decide_core` is called directly in tests and is
        # not itself responsible for enforcing that invariant.
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "No macro tracker signal available.", _base_meta(yield_signal, crude_signal),
        )

    meta = _base_meta(yield_signal, crude_signal)

    for label, sig in (("10Y yield", yield_signal), ("crude (USO)", crude_signal)):
        if sig.status == "no_data":
            return _maybe_close_unsupported(
                ctx, held_symbol, held_quantity, held_type, no,
                f"No {label} price history recorded yet.", meta,
            )
        if sig.status == "warming_up":
            return _maybe_close_unsupported(
                ctx, held_symbol, held_quantity, held_type, no,
                f"{label} tracker only has {sig.sample_count} observation(s) "
                f"so far; still warming up to a {sig.lookback_minutes:.0f}-"
                f"minute lookback.",
                meta,
            )
        if sig.status == "stale_anchor":
            return _maybe_close_unsupported(
                ctx, held_symbol, held_quantity, held_type, no,
                f"{label} tracker's nearest usable anchor is "
                f"{sig.anchor_age_minutes:.1f} minutes old -- a gap in "
                f"observations makes it too stale to trust for a "
                f"{sig.lookback_minutes:.0f}-minute lookback.",
                meta,
            )

    params = ctx.params or {}
    w_yield = params.get("w_yield", DEFAULT_W_YIELD)
    w_crude = params.get("w_crude", DEFAULT_W_CRUDE)
    score_threshold = params.get("score_threshold", DEFAULT_SCORE_THRESHOLD)

    score = -((w_yield * yield_signal.return_pct) + (w_crude * crude_signal.return_pct))
    meta["composite_score"] = score

    if -score_threshold < score < score_threshold:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Composite macro score is within the neutral band "
            f"(score={score:.5f}, threshold={score_threshold}).",
            meta,
        )

    supported_type = "call" if score >= score_threshold else "put"
    direction = "up" if supported_type == "call" else "down"

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; composite "
                f"macro score still supports it (score={score:.5f}).",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"Composite macro score now points {direction} "
                f"(score={score:.5f}); closing stale {held_type} position "
                f"before considering a new one."
            ),
            meta=meta,
            no=no,
        )

    row = ctx.snapshot.atm(supported_type)
    if not row:
        return no(f"No quoted {supported_type} in the snapshot to trade.", **meta)
    symbol = row["OptionSymbol"]

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}.", symbol=symbol, **meta)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
            **meta,
        )

    return Decision(
        action="buy",
        symbol=symbol,
        quantity=1,
        reason=(
            f"Composite macro score points {direction} (score={score:.5f}; "
            f"yield_ret={yield_signal.return_pct:.4f}, "
            f"crude_ret={crude_signal.return_pct:.4f} over the last "
            f"{yield_signal.lookback_minutes:.0f}m lookback); opening one "
            f"{supported_type}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            **meta,
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


def _reset_tracker_on_source_change(ticker: str, current_source: str | None, tracker: PriceHistoryTracker) -> PriceHistoryTracker:
    """Return `tracker`, or a fresh replacement if `ticker`'s provider
    changed since the last observation.

    `10Y` is published from two sources at two different scales
    (`$TNX.X` on the DXLink path is the yield x10; `^TNX` on the yfinance
    fallback is the rate directly), and both are flagged in
    `collector.py`. A trailing return is scale-invariant *within* one
    source, which is why a fixed-scale failover otherwise sails through
    every check in this module -- it injects a single spurious step
    (~-90% on failover, ~+900% on recovery) that reads as a genuine,
    maximum-conviction move. Resetting the tracker discards the
    now-incomparable prior history instead of computing a return across
    the boundary; the existing `"warming_up"` handling then naturally
    covers the gap until enough same-source samples accumulate again.
    """
    previous_source = _last_seen_source.get(ticker)
    _last_seen_source[ticker] = current_source
    if previous_source is not None and current_source is not None and current_source != previous_source:
        return PriceHistoryTracker(retain_minutes=DEFAULT_RETAIN_MINUTES)
    return tracker


def _decide(ctx: StrategyContext) -> Decision:
    global _last_recorded_timestamp, _yield_tracker, _crude_tracker

    if ctx.session_phase != "open":
        return _decide_core(ctx, None, None)

    try:
        snapshot = _reader.read()
    except Exception as exc:  # network failure, HTTP error, malformed JSON, etc.
        return _decide_core(ctx, None, None, fetch_error=str(exc))

    if snapshot.feed_stale:
        return _decide_core(
            ctx, None, None,
            fetch_error=f"tickers feed is marked stale (timestamp={snapshot.timestamp!r})",
        )

    yield_price = snapshot.price(YIELD_TICKER)
    crude_price = snapshot.price(CRUDE_TICKER)
    missing = [t for t, p in ((YIELD_TICKER, yield_price), (CRUDE_TICKER, crude_price)) if p is None]
    if missing:
        return _decide_core(
            ctx, None, None,
            fetch_error=f"missing price(s) for {', '.join(missing)}",
        )

    # A carried-forward (last-known) price reads as a live observation to
    # every check above -- it has a value, it isn't None, and it doesn't
    # trip feed_stale (that's a feed-level flag, not per-ticker). Recording
    # it would feed a frozen quote into the momentum trackers as a genuine
    # tick, yielding a confident-looking ~0% trailing return instead of the
    # "no fresh data" this actually is.
    stale = [t for t in (YIELD_TICKER, CRUDE_TICKER) if snapshot.is_stale(t)]
    if stale:
        return _decide_core(
            ctx, None, None,
            fetch_error=f"stale (carried-forward) price(s) for {', '.join(stale)}",
        )

    observed_at = _snapshot_observed_at(snapshot.timestamp)
    if observed_at is None:
        return _decide_core(
            ctx, None, None,
            fetch_error=f"unparseable tickers timestamp {snapshot.timestamp!r}",
        )

    params = ctx.params or {}
    max_tickers_age = params.get("max_tickers_age_minutes", DEFAULT_MAX_TICKERS_AGE_MINUTES)
    age_minutes = (ctx.now_et - observed_at).total_seconds() / 60.0
    if age_minutes > max_tickers_age:
        return _decide_core(
            ctx, None, None,
            fetch_error=(
                f"tickers snapshot is {age_minutes:.1f} minutes old "
                f"(limit={max_tickers_age}m) -- the feed looks stalled"
            ),
        )

    if snapshot.timestamp != _last_recorded_timestamp:
        _yield_tracker = _reset_tracker_on_source_change(YIELD_TICKER, snapshot.source.get(YIELD_TICKER), _yield_tracker)
        _crude_tracker = _reset_tracker_on_source_change(CRUDE_TICKER, snapshot.source.get(CRUDE_TICKER), _crude_tracker)
        _yield_tracker.observe(observed_at, yield_price)
        _crude_tracker.observe(observed_at, crude_price)
        _last_recorded_timestamp = snapshot.timestamp

    lookback_minutes = params.get("lookback_minutes", DEFAULT_LOOKBACK_MINUTES)
    max_overshoot = params.get("max_anchor_overshoot_minutes", DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES)

    yield_signal = compute_momentum(
        _yield_tracker.snapshot(), now=ctx.now_et,
        lookback_minutes=lookback_minutes, max_anchor_overshoot_minutes=max_overshoot,
    )
    crude_signal = compute_momentum(
        _crude_tracker.snapshot(), now=ctx.now_et,
        lookback_minutes=lookback_minutes, max_anchor_overshoot_minutes=max_overshoot,
    )
    return _decide_core(ctx, yield_signal, crude_signal)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
macro_cross_market_qqq = register(_decide)
