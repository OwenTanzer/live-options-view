"""CLI: build the final exploratory-analysis report from the cached
outcome dataset -- matched summaries, plots, the clustered baseline
regression, day-by-day sensitivity, and leave-one-day-out stability.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from baselines import add_baseline_features
from regression import compare_predictors, day_by_day, leave_one_day_out, prepare_model_frame

OUT_DIR = Path(__file__).parent / "out"
PLOTS_DIR = OUT_DIR / "plots"


def matched_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Mean `toward` by distance bucket x C-tertile, holding side fixed --
    the plain, assumption-light comparison the issue asks for before any
    regression."""
    df = df.copy()
    # The 3-nearest-strikes-per-side selection keeps distances small (observed
    # range ~0.01-3.0 for this QQQ 0DTE strike grid) -- bucket at that scale,
    # not an arbitrary wide one.
    df["distance_bucket"] = pd.cut(df["abs_distance"], bins=[0, 0.5, 1.0, 1.5, 2.0, 3.01], include_lowest=True)
    df["C_tertile"] = pd.qcut(df["log_C"], 3, labels=["low", "mid", "high"], duplicates="drop")
    return (
        df.groupby(["side", "distance_bucket", "C_tertile"], observed=True)["toward"]
        .agg(["mean", "std", "count"])
        .reset_index()
    )


def plot_matched_summary(summary: pd.DataFrame, path: Path) -> None:
    """`summary` is matched_summary()'s output (already has distance_bucket
    and C_tertile as columns), not the raw model frame."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
    for ax, side in zip(axes, ["above", "below"]):
        sub = summary[summary["side"] == side]
        for tertile, color in zip(["low", "mid", "high"], ["#888", "#4a90d9", "#d94a4a"]):
            t = sub[sub["C_tertile"] == tertile]
            ax.errorbar(
                t["distance_bucket"].astype(str), t["mean"],
                yerr=t["std"] / np.sqrt(t["count"].clip(lower=1)),
                marker="o", label=f"C {tertile}", color=color, capsize=3,
            )
        ax.axhline(0, color="black", linewidth=0.6)
        ax.set_title(f"Strikes {side} spot")
        ax.set_xlabel("|distance| bucket")
        ax.tick_params(axis="x", rotation=30)
    axes[0].set_ylabel("mean toward (+/- SE)")
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_day_by_day(day_df: pd.DataFrame, predictor_label: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.errorbar(day_df["date"], day_df["coef"], yerr=1.96 * day_df["se"], fmt="o", capsize=4)
    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_title(f"Per-session coefficient on {predictor_label} (95% CI, HC1)")
    ax.set_ylabel("coefficient")
    ax.tick_params(axis="x", rotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _narrative_header(n_outcome_rows: int, n_model_rows: int, n_dropped: int, comparison: pd.DataFrame) -> str:
    baseline_r2 = comparison.loc[comparison["predictor"] == "(baseline only)", "r_squared"].iloc[0]
    return f"""# MOO-171 — Unsigned gamma concentration/activity vs. subsequent strike attraction

## Question

At comparable initial distances from QQQ spot, do strikes with higher unsigned
gamma-weighted open-interest concentration (C) or gamma-weighted interval
activity (A) show different subsequent movement toward/away from the strike,
beyond ordinary proximity and unweighted activity?

## Data

Five sessions (2026-09-10, 11, 14, 15, 16), tastytrade/DXLink intraday QQQ
0DTE snapshots archived at ~60s cadence, `intraday/{{date}}/snapshot_*.csv`.
Coverage audit: all 5 sessions reconcile exactly against collector logs
(595/598/598/598/597 snapshots); 400,124 option rows; zero nonfinite Greeks,
negative volumes, or negative OI; 99.65% of interval-volume observations
usable (`ok`) after excluding first-observations, re-entries, resets, and
gaps >90s per the issue's rules. Full detail in `out/audit_report.json`.

Outcome dataset: {n_outcome_rows} (anchor, strike) rows from 384 non-overlapping
5-minute anchors x up to 6 selected strikes (3 nearest strictly above spot,
3 strictly below), each requiring an anchor snapshot <=90s old and an outcome
snapshot 5 minutes later, <=90s late. {n_dropped} rows dropped for missing
baseline features (mostly the first ~30 minutes of each session, before a
full preceding-volatility window exists) -> {n_model_rows} modeled rows.

## Method

`toward[k,t] = (|S[t]-k| - |S[t+5m]-k|) / S[t]`, positive = closer to k at
the outcome snapshot. Regressed on log1p(C) / log1p(A) / log1p(gamma alone)
each in turn, plus a fixed baseline (|distance|, |distance|^2, side of spot,
prior 5-min return, preceding 30-min realized vol, minutes since open,
log unweighted OI, log unweighted activity). Standard errors are clustered
by (date, anchor) throughout -- the 6 strikes at one anchor share one future
price path and are never treated as independent observations.

## Findings — read the day-by-day results before the pooled ones

