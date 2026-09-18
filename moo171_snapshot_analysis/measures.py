"""Unsigned gamma-weighted concentration (C) and activity (A) per MOO-171 §2.

    C[k,t] = S[t]^2 * sum_over_c_at_k( m[c] * OI[c,t] * Gamma[c,t] )
    A[k,t] = S[t]^2 * sum_over_c_at_k( m[c] * dV[c,t] * Gamma[c,t] )

C is unsigned gamma-weighted reported-open-interest concentration.
A is unsigned gamma-weighted recent activity using end-of-interval snapshot
gamma -- NOT inventory, a signed flow, or trade-time gamma integration.
Gamma for long calls and puts contributes positively; dealer sign is never
assigned from option type here.

Kept deliberately separate (never combined into one composite) per the
issue's instruction not to choose a composite after inspecting results.

Missing/unusable inputs are preserved as unavailable (NaN), never admitted
as an observed zero: a strike where every contract is missing OI or Gamma
has C = NaN, not C = 0, and a strike with no `dv_flag == "ok"` observation
at all has A = NaN, not A = 0. This matters because a numeric 0 and "we
don't know" are not the same thing, and only the former should be eligible
for the outcome/regression pipeline as a genuine observation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CONTRACT_MULTIPLIER = 100  # verified against OptionSymbol/strike encoding; standard for QQQ 0DTE


def compute_concentration_and_activity(option_rows: pd.DataFrame) -> pd.DataFrame:
    """One row per (date, snapshot_key, Strike) with C, A, and their
    unweighted/diagnostic components.

    Requires `dV`/`dv_flag` columns from `panel.recompute_interval_volume`.

    Eligibility is tracked and computed separately for C and A:
    - C[k,t] is NaN unless at least one contract at that strike/time has
      both a finite OpenInterest and a finite Gamma; otherwise there is no
      OI/gamma information for that strike at all, and 0 would falsely
      claim "reported open interest is zero" instead of "unknown."
    - A[k,t] is NaN unless at least one contract has a `dv_flag == "ok"`
      (i.e. genuinely usable) interval volume observation; a strike whose
      only contracts are first-observations/re-entries/resets/long-gaps has
      no usable activity information, not zero activity.
    Contracts that individually lack OI/Gamma (for C) or a usable dV (for
    A) are simply excluded from that strike's sum -- they don't zero out
    the whole strike as long as at least one contract is usable.
    """
    required = {"dV", "dv_flag"}
    missing = required - set(option_rows.columns)
    if missing:
        raise ValueError(f"option_rows missing {missing}; run recompute_interval_volume first")

    df = option_rows.copy()
    df["Gamma"] = pd.to_numeric(df["Gamma"], errors="coerce")
    df["OpenInterest"] = pd.to_numeric(df["OpenInterest"], errors="coerce")

    oi_gamma_valid = df["Gamma"].notna() & df["OpenInterest"].notna() & np.isfinite(df["Gamma"]) & np.isfinite(df["OpenInterest"])
    dv_valid = (df["dv_flag"] == "ok") & df["dV"].notna()

    df["_oi_gamma"] = np.where(oi_gamma_valid, CONTRACT_MULTIPLIER * df["OpenInterest"] * df["Gamma"], 0.0)
    df["_dv_gamma"] = np.where(dv_valid, CONTRACT_MULTIPLIER * df["dV"] * df["Gamma"], 0.0)
    df["_call_oi_gamma"] = np.where(df["Type"] == "call", df["_oi_gamma"], 0.0)
    df["_put_oi_gamma"] = np.where(df["Type"] == "put", df["_oi_gamma"], 0.0)
    df["_oi_gamma_valid"] = oi_gamma_valid.astype(int)
    df["_dv_valid"] = dv_valid.astype(int)
    df["_excluded_dv"] = (~dv_valid).astype(int)

    grouped = (
        df.groupby(["date", "snapshot_key", "ts_et", "Strike"], as_index=False)
        .agg(
            spot=("UnderlyingPrice", "first"),
            sum_oi_gamma=("_oi_gamma", "sum"),
            sum_dv_gamma=("_dv_gamma", "sum"),
            sum_call_oi_gamma=("_call_oi_gamma", "sum"),
            sum_put_oi_gamma=("_put_oi_gamma", "sum"),
            total_oi=("OpenInterest", "sum"),
            total_dv=("dV", lambda s: s.fillna(0.0).sum()),
            total_gamma=("Gamma", "sum"),
            n_contracts=("OptionSymbol", "nunique"),
            n_oi_gamma_valid=("_oi_gamma_valid", "sum"),
            n_dv_valid=("_dv_valid", "sum"),
            n_dv_excluded=("_excluded_dv", "sum"),
        )
    )

    spot2 = grouped["spot"].astype(float) ** 2
    grouped["C"] = np.where(grouped["n_oi_gamma_valid"] > 0, spot2 * grouped["sum_oi_gamma"], np.nan)
    grouped["A"] = np.where(grouped["n_dv_valid"] > 0, spot2 * grouped["sum_dv_gamma"], np.nan)
    grouped["distance"] = grouped["Strike"].astype(float) - grouped["spot"].astype(float)
    grouped["side"] = np.where(grouped["distance"] >= 0, "above", "below")

    return grouped.drop(columns=["sum_oi_gamma", "sum_dv_gamma"])
