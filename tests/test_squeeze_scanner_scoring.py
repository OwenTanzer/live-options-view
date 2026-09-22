"""Pure-function tests for squeeze_scanner.scoring -- no network, no fixtures
beyond hand-built dataclasses, matching this repo's convention for
market_signals.py (see tests/verify_collector_vwap_rvol.py)."""

from squeeze_scanner.scoring import (
    FactorInputs,
    OptionsInputs,
    compute_composite_score,
    compute_factor_score,
    compute_options_score,
)


def test_factor_score_rewards_classic_squeeze_setup():
    hot = FactorInputs(
        short_float_pct=35.0,
        days_to_cover=8.0,
        float_shares=8_000_000,
        relative_volume=4.0,
        price_change_5d_pct=25.0,
    )
    cold = FactorInputs(
        short_float_pct=6.0,
        days_to_cover=1.5,
        float_shares=400_000_000,
        relative_volume=1.0,
        price_change_5d_pct=0.0,
    )
    assert compute_factor_score(hot).score > compute_factor_score(cold).score
    assert 0.0 <= compute_factor_score(hot).score <= 1.0


def test_factor_score_ignores_negative_momentum_rather_than_penalizing():
    falling = FactorInputs(
        short_float_pct=20.0, days_to_cover=5.0, float_shares=20_000_000,
        relative_volume=2.0, price_change_5d_pct=-40.0,
    )
    flat = FactorInputs(
        short_float_pct=20.0, days_to_cover=5.0, float_shares=20_000_000,
        relative_volume=2.0, price_change_5d_pct=0.0,
    )
    # -40% and 0% both clamp to the same zero momentum component.
    assert compute_factor_score(falling).score == compute_factor_score(flat).score


def test_options_score_requires_negative_dealer_gamma_for_pressure_credit():
    base_kwargs = dict(iv_rank=80.0, call_put_oi_ratio=3.0, gamma_notional_per_1pct_move=0.2)
    short_gamma = OptionsInputs(net_dealer_gamma=-500.0, **base_kwargs)
    long_gamma = OptionsInputs(net_dealer_gamma=500.0, **base_kwargs)

    assert compute_options_score(short_gamma).gamma_component > 0.0
    assert compute_options_score(long_gamma).gamma_component == 0.0
    assert compute_options_score(short_gamma).score > compute_options_score(long_gamma).score


def test_composite_falls_back_to_factor_only_when_no_options_signal():
    factor_inputs = FactorInputs(
        short_float_pct=25.0, days_to_cover=6.0, float_shares=15_000_000,
        relative_volume=3.0, price_change_5d_pct=10.0,
    )
    composite = compute_composite_score(factor_inputs, options_inputs=None)
    assert composite.composite == compute_factor_score(factor_inputs).score


def test_composite_blends_both_groups_when_options_signal_present():
    factor_inputs = FactorInputs(
        short_float_pct=25.0, days_to_cover=6.0, float_shares=15_000_000,
        relative_volume=3.0, price_change_5d_pct=10.0,
    )
    options_inputs = OptionsInputs(
        iv_rank=85.0, call_put_oi_ratio=3.5, net_dealer_gamma=-100.0, gamma_notional_per_1pct_move=0.2,
    )
    composite = compute_composite_score(factor_inputs, options_inputs)
    factor_only = compute_factor_score(factor_inputs).score
    options_only = compute_options_score(options_inputs).score
    assert min(factor_only, options_only) <= composite.composite <= max(factor_only, options_only)
