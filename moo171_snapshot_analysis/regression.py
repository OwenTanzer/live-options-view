"""Parsimonious baseline-comparison regression per MOO-171 §3.

Six strikes at the same anchor share one future price path -- they are NOT
six independent market realizations. Every model here clusters standard
errors by (date, anchor_ts_et), never by row. With only 5 day-clusters,
this is explicitly not enough for asymptotic cluster-robust inference to be
trustworthy on its own -- day-by-day and leave-one-day-out stability checks
are the load-bearing evidence, not the pooled p-value.

Weighting variables (C, A, gamma_alone, unweighted OI/activity) are log1p
transformed before entering the model: their raw scale spans many orders of
magnitude (observed C range: 0 to ~5.6e11), which would otherwise let a
handful of huge-OI strikes dominate an OLS fit.
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


def prepare_model_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["log_C"] = np.log1p(df["C"].clip(lower=0))
    df["log_A"] = np.log1p(df["A"].clip(lower=0))
    df["log_gamma_alone"] = np.log1p(df["gamma_alone"].clip(lower=0))
    df["log_unweighted_oi"] = np.log1p(df["unweighted_oi"].clip(lower=0))
    df["log_unweighted_activity"] = np.log1p(df["unweighted_activity"].clip(lower=0))
    df["abs_distance_sq"] = df["abs_distance"] ** 2
    df["side_above"] = (df["side"] == "above").astype(int)
    df["cluster_id"] = df["date"].astype(str) + "_" + df["anchor_ts_et"].astype(str)

    required = BASELINE_TERMS + ["log_C", "log_A", "log_gamma_alone", "toward"]
    before = len(df)
    df = df.dropna(subset=[c for c in required if c != "side_above"])
    dropped = before - len(df)
    return df, dropped


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
    (heteroskedasticity-robust SE within-day; a single day has only ~77
    clusters of size <=6, too few for its own cluster-robust fit to be
    meaningful, so this uses HC1 instead of clustering)."""
    rows = []
    for date, g in df.groupby("date"):
        terms = [predictor] + BASELINE_TERMS
        formula = "toward ~ " + " + ".join(terms)
        try:
            res = smf.ols(formula, data=g).fit(cov_type="HC1")
            rows.append({
                "date": date, "n": len(g), "coef": res.params[predictor],
                "se": res.bse[predictor], "p": res.pvalues[predictor],
            })
        except Exception as exc:
            rows.append({"date": date, "n": len(g), "coef": np.nan, "se": np.nan, "p": np.nan, "error": str(exc)})
    return pd.DataFrame(rows)


def leave_one_day_out(df: pd.DataFrame, predictor: str) -> pd.DataFrame:
    """Refit the full clustered model excluding each date in turn, to show
    whether the pooled coefficient depends heavily on any single session."""
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
    log_gamma_alone), so the report can show whether weighting adds
    anything over the baseline/gamma-alone comparators -- not just that
    each model individually 'looks significant'."""
    rows = []
    baseline_res = fit_clustered_ols(df, None)
    rows.append({
        "predictor": "(baseline only)", "coef": np.nan, "se": np.nan, "p": np.nan,
        "r_squared": baseline_res.rsquared, "n": int(baseline_res.nobs),
    })
    for predictor in ["log_C", "log_A", "log_gamma_alone"]:
        res = fit_clustered_ols(df, predictor)
        rows.append({
            "predictor": predictor, "coef": res.params[predictor], "se": res.bse[predictor],
            "p": res.pvalues[predictor], "r_squared": res.rsquared, "n": int(res.nobs),
        })
    return pd.DataFrame(rows)
