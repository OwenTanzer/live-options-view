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

import requests

from squeeze_scanner.scoring import OptionsInputs

BASE_URL = "https://api.tradier.com/v1"


def _headers() -> dict[str, str]:
    token = os.environ.get("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is required (see .env / Railway variable)")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_quote(symbol: str) -> dict:
    resp = requests.get(f"{BASE_URL}/markets/quotes", params={"symbols": symbol}, headers=_headers())
    resp.raise_for_status()
    quote = resp.json()["quotes"]["quote"]
    return quote[0] if isinstance(quote, list) else quote


def get_nearest_expiration(symbol: str) -> str | None:
    """Nearest expiration with at least ~5 trading days left, to skip 0-1 DTE
    noise that dominates single-name gamma calcs without being a real
    squeeze-catalyst window."""
    resp = requests.get(
        f"{BASE_URL}/markets/options/expirations",
        params={"symbol": symbol, "includeAllRoots": "true"},
        headers=_headers(),
    )
    resp.raise_for_status()
    dates = resp.json().get("expirations", {}).get("date")
    if not dates:
        return None
    dates = [dates] if isinstance(dates, str) else dates
    return dates[min(1, len(dates) - 1)]  # index 1 skips the nearest (often weekly/0-2 DTE) expiry when available


def get_option_chain(symbol: str, expiration: str) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/markets/options/chains",
        params={"symbol": symbol, "expiration": expiration, "greeks": "true"},
        headers=_headers(),
    )
    resp.raise_for_status()
    options = resp.json().get("options", {}).get("option")
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


def summarize_chain(chain: list[dict], spot_price: float) -> ChainSummary:
    """Aggregate a single expiration's chain into the inputs OptionsInputs needs.

    Dealer positioning convention: retail/institutional flow is assumed net
    long calls and net short puts against dealers (the standard simplifying
    assumption used by every public "gamma exposure" tracker, e.g. SqueezeMetrics'
    GEX) -- so dealer gamma = sum(call_gamma * call_OI) - sum(put_gamma * put_OI).
    This is a market-wide convention, not specific to any single-name
    positioning we'd actually know; treat it as an approximation, not fact.
    """
    call_oi = 0
    put_oi = 0
    net_gamma = 0.0
    atm_iv = None
    atm_distance = math.inf

    for contract in chain:
        oi = contract.get("open_interest") or 0
        greeks = contract.get("greeks") or {}
        gamma = greeks.get("gamma") or 0.0
        strike = contract.get("strike") or 0.0

        if contract.get("option_type") == "call":
            call_oi += oi
            net_gamma += gamma * oi
        elif contract.get("option_type") == "put":
            put_oi += oi
            net_gamma -= gamma * oi

        distance = abs(strike - spot_price)
        iv = greeks.get("mid_iv") or greeks.get("smv_vol")
        if iv and distance < atm_distance:
            atm_distance = distance
            atm_iv = iv

    # Standard GEX-style dollarization: gamma is "delta change per $1 move,"
    # so scaling by spot^2 * 0.01 * 100 (shares/contract) converts it to a
    # dollar amount dealers must trade for a 1% move in the underlying.
    gamma_notional = abs(net_gamma) * (spot_price**2) * 0.01 * 100

    return ChainSummary(
        call_open_interest=call_oi,
        put_open_interest=put_oi,
        net_dealer_gamma=net_gamma,
        gamma_notional_per_1pct_move=gamma_notional,
        atm_iv=atm_iv,
    )


def to_options_inputs(summary: ChainSummary, *, iv_rank: float, avg_dollar_volume: float) -> OptionsInputs | None:
    """Normalize a ChainSummary + externally-supplied iv_rank into scoring's
    OptionsInputs. Returns None (no usable signal) when the chain was too
    thin to mean anything -- see scoring.compute_composite_score for how a
    None here falls back to the factor score alone rather than zeroing it.

    iv_rank is NOT computed in this module: a real IV rank needs a rolling
    52-week history of this underlying's own IV, which this scanner doesn't
    yet persist (see docs/plans/2026-09-short-squeeze-scanner.md, "Known
    gaps"). Callers currently pass a proxy or a placeholder; treat any
    iv_rank-driven ranking as provisional until that history exists.
    """
    total_oi = summary.call_open_interest + summary.put_open_interest
    if total_oi < 50 or avg_dollar_volume <= 0:
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
