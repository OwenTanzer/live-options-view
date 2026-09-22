"""Pure-function tests for squeeze_scanner.finviz_client.parse_candidate_row,
exercised against representative rows shaped like finvizfinance 1.5.0's
actual (quirky) output -- verified live on 2026-09-22, see that module's
docstring -- rather than only against already-correct fixtures. This is the
gap PR #99's review flagged directly: the original tests never covered the
finviz-row-to-FactorInputs translation at all."""

import math
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from squeeze_scanner.finviz_client import FinvizSchemaError, _to_float, fetch_candidates, parse_candidate_row


def test_parses_a_representative_finviz_1_5_0_row():
    # Short Float: finvizfinance leaves this one as a raw percent string.
    # Perf Week: finvizfinance auto-converts this one to a FRACTION.
    # Float / Short Ratio / Rel Volume / Price: already plain floats.
    row = {
        "Ticker": "WOLF",
        "Company": "Wolfspeed Inc",
        "Short Float": "70.68%",
        "Short Ratio": 6.24,
        "Float": 34_860_000.0,
        "Rel Volume": 0.47,
        "Perf Week": 0.1672,
        "Price": 28.03,
    }
    candidate = parse_candidate_row(row)
    assert candidate is not None
    assert candidate.ticker == "WOLF"
    assert candidate.factor_inputs.short_float_pct == 70.68
    assert candidate.factor_inputs.days_to_cover == 6.24
    assert candidate.factor_inputs.float_shares == 34_860_000.0  # NOT *1_000_000
    assert candidate.factor_inputs.relative_volume == 0.47
    assert math.isclose(candidate.factor_inputs.price_change_5d_pct, 16.72)  # 0.1672 * 100, not 0.1672


def test_negative_perf_week_fraction_converts_correctly():
    row = {
        "Ticker": "XYZ", "Company": "Xyz Co", "Short Float": "12.00%",
        "Short Ratio": 2.0, "Float": 5_000_000.0, "Rel Volume": 1.0, "Perf Week": -0.0842, "Price": 10.0,
    }
    candidate = parse_candidate_row(row)
    assert math.isclose(candidate.factor_inputs.price_change_5d_pct, -8.42)


def test_row_missing_core_short_interest_field_is_dropped():
    row = {
        "Ticker": "ABC", "Company": "Abc Co", "Short Float": "-",  # finviz's own missing-value marker
        "Short Ratio": 3.0, "Float": 1_000_000.0, "Rel Volume": 1.0, "Perf Week": 0.01, "Price": 5.0,
    }
    assert parse_candidate_row(row) is None


def test_nan_in_core_field_is_dropped_not_treated_as_a_perfect_score():
    row = {
        "Ticker": "NAN1", "Company": "Nan Co", "Short Float": float("nan"),
        "Short Ratio": float("nan"), "Float": float("nan"), "Rel Volume": float("nan"),
        "Perf Week": float("nan"), "Price": float("nan"),
    }
    # Previously: _to_float accepted NaN, and the required-field check only
    # rejected None, so this row reached scoring.py and produced a false
    # composite_score of 1.0 (PR #99 review, finding 3).
    assert parse_candidate_row(row) is None


def test_to_float_rejects_non_finite():
    assert _to_float(float("nan")) is None
    assert _to_float(float("inf")) is None
    assert _to_float(float("-inf")) is None
    assert _to_float("nan") is None
    assert _to_float(None) is None
    assert _to_float("-") is None
    assert _to_float("22.5%") == 22.5
    assert _to_float(6.24) == 6.24


def test_missing_optional_fields_default_rather_than_drop_the_row():
    row = {
        "Ticker": "OPT", "Company": "Opt Co", "Short Float": "15.0%",
        "Short Ratio": 2.5, "Float": 2_000_000.0, "Rel Volume": None, "Perf Week": None, "Price": None,
    }
    candidate = parse_candidate_row(row)
    assert candidate is not None
    assert candidate.factor_inputs.relative_volume == 1.0
    assert candidate.factor_inputs.price_change_5d_pct == 0.0
    assert candidate.price == 0.0


# -- gap 3: a finviz schema change must not look like a genuine empty scan --


def test_fetch_candidates_raises_schema_error_when_required_columns_are_missing():
    # A provider header/schema change: only Ticker and Price came back.
    # Before this fix, every row failed parse_candidate_row's required-
    # field check and fetch_candidates returned [] -- indistinguishable
    # from "the filter genuinely matched zero stocks today."
    fake_df = pd.DataFrame({"Ticker": ["AAA"], "Price": [10.0]})
    fake_screener = MagicMock()
    fake_screener.screener_view.return_value = fake_df
    with patch("finvizfinance.screener.custom.Custom", return_value=fake_screener):
        with pytest.raises(FinvizSchemaError):
            fetch_candidates(limit=10)


def test_fetch_candidates_returns_empty_list_for_a_genuinely_empty_result_with_correct_schema():
    # All required columns present, just zero matching rows -- a real
    # empty scan, must NOT raise.
    fake_df = pd.DataFrame(columns=["Ticker", "Company", "Short Float", "Short Ratio", "Float", "Rel Volume", "Perf Week", "Price"])
    fake_screener = MagicMock()
    fake_screener.screener_view.return_value = fake_df
    with patch("finvizfinance.screener.custom.Custom", return_value=fake_screener):
        assert fetch_candidates(limit=10) == []
