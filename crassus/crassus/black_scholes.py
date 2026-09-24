"""Black-Scholes option pricing, Greeks, and implied-volatility solver.

https://en.wikipedia.org/wiki/Black%E2%80%93Scholes_model

Pure math, no I/O and no dependency on the rest of the strategy stack --
same split as `momentum.py` (accumulation) vs `vwap_rvol.py` (evaluation):
this module knows nothing about `MarketSnapshot`, `ctx.params`, or accounts.
It exists as a standalone tool two different callers can reach for:

1. Directly, for ad hoc "what should this be worth" questions (backtests,
   `synthetic_days/`, a one-off script) where the live DXLink feed's own
   Greeks (`collector.py`'s `"Greeks"` event -- `volatility`/`delta`/`gamma`/
   `theta`/`vega`, already carried through to snapshot rows as `IV`/`Delta`/
   `Gamma`/`Theta`/`Vega`) aren't available or aren't trusted.
2. Through `bs_edge.py`, which wraps `theoretical_price` into an optional
   sanity gate `strategies/momentum_qqq.py` (Newton) can require before
   opening a position -- see that module's docstring.

European-exercise closed-form only (no early-exercise adjustment): these are
short-dated, cash-settled-in-spirit QQQ single-name options this repo never
holds through a dividend record date, so the American-vs-European premium
this ignores is negligible relative to the bid/ask spreads already being
traded through.

`d1`/`d2` and the Greeks formulas below are the textbook closed forms (see
the Wikipedia article linked above, or Hull's *Options, Futures, and Other
Derivatives*); `_norm_cdf`/`_norm_pdf` use `math.erf` so this module needs no
`scipy`/`numpy` dependency, matching the rest of `crassus` (see
`exchange_calendar.py`'s docstring for the one place that tradeoff went the
other way, and why).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time

from .exchange_calendar import session_close

_SQRT_2PI = math.sqrt(2.0 * math.pi)

# Below this, T is treated as expired -- d1/d2 blow up (division by ~0) as
# T -> 0, and an option seconds from expiry is worth its intrinsic value,
# not a Black-Scholes extrapolation of it.
MIN_T_YEARS = 1.0 / (365.0 * 24.0 * 60.0 * 60.0)  # one second


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if sigma <= 0.0:
        raise ValueError(f"sigma must be positive, got {sigma!r}")
    if T <= 0.0:
        raise ValueError(f"T must be positive, got {T!r}")
    if S <= 0.0 or K <= 0.0:
        raise ValueError(f"S and K must be positive, got S={S!r} K={K!r}")
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def intrinsic_value(option_type: str, S: float, K: float) -> float:
    """What the option is worth with zero time value left (T <= 0)."""
    if option_type == "call":
        return max(S - K, 0.0)
    if option_type == "put":
        return max(K - S, 0.0)
    raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")


def theoretical_price(option_type: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes fair value. Falls back to intrinsic value at T <= 0."""
    if T <= 0.0:
        return intrinsic_value(option_type, S, K)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    if option_type == "put":
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day, matching the feed's own `Theta` convention
    vega: float  # per 1.00 (100 vol points) change in sigma
    rho: float


