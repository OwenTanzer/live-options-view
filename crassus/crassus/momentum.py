"""Time-series momentum over the QQQ underlying price.

This is the textbook time-series momentum construction (Moskowitz, Ooi &
Pedersen (2012), "Time Series Momentum"): compare the current price to the
price some fixed lookback window ago and use the sign/magnitude of that
trailing return as the signal, rather than ranking the return against other
assets (cross-sectional momentum needs a universe of tickers to rank against;
this repo only ever quotes one underlying, QQQ, so time-series is the
applicable variant -- see `strategies/momentum_qqq.py`'s docstring).

No new data source: the price observed here is `MarketSnapshot.underlying_price`,
the same durable QQQ mark every other strategy already reads off the R2
snapshot every polling cycle. What's new is retaining a rolling window of
those observations across cycles so a trailing return can be computed at all
-- the market layer (`market.py`) only ever hands a strategy the single most
recent snapshot.

Kept as a separate, network-free, clock-free module from the decision logic
in `strategies/momentum_qqq.py` for the same reason `trump_sentiment.py` and
`sentiment.py` are split from their respective strategies: the accumulation/
math here is independently testable (feed it hand-built timestamps and
prices) without needing a live snapshot, a fake HTTP layer, or the strategy
registry at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_LOOKBACK_MINUTES = 60.0
DEFAULT_MAX_ANCHOR_STALENESS_MULTIPLE = 3.0

# Generous, fixed cap decoupled from any one strategy's configured lookback --
# the tracker is a shared singleton (one QQQ price history, not one per
# account/param-set), so it must hold enough history for the longest lookback
# any caller might reasonably configure, for the lifetime of one trading day.
DEFAULT_RETAIN_MINUTES = 24 * 60.0


@dataclass(frozen=True)
class PricePoint:
    observed_at: datetime
    price: float


@dataclass(frozen=True)
class MomentumSignal:
    """The result of `compute_momentum`.

    `status` distinguishes *why* there's no trustworthy return_pct, which
    matters for the audit trail and for deciding whether a held position
    should be closed:

      * "no_data" -- no observations at all yet.
      * "warming_up" -- observations exist, but none is old enough yet to
        serve as the lookback anchor. Transient; resolves itself as time
        passes, same status every polling cycle until it does.
      * "stale_anchor" -- the *newest* observation old enough to qualify as
        an anchor is nonetheless older than `max_anchor_staleness_multiple *
        lookback_minutes` -- i.e. there's a gap in the observation history
        (an outage, a paused runner, an overnight/weekend boundary) wide
        enough that comparing against it would silently answer a different
        question than "return over the last `lookback_minutes` minutes."
      * "ok" -- `return_pct` is a trustworthy trailing return.
    """

    lookback_minutes: float
    current_price: float | None
    anchor_price: float | None
    return_pct: float | None
    sample_count: int
    anchor_age_minutes: float | None
    status: str


def compute_momentum(
    history: list[PricePoint],
    now: datetime,
    lookback_minutes: float = DEFAULT_LOOKBACK_MINUTES,
    max_anchor_staleness_multiple: float = DEFAULT_MAX_ANCHOR_STALENESS_MULTIPLE,
) -> MomentumSignal:
    """Pure function: trailing return of the newest point in `history` vs.
    the best available anchor `lookback_minutes` ago.

    `history` need not be sorted by the caller; every point's age is measured
    against `now` directly, and the newest point (by `observed_at`) is taken
    as the current price. The anchor is the *newest* point that is still at
    least `lookback_minutes` old -- the freshest candidate that actually
    satisfies the lookback, which minimizes how much a normal ~60s polling
    cadence overshoots the requested window. If that candidate is itself
    older than `lookback_minutes * max_anchor_staleness_multiple`, it is
    rejected as a stale anchor rather than silently used.
    """
    if not history:
        return MomentumSignal(
            lookback_minutes=lookback_minutes,
            current_price=None,
            anchor_price=None,
            return_pct=None,
            sample_count=0,
            anchor_age_minutes=None,
            status="no_data",
        )

    ordered = sorted(history, key=lambda p: p.observed_at)
    current = ordered[-1]
    sample_count = len(ordered)
    target_age = timedelta(minutes=lookback_minutes)
    max_age = timedelta(minutes=lookback_minutes * max_anchor_staleness_multiple)

    anchor: PricePoint | None = None
    for point in reversed(ordered):
        age = now - point.observed_at
        if age >= target_age:
            anchor = point
            anchor_age = age
            break

    if anchor is None:
        return MomentumSignal(
            lookback_minutes=lookback_minutes,
            current_price=current.price,
            anchor_price=None,
            return_pct=None,
            sample_count=sample_count,
            anchor_age_minutes=None,
            status="warming_up",
        )

    if anchor_age > max_age:
        return MomentumSignal(
            lookback_minutes=lookback_minutes,
            current_price=current.price,
            anchor_price=anchor.price,
            return_pct=None,
            sample_count=sample_count,
            anchor_age_minutes=anchor_age.total_seconds() / 60.0,
            status="stale_anchor",
        )

    return_pct = (current.price / anchor.price) - 1.0 if anchor.price else None
    return MomentumSignal(
        lookback_minutes=lookback_minutes,
        current_price=current.price,
        anchor_price=anchor.price,
        return_pct=return_pct,
        sample_count=sample_count,
        anchor_age_minutes=anchor_age.total_seconds() / 60.0,
        status="ok",
    )


class PriceHistoryTracker:
    """Accumulates underlying-price observations across polling cycles.

    Points older than `retain_minutes` are dropped on each `observe()` so
    memory stays bounded across a long-running process; `retain_minutes`
    should exceed the longest lookback any caller will ask `compute_momentum`
    for, plus its staleness allowance.
    """

    def __init__(self, retain_minutes: float = DEFAULT_RETAIN_MINUTES):
        self._retain = timedelta(minutes=retain_minutes)
        self._points: list[PricePoint] = []

    def observe(self, observed_at: datetime, price: float) -> None:
        self._points.append(PricePoint(observed_at, price))
        cutoff = observed_at - self._retain
        self._points = [p for p in self._points if p.observed_at >= cutoff]

    def snapshot(self) -> list[PricePoint]:
        return list(self._points)
