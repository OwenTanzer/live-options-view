"""Black-Scholes theoretical-edge gate for QQQ options strategies.

Same shape as `vwap_rvol.py` on purpose: pure evaluation of already-observed
inputs against a strategy's thresholds, no accumulation, no network. Where
`vwap_rvol.evaluate_gate` confirms momentum's *direction* against the tape,
`evaluate_edge_gate` confirms a specific quote's *price* against Black-
Scholes theoretical fair value (see `black_scholes.py`) -- a sanity check on
the quote itself, not a second opinion on momentum.

The feed already delivers its own live Greeks per row (`collector.py`'s
`"Greeks"` event -- `IV`/`Delta`/`Gamma`/`Theta`/`Vega` on each snapshot
row), so this gate isn't recomputing IV from nothing: it takes the row's own
`IV`, feeds it back through `black_scholes.theoretical_price` at the current
underlying price and time-to-expiry, and checks that the live bid/ask this
strategy is about to trade on hasn't drifted implausibly far from what that
IV implies. A quote that has decoupled from its own row's IV by more than
`max_edge_pct` is more likely a stale or corrupted feed read than a genuine
mispricing an 0DTE options desk left on the table -- same "don't act on a
broken observation" posture `momentum_qqq.py` already takes toward a stale
snapshot or an unavailable VWAP/RVOL read.

Like `evaluate_gate`, this never fabricates a verdict from untrustworthy
data: a missing row IV, a missing quote, or a T that's already collapsed to
the expiry floor all report `status` accordingly with `edge_pct`/
`theoretical_price` left `None`, rather than guessed. It is the caller's job
to treat "hasn't looked" the same as an absent VWAP/RVOL read -- see
`momentum_qqq.py`'s use of this gate, which (unlike the VWAP/RVOL gate)
applies only to *opening* a new position; a close is never vetoed on a
quote-sanity read, since standing down from managing risk on a held
position because of a suspect quote is exactly backwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .black_scholes import MIN_T_YEARS, theoretical_price, time_to_expiry_years

DEFAULT_RISK_FREE_RATE = 0.05


@dataclass(frozen=True)
class BsEdgeGate:
    """The result of `evaluate_edge_gate`."""

    status: str  # "no_iv" | "expired" | "ok"
    edge_ok: bool | None
    edge_pct: float | None
    theoretical_price: float | None
    quoted_price: float | None
    iv: float | None
    time_to_expiry_years: float | None


def evaluate_edge_gate(
    row: dict[str, Any],
    underlying_price: float,
    now_et: datetime,
    expiration: date,
    quoted_price: float,
    *,
    max_edge_pct: float,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> BsEdgeGate:
    """`row` is a snapshot row (`MarketSnapshot.atm(...)`'s return) carrying
    at least `Type` and `IV`. `quoted_price` is the price actually being
    considered for execution -- callers pass the live quote's mid, not the
    row's own possibly-stale `Bid`/`Ask`, so this gate is checking the same
    number the strategy is about to trade on.

    `max_edge_pct` disables nothing when `None` would be passed -- unlike
    `vwap_rvol.evaluate_gate`'s `rvol_floor`/`require_vwap_agreement`, this
    gate has no "off" reading of its own; the caller decides whether to call
    it at all (see `momentum_qqq.py`'s guard before invoking it).
    """
    iv = row.get("IV")
    option_type = row.get("Type")
    if iv is None or iv <= 0 or option_type not in ("call", "put"):
        return BsEdgeGate(
            status="no_iv", edge_ok=None, edge_pct=None, theoretical_price=None,
            quoted_price=quoted_price, iv=iv, time_to_expiry_years=None,
        )

    T = time_to_expiry_years(now_et, expiration)
    if T <= MIN_T_YEARS:
        return BsEdgeGate(
            status="expired", edge_ok=None, edge_pct=None, theoretical_price=None,
            quoted_price=quoted_price, iv=iv, time_to_expiry_years=T,
        )

    strike = row["Strike"]
    fair_value = theoretical_price(option_type, underlying_price, strike, T, risk_free_rate, iv)
    if fair_value <= 0:
        return BsEdgeGate(
            status="no_iv", edge_ok=None, edge_pct=None, theoretical_price=fair_value,
            quoted_price=quoted_price, iv=iv, time_to_expiry_years=T,
        )

    edge_pct = (quoted_price - fair_value) / fair_value
    return BsEdgeGate(
        status="ok",
        edge_ok=abs(edge_pct) <= max_edge_pct,
        edge_pct=edge_pct,
        theoretical_price=fair_value,
        quoted_price=quoted_price,
        iv=iv,
        time_to_expiry_years=T,
    )
