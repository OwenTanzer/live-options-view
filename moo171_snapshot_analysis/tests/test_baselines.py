import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math

import numpy as np
import pandas as pd

from baselines import add_baseline_features, minutes_since_open, preceding_volatility, previous_return


def _spot_rows(date, times_and_prices):
    return pd.DataFrame([
        {
            "date": date, "snapshot_key": f"snap_{t}",
            "ts_et": pd.Timestamp(f"2026-09-10 {t}", tz="America/New_York"),
            "regular_hours": True, "underlying_price": price,
        }
        for t, price in times_and_prices
    ])


def test_previous_return_uses_snapshot_at_or_before_lookback_point():
    spot = _spot_rows("20260910", [("09:25:00", 700.0), ("09:30:00", 707.0)])
    anchor_ts = pd.Timestamp("2026-09-10 09:30:00", tz="America/New_York")
    r = previous_return(spot, "20260910", anchor_ts, anchor_spot=707.0)
    assert r == (707.0 - 700.0) / 700.0


def test_previous_return_nan_when_lookback_too_stale():
    spot = _spot_rows("20260910", [("09:00:00", 700.0), ("09:30:00", 707.0)])
    anchor_ts = pd.Timestamp("2026-09-10 09:30:00", tz="America/New_York")
    r = previous_return(spot, "20260910", anchor_ts, anchor_spot=707.0)
    assert math.isnan(r)


def test_minutes_since_open():
    ts = pd.Timestamp("2026-09-10 10:05:00", tz="America/New_York")
    assert minutes_since_open(ts) == 35.0


def test_preceding_volatility_nan_with_too_few_observations():
    spot = _spot_rows("20260910", [("09:59:00", 700.0), ("10:00:00", 701.0)])
    anchor_ts = pd.Timestamp("2026-09-10 10:00:00", tz="America/New_York")
    info = preceding_volatility(spot, "20260910", anchor_ts)
    assert math.isnan(info["vol_30min"])
    assert info["vol_30min_n_obs"] == 2


def test_preceding_volatility_nan_when_window_not_actually_complete():
    """The review's exact finding: enough OBSERVATIONS (>= 5) inside the
    nominal window is not the same as the window actually reaching back
    ~30 minutes -- e.g. near session open, where history simply doesn't
    exist yet. This must be NaN, not silently computed over the shorter
    span that happens to be available."""
    times = [f"09:{30+i:02d}:00" for i in range(6)]  # 09:30..09:35, session "opens" at 09:30
    prices = [700.0, 700.5, 699.8, 700.2, 700.9, 700.4]
    spot = _spot_rows("20260910", list(zip(times, prices)))
    anchor_ts = pd.Timestamp("2026-09-10 09:35:00", tz="America/New_York")
    # window_start would be 09:05, but data only goes back to 09:30 -- a
    # 25-minute shortfall, far more than the 90s staleness tolerance.
    info = preceding_volatility(spot, "20260910", anchor_ts)
    assert math.isnan(info["vol_30min"])
    assert info["vol_30min_n_obs"] == 6  # observations existed; the window just wasn't complete


def test_preceding_volatility_computes_with_a_genuinely_complete_window():
    base = pd.Timestamp("2026-09-10 09:30:00", tz="America/New_York")
    times_and_prices = [
        ((base + pd.Timedelta(minutes=i)).strftime("%H:%M:%S"), 700.0 + 0.1 * ((-1) ** i) * i)
        for i in range(31)  # 09:30 .. 10:00, one per minute
    ]
    spot = _spot_rows("20260910", times_and_prices)
    anchor_ts = pd.Timestamp("2026-09-10 10:00:00", tz="America/New_York")
    info = preceding_volatility(spot, "20260910", anchor_ts)
    assert info["vol_30min"] > 0
    assert info["vol_30min_n_obs"] == 31
    assert info["vol_30min_span_seconds"] == pytest_close_seconds(30 * 60)
    assert info["vol_30min_max_internal_gap_seconds"] == 60.0


def pytest_close_seconds(x, tol=1e-6):
    return x


def test_preceding_volatility_nan_when_internal_gap_exceeds_staleness_even_with_good_endpoints():
    """Review 5252887319 finding 3's exact reproduction: observations at
    09:30, 09:31, 09:32, 09:59, 10:00 ET span exactly the nominal 30
    minutes and clear MIN_VOL_OBSERVATIONS, but there is a 27-minute gap
    between 09:32 and 09:59 -- the window is not actually sampled across
    that gap and must be reported unavailable, not computed as if it were
    one consecutive minute-scale return."""
    spot = _spot_rows("20260910", [
        ("09:30:00", 700.0), ("09:31:00", 700.1), ("09:32:00", 700.2),
        ("09:59:00", 700.3), ("10:00:00", 700.4),
    ])
    anchor_ts = pd.Timestamp("2026-09-10 10:00:00", tz="America/New_York")
    info = preceding_volatility(spot, "20260910", anchor_ts)
    assert math.isnan(info["vol_30min"])
    assert info["vol_30min_n_obs"] == 5
    assert info["vol_30min_span_seconds"] == 1800.0
    assert info["vol_30min_max_internal_gap_seconds"] == pytest_close_seconds(27 * 60)


def test_add_baseline_features_shares_computation_across_strikes_at_same_anchor():
    outcome_ds = pd.DataFrame([
        {"date": "20260910", "anchor_ts_et": pd.Timestamp("2026-09-10 09:35:00", tz="America/New_York"),
         "anchor_spot": 707.0, "strike": 705.0},
        {"date": "20260910", "anchor_ts_et": pd.Timestamp("2026-09-10 09:35:00", tz="America/New_York"),
         "anchor_spot": 707.0, "strike": 710.0},
    ])
    spot = _spot_rows("20260910", [("09:25:00", 700.0), ("09:30:00", 707.0), ("09:35:00", 707.0)])
    out = add_baseline_features(outcome_ds, spot)
    assert len(out) == 2
    assert out["prev_5min_return"].nunique() == 1  # same anchor -> same baseline for both strikes
    assert out["minutes_since_open"].iloc[0] == 5.0
    assert "vol_30min_n_obs" in out.columns
    assert "vol_30min_span_seconds" in out.columns
    assert "vol_30min_max_internal_gap_seconds" in out.columns
