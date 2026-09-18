import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from regression import compare_predictors, day_by_day, fit_clustered_ols, leave_one_day_out, prepare_model_frame


def _synthetic_dataset(n_days=3, anchors_per_day=20, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        date = f"2026091{d}"
        for a in range(anchors_per_day):
            anchor_ts = pd.Timestamp("2026-09-10 09:30", tz="America/New_York") + pd.Timedelta(minutes=5 * a)
            for side, distance in [("above", 5.0), ("above", 10.0), ("above", 15.0),
                                    ("below", -5.0), ("below", -10.0), ("below", -15.0)]:
                c_val = rng.uniform(1e6, 1e9)
                # Plant a real positive relationship between log(C) and toward.
                toward = 0.00002 * np.log1p(c_val) + rng.normal(0, 0.0003)
                rows.append({
                    "date": date, "anchor_ts_et": anchor_ts, "strike": 700 + distance,
                    "side": side, "distance": distance, "abs_distance": abs(distance),
                    "C": c_val, "A": rng.uniform(0, 1e7), "gamma_alone": rng.uniform(0, 1),
                    "unweighted_oi": rng.uniform(0, 1000), "unweighted_activity": rng.uniform(0, 500),
                    "prev_5min_return": rng.normal(0, 0.0005), "vol_30min": rng.uniform(0.0001, 0.001),
                    "minutes_since_open": 5.0 * a, "toward": toward,
                })
    return pd.DataFrame(rows)


def test_prepare_model_frame_adds_transforms_and_drops_nan_rows():
    df = _synthetic_dataset()
    df.loc[0, "vol_30min"] = np.nan
    prepared, dropped = prepare_model_frame(df)
    assert dropped == 1
    assert "log_C" in prepared.columns
    assert "cluster_id" in prepared.columns
    assert prepared["side_above"].isin([0, 1]).all()


def test_fit_clustered_ols_recovers_planted_direction():
    df = _synthetic_dataset(n_days=4, anchors_per_day=30, seed=1)
    prepared, _ = prepare_model_frame(df)
    res = fit_clustered_ols(prepared, "log_C")
    assert res.params["log_C"] > 0  # planted relationship is positive


def test_day_by_day_returns_one_row_per_date():
    df = _synthetic_dataset(n_days=3)
    prepared, _ = prepare_model_frame(df)
    out = day_by_day(prepared, "log_C")
    assert len(out) == 3
    assert set(out["date"]) == set(prepared["date"].unique())


def test_leave_one_day_out_returns_one_row_per_held_out_date():
    df = _synthetic_dataset(n_days=3)
    prepared, _ = prepare_model_frame(df)
    out = leave_one_day_out(prepared, "log_C")
    assert len(out) == 3
    for _, row in out.iterrows():
        assert row["n"] < len(prepared)  # strictly fewer rows than the full set


def test_compare_predictors_includes_baseline_and_all_three_predictors():
    df = _synthetic_dataset(n_days=3)
    prepared, _ = prepare_model_frame(df)
    out = compare_predictors(prepared)
    assert set(out["predictor"]) == {"(baseline only)", "log_C", "log_A", "log_gamma_alone"}
