import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math

import pandas as pd

from measures import compute_concentration_and_activity


def _row(strike, opt_type, oi, gamma, dv, dv_flag, spot=700.0, snap="s1", date="20260910", ts=None, symbol=None):
    return {
        "date": date, "snapshot_key": snap, "ts_et": ts or pd.Timestamp("2026-09-10 09:30", tz="America/New_York"),
        "OptionSymbol": symbol or f"Q{strike}{opt_type}", "Strike": strike, "Type": opt_type,
        "OpenInterest": oi, "Gamma": gamma, "UnderlyingPrice": spot, "dV": dv, "dv_flag": dv_flag,
    }


def test_concentration_uses_oi_gamma_spot_squared():
    df = pd.DataFrame([
        _row(700, "call", oi=10, gamma=0.02, dv=None, dv_flag="first_observation"),
        _row(700, "put", oi=5, gamma=0.01, dv=None, dv_flag="first_observation"),
    ])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    expected_c = (700.0 ** 2) * 100 * (10 * 0.02 + 5 * 0.01)
    assert row["C"] == expected_c
    assert math.isnan(row["A"])  # no usable dV yet -- unavailable, not zero activity
    assert row["n_contracts"] == 2


def test_activity_only_counts_ok_flagged_intervals():
    df = pd.DataFrame([
        _row(700, "call", oi=0, gamma=0.02, dv=100, dv_flag="ok", symbol="A"),
        _row(700, "put", oi=0, gamma=0.02, dv=999, dv_flag="reset_or_decrease", symbol="B"),
    ])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    expected_a = (700.0 ** 2) * 100 * (100 * 0.02)  # the reset-flagged 999 must NOT contribute
    assert row["A"] == expected_a
    assert row["n_dv_excluded"] == 1


def test_call_put_components_and_side_of_spot():
    df = pd.DataFrame([
        _row(705, "call", oi=10, gamma=0.02, dv=None, dv_flag="first_observation", spot=700.0),
        _row(695, "put", oi=10, gamma=0.02, dv=None, dv_flag="first_observation", spot=700.0),
    ])
    out = compute_concentration_and_activity(df)
    above = out[out["Strike"] == 705].iloc[0]
    below = out[out["Strike"] == 695].iloc[0]
    assert above["side"] == "above"
    assert below["side"] == "below"
    assert above["sum_call_oi_gamma"] > 0
    assert above["sum_put_oi_gamma"] == 0
    assert below["sum_put_oi_gamma"] > 0


def test_never_combines_c_and_a_into_one_composite_column():
    df = pd.DataFrame([_row(700, "call", oi=10, gamma=0.02, dv=50, dv_flag="ok")])
    out = compute_concentration_and_activity(df)
    assert "C" in out.columns and "A" in out.columns
    assert not any(col.lower() in ("score", "composite", "combined") for col in out.columns)


def test_strike_with_no_oi_gamma_and_no_usable_activity_is_fully_unavailable():
    """The review's reproduction 1: a strike where every contract is
    missing Gamma, missing OpenInterest, and has no usable dV must have
    BOTH C and A as NaN (unavailable), not 0 -- and must therefore fail
    the eligibility gate a caller applies (e.g. dropna), not silently pass
    it as if it were an observed all-zero strike."""
    df = pd.DataFrame([_row(700, "call", oi=None, gamma=None, dv=None, dv_flag="first_observation")])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert math.isnan(row["C"])
    assert math.isnan(row["A"])


def test_strike_with_valid_oi_gamma_but_no_usable_activity_has_available_c_and_unavailable_a():
    """The review's reproduction 2: valid gamma/open-interest but the only
    contract's activity observation is a first-observation (no usable dV)
    must produce a real C, an unavailable (NaN) A, and n_dv_excluded=1 --
    not a numeric A=0 that looks like a real zero-activity observation."""
    df = pd.DataFrame([_row(700, "call", oi=10, gamma=0.02, dv=None, dv_flag="first_observation")])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert row["C"] == (700.0 ** 2) * 100 * 10 * 0.02
    assert math.isnan(row["A"])
    assert row["n_dv_excluded"] == 1
    assert row["n_dv_valid"] == 0


def test_activity_requires_joint_valid_gamma_and_volume_on_the_same_contract():
    """Review 5252887319 finding 1's exact reproduction: contract 1 has a
    usable Gamma but no usable dV; contract 2 has a usable dV but no usable
    Gamma. Neither contract alone has BOTH -- A must be unavailable (NaN),
    not a numeric 0 built from pairing one contract's volume with the
    other's gamma. C is unaffected (contract 1 alone makes it available)."""
    df = pd.DataFrame([
        _row(700, "call", oi=10, gamma=0.02, dv=None, dv_flag="first_observation", symbol="c1"),
        _row(700, "put", oi=10, gamma=None, dv=5, dv_flag="ok", symbol="c2"),
    ])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert row["C"] == (700.0 ** 2) * 100 * 10 * 0.02
    assert math.isnan(row["A"])
    assert row["n_dv_valid"] == 0


def test_negative_gamma_excluded_from_c_and_a_like_a_missing_value():
    df = pd.DataFrame([
        _row(700, "call", oi=10, gamma=-0.02, dv=5, dv_flag="ok", symbol="bad"),
        _row(700, "put", oi=10, gamma=0.01, dv=5, dv_flag="ok", symbol="good"),
    ])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert row["C"] == (700.0 ** 2) * 100 * 10 * 0.01
    assert row["A"] == (700.0 ** 2) * 100 * 5 * 0.01
    assert row["n_oi_gamma_valid"] == 1
    assert row["n_dv_valid"] == 1


def test_negative_open_interest_excluded_from_c():
    df = pd.DataFrame([_row(700, "call", oi=-10, gamma=0.02, dv=None, dv_flag="first_observation")])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert math.isnan(row["C"])
    assert row["n_oi_gamma_valid"] == 0


def test_negative_dv_excluded_from_a():
    df = pd.DataFrame([_row(700, "call", oi=10, gamma=0.02, dv=-5, dv_flag="ok")])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert math.isnan(row["A"])
    assert row["n_dv_valid"] == 0


def test_partial_coverage_strike_uses_only_the_valid_contract_not_zero_for_missing_one():
    """One contract usable, one contract missing OI/Gamma at the same
    strike: C must reflect only the valid contract's contribution (not be
    zeroed out by the missing one), while still being a real, available
    number since at least one contract is usable."""
    df = pd.DataFrame([
        _row(700, "call", oi=10, gamma=0.02, dv=None, dv_flag="first_observation", symbol="valid"),
        _row(700, "put", oi=None, gamma=None, dv=None, dv_flag="first_observation", symbol="missing"),
    ])
    out = compute_concentration_and_activity(df)
    row = out.iloc[0]
    assert row["C"] == (700.0 ** 2) * 100 * 10 * 0.02
    assert row["n_oi_gamma_valid"] == 1
    assert row["n_contracts"] == 2
