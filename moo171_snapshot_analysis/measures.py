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
has C = NaN, not C = 0, and a strike with no contract that has BOTH a
usable (`dv_flag == "ok"`) interval volume AND a finite, non-negative Gamma
on that same contract has A = NaN, not A = 0. A contract with usable volume
but unusable gamma (or vice versa) contributes nothing to A -- pairing a
volume observation from one contract with a gamma value from a different
contract at the same strike would misattribute activity that was never
actually observed together. This matters because a numeric 0 and "we don't
know" are not the same thing, and only the former should be eligible for
the outcome/regression pipeline as a genuine observation.

All three unsigned inputs (OpenInterest, Gamma, dV) are also required to be
non-negative wherever they are used: this is an unsigned-quantity model, so
a negative value (data noise or a contamination artifact, since none of
these three are supposed to be signed here) is treated the same as a
missing one -- excluded from the sum, not passed through to quietly produce
a small negative C/A/gamma_alone that would otherwise survive the log1p
transform in regression.prepare_model_frame undetected.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CONTRACT_MULTIPLIER = 100  # see panel.CONTRACT_MULTIPLIER_EVIDENCE / run_audit._multiplier_evidence


def compute_concentration_and_activity(option_rows: pd.DataFrame) -> pd.DataFrame:
    """One row per (date, snapshot_key, Strike) with C, A, and their
    unweighted/diagnostic components.

    Requires `dV`/`dv_flag` columns from `panel.recompute_interval_volume`.

    Eligibility is tracked and computed separately for C and A, and both
    require non-negative finite values (a negative OpenInterest/Gamma/dV is
    treated as unusable, not as a signed observation):
    - C[k,t] is NaN unless at least one contract at that strike/time has
      both a usable OpenInterest and a usable Gamma; otherwise there is no
      OI/gamma information for that strike at all, and 0 would falsely
      claim "reported open interest is zero" instead of "unknown."
    - A[k,t] is NaN unless at least one contract has a usable interval
      volume (`dv_flag == "ok"`) AND a usable Gamma ON THAT SAME CONTRACT;
      a strike whose only contracts are first-observations/re-entries/
      resets/long-gaps, or that only pairs usable volume on one contract
      with usable gamma on a different contract, has no usable activity
      information, not zero activity.
    Contracts that individually lack a usable OI/Gamma (for C) or a usable
    dV/Gamma pair (for A) are simply excluded from that strike's sum --
    they don't zero out the whole strike as long as another contract there
    is usable.
    """
    required = {"dV", "dv_flag"}
    missing = required - set(option_rows.columns)
    if missing:
        raise ValueError(f"option_rows missing {missing}; run recompute_interval_volume first")

    df = option_rows.copy()
    df["Gamma"] = pd.to_numeric(df["Gamma"], errors="coerce")
    df["OpenInterest"] = pd.to_numeric(df["OpenInterest"], errors="coerce")
    df["dV"] = pd.to_numeric(df["dV"], errors="coerce")

    gamma_ok = df["Gamma"].notna() & np.isfinite(df["Gamma"]) & (df["Gamma"] >= 0)
    oi_ok = df["OpenInterest"].notna() & np.isfinite(df["OpenInterest"]) & (df["OpenInterest"] >= 0)
    dv_ok = (df["dv_flag"] == "ok") & df["dV"].notna() & np.isfinite(df["dV"]) & (df["dV"] >= 0)

    oi_gamma_valid = gamma_ok & oi_ok
    # A requires a usable volume AND a usable gamma on the SAME contract --
    # a contract with one but not the other contributes nothing to A rather
    # than being paired with another contract's value at the same strike.
    dv_gamma_valid = dv_ok & gamma_ok

    df["_oi_gamma"] = np.where(oi_gamma_valid, CONTRACT_MULTIPLIER * df["OpenInterest"] * df["Gamma"], 0.0)
    df["_dv_gamma"] = np.where(dv_gamma_valid, CONTRACT_MULTIPLIER * df["dV"] * df["Gamma"], 0.0)
    df["_call_oi_gamma"] = np.where(df["Type"] == "call", df["_oi_gamma"], 0.0)
    df["_put_oi_gamma"] = np.where(df["Type"] == "put", df["_oi_gamma"], 0.0)
    df["_oi_gamma_valid"] = oi_gamma_valid.astype(int)
    df["_dv_valid"] = dv_gamma_valid.astype(int)
    df["_excluded_dv"] = (~dv_gamma_valid).astype(int)
    df["_oi_if_valid"] = np.where(oi_ok, df["OpenInterest"], 0.0)
    df["_dv_if_valid"] = np.where(dv_ok, df["dV"], 0.0)
    df["_gamma_if_valid"] = np.where(gamma_ok, df["Gamma"], 0.0)

    grouped = (
        df.groupby(["date", "snapshot_key", "ts_et", "Strike"], as_index=False)
        .agg(
            spot=("UnderlyingPrice", "first"),
            sum_oi_gamma=("_oi_gamma", "sum"),
            sum_dv_gamma=("_dv_gamma", "sum"),
            sum_call_oi_gamma=("_call_oi_gamma", "sum"),
            sum_put_oi_gamma=("_put_oi_gamma", "sum"),
            total_oi=("_oi_if_valid", "sum"),
            total_dv=("_dv_if_valid", "sum"),
            total_gamma=("_gamma_if_valid", "sum"),
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
