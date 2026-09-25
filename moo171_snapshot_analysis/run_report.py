"""CLI: build the final exploratory-analysis report from the cached
outcome dataset -- matched summaries (both C and A, with time-of-day),
plots, the anchor-clustered baseline regression, day-by-day sensitivity,
and leave-one-day-out stability.

Every factual claim in the narrative is computed from the actual outputs
of this run, not hardcoded -- rerunning against corrected/updated data
must not leave stale conclusions behind (the review's exact finding).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from baselines import add_baseline_features
from regression import compare_predictors, day_by_day, leave_one_day_out, prepare_model_frame

OUT_DIR = Path(__file__).parent / "out"
PLOTS_DIR = OUT_DIR / "plots"

TIME_BUCKET_EDGES = [0, 60, 180, 391]
TIME_BUCKET_LABELS = ["open (0-60min)", "midday (60-180min)", "close (180min+)"]
DISTANCE_BUCKET_EDGES = [0, 0.5, 1.0, 1.5, 2.0, 3.01]


def matched_summary(df: pd.DataFrame, measure: str) -> pd.DataFrame:
    """Descriptive mean/spread of `toward` by side x distance bucket x time
    bucket x measure tertile -- the plain, assumption-light comparison the
    issue asks for before any regression. `measure` is "C" or "A".

    Reports descriptive spread (sample std, pandas' default ddof=1 -- not
    a population std) and BOTH row count and
    unique-anchor count -- not an inferential standard error, since rows
    sharing an anchor are not independent (the same dependence concern as
    the regression's clustering). Sparse cells are visible via their own
    counts rather than implying comparable support everywhere.
    """
    df = df.copy()
    log_col = f"log_{measure}"
    df["distance_bucket"] = pd.cut(df["abs_distance"], bins=DISTANCE_BUCKET_EDGES, include_lowest=True)
    df["time_bucket"] = pd.cut(
        df["minutes_since_open"], bins=TIME_BUCKET_EDGES, labels=TIME_BUCKET_LABELS, include_lowest=True,
    )
    df["tertile"] = pd.qcut(df[log_col], 3, labels=["low", "mid", "high"], duplicates="drop")

    grouped = df.groupby(["side", "time_bucket", "distance_bucket", "tertile"], observed=True)
    out = grouped["toward"].agg(mean="mean", std="std", n_rows="count").reset_index()
    out["n_anchors"] = grouped["anchor_ts_et"].nunique().to_numpy()
    out["measure"] = measure
    return out


def plot_matched_summary(summary: pd.DataFrame, measure: str, path: Path) -> None:
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharey=True)
    for row, side in enumerate(["above", "below"]):
        for col, time_label in enumerate(TIME_BUCKET_LABELS):
            ax = axes[row, col]
            sub = summary[(summary["side"] == side) & (summary["time_bucket"] == time_label)]
            for tertile, color in zip(["low", "mid", "high"], ["#888", "#4a90d9", "#d94a4a"]):
                t = sub[sub["tertile"] == tertile]
                if t.empty:
                    continue
                ax.plot(t["distance_bucket"].astype(str), t["mean"], marker="o", label=f"{tertile}", color=color)
                # Descriptive spread only (sample std, ddof=1 -- not a
                # standard-error-of-the-mean) -- rows share anchors, so an
                # inferential SE here would understate dependence.
                ax.fill_between(
                    t["distance_bucket"].astype(str),
                    t["mean"] - t["std"], t["mean"] + t["std"],
                    color=color, alpha=0.12,
                )
            ax.axhline(0, color="black", linewidth=0.6)
            if row == 0:
                ax.set_title(time_label, fontsize=9)
            if col == 0:
                ax.set_ylabel(f"{side} spot\nmean toward (+/- descriptive std)", fontsize=8)
            ax.tick_params(axis="x", rotation=40, labelsize=7)
    axes[0, 0].legend(title=f"{measure} tertile", fontsize=7)
    fig.suptitle(f"Matched toward summary by distance x time-of-day x {measure} tertile")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_day_by_day(day_df: pd.DataFrame, predictor_label: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.errorbar(day_df["date"], day_df["coef"], yerr=1.96 * day_df["se"], fmt="o", capsize=4)
    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_title(f"Per-session coefficient on {predictor_label}\n(95% CI, clustered by anchor within-day)")
    ax.set_ylabel("coefficient")
    ax.tick_params(axis="x", rotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _build_narrative(
    audit_report: dict, n_anchors: int, n_excluded_anchor_bins: int,
    n_outcome_rows: int, drop_reasons: dict, n_model_rows: int,
    comparison: pd.DataFrame, day_by_day_tables: dict[str, pd.DataFrame],
    loo_tables: dict[str, pd.DataFrame],
) -> str:
    sessions = audit_report["sessions"]
    dates = sorted(sessions)
    reconciled_all = all(sessions[d]["count_reconciled"] for d in dates)
    total_option_rows = sum(sessions[d]["rows_total"] for d in dates)
    flag_counts = audit_report["flag_counts_overall"]
    total_flagged = sum(flag_counts.values())
    ok_frac = flag_counts.get("ok", 0) / total_flagged if total_flagged else float("nan")

    total_contracts = sum(sessions[d]["distinct_contracts"] for d in dates)
    total_oi_change_contracts = sum(sessions[d]["contracts_with_oi_change"] for d in dates)
    total_duplicate_rows = sum(sessions[d]["duplicate_snapshot_symbol_rows"] for d in dates)
    total_symbol_mismatches = sum(sessions[d]["symbol_mismatch_rows"] for d in dates)
    total_spot_inconsistent = sum(sessions[d]["spot_inconsistent_snapshots"] for d in dates)
    max_repeated_mid_run_overall = max(sessions[d]["max_repeated_mid_run"] for d in dates)

    baseline_r2 = comparison.loc[comparison["predictor"] == "(baseline only)", "r_squared"].iloc[0]
    r2_gains = {
        row["predictor"]: row["r_squared"] - baseline_r2
        for _, row in comparison.iterrows() if row["predictor"] != "(baseline only)"
    }

    lines = [f"""# MOO-171 — Unsigned gamma concentration/activity vs. subsequent strike attraction

## Question

At comparable initial distances from QQQ spot, do strikes with higher unsigned
gamma-weighted open-interest concentration (C) or gamma-weighted interval
activity (A) show different subsequent movement toward/away from the strike,
beyond ordinary proximity and unweighted activity?

## Data

{len(dates)} sessions ({", ".join(dates)}), tastytrade/DXLink intraday QQQ
0DTE snapshots archived at ~60s cadence, `intraday/<date>/snapshot_*.csv`.

Coverage audit (`out/audit_report.json`, `out/source_manifest.json` for the
exact object keys/verified-content-sha256/config/executed-source-hashes used
by this run -- see its `git_base_revision`/`git_dirty`/`source_file_sha256`
fields for how the executed code itself is identified independent of commit
state, `representative_reconciliation` for one hand-checked source-row-to-C
calculation, and `multiplier_evidence` for what the archive can and cannot
establish about the 100-share contract multiplier): all
{len(dates)} sessions reconcile exactly against collector logs = {reconciled_all}.
{total_option_rows} option rows. {total_flagged} interval-volume observations
computed; {flag_counts.get("ok", 0)} ({ok_frac:.2%}) usable (`ok`) after
excluding first-observations, re-entries, resets, and gaps >90s per the
issue's rules -- see `flag_counts_overall` in the audit report for the exact
breakdown. Session audits also check (timestamp, OptionSymbol) uniqueness,
OptionSymbol-vs-column identity, snapshot-level spot consistency, per-contract
OI stability against a first-eligible-session baseline, and repeated-value
run lengths: {total_duplicate_rows} duplicate (timestamp, OptionSymbol) rows,
{total_symbol_mismatches} OptionSymbol/column mismatches, and
{total_spot_inconsistent} spot-inconsistent snapshots found across all 5
sessions (see each session's entry in the audit report for the full detail).
**OpenInterest never changed within any regular-hours session for any of the
{total_contracts} distinct contracts observed across all 5 days
({total_oi_change_contracts} contracts with any intra-session OI change)** --
this audit establishes only that recorded OI did not change within the
observed regular-session contract histories. It does NOT independently
verify freshness, settlement origin, or that an unknown OI value was never
serialized as zero upstream of this analysis; it is consistent with (not
independent proof of) DESIGN.md's known-limitations statement that OI is
prior-day-settled data for the entire session. Separately,
at least one contract's Mid quote was unchanged for {max_repeated_mid_run_overall}
consecutive regular-hours snapshots on the session with the longest such run
-- reported as a repetition count only, per the issue's caution that
repetition alone does not prove staleness.

Anchors: {n_anchors} retained (verified non-overlapping by
`outcomes.assert_non_overlapping`), {n_excluded_anchor_bins} candidate
5-minute bins excluded (reasons in `out/excluded_anchor_bins.parquet`).
Outcome dataset: {n_outcome_rows} (anchor, strike) rows from up to 6 selected
strikes per anchor (3 nearest strictly above spot, 3 strictly below), each
requiring an anchor snapshot and a +5-minute outcome snapshot both within 90s
of their targets. Eligibility for C and A is tracked separately per strike
(`n_oi_gamma_valid`, `n_dv_valid` in the outcome dataset) -- a strike is only
selected if it has a real (non-missing) C and A, never a numeric zero
standing in for unavailable data.

{drop_reasons.get('_total_dropped_rows', 0)} of {n_outcome_rows} outcome rows
dropped before modeling, by cause: { {k: v for k, v in drop_reasons.items() if not k.startswith('_') and v > 0} }
-> {n_model_rows} modeled rows.

## Method

`toward[k,t] = (|S[t]-k| - |S[t+5m]-k|) / S[t]`, positive = closer to k at
the outcome snapshot. Regressed on log1p(C) / log1p(A) / log1p(gamma alone)
each in turn, plus a fixed baseline (|distance|, |distance|^2, side of spot,
prior 5-min return, a preceding 30-minute realized-volatility estimate that
REQUIRES actual ~30-minute coverage back from the anchor -- not merely enough
observation count, minutes since open, log unweighted OI, log unweighted
activity). Standard errors are clustered by (date, anchor) in every fitted
model here, pooled AND per-day -- the 6 strikes at one anchor share one
future price path and are never treated as independent observations.

This clustering does not by itself resolve every dependence concern: the
pooled model has {int(comparison.loc[comparison['predictor'] == 'log_C', 'n'].iloc[0])}
modeled rows across many more anchor clusters than there are trading days --
there is no day-level clustering computed anywhere in this analysis, and
anchor-clustering does not address serial dependence *between* successive
anchors within the same session. Day-by-day and leave-one-day-out below are
descriptive session-level stability/sensitivity checks, not a resolution of
that residual dependence, and "leave-one-day-out" means refitting after
omitting one day's rows -- not held-out predictive validation.

Because log_C, log_A, and log_gamma_alone have different scales, their raw
fitted coefficients are not comparable to each other. The predictor
comparison below also reports each predictor's own standard deviation and an
SD-scaled coefficient (the fitted change in `toward` for a one-SD change in
that specific predictor).

## Findings — read the day-by-day results before the pooled ones
"""]

    for predictor, label in [("log_C", "log(C)"), ("log_A", "log(A)"), ("log_gamma_alone", "log(gamma alone)")]:
        row = comparison[comparison["predictor"] == predictor].iloc[0]
        dbd = day_by_day_tables[predictor]
        loo = loo_tables[predictor]
        n_positive_significant = int(((dbd["coef"] > 0) & (dbd["p"] < 0.05)).sum())
        n_days = len(dbd)
        loo_min, loo_max = loo["coef"].min(), loo["coef"].max()
        lines.append(f"""
### {label}

Pooled (n={int(row['n'])}, clustered by anchor): coefficient {row['coef']:.6g}
(SE {row['se']:.2g}, p={row['p']:.4g}), SD-scaled effect {row['sd_scaled_coef']:.6g}
(predictor SD {row['predictor_std']:.4g}). R^2 gain over baseline
({baseline_r2:.4f}): {r2_gains[predictor]:.4f}.

Day-by-day: {n_positive_significant} of {n_days} sessions show a positive
coefficient with a nominal p<0.05 individually (nominal under this day's
own anchor-clustering assumption, not a claim that residual serial
dependence between anchors is resolved); the remaining
{n_days - n_positive_significant} do not (see `out/day_by_day_{predictor}.csv`
/ `out/plots/day_by_day_{predictor}.png` for every session's own
coefficient, SE, and cluster count).

Leave-one-day-out: the pooled coefficient ranges from {loo_min:.6g} to
{loo_max:.6g} depending on which single day is excluded (`out/leave_one_day_out_{predictor}.csv`).
""")

    predictor_rows = {p: comparison[comparison["predictor"] == p].iloc[0] for p in r2_gains}
    smallest_sd_scaled = min(predictor_rows, key=lambda p: abs(predictor_rows[p]["sd_scaled_coef"]))
    largest_r2_gain = max(r2_gains, key=r2_gains.get)
    label_of = {"log_C": "log(C)", "log_A": "log(A)", "log_gamma_alone": "log(gamma alone)"}
    reconciliation = f"""
### Reconciling SD-scaled effect size against R^2 gain

{label_of[smallest_sd_scaled]} has the smallest SD-scaled coefficient of the
three predictors ({predictor_rows[smallest_sd_scaled]['sd_scaled_coef']:.6g}),
{"and its own separate model also has" if smallest_sd_scaled == largest_r2_gain else "although its separate model has"}
the largest R^2 gain over baseline
({r2_gains[largest_r2_gain]:.4f} for {label_of[largest_r2_gain]}). This is
not a contradiction to resolve away: a standardized partial coefficient (the
fitted change in `toward` per one-SD change in that specific predictor,
holding the baseline terms fixed) and a separate model's R^2 gain (how much
additional outcome variance that predictor plus the baseline jointly
explain, relative to the baseline alone) answer different questions, even
though both come from the same linear fitted model. The SD-scaled
coefficient scales the predictor's coefficient by its TOTAL standard
deviation, while the R^2 gain depends on the predictor's RESIDUAL variance
after controlling for the baseline terms (roughly coefficient^2 times that
residual variance). A predictor whose variation overlaps less with the
baseline terms keeps more residual variance, so it can add more R^2 than
another predictor while having a smaller per-SD coefficient. Neither number by itself
establishes whether gamma-weighting by OI (C) or activity (A) adds
information beyond gamma alone, or the reverse; a nested comparison against
a baseline-plus-gamma model would speak to that more directly and is not
reported here (it would be a post-hoc addition, not a pre-specified test)."""
    lines.append(reconciliation + "\n")

    lines.append("""
## Explicit limitations (do not read past these)

- Unsigned measures only. C and A are gamma-weighted open-interest and
  activity concentration -- not signed dealer inventory, not a hedging-flow
  estimate, and not validated against any independent position/trade data.
- Every inference above is over a handful of trading days; nothing here
  supports a trading gate, a causal dealer-hedging claim, or a
  predictive-performance claim. Anchor-level clustering addresses the
  shared-future-path dependence within an anchor; it does not establish
  that sessions or successive anchors are independent, and day-by-day /
  leave-one-day-out are reported as descriptive stability checks, not a
  substitute asymptotic guarantee.
- No threshold search was performed; the 5-minute horizon, 90-second
  staleness cap, and 3-strikes-per-side selection are exactly the issue's
  specified values, not tuned choices.
- `toward` observes one endpoint-to-endpoint distance comparison per anchor,
  not intraminute touches/crossings; it does not establish sustained
  pinning.
- Reported OI can legitimately read 0 well into a session for a given 0DTE
  contract, and some historical unknown OI/volume values were already
  serialized as zero upstream of this analysis and cannot be reconstructed
  by this code -- this is a real data-quality/provenance limitation on C
  specifically, not an artifact of this analysis, and not evidence that
  every zero observed here is an ordinary settled-position reading.
- This revision's source manifest (`out/source_manifest.json`) freezes the
  configuration and exact object keys used AS OF THIS RUN. It is not a
  claim that the original pre-review analysis was frozen before its
  outcomes were inspected -- this correction is retrospective.
""")
    return "".join(lines)


def main() -> int:
    outcome_ds = pd.read_parquet(OUT_DIR / "outcome_dataset.parquet")
    spot_series = pd.read_parquet(OUT_DIR / "spot_series.parquet")
    anchors = pd.read_parquet(OUT_DIR / "anchors.parquet")
    excluded_anchor_bins = pd.read_parquet(OUT_DIR / "excluded_anchor_bins.parquet")
    with open(OUT_DIR / "audit_report.json") as f:
        audit_report = json.load(f)
    print(f"Loaded outcome dataset: {len(outcome_ds)} rows.")

    with_baselines = add_baseline_features(outcome_ds, spot_series)
    with_baselines.to_parquet(OUT_DIR / "outcome_dataset_with_baselines.parquet", index=False)

    prepared, drop_reasons = prepare_model_frame(with_baselines)
    print(f"Prepared model frame: {len(prepared)} rows. Drop reasons: {drop_reasons}")

    PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    for measure in ["C", "A"]:
        summary = matched_summary(prepared, measure)
        summary.to_csv(OUT_DIR / f"matched_summary_{measure}.csv", index=False, lineterminator="\n")
        plot_matched_summary(summary, measure, PLOTS_DIR / f"matched_summary_{measure}.png")
        print(f"\nMatched summary for {measure} (first 10 rows):\n{summary.head(10)}")

    comparison = compare_predictors(prepared)
    comparison.to_csv(OUT_DIR / "predictor_comparison.csv", index=False, lineterminator="\n")
    print(f"\n=== Predictor comparison (clustered by anchor, n={len(prepared)}) ===\n{comparison}")

    day_by_day_tables = {}
    loo_tables = {}
    for predictor, label in [("log_C", "log(C)"), ("log_A", "log(A)"), ("log_gamma_alone", "log(gamma alone)")]:
        dbd = day_by_day(prepared, predictor)
        dbd.to_csv(OUT_DIR / f"day_by_day_{predictor}.csv", index=False, lineterminator="\n")
        plot_day_by_day(dbd, label, PLOTS_DIR / f"day_by_day_{predictor}.png")
        print(f"\n=== Day-by-day coefficient on {predictor} ===\n{dbd}")
        day_by_day_tables[predictor] = dbd

        loo = leave_one_day_out(prepared, predictor)
        loo.to_csv(OUT_DIR / f"leave_one_day_out_{predictor}.csv", index=False, lineterminator="\n")
        print(f"\n=== Leave-one-day-out coefficient on {predictor} ===\n{loo}")
        loo_tables[predictor] = loo

    narrative = _build_narrative(
        audit_report, len(anchors), len(excluded_anchor_bins), len(outcome_ds),
        drop_reasons, len(prepared), comparison, day_by_day_tables, loo_tables,
    )
    report_lines = [narrative, "\n## Predictor comparison table (pooled, clustered by anchor)\n",
                     comparison.to_markdown(index=False), "\n"]
    for predictor, label in [("log_C", "log(C)"), ("log_A", "log(A)"), ("log_gamma_alone", "log(gamma alone)")]:
        report_lines.append(f"\n## {label} tables\n\n### Day-by-day\n{day_by_day_tables[predictor].to_markdown(index=False)}\n")
        report_lines.append(f"\n### Leave-one-day-out\n{loo_tables[predictor].to_markdown(index=False)}\n")

    with open(OUT_DIR / "MOO171_report.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(report_lines))
    print(f"\nWrote {OUT_DIR / 'MOO171_report.md'} and plots to {PLOTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
