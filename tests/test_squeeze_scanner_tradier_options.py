"""Pure-function tests for squeeze_scanner.tradier_options -- the
chain-to-score path (summarize_chain, to_options_inputs, select_expiration),
none of which touch the network. PR #99's review flagged that the original
tests only ever supplied an already-signed OptionsInputs and never tested
this translation at all -- that's the gap these fill."""

from datetime import date
from unittest.mock import patch

import pytest

from squeeze_scanner.tradier_options import (
    ChainSummary,
    TradierDataError,
    get_option_chain,
    get_quote,
    select_expiration,
    summarize_chain,
    to_options_inputs,
)


def _contract(option_type: str, strike: float, oi: int, gamma: float | None, iv: float | None = None) -> dict:
    greeks = {}
    if gamma is not None:
        greeks["gamma"] = gamma
    if iv is not None:
        greeks["mid_iv"] = iv
    return {"option_type": option_type, "strike": strike, "open_interest": oi, "greeks": greeks}


def test_call_heavy_chain_produces_negative_dealer_gamma():
    # Customers net long these 100 calls (gamma 0.1 each) -> dealers are
    # short them -> dealer gamma must be NEGATIVE (squeeze fuel).
    # PR #99 review caught this returning +10 (the customer's own gamma,
    # mislabeled as the dealer's).
    chain = [_contract("call", 100.0, 100, 0.1, iv=0.8)]
    summary = summarize_chain(chain, spot_price=100.0)
    assert summary.net_dealer_gamma < 0
    assert summary.net_dealer_gamma == -10.0


def test_put_heavy_chain_produces_positive_dealer_gamma():
    # Customers net short these puts -> dealers are net long them -> dealer
    # gamma is POSITIVE (stabilizing).
    chain = [_contract("put", 100.0, 100, 0.1, iv=0.8)]
    summary = summarize_chain(chain, spot_price=100.0)
    assert summary.net_dealer_gamma > 0
    assert summary.net_dealer_gamma == 10.0


def test_chain_with_no_greeks_at_all_is_flagged_unusable():
    chain = [_contract("call", 100.0, 200, gamma=None), _contract("put", 100.0, 150, gamma=None)]
    summary = summarize_chain(chain, spot_price=100.0)
    assert summary.has_usable_greeks is False
    assert summary.atm_iv is None


def test_to_options_inputs_rejects_chain_with_no_usable_greeks():
    # PR #99 review: a chain with 100 call contracts and no greeks
    # previously passed the total_oi>=50 check alone and was treated as a
    # valid options signal built from nothing but OI skew.
    no_greeks_summary = ChainSummary(
        call_open_interest=100, put_open_interest=0, net_dealer_gamma=0.0,
        gamma_notional_per_1pct_move=0.0, atm_iv=None, has_usable_greeks=False,
    )
    assert to_options_inputs(no_greeks_summary, iv_rank=50.0, avg_dollar_volume=1_000_000.0) is None


def test_to_options_inputs_rejects_thin_chain():
    thin_summary = ChainSummary(
        call_open_interest=5, put_open_interest=3, net_dealer_gamma=-1.0,
        gamma_notional_per_1pct_move=100.0, atm_iv=0.9, has_usable_greeks=True,
    )
    assert to_options_inputs(thin_summary, iv_rank=50.0, avg_dollar_volume=1_000_000.0) is None


def test_to_options_inputs_accepts_a_valid_chain():
    valid_summary = ChainSummary(
        call_open_interest=300, put_open_interest=100, net_dealer_gamma=-500.0,
        gamma_notional_per_1pct_move=200_000.0, atm_iv=0.9, has_usable_greeks=True,
    )
    result = to_options_inputs(valid_summary, iv_rank=70.0, avg_dollar_volume=1_000_000.0)
    assert result is not None
    assert result.call_put_oi_ratio == 3.0
    assert result.net_dealer_gamma == -500.0


def test_select_expiration_returns_none_for_a_same_day_singleton():
    # PR #99 follow-up review, gap 2: round 1 fixed the array-index bug but
    # still fell back to "the furthest-out listed date" when nothing
    # cleared the horizon, so a same-day singleton was STILL accepted (just
    # via the fallback branch). There is no acceptable fallback now: no
    # eligible expiration means None, treated as OPTIONS_STATUS_UNAVAILABLE.
    today = date(2026, 9, 22)
    assert select_expiration(["2026-09-22"], today) is None
    # With a later option available, the near one must be skipped in favor
    # of it, not merely tolerated as a fallback.
    result = select_expiration(["2026-09-22", "2026-09-29"], today)
    assert result == "2026-09-29"


