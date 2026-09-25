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
    prepared, drop_reasons = prepare_model_frame(df)
    assert drop_reasons["_total_dropped_rows"] == 1
    assert drop_reasons["vol_30min"] == 1
    assert drop_reasons["_total_kept_rows"] == len(prepared)
    assert "log_C" in prepared.columns
    assert "cluster_id" in prepared.columns
    assert prepared["side_above"].isin([0, 1]).all()


def test_prepare_model_frame_attributes_drops_to_the_specific_missing_field():
    """Distinct fields missing on distinct rows must be attributed to the
    right cause, not just counted as one lump 'dropped' total."""
    df = _synthetic_dataset()
    df.loc[0, "vol_30min"] = np.nan
    df.loc[1, "prev_5min_return"] = np.nan
    _, drop_reasons = prepare_model_frame(df)
    assert drop_reasons["vol_30min"] == 1
    assert drop_reasons["prev_5min_return"] == 1
    assert drop_reasons["_total_dropped_rows"] == 2


def test_prepare_model_frame_does_not_clip_negative_measurements():
    """The review's finding: negative/invalid measurement values must not
    be silently clipped to zero -- they should surface as nonfinite after
    log1p (for a value <= -1) or otherwise be left visible, never masked."""
    df = _synthetic_dataset()
    df.loc[0, "C"] = -5.0  # log1p(-5) is NaN -- must propagate, not clip-to-zero first
    prepared, drop_reasons = prepare_model_frame(df)
    assert drop_reasons["log_C"] == 1


def test_prepare_model_frame_rejects_small_negative_measurement_between_minus_one_and_zero():
    """Review 5252887319 finding 1's exact gap: log1p(-0.5) is finite
    (~-0.69), so the plain finite-check alone lets a small negative,
    physically-impossible unsigned measurement silently enter the model.
    The explicit non-negativity check must catch this even though log1p
    itself does not."""
    df = _synthetic_dataset()
    df.loc[0, "C"] = -0.5
    assert np.isfinite(np.log1p(-0.5))  # confirms the finite-check alone would miss this
    prepared, drop_reasons = prepare_model_frame(df)
    assert drop_reasons["C"] == 1
    assert 0 not in prepared.index


def test_prepare_model_frame_rejects_zero_or_negative_anchor_spot_when_present():
    df = _synthetic_dataset()
    df["anchor_spot"] = 700.0
    df["outcome_spot"] = 701.0
    df.loc[0, "anchor_spot"] = 0.0
    df.loc[1, "outcome_spot"] = -1.0
    prepared, drop_reasons = prepare_model_frame(df)
    assert drop_reasons["anchor_spot"] == 1
    assert drop_reasons["outcome_spot"] == 1
    assert drop_reasons["_total_dropped_rows"] == 2


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
    assert "n_anchor_clusters" in out.columns


def test_day_by_day_uses_anchor_clustering_not_independent_row_uncertainty():
    """The review's exact finding: day_by_day() must use the same
    shared-anchor clustered covariance as the pooled model -- not fall
    back to HC1 (independent-row) uncertainty, which ignores that 6
    strikes at one anchor share a future price path. Verified by checking
    day_by_day's per-day SE against an explicit same-subset clustered fit."""
    df = _synthetic_dataset(n_days=2, anchors_per_day=25, seed=7)
    prepared, _ = prepare_model_frame(df)
    out = day_by_day(prepared, "log_C")
    for _, row in out.iterrows():
        subset = prepared[prepared["date"] == row["date"]]
        direct = fit_clustered_ols(subset, "log_C")
        assert row["se"] == direct.bse["log_C"]
        assert row["n_anchor_clusters"] == subset["cluster_id"].nunique()


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


def test_compare_predictors_reports_sd_scaled_coefficients_for_comparability():
    """The review's finding: raw coefficients on log_C/log_A/log_gamma_alone
    are not comparable across predictors with different scales. The
    SD-scaled coefficient must equal coef * that predictor's own std."""
    df = _synthetic_dataset(n_days=3, seed=3)
    prepared, _ = prepare_model_frame(df)
    out = compare_predictors(prepared)
    for _, row in out[out["predictor"] != "(baseline only)"].iterrows():
        expected_std = prepared[row["predictor"]].std()
        assert row["predictor_std"] == expected_std
        assert row["sd_scaled_coef"] == row["coef"] * expected_std
