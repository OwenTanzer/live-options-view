"""Pure VWAP/RVOL math, shared by collector.py and its Python tests.

No I/O, no global state, no R2/websocket/collector-loop dependencies --
every function here takes its inputs (a current accumulator state, a new
observation, a baseline list) and returns a new value, so collector.py's own
module-level state (mirroring its existing `_prev_vol`/`_last_spot` pattern)
can be exercised with hand-built fixtures in tests/verify_collector_vwap_rvol.py
without spinning up the whole collector, tastytrade auth, or a DXLink
connection -- this file is collector.py's first extracted, independently
tested logic.

VWAP here is a **spot-price-at-snapshot-time x volume-delta-since-last-snapshot**
approximation, not tick-accurate volume-weighted-by-trade-price VWAP -- the
collector only ever sees the underlying's own cumulative dayVolume and a
mid/last price once per SNAPSHOT_SECS-second poll, not a full trade tape.
True tick-level VWAP (weighting by each individual trade's own price) would
need each DXLink Trade event's price captured alongside dayVolume -- a
bigger change to the feed-ingestion layer, deferred; see
docs/plans/2026-07-vwap-rvol.md.

RVOL compares the session's cumulative volume as of a given time-of-day
bucket against a rolling historical average for that same bucket -- not two
rates. A bucket with fewer than `min_days_required` historical samples is
reported as "insufficient_history" rather than a real multiple, the same way
`crassus/crassus/momentum.py`'s `MomentumSignal.status` separates "nothing"
from "not enough yet" from "trustworthy."
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

# -- VWAP ----------------------------------------------------------------


@dataclass(frozen=True)
class VwapState:
    session_date: date | None = None
    # observed_at of the first tick actually folded into the accumulator this
    # session -- lets a consumer tell a VWAP that has covered the whole
    # session-to-date apart from one that started late (a restart without
    # full recovery, or a delayed process start), see is_partial_session().
    session_started_at: datetime | None = None
    # observed_at of the most recent tick folded in (or skipped-but-seen) --
    # enforces that provider events are only ever applied in non-decreasing
    # timestamp order, see accumulate_vwap's ordering guard below.
    last_observed_at: datetime | None = None
    cum_pv: float = 0.0
    cum_vol: int = 0
    last_dayvolume: int | None = None
    vwap: float | None = None
    vwap_ts: str | None = None


def reset_if_new_session(state: VwapState, today: date) -> VwapState:
    """A new trading day means a fresh VWAP accumulation from zero."""
    if state.session_date == today:
        return state
    return VwapState(session_date=today)


def accumulate_vwap(
    state: VwapState,
    *,
    price: float | None,
    raw_day_volume: int | None,
    observed_at: datetime | None,
) -> VwapState:
    """One snapshot tick's worth of VWAP accumulation.

    Mirrors collector.py's existing per-option VolDelta math
    (`vol_delta = max(0, vol - _prev_vol.get(sym, vol))`), applied to the
    underlying's own cumulative dayVolume instead of a per-strike one.

    `observed_at` must be the *provider's* own timestamp for the price/volume
    pair being accumulated (e.g. the DXLink bid/ask/last event that produced
    `price` -- see collector.py's `_compute_underlying_market`), not the
    collector's local wall-clock read time. That's what makes the ordering
    guard below meaningful: a `None` timestamp (no paired provider
    observation available) or one that isn't strictly newer than the last
    tick actually folded in is treated as unusable -- an out-of-order or
    duplicate delivery -- and the accumulator is left untouched rather than
    risk computing a delta against, or from, an observation that arrived out
    of sequence.
    """
    if raw_day_volume is None or observed_at is None:
        return state

    if state.last_observed_at is not None and observed_at <= state.last_observed_at:
        return state

    if state.last_dayvolume is None:
        # First volume reading this session (or since a restart with no
        # recovered state) -- nothing to delta against yet, just record the
        # baseline to delta the *next* tick against.
        return replace(state, last_dayvolume=raw_day_volume, last_observed_at=observed_at)

    delta_vol = max(0, raw_day_volume - state.last_dayvolume)
    if delta_vol == 0 or price is None:
        # Either no new volume ticked, or volume ticked but there's no price
        # to weight it by this cycle -- no VWAP update either way, but the
        # dayvolume baseline still advances so a future delta isn't computed
        # across this gap.
        return replace(state, last_dayvolume=raw_day_volume, last_observed_at=observed_at)

    cum_pv = state.cum_pv + price * delta_vol
    cum_vol = state.cum_vol + delta_vol
    return VwapState(
        session_date=state.session_date,
        session_started_at=state.session_started_at or observed_at,
        last_observed_at=observed_at,
        cum_pv=cum_pv,
        cum_vol=cum_vol,
        last_dayvolume=raw_day_volume,
        vwap=round(cum_pv / cum_vol, 4),
        vwap_ts=observed_at.isoformat(),
    )


def is_partial_session(state: VwapState, session_start: datetime, max_late_start_s: float = 300.0) -> bool | None:
    """Whether this VWAP's coverage started meaningfully after the session's
    official open -- a restart that couldn't recover prior accumulator state,
    or a delayed process start -- rather than running from the true open.
    `None` (not True/False) when nothing has been accumulated yet at all;
    that's "no_data", a distinct condition from "partial."
    """
    if state.session_started_at is None:
        return None
    return (state.session_started_at - session_start).total_seconds() > max_late_start_s


# -- RVOL ------------------------------------------------------------------


def bucket_label(et: datetime, bucket_minutes: int = 5) -> str:
    """Floor a timestamp to its time-of-day bucket, e.g. 10:32:07 -> '10:30'."""
    floored_minute = (et.minute // bucket_minutes) * bucket_minutes
    return et.replace(minute=floored_minute, second=0, microsecond=0).strftime("%H:%M")


@dataclass(frozen=True)
class RvolResult:
    status: str  # "no_data" | "insufficient_history" | "ok"
    multiple: float | None = None
    baseline_volume: float | None = None
    baseline_days_used: int = 0


def compute_rvol(
    session_volume: int | None,
    baseline_samples: list[dict] | None,
    *,
    min_days_required: int,
) -> RvolResult:
    """`baseline_samples` is this bucket's historical readings:
    `[{"date": "YYYY-MM-DD", "cum_volume": int}, ...]`.
    """
    if session_volume is None or not baseline_samples:
        return RvolResult(status="no_data")

    n = len(baseline_samples)
    if n < min_days_required:
        return RvolResult(status="insufficient_history", baseline_days_used=n)

    baseline_volume = sum(s["cum_volume"] for s in baseline_samples) / n
    if not baseline_volume:
        return RvolResult(status="no_data", baseline_days_used=n)

    return RvolResult(
        status="ok",
        multiple=round(session_volume / baseline_volume, 4),
        baseline_volume=baseline_volume,
        baseline_days_used=n,
    )


def prune_baseline_samples(samples: list[dict], today: date, lookback_days: int) -> list[dict]:
    """Drop samples older than `lookback_days` calendar days before `today`,
    and any sample dated `today` itself (a session in progress is never a
    complete historical reading -- only `append_session_reading`, called at
    end-of-session, may add today's entry).
    """
    cutoff = today - timedelta(days=lookback_days)
    kept = []
    for s in samples:
        try:
            sample_date = date.fromisoformat(s["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if cutoff <= sample_date < today:
            kept.append(s)
    return kept


def append_session_reading(samples: list[dict], today: date, cum_volume: int) -> list[dict]:
    """Add today's final cum_volume reading for one bucket.

    Replaces any existing entry for `today` rather than duplicating it, so
    calling this twice for the same session (e.g. a retried end-of-session
    write) is idempotent.
    """
    today_str = today.isoformat()
    kept = [s for s in samples if s.get("date") != today_str]
    kept.append({"date": today_str, "cum_volume": cum_volume})
    return kept


# -- freshness ---------------------------------------------------------------


def classify_freshness(
    *,
    inside_session_window: bool,
    spot_ts: datetime | None,
    now: datetime,
    stale_after_s: float,
) -> str:
    """"closed" outside the session window (the whole record describes a
    prior session, not "now"); "stale" if no spot observation exists yet or
    it's older than `stale_after_s`; "live" otherwise.
    """
    if not inside_session_window:
        return "closed"
    if spot_ts is None:
        return "stale"
    age_s = (now - spot_ts).total_seconds()
    return "live" if age_s <= stale_after_s else "stale"
