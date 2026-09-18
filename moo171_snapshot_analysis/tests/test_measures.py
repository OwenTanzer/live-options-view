import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    assert row["A"] == 0.0  # no usable dV yet
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
