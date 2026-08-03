"""Session-relative extremity of the put/call open-interest ratio.

The put/call ratio (PCR) here is `sum(put OpenInterest) / sum(call
OpenInterest)` across every row in a single 0DTE options-chain snapshot. It
is a crowd-positioning gauge, not a price signal: it says how the resting
open interest is split between puts and calls right now, nothing about where
the underlying is going on its own.

`strategies/put_call_ratio.py` reads that ratio *contrarian*, not
directionally -- see that module's docstring for the crowd-positioning
argument. What this module provides is the piece that argument depends on:
turning one scalar PCR reading into a judgment of how *extreme* it is,
relative to the session's own history of readings so far, rather than
against a fixed universal threshold.

A fixed absolute threshold (e.g. "PCR > 1.2 is extreme") is the wrong tool
here for the same reason `momentum.py` computes a trailing return relative
to the instrument's own recent price rather than an absolute price level:
0DTE QQQ's baseline PCR is not 0DTE SPX's, is not equity-only PCR, and can
also drift session to session with dealer positioning and expiration mix.
What is stationary enough to threshold against is *this session's own
distribution of readings so far* -- so a reading is "extreme" when it is far
from what this session has been showing, measured in standard deviations
(a z-score), not when it crosses some hand-picked absolute number.

Design choice: the baseline (mean, stdev) used to score the current reading
is built from every *prior* recorded point in the session -- the current
reading itself is excluded from its own baseline. Folding the newest point
into the mean/stdev it's being scored against would mechanically pull the
baseline toward the very reading being judged, damping the z-score of
exactly the outlier readings this is meant to catch. This mirrors
`momentum.py`'s anchor concept: a signal is computed by comparing the newest
observation against a baseline built from *earlier* observations, never
against itself.

Needs a warm-up period before the baseline is trustworthy -- `min_samples`
prior points, analogous to momentum's `warming_up` state before a lookback
anchor exists. Below that, or with fewer than one prior observation, there
is no defensible baseline yet and the signal reports `status="warming_up"`
rather than guessing.

Kept as a separate, network-free, clock-free module from the decision logic
in `strategies/put_call_ratio.py`, for the same reason `momentum.py` is split
from `strategies/momentum_qqq.py`: the accumulation/statistics here are
independently testable with hand-built timestamps and ratios, without a live
snapshot, HTTP layer, or the strategy registry.

This is a heuristic, not a validated edge. Crowd-positioning contrarianism
("everyone's already bought puts, so who's left to sell into a rally?") is a
plausible story with real academic and practitioner interest, but it is not
a guarantee -- a persistently extreme PCR can also just mean the crowd is
right, or that dealer hedging flows dominate whatever retail positioning
this ratio is picking up. Treat `extreme_z_threshold` as a knob to be
back-tested and tuned, not a discovered constant.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

# Generous, fixed cap decoupled from any one strategy's configured
# min_samples -- the tracker is a shared singleton (one PCR history, not one
# per account/param-set), so it must hold enough history for the session,
# not reset every time a caller asks for a shorter/longer baseline.
DEFAULT_RETAIN_MINUTES = 24 * 60.0

DEFAULT_MIN_BASELINE_SAMPLES = 10


@dataclass(frozen=True)
class PCRPoint:
    observed_at: datetime
    pcr: float


@dataclass(frozen=True)
class PCRExtremitySignal:
    """The result of `compute_pcr_extremity`.

    `status` distinguishes *why* there's no trustworthy z_score, which
    matters for the audit trail and for deciding whether a held position
    should be closed:

      * "no_data" -- no observations at all yet.
      * "warming_up" -- observations exist, but fewer than `min_samples`
        *prior* readings exist to build a session baseline against. Same
        "absence of evidence" treatment as momentum's warming_up state.
      * "ok" -- `z_score` is a trustworthy measure of how extreme the
        current reading is relative to the session baseline so far.
    """

    current_pcr: float | None
    baseline_mean: float | None
    baseline_stdev: float | None
    baseline_sample_count: int
    sample_count: int
    z_score: float | None
    status: str


def compute_pcr_extremity(
    history: list[PCRPoint],
    min_samples: int = DEFAULT_MIN_BASELINE_SAMPLES,
) -> PCRExtremitySignal:
    """Pure function: how extreme is the newest reading in `history`
    relative to a baseline built from every earlier reading?

    `history` need not be pre-sorted; the newest point (by `observed_at`) is
    taken as the current reading, and every other point becomes the
    baseline population. Requires at least `min_samples` baseline points
    before reporting `status="ok"` -- fewer than that and the mean/stdev
    would be too noisy to trust.
    """
    if not history:
        return PCRExtremitySignal(
            current_pcr=None,
            baseline_mean=None,
            baseline_stdev=None,
            baseline_sample_count=0,
            sample_count=0,
            z_score=None,
            status="no_data",
        )

    ordered = sorted(history, key=lambda p: p.observed_at)
    current = ordered[-1]
    baseline_points = ordered[:-1]
    sample_count = len(ordered)

    if len(baseline_points) < min_samples:
        return PCRExtremitySignal(
            current_pcr=current.pcr,
            baseline_mean=None,
            baseline_stdev=None,
            baseline_sample_count=len(baseline_points),
            sample_count=sample_count,
            z_score=None,
            status="warming_up",
        )

    baseline_values = [p.pcr for p in baseline_points]
    mean = statistics.mean(baseline_values)
    stdev = statistics.pstdev(baseline_values)

    # A perfectly flat baseline (every prior reading identical) has no
    # variability to measure extremity against -- report z_score=0.0 (not
    # extreme) rather than dividing by zero.
    z_score = (current.pcr - mean) / stdev if stdev > 0 else 0.0

    return PCRExtremitySignal(
        current_pcr=current.pcr,
        baseline_mean=mean,
        baseline_stdev=stdev,
        baseline_sample_count=len(baseline_points),
        sample_count=sample_count,
        z_score=z_score,
        status="ok",
    )


class PCRHistoryTracker:
    """Accumulates PCR observations across polling cycles.

    Points older than `retain_minutes` are dropped on each `observe()` so
    memory stays bounded across a long-running process; `retain_minutes`
    should exceed the length of one trading session.
    """

    def __init__(self, retain_minutes: float = DEFAULT_RETAIN_MINUTES):
        self._retain = timedelta(minutes=retain_minutes)
        self._points: list[PCRPoint] = []

    def observe(self, observed_at: datetime, pcr: float) -> None:
        self._points.append(PCRPoint(observed_at, pcr))
        cutoff = observed_at - self._retain
        self._points = [p for p in self._points if p.observed_at >= cutoff]

    def snapshot(self) -> list[PCRPoint]:
        return list(self._points)
