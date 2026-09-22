"""Tradier options-chain fetch + the raw-inputs-to-OptionsInputs translation.

Reads TRADIER_TOKEN from the environment, matching the convention already
used by scripts/moo144_tradier_collector.py in this repo (NOT the
TRADIER_API_TOKEN name used by the standalone donkeyballs/tradier_opra_pull.py
script -- those are two different local scripts with independently-chosen
env var names; this module follows this repo's own name since it lives here).

Everything that touches the network lives in this file; squeeze_scanner.scoring
never does. See that module's docstring for why the split matters for testing.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

from squeeze_scanner.scoring import OptionsInputs

BASE_URL = "https://api.tradier.com/v1"
REQUEST_TIMEOUT_SECONDS = 10  # every Tradier call below is bounded; an unbounded
# call previously let one stalled response block the whole sequential scan
# (PR #99 review, additional reliability gap).
MIN_DAYS_TO_EXPIRATION = 5  # skip 0-4 DTE expirations: single-name gamma/IV
# from a contract expiring almost immediately is mostly pin-risk noise, not
# the multi-session squeeze-catalyst window this scanner is looking for.


def _headers() -> dict[str, str]:
    token = os.environ.get("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is required (see .env / Railway variable)")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get(path: str, params: dict) -> dict:
    resp = requests.get(f"{BASE_URL}{path}", params=params, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    if resp.status_code == 429:
        raise RuntimeError(f"Tradier rate limit hit on {path} (429) -- back off before retrying")
    resp.raise_for_status()
    return resp.json()


def get_quote(symbol: str) -> dict:
    data = _get("/markets/quotes", {"symbols": symbol})
    quote = data["quotes"]["quote"]
    return quote[0] if isinstance(quote, list) else quote


def select_expiration(dates: list[str], today: date) -> str | None:
    """Pure selection logic: first of `dates` (each "YYYY-MM-DD") at least
    MIN_DAYS_TO_EXPIRATION calendar days past `today`, or the furthest-out
    date if none clear that horizon. None only for an empty `dates`.

    Fixes PR #99 review finding: the previous version took array index 1
    (or 0 for a singleton) with no date check at all, so a same-day
    singleton expiration was accepted outright. Split out from
    get_nearest_expiration so tests/test_squeeze_scanner_tradier_options.py
    can exercise singleton/monthly/near-expiry cases without a network call.
    """
    if not dates:
        return None
    parsed = [(d, datetime.strptime(d, "%Y-%m-%d").date()) for d in dates]
    far_enough = [d for d, exp_date in parsed if (exp_date - today) >= timedelta(days=MIN_DAYS_TO_EXPIRATION)]
    if far_enough:
        return far_enough[0]
    return parsed[-1][0]  # every listed expiration is inside the horizon -- take the furthest-out anyway


def get_nearest_expiration(symbol: str, *, today: date | None = None) -> str | None:
    """First expiration at least MIN_DAYS_TO_EXPIRATION calendar days out
    (see select_expiration for the actual selection logic)."""
    today = today or date.today()
    data = _get("/markets/options/expirations", {"symbol": symbol, "includeAllRoots": "true"})
    # Tradier returns {"expirations": null} outright for a symbol with no
    # listed options at all (not {"expirations": {"date": null}}) -- caught
    # live while re-verifying this fix, not in the original review.
    dates = (data.get("expirations") or {}).get("date")
    if not dates:
        return None
    dates = [dates] if isinstance(dates, str) else dates
    return select_expiration(dates, today)


def get_option_chain(symbol: str, expiration: str) -> list[dict]:
    data = _get("/markets/options/chains", {"symbol": symbol, "expiration": expiration, "greeks": "true"})
    options = data.get("options", {}).get("option")
    if options is None:
        return []
    return options if isinstance(options, list) else [options]


@dataclass(frozen=True)
class ChainSummary:
    call_open_interest: int
    put_open_interest: int
    net_dealer_gamma: float  # negative = dealers net short gamma
    gamma_notional_per_1pct_move: float  # $ dealers must transact per 1% underlying move
    atm_iv: float | None  # decimal (0.85 = 85% IV), from the strike nearest spot
    has_usable_greeks: bool  # False if no contract in the chain carried greeks at all


def summarize_chain(chain: list[dict], spot_price: float) -> ChainSummary:
    """Aggregate a single expiration's chain into the inputs OptionsInputs needs.

    Dealer positioning convention: customers (retail/institutional flow) are
    assumed net LONG calls and net SHORT puts against dealers (the standard
    simplifying assumption used by every public "gamma exposure" tracker,
    e.g. SqueezeMetrics' GEX) -- so:

        customer_net_gamma = sum(call_gamma * call_OI) - sum(put_gamma * put_OI)
        dealer_net_gamma   = -customer_net_gamma

    (customers long calls contributes positive gamma to the customer side;
    customers short puts contributes negative gamma to the customer side,
    since being short an option is short gamma regardless of call/put --
    puts' own gamma value is positive, so a short-put position is
    -put_gamma. Dealers are the customers' counterparty on both legs, so
    dealer gamma is the negative of all of that.)

    PR #99 review caught this backwards: the previous version returned
    customer_net_gamma and labeled it dealer gamma, so a call-heavy chain
    (dealers actually net SHORT gamma, squeeze fuel) reported as positive
    net_dealer_gamma, which compute_options_score treats as dealers being
    LONG gamma (stabilizing, i.e. exactly the wrong squeeze signal). This is
    still a market-wide positioning convention, not known fact about any
    specific name's actual dealer book -- treat it as a proxy.
    """
    call_oi = 0
    put_oi = 0
    customer_net_gamma = 0.0
    atm_iv = None
    atm_distance = math.inf
    has_usable_greeks = False

    for contract in chain:
        oi = contract.get("open_interest") or 0
        greeks = contract.get("greeks") or {}
        gamma = greeks.get("gamma")
        strike = contract.get("strike") or 0.0

        if gamma is not None:
            has_usable_greeks = True
            if contract.get("option_type") == "call":
                call_oi += oi
                customer_net_gamma += gamma * oi
            elif contract.get("option_type") == "put":
                put_oi += oi
                customer_net_gamma -= gamma * oi
        else:
            # Still count OI for the call/put skew even without greeks --
            # only the gamma aggregate needs has_usable_greeks to gate it.
            if contract.get("option_type") == "call":
                call_oi += oi
            elif contract.get("option_type") == "put":
                put_oi += oi

        distance = abs(strike - spot_price)
        iv = greeks.get("mid_iv") or greeks.get("smv_vol")
        if iv and distance < atm_distance:
            atm_distance = distance
            atm_iv = iv

    dealer_net_gamma = -customer_net_gamma

    # Standard GEX-style dollarization: gamma is "delta change per $1 move,"
    # so scaling by spot^2 * 0.01 * 100 (shares/contract) converts it to a
    # dollar amount dealers must trade for a 1% move in the underlying.
    gamma_notional = abs(dealer_net_gamma) * (spot_price**2) * 0.01 * 100

    return ChainSummary(
        call_open_interest=call_oi,
        put_open_interest=put_oi,
        net_dealer_gamma=dealer_net_gamma,
        gamma_notional_per_1pct_move=gamma_notional,
        atm_iv=atm_iv,
        has_usable_greeks=has_usable_greeks,
    )


def to_options_inputs(summary: ChainSummary, *, iv_rank: float, avg_dollar_volume: float) -> OptionsInputs | None:
    """Normalize a ChainSummary + externally-supplied iv_rank into scoring's
    OptionsInputs. Returns None (no usable signal) when the chain was too
    thin, or carried no greeks at all, to mean anything -- see
    scoring.compute_composite_score for how a None here falls back to the
    factor score alone rather than zeroing it.

    PR #99 review caught a chain with 100 call contracts and zero greeks
    passing this check (total_oi >= 50 alone), producing a spurious
    "valid" options score built entirely from OI skew with silently-zeroed
    gamma/IV components. `has_usable_greeks` now gates that explicitly.

    iv_rank is NOT computed in this module: a real IV rank needs a rolling
    52-week history of this underlying's own IV, which this scanner doesn't
    yet persist (see docs/plans/2026-09-short-squeeze-scanner.md, "Known
    gaps"). Callers currently pass a proxy or a placeholder; treat any
    iv_rank-driven ranking as provisional until that history exists.
    """
    total_oi = summary.call_open_interest + summary.put_open_interest
    if total_oi < 50 or avg_dollar_volume <= 0 or not summary.has_usable_greeks or summary.atm_iv is None:
        return None

    call_put_ratio = summary.call_open_interest / max(summary.put_open_interest, 1)
    # Normalize gamma notional against the name's own dollar volume so a
    # $2 microcap and a $200 stock are comparable (see scoring.py's
    # _GAMMA_PRESSURE_RANGE docstring note).
    normalized_gamma_pressure = summary.gamma_notional_per_1pct_move / avg_dollar_volume

    return OptionsInputs(
        iv_rank=iv_rank,
        call_put_oi_ratio=call_put_ratio,
        net_dealer_gamma=summary.net_dealer_gamma,
        gamma_notional_per_1pct_move=normalized_gamma_pressure,
    )
