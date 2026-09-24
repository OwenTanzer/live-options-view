"""Black-Scholes theoretical-edge diagnostic for QQQ options strategies.

Same shape as `vwap_rvol.py` on purpose: pure evaluation of already-observed
inputs against a threshold, no accumulation, no network. Where
`vwap_rvol.evaluate_gate` confirms momentum's *direction* against the tape,
`evaluate_edge_gate` compares a specific quote's *price* against Black-
Scholes theoretical fair value (see `black_scholes.py`) -- a read on the
quote itself, not a second opinion on momentum.

The feed already delivers its own live Greeks per row (`collector.py`'s
`"Greeks"` event -- `IV`/`Delta`/`Gamma`/`Theta`/`Vega` on each snapshot
row), so this isn't recomputing IV from nothing: it takes the row's own
`IV`, feeds it back through `black_scholes.theoretical_price` at the current
underlying price and time-to-expiry, and reports how far the live bid/ask a
strategy is considering has drifted from what that IV implies.

IMPORTANT -- this is diagnostic only; `momentum_qqq.py` (Newton) attaches it
to a decision's metadata via `bs_edge_diagnostics_enabled` and never gates a
trade on it, for two reasons review surfaced that this module cannot resolve
on its own:

1. `underlying_price` and the row's `IV` come from the collector's ~60s
   durable board (see `market.py`), while `quoted_price` is a separately
   fetched execution quote refreshed roughly every 15s
   (`market.EXECUTION_QUOTE_MAX_AGE_S`). `collector.py` records no
   observation timestamp on Greeks, so there is no way from here to confirm
   the two are simultaneous -- a several-second underlying move alone can
   produce a large `edge_pct` with nothing actually wrong.
2. dxFeed's own IV/Greeks calculation freezes time-to-expiry at a fixed 30
   minutes near the close
   (https://dxfeed.com/new-implied-volatility-and-greeks-calculation-update/),
   while `time_to_expiry_years` (see `black_scholes.py`) uses an actual
   wall-clock countdown -- so even perfectly simultaneous, internally
   consistent provider data can disagree with this module's own math near
   expiry, for reasons that have nothing to do with the quote being wrong.

Resolving either requires a feed contract this repo doesn't have yet
(timestamped Greeks, or the provider's own valuation convention) -- until
then, `edge_pct`/`edge_ok` are worth logging and inspecting, not worth
standing between momentum and a fill.

Like `evaluate_gate`, this never fabricates a verdict from data it hasn't
looked at: a missing row IV, a missing quote, or a T that's already
collapsed to the expiry floor all report `status` accordingly with
`edge_pct`/`theoretical_price` left `None`, rather than guessed.
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