def test_select_expiration_returns_none_when_no_date_clears_the_horizon():
    today = date(2026, 9, 22)
    # All three are inside MIN_DAYS_TO_EXPIRATION (5 days) -- none eligible.
    assert select_expiration(["2026-09-22", "2026-09-24", "2026-09-25"], today) is None


def test_select_expiration_handles_unsorted_input():
    today = date(2026, 9, 22)
    dates = ["2026-10-17", "2026-09-23", "2026-09-30"]  # deliberately out of order
    # 2026-09-30 is the first (earliest) that clears the horizon, even
    # though it's not first in the input list.
    assert select_expiration(dates, today) == "2026-09-30"


def test_select_expiration_picks_first_date_past_the_horizon():
    today = date(2026, 9, 22)
    dates = ["2026-09-23", "2026-09-25", "2026-09-30", "2026-10-17"]
    # MIN_DAYS_TO_EXPIRATION=5 -> 2026-09-23/25 (1, 3 days out) are too near;
    # 2026-09-30 (8 days out) is the first that clears the horizon.
    assert select_expiration(dates, today) == "2026-09-30"


def test_select_expiration_empty_list_returns_none():
    assert select_expiration([], date(2026, 9, 22)) is None


# -- gap 1: malformed payloads raise a defined error, not AttributeError --


def test_get_quote_raises_defined_error_on_null_quotes_object():
    with patch("squeeze_scanner.tradier_options._get", return_value={"quotes": {"quote": None}}):
        with pytest.raises(TradierDataError):
            get_quote("BAD")


def test_get_quote_raises_defined_error_when_quotes_key_itself_is_missing():
    with patch("squeeze_scanner.tradier_options._get", return_value={}):
        with pytest.raises(TradierDataError):
            get_quote("BAD")


def test_get_option_chain_raises_defined_error_on_null_options_object():
    with patch("squeeze_scanner.tradier_options._get", return_value={"options": None}):
        with pytest.raises(TradierDataError):
            get_option_chain("BAD", "2026-10-17")


def test_get_option_chain_returns_empty_list_for_a_genuinely_empty_chain():
    # {"option": None} under a well-formed 'options' object is a real "no
    # contracts here" answer, not malformed data -- must NOT raise.
    with patch("squeeze_scanner.tradier_options._get", return_value={"options": {"option": None}}):
        assert get_option_chain("THIN", "2026-10-17") == []


# -- gap 4: non-finite Greeks must not silently produce a valid-looking score --


def test_nan_gamma_contract_does_not_set_has_usable_greeks():
    chain = [_contract("call", 100.0, 100, gamma=float("nan"), iv=0.8)]
    summary = summarize_chain(chain, spot_price=100.0)
    assert summary.has_usable_greeks is False
    assert summary.net_dealer_gamma == 0.0  # NaN never entered the aggregate


def test_nan_gamma_mixed_with_valid_contracts_only_uses_the_valid_ones():
    chain = [
        _contract("call", 100.0, 100, gamma=float("nan"), iv=0.8),
        _contract("put", 100.0, 50, gamma=0.05, iv=0.8),
    ]
    summary = summarize_chain(chain, spot_price=100.0)
    assert summary.has_usable_greeks is True  # the put contract alone makes this true
    # customer_net_gamma = 0 (NaN call rejected) - 0.05*50 = -2.5; dealer_net_gamma = -(-2.5) = 2.5
    assert summary.net_dealer_gamma == pytest.approx(2.5)


def test_infinite_iv_is_rejected_as_atm_iv():
    chain = [_contract("call", 100.0, 100, gamma=0.1, iv=float("inf"))]
    summary = summarize_chain(chain, spot_price=100.0)
    assert summary.atm_iv is None


def test_summarize_chain_rejects_non_finite_spot_price():
    with pytest.raises(ValueError):
        summarize_chain([_contract("call", 100.0, 100, gamma=0.1, iv=0.8)], spot_price=float("nan"))


def test_to_options_inputs_rejects_non_finite_avg_dollar_volume():
    valid_summary = ChainSummary(
        call_open_interest=300, put_open_interest=100, net_dealer_gamma=-500.0,
        gamma_notional_per_1pct_move=200_000.0, atm_iv=0.9, has_usable_greeks=True,
    )
    assert to_options_inputs(valid_summary, iv_rank=70.0, avg_dollar_volume=float("nan")) is None
    assert to_options_inputs(valid_summary, iv_rank=70.0, avg_dollar_volume=float("inf")) is None
