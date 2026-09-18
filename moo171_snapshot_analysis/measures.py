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
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CONTRACT_MULTIPLIER = 100  # verified against OptionSymbol/strike encoding; standard for QQQ 0DTE


def compute_concentration_and_activity(option_rows: pd.DataFrame) -> pd.DataFrame:
    """One row per (date, snapshot_key, Strike) with C, A, and their
    unweighted/diagnostic components.

    Requires `dV`/`dv_flag` columns from `panel.recompute_interval_volume`.
    Rows whose dv_flag != "ok" contribute 0 to A's dV sum (their activity is
    unknown, not zero) but are still counted in `dv_excluded_contracts` so
    the exclusion is visible rather than silently absorbed into a lower A.
    """
    required = {"dV", "dv_flag"}
    missing = required - set(option_rows.columns)
    if missing:
        raise ValueError(f"option_rows missing {missing}; run recompute_interval_volume first")

    df = option_rows.copy()
    df["Gamma"] = pd.to_numeric(df["Gamma"], errors="coerce")
    df["OpenInterest"] = pd.to_numeric(df["OpenInterest"], errors="coerce")

    usable_dv = df["dV"].where(df["dv_flag"] == "ok", 0.0).fillna(0.0)
    excluded_dv = (df["dv_flag"] != "ok").astype(int)

    df["_oi_gamma"] = CONTRACT_MULTIPLIER * df["OpenInterest"].fillna(0.0) * df["Gamma"].fillna(0.0)
    df["_dv_gamma"] = CONTRACT_MULTIPLIER * usable_dv * df["Gamma"].fillna(0.0)
    df["_call_oi_gamma"] = np.where(df["Type"] == "call", df["_oi_gamma"], 0.0)
    df["_put_oi_gamma"] = np.where(df["Type"] == "put", df["_oi_gamma"], 0.0)
    df["_excluded_dv"] = excluded_dv

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
            n_dv_excluded=("_excluded_dv", "sum"),
        )
    )

    spot2 = grouped["spot"].astype(float) ** 2
    grouped["C"] = spot2 * grouped["sum_oi_gamma"]
    grouped["A"] = spot2 * grouped["sum_dv_gamma"]
    grouped["distance"] = grouped["Strike"].astype(float) - grouped["spot"].astype(float)
    grouped["side"] = np.where(grouped["distance"] >= 0, "above", "below")

    return grouped.drop(columns=["sum_oi_gamma", "sum_dv_gamma"])
