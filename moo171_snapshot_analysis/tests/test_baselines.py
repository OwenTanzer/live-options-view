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
    assert r == pytest_close((707.0 - 700.0) / 700.0)


def pytest_close(x, tol=1e-9):
    return x


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
    v = preceding_volatility(spot, "20260910", anchor_ts)
    assert math.isnan(v)


def test_preceding_volatility_computes_with_enough_observations():
    times = [f"09:{30+i:02d}:00" for i in range(6)]
    prices = [700.0, 700.5, 699.8, 700.2, 700.9, 700.4]
    spot = _spot_rows("20260910", list(zip(times, prices)))
    anchor_ts = pd.Timestamp("2026-09-10 09:35:00", tz="America/New_York")
    v = preceding_volatility(spot, "20260910", anchor_ts)
    assert v > 0
    expected = np.std(np.diff(np.log(prices)), ddof=1)
    assert abs(v - expected) < 1e-12


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
