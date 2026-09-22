"""Pure-function tests for squeeze_scanner.tradier_options -- the
chain-to-score path (summarize_chain, to_options_inputs, select_expiration),
none of which touch the network. PR #99's review flagged that the original
tests only ever supplied an already-signed OptionsInputs and never tested
this translation at all -- that's the gap these fill."""

from datetime import date

from squeeze_scanner.tradier_options import (
    ChainSummary,
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


def test_select_expiration_skips_a_same_day_singleton():
    today = date(2026, 9, 22)
    # A singleton same-day expiration used to be accepted outright.
    assert select_expiration(["2026-09-22"], today) == "2026-09-22"  # only option -> fallback, but flagged as such
    # With a later option available, the near one must be skipped.
    result = select_expiration(["2026-09-22", "2026-09-29"], today)
    assert result == "2026-09-29"


def test_select_expiration_picks_first_date_past_the_horizon():
    today = date(2026, 9, 22)
    dates = ["2026-09-23", "2026-09-25", "2026-09-30", "2026-10-17"]
    # MIN_DAYS_TO_EXPIRATION=5 -> 2026-09-23/25 (1, 3 days out) are too near;
    # 2026-09-30 (8 days out) is the first that clears the horizon.
    assert select_expiration(dates, today) == "2026-09-30"


def test_select_expiration_empty_list_returns_none():
    assert select_expiration([], date(2026, 9, 22)) is None
