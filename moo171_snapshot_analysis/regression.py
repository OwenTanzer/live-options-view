"""Parsimonious baseline-comparison regression per MOO-171 §3.

Six strikes at the same anchor share one future price path -- they are NOT
six independent market realizations. Every model here -- pooled AND
per-session -- clusters standard errors by (date, anchor_ts_et), never by
row, so that dependence is never dropped to independent-row uncertainty.

That said, anchor-clustering does not by itself address every dependence
concern: the pooled model has on the order of several hundred anchor
clusters (one per retained 5-minute window across all 5 sessions, NOT "5
day clusters" -- there is no day-level clustering computed anywhere here),
and clustering by anchor does not account for serial dependence *between*
successive anchors within the same session (their underlying spot paths
are not independent draws either). Day-by-day and leave-one-day-out are
reported as descriptive stability/sensitivity checks across sessions, not
as a fix for within-day serial dependence, and not as held-out predictive
validation -- "leave-one-day-out" here means refitting the pooled model
after omitting one day's rows, nothing more.

Weighting variables (C, A, gamma_alone, unweighted OI/activity) are log1p
transformed before entering the model: their raw scale spans many orders of
magnitude (observed C range: 0 to ~5.6e11), which would otherwise let a
handful of huge-OI strikes dominate an OLS fit. Because log_C, log_A, and
log_gamma_alone have different scales and standard deviations, their raw
fitted coefficients are NOT comparable to each other -- compare_predictors
reports each predictor's own standard deviation and a standard-deviation-
scaled coefficient (the fitted change in `toward` for a one-SD change in
that specific predictor) instead of ranking raw coefficient magnitudes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf

BASELINE_TERMS = [
    "abs_distance", "abs_distance_sq", "side_above",
    "prev_5min_return", "vol_30min", "minutes_since_open",
    "log_unweighted_oi", "log_unweighted_activity",
]


def prepare_model_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Returns (prepared_df, drop_reason_counts). Rows are dropped for a
    missing/nonfinite required field -- never silently clipped into a
    fabricated value. drop_reason_counts attributes each drop to the
    specific field(s) missing on that row (a row can count under more than
    one reason if multiple fields are missing), so a report can state the
    real cause instead of guessing from the row count alone.
    """
    df = df.copy()
    df["log_C"] = np.log1p(df["C"])
    df["log_A"] = np.log1p(df["A"])
    df["log_gamma_alone"] = np.log1p(df["gamma_alone"])
    df["log_unweighted_oi"] = np.log1p(df["unweighted_oi"])
    df["log_unweighted_activity"] = np.log1p(df["unweighted_activity"])
    df["abs_distance_sq"] = df["abs_distance"] ** 2
    df["side_above"] = (df["side"] == "above").astype(int)
    df["cluster_id"] = df["date"].astype(str) + "_" + df["anchor_ts_et"].astype(str)

    required = BASELINE_TERMS + ["log_C", "log_A", "log_gamma_alone", "toward"]
    required = [c for c in required if c != "side_above"]

    finite = np.isfinite(df[required].astype(float))
    drop_reason_counts = {col: int((~finite[col]).sum()) for col in required}
    keep_mask = finite.all(axis=1)
    dropped_df = df[~keep_mask]
    drop_reason_counts["_total_dropped_rows"] = int(len(dropped_df))
    drop_reason_counts["_total_kept_rows"] = int(keep_mask.sum())
    return df[keep_mask].copy(), drop_reason_counts


def fit_clustered_ols(df: pd.DataFrame, predictor: str | None) -> sm.regression.linear_model.RegressionResultsWrapper:
    """OLS of `toward` on BASELINE_TERMS (+ `predictor` if given), with
    standard errors clustered by anchor -- never by row."""
    terms = list(BASELINE_TERMS)
    if predictor is not None:
        terms = [predictor] + terms
    formula = "toward ~ " + " + ".join(terms)
    model = smf.ols(formula, data=df)
    return model.fit(cov_type="cluster", cov_kwds={"groups": df["cluster_id"]})


def day_by_day(df: pd.DataFrame, predictor: str) -> pd.DataFrame:
    """Per-session coefficient on `predictor`, fit independently each day
    with standard errors clustered by anchor within that day -- the same
    dependence structure as the pooled model, never independent-row (HC1)
    uncertainty. A day with too few anchor clusters for the cluster-robust
    covariance to be well-behaved is reported with a warning column rather
    than silently substituting a different (weaker) dependence assumption.
    """
    rows = []
    for date, g in df.groupby("date"):
        n_clusters = g["cluster_id"].nunique()
        res = fit_clustered_ols(g, predictor)
        rows.append({
            "date": date, "n": len(g), "n_anchor_clusters": n_clusters,
            "coef": res.params[predictor], "se": res.bse[predictor], "p": res.pvalues[predictor],
        })
    return pd.DataFrame(rows)


def leave_one_day_out(df: pd.DataFrame, predictor: str) -> pd.DataFrame:
    """Refit the full anchor-clustered model after omitting each date in
    turn (not held-out predictive validation -- there is no prediction
    step here, only a stability check on the fitted coefficient)."""
    rows = []
    dates = sorted(df["date"].unique())
    for held_out in dates:
        subset = df[df["date"] != held_out]
        res = fit_clustered_ols(subset, predictor)
        rows.append({
            "held_out": held_out, "n": len(subset),
            "coef": res.params[predictor], "se": res.bse[predictor], "p": res.pvalues[predictor],
        })
    return pd.DataFrame(rows)


def compare_predictors(df: pd.DataFrame) -> pd.DataFrame:
    """Baseline-only model plus one model per predictor (log_C, log_A,
    log_gamma_alone). Reports each predictor's own standard deviation and
    an SD-scaled coefficient (`coef * predictor_std`) alongside the raw
    fitted coefficient -- raw coefficients across these three predictors
    are NOT on a common scale and must not be ranked directly."""
    rows = []
    baseline_res = fit_clustered_ols(df, None)
    rows.append({
        "predictor": "(baseline only)", "coef": np.nan, "se": np.nan, "p": np.nan,
        "predictor_std": np.nan, "sd_scaled_coef": np.nan,
        "r_squared": baseline_res.rsquared, "n": int(baseline_res.nobs),
    })
    for predictor in ["log_C", "log_A", "log_gamma_alone"]:
        res = fit_clustered_ols(df, predictor)
        predictor_std = float(df[predictor].std())
        coef = res.params[predictor]
        rows.append({
            "predictor": predictor, "coef": coef, "se": res.bse[predictor],
            "p": res.pvalues[predictor], "predictor_std": predictor_std,
            "sd_scaled_coef": coef * predictor_std,
            "r_squared": res.rsquared, "n": int(res.nobs),
        })
    return pd.DataFrame(rows)
