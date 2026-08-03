"""Session-relative baseline for a scalar signal (here, 25-delta skew).

This is the same "accumulate observations across polling cycles, then judge
the newest one against its own recent history" shape as `momentum.py`, but
answering a different question. `momentum.compute_momentum` compares the
*current* value to a value from a fixed *lookback window in the past*
(a trailing return). This module instead compares the current value to the
*distribution* of everything observed so far this session (a z-score against
a running mean/stdev) -- there is no natural "N minutes ago" anchor for an
IV-skew reading the way there is for a price return; skew has no time-series-
momentum literature to borrow a lookback convention from. What is reusable
without modification is the accumulation primitive itself: `PricePoint` and
`PriceHistoryTracker` from `momentum.py` are generic on "a scalar observed at
a timestamp" -- nothing about them is price-specific -- so `iv_skew.py` feeds
skew values into the very same tracker class rather than this module
duplicating that bookkeeping (prune-by-age, accumulate-in-order) a second
time. Only the *judgment* function differs, hence a new `compute_skew_signal`
here instead of reusing `compute_momentum`.

`SkewSignal.status` intentionally mirrors `MomentumSignal.status`'s "absence
of evidence vs. evidence of nothing" vocabulary, minus the states that don't
apply here: there is no `"stale_anchor"` (no lookback anchor to go stale --
the baseline is the whole retained history, not one specific past point), but
`"no_data"` (nothing observed yet) and `"warming_up"` (some observations
exist, but fewer than `min_samples` -- too few to trust a mean/stdev) both
carry over unchanged in meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Sequence

from .momentum import PricePoint  # reused as-is: (observed_at, price) is a generic scalar-timeseries point, not price-specific

DEFAULT_MIN_SAMPLES = 10
DEFAULT_RETAIN_MINUTES = 24 * 60.0


@dataclass(frozen=True)
class SkewSignal:
    """The result of `compute_skew_signal`.

    `baseline_mean`/`baseline_stdev` are computed over the *entire* retained
    history, including the current reading itself -- the session's own
    running distribution, not a strictly-prior "everything but now" baseline.
    That keeps the definition simple and matches how a trader would describe
    it informally ("today's skew is N standard deviations from today's
    average skew"), at the cost of the current point mildly pulling on its
    own baseline; with `min_samples` defaulting to 10, one point's influence
    on the mean/stdev is small enough not to matter in practice.
    """

    current_skew: float | None
    baseline_mean: float | None
    baseline_stdev: float | None
    sample_count: int
    z_score: float | None
    status: str  # "no_data" | "warming_up" | "ok"


def compute_skew_signal(
    history: Sequence[PricePoint],
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> SkewSignal:
    """Pure function: how extreme is the newest point in `history` relative
    to the mean/stdev of everything retained so far.

    `history` need not be pre-sorted; the newest point (by `observed_at`) is
    taken as the current reading. Fewer than `min_samples` total points is
    `"warming_up"` -- a mean/stdev from a handful of points isn't a baseline
    worth trading against, the same "not enough yet" reasoning
    `compute_momentum` applies to a too-young anchor. A baseline with zero
    variance (every retained reading identical so far -- possible early in a
    session with a coarse IV feed) reports `z_score=0.0` rather than dividing
    by zero; a flat history has nothing extreme in it by construction.
    """
    if not history:
        return SkewSignal(
            current_skew=None,
            baseline_mean=None,
            baseline_stdev=None,
            sample_count=0,
            z_score=None,
            status="no_data",
        )

    ordered = sorted(history, key=lambda p: p.observed_at)
    current = ordered[-1].price
    sample_count = len(ordered)

    if sample_count < min_samples:
        return SkewSignal(
            current_skew=current,
            baseline_mean=None,
            baseline_stdev=None,
            sample_count=sample_count,
            z_score=None,
            status="warming_up",
        )

    values = [p.price for p in ordered]
    mean = fmean(values)
    stdev = pstdev(values)

    if stdev == 0.0:
        return SkewSignal(
            current_skew=current,
            baseline_mean=mean,
            baseline_stdev=stdev,
            sample_count=sample_count,
            z_score=0.0,
            status="ok",
        )

    return SkewSignal(
        current_skew=current,
        baseline_mean=mean,
        baseline_stdev=stdev,
        sample_count=sample_count,
        z_score=(current - mean) / stdev,
        status="ok",
    )