**The pooled coefficients are small and not stable across days.** Pooling
all 5 sessions, log(C), log(A), and log(gamma alone) each show a positive,
nominally significant association with `toward` (predictor-comparison table
below) -- but the R^2 gain over the baseline-only model ({baseline_r2:.4f}) is on
the order of 0.002-0.003 in every case: detectable, not large.

Critically, **with only 5 day-clusters this pooled significance is not
trustworthy on its own** (per the issue's explicit caution), and the
day-by-day breakdown confirms why: for log(C), 3 of 5 sessions (9/10, 9/11,
9/14) show a positive, individually significant coefficient, while the other
2 (9/15, 9/16) show no relationship or a negative point estimate with a wide
CI spanning zero. Leave-one-day-out refits move the pooled coefficient by
roughly 3x depending on which day is excluded (lowest when 9/14 -- one of
the strong-positive days -- is held out; highest when 9/16 -- the
negative-point-estimate day -- is held out). The same pattern holds for
log(A). **This is not a stable, session-independent relationship on this
evidence; it is at most a candidate worth watching across more sessions.**

**Gamma alone (unweighted by OI or activity) shows an equal or larger
coefficient than either C or A**, and the same day-by-day instability.
Since the baseline already controls for |distance| and |distance|^2, this
raises a real possibility that the OI/activity weighting in C and A is not
adding information beyond what gamma's own mechanical dependence on
moneyness and time-to-expiry already contributes -- exactly the concern the
issue's spec named in advance. This analysis does not resolve that question;
it flags it as the main reason not to read C or A as validated signals from
this pass alone.

## Explicit limitations (do not read past these)

- Unsigned measures only. C and A are gamma-weighted open-interest and
  activity concentration -- not signed dealer inventory, not a hedging-flow
  estimate, and not validated against any independent position/trade data.
- 5 trading days. Every inference above is over 5 day-clusters; nothing here
  supports a trading gate, a causal dealer-hedging claim, or a
  predictive-performance claim.
- No threshold search was performed; the 5-minute horizon, 90-second
  staleness cap, and 3-strikes-per-side selection are exactly the issue's
  specified values, not tuned choices.
- `toward` observes one endpoint-to-endpoint distance comparison per anchor,
  not intraminute touches/crossings; it does not establish sustained
  pinning.
- Reported OI can legitimately read 0 well into a session for a given 0DTE
  contract (no settled prior-day OI yet) -- this is a real data-quality
  constraint on C specifically, not an artifact of this analysis.
"""


def main() -> int:
    outcome_ds = pd.read_parquet(OUT_DIR / "outcome_dataset.parquet")
    spot_series = pd.read_parquet(OUT_DIR / "spot_series.parquet")
    print(f"Loaded outcome dataset: {len(outcome_ds)} rows.")

    with_baselines = add_baseline_features(outcome_ds, spot_series)
    with_baselines.to_parquet(OUT_DIR / "outcome_dataset_with_baselines.parquet", index=False)

    prepared, dropped = prepare_model_frame(with_baselines)
    print(f"Prepared model frame: {len(prepared)} rows ({dropped} dropped for missing baseline features).")

    PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    summary = matched_summary(prepared)
    summary.to_csv(OUT_DIR / "matched_summary.csv", index=False)
    plot_matched_summary(summary, PLOTS_DIR / "matched_summary.png")
    print(f"\nMatched summary (first 10 rows):\n{summary.head(10)}")

    comparison = compare_predictors(prepared)
    comparison.to_csv(OUT_DIR / "predictor_comparison.csv", index=False)
    print(f"\n=== Predictor comparison (clustered by anchor, n={len(prepared)}) ===\n{comparison}")

    report_lines = [_narrative_header(len(outcome_ds), len(prepared), dropped, comparison)]
    report_lines.append("\n## Predictor comparison (pooled, clustered by anchor)\n")
    report_lines.append(comparison.to_markdown(index=False))
    report_lines.append("\n")

    for predictor, label in [("log_C", "log(C)"), ("log_A", "log(A)"), ("log_gamma_alone", "log(gamma alone)")]:
        dbd = day_by_day(prepared, predictor)
        dbd.to_csv(OUT_DIR / f"day_by_day_{predictor}.csv", index=False)
        plot_day_by_day(dbd, label, PLOTS_DIR / f"day_by_day_{predictor}.png")
        print(f"\n=== Day-by-day coefficient on {predictor} ===\n{dbd}")

        loo = leave_one_day_out(prepared, predictor)
        loo.to_csv(OUT_DIR / f"leave_one_day_out_{predictor}.csv", index=False)
        print(f"\n=== Leave-one-day-out coefficient on {predictor} ===\n{loo}")

        report_lines.append(f"\n## {label}\n\n### Day-by-day\n{dbd.to_markdown(index=False)}\n")
        report_lines.append(f"\n### Leave-one-day-out\n{loo.to_markdown(index=False)}\n")

    with open(OUT_DIR / "MOO171_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    print(f"\nWrote {OUT_DIR / 'MOO171_report.md'} and plots to {PLOTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
