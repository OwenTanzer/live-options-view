"""Pure scoring math for the short-squeeze scanner, no I/O.

Two independent factor groups feed a single composite score, mirroring the
"same kind of signal, computed the same way" split already used elsewhere in
this repo (see market_signals.py's docstring) between a pure-math layer and
the data-fetching layer that calls it (finviz_client.py, tradier_options.py).

Group 1 -- "rule/factor" score: the classic short-interest mechanics (days to
cover, float size, short float %) plus a momentum/volume confirmation
(relative volume, recent price change). None of this needs options data.

Group 2 -- "options" score: what the options market is pricing in and what
dealer positioning implies. A stock can look mechanically squeeze-prone on
Group 1 and still go nowhere if there's no options-side catalyst; a
short-dated IV already elevated, skewed toward calls, with dealers net short
gamma (so a rally forces them to buy stock to stay hedged) is the options
market's own version of "primed."

Every function here takes plain numbers and returns a plain float or
dataclass -- no network calls, no pandas, so these are cheap to unit-test
against hand-built fixtures the way market_signals.py's VWAP/RVOL functions
are (tests/test_squeeze_scanner_scoring.py).
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _normalize(value: float, low: float, high: float) -> float:
    """Linearly map value from [low, high] to [0, 1], clamped at the ends."""
    if high == low:
        return 0.0
    return _clamp01((value - low) / (high - low))


@dataclass(frozen=True)
class FactorInputs:
    short_float_pct: float  # shares short / float, as a percentage (e.g. 22.5)
    days_to_cover: float  # short interest / avg daily volume
    float_shares: float  # shares in the public float
    relative_volume: float  # today's volume / its own trailing average (1.0 = normal)
    price_change_5d_pct: float  # trailing 5-session return, as a percentage


@dataclass(frozen=True)
class FactorScore:
    score: float  # 0-1 composite
    short_float_component: float
    days_to_cover_component: float
    float_size_component: float
    volume_component: float
    momentum_component: float


# Normalization ranges are deliberately generous rather than tuned to any one
# past squeeze (GME/AMC-era prints were extreme outliers) -- see
# docs/plans/2026-09-short-squeeze-scanner.md for the reasoning and the
# follow-up to recalibrate once real scan history exists.
_SHORT_FLOAT_RANGE = (5.0, 40.0)  # % of float short
_DAYS_TO_COVER_RANGE = (1.0, 10.0)
_FLOAT_SIZE_RANGE_SHARES = (100_000_000.0, 5_000_000.0)  # inverted: smaller float = higher score
_RVOL_RANGE = (1.0, 5.0)
_MOMENTUM_RANGE_PCT = (0.0, 30.0)  # only rewards upward momentum; a falling
# squeeze candidate scores 0 on this component, not negative


def compute_factor_score(inputs: FactorInputs) -> FactorScore:
    short_float_component = _normalize(inputs.short_float_pct, *_SHORT_FLOAT_RANGE)
    days_to_cover_component = _normalize(inputs.days_to_cover, *_DAYS_TO_COVER_RANGE)
    float_size_component = _normalize(inputs.float_shares, *_FLOAT_SIZE_RANGE_SHARES)
    volume_component = _normalize(inputs.relative_volume, *_RVOL_RANGE)
    momentum_component = _normalize(inputs.price_change_5d_pct, *_MOMENTUM_RANGE_PCT)

    # Short-interest mechanics matter most; volume/momentum are confirmation,
    # not the thesis -- a quiet, un-squeezed high-short-interest name still
    # deserves a watchlist slot, just not the top one.
    score = (
        0.35 * short_float_component
        + 0.25 * days_to_cover_component
        + 0.15 * float_size_component
        + 0.15 * volume_component
        + 0.10 * momentum_component
    )
    return FactorScore(
        score=score,
        short_float_component=short_float_component,
        days_to_cover_component=days_to_cover_component,
        float_size_component=float_size_component,
        volume_component=volume_component,
        momentum_component=momentum_component,
    )


@dataclass(frozen=True)
class OptionsInputs:
    iv_rank: float  # 0-100, current IV's percentile within its own trailing range
    call_put_oi_ratio: float  # total call OI / total put OI, near-dated chain
    net_dealer_gamma: float  # signed; negative = dealers net short gamma (squeeze fuel)
    gamma_notional_per_1pct_move: float  # abs($ dealers must trade per 1% underlying move)


@dataclass(frozen=True)
class OptionsScore:
    score: float  # 0-1 composite
    iv_rank_component: float
    call_skew_component: float
    gamma_component: float


_IV_RANK_RANGE = (30.0, 90.0)
_CALL_PUT_RATIO_RANGE = (1.0, 4.0)
# Gamma notional is name-specific (a $2 stock and a $200 stock don't compare
# on raw dollars); callers normalize it upstream (see tradier_options.py's
# gamma_notional_per_1pct_move relative to the underlying's own market cap or
# ADV$ before this range is applied) -- this range assumes that normalization
# already happened and the value is a 0-1-ish "how much of ADV$ dealers must
# transact" ratio.
_GAMMA_PRESSURE_RANGE = (0.02, 0.25)


def compute_options_score(inputs: OptionsInputs) -> OptionsScore:
    iv_rank_component = _normalize(inputs.iv_rank, *_IV_RANK_RANGE)
    call_skew_component = _normalize(inputs.call_put_oi_ratio, *_CALL_PUT_RATIO_RANGE)

    # Only a NEGATIVE net dealer gamma is squeeze fuel (dealers must buy into
    # a rally to stay hedged); positive dealer gamma is stabilizing, so it
    # contributes zero rather than a penalty -- this is a "does this signal
    # exist" component, not a symmetric one.
    gamma_pressure = abs(inputs.gamma_notional_per_1pct_move) if inputs.net_dealer_gamma < 0 else 0.0
    gamma_component = _normalize(gamma_pressure, *_GAMMA_PRESSURE_RANGE)

    score = 0.35 * iv_rank_component + 0.25 * call_skew_component + 0.40 * gamma_component
    return OptionsScore(
        score=score,
        iv_rank_component=iv_rank_component,
        call_skew_component=call_skew_component,
        gamma_component=gamma_component,
    )


@dataclass(frozen=True)
class CompositeScore:
    composite: float  # 0-1
    factor: FactorScore
    options: OptionsScore


def compute_composite_score(
    factor_inputs: FactorInputs,
    options_inputs: OptionsInputs | None,
    *,
    factor_weight: float = 0.6,
    options_weight: float = 0.4,
) -> CompositeScore:
    """Combine both groups. `options_inputs=None` means no usable chain was
    found for this ticker (illiquid options, or Tradier returned nothing) --
    the composite then falls back to the factor score alone at full weight,
    rather than silently averaging in a zero and punishing thinly-optioned
    small caps, which are exactly the names most likely to actually squeeze.
    """
    factor = compute_factor_score(factor_inputs)
    if options_inputs is None:
        return CompositeScore(composite=factor.score, factor=factor, options=OptionsScore(0.0, 0.0, 0.0, 0.0))

    options = compute_options_score(options_inputs)
    composite = factor_weight * factor.score + options_weight * options.score
    return CompositeScore(composite=composite, factor=factor, options=options)