def greeks(option_type: str, S: float, K: float, T: float, r: float, sigma: float) -> Greeks:
    """Analytic Black-Scholes Greeks. Undefined at T <= 0 (raises ValueError) --
    callers past expiry want `intrinsic_value`, not a Greeks read on a dead
    option; same "don't fabricate a value" stance `_d1_d2` already takes.
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = _norm_pdf(d1)
    sqrt_t = math.sqrt(T)
    discount = math.exp(-r * T)

    gamma = pdf_d1 / (S * sigma * sqrt_t)
    vega = S * pdf_d1 * sqrt_t / 100.0  # scaled to "per 1 vol point" like the feed

    if option_type == "call":
        delta = _norm_cdf(d1)
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_t)
            - r * K * discount * _norm_cdf(d2)
        )
        rho = K * T * discount * _norm_cdf(d2) / 100.0
    elif option_type == "put":
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_t)
            + r * K * discount * _norm_cdf(-d2)
        )
        rho = -K * T * discount * _norm_cdf(-d2) / 100.0
    else:
        raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")

    return Greeks(delta=delta, gamma=gamma, theta=theta_annual / 365.0, vega=vega, rho=rho)


def implied_volatility(
    option_type: str,
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    *,
    initial_guess: float = 0.5,
    max_iterations: int = 100,
    tolerance: float = 1e-6,
) -> float | None:
    """Solve for sigma given an observed market price.

    Newton-Raphson using vega as the derivative, with a bisection fallback
    (and as the actual driver whenever Newton's step would leave the
    search bracket or vega goes numerically flat near either boundary) --
    Newton alone can overshoot or divide by a near-zero vega for deep
    ITM/OTM strikes, which is exactly the regime 0DTE strikes drift into as
    expiry approaches. Returns `None` rather than raising when no sigma in
    (1e-4, 5.0) reproduces `market_price` within tolerance (e.g. a price
    below the minimum any sigma in range can produce, or above the maximum)
    -- a broken quote should read as "no usable IV," not crash the caller.

    The lower bound on a valid price is `theoretical_price` evaluated at
    `lo`, not the undiscounted intrinsic value: at a positive risk-free
    rate a European option's own minimum (as sigma -> 0) is the *discounted*
    intrinsic value (`max(S - K*e^-rT, 0)` for a call, `max(K*e^-rT - S, 0)`
    for a put), which sits below undiscounted intrinsic for a put. An
    earlier version pre-checked against undiscounted intrinsic and rejected
    valid European put prices in exactly that gap -- see
    `verify_black_scholes.py`'s `scenario_iv_put_below_undiscounted_intrinsic`.
    The `price_lo`/`price_hi` bracket below is already expressed in terms of
    `theoretical_price` itself, so it's the correct bound on its own; no
    separate intrinsic-value pre-check is needed.
    """
    if T <= 0.0 or market_price <= 0.0:
        return None

    lo, hi = 1e-4, 5.0
    price_lo = theoretical_price(option_type, S, K, T, r, lo) - market_price
    price_hi = theoretical_price(option_type, S, K, T, r, hi) - market_price
    if price_lo > 0 or price_hi < 0:
        return None  # market price outside what any sigma in range can produce

    sigma = initial_guess
    for _ in range(max_iterations):
        if not (lo < sigma < hi):
            sigma = (lo + hi) / 2.0

        price = theoretical_price(option_type, S, K, T, r, sigma)
        diff = price - market_price
        if abs(diff) < tolerance:
            return sigma

        if diff > 0:
            hi = sigma
        else:
            lo = sigma

        vega_per_unit = greeks(option_type, S, K, T, r, sigma).vega * 100.0
        if vega_per_unit > 1e-8:
            newton_step = sigma - diff / vega_per_unit
            if lo < newton_step < hi:
                sigma = newton_step
                continue
        sigma = (lo + hi) / 2.0

    return None


def time_to_expiry_years(
    now: datetime,
    expiration: date,
    *,
    close_time: time | None = None,
) -> float:
    """Years remaining until the option's expiration-day close.

    `now` must be tz-aware in the exchange's local time (callers pass
    `ctx.now_et`, same convention `momentum_qqq.py` already uses throughout).
    `close_time` defaults to `exchange_calendar.session_close(expiration)` so
    an early-close expiration day (see that module's docstring) shortens the
    countdown the same way it shortens the actual session -- a 0DTE option
    on the day before Thanksgiving stops trading at 13:00, not 16:00.
    Clamped to `MIN_T_YEARS` rather than 0 so callers can feed the result
    straight into `theoretical_price`/`greeks` without a separate T<=0
    branch; at that floor the result is indistinguishable from intrinsic
    value anyway.
    """
    close = close_time if close_time is not None else session_close(expiration)
    expiry_dt = datetime.combine(expiration, close, tzinfo=now.tzinfo)
    seconds_remaining = (expiry_dt - now).total_seconds()
    years = seconds_remaining / (365.0 * 24.0 * 60.0 * 60.0)
    return max(years, MIN_T_YEARS)
