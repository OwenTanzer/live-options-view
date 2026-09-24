"""Black-Scholes snapshot-IV comparison for option-buy audit records.

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

IMPORTANT -- this is diagnostic only; the shared runner attaches it to every
option-buy decision's metadata by default and never gates a
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
standing between an option purchase and a fill.

Like `evaluate_gate`, this never fabricates a verdict from data it hasn't
looked at: a missing row IV, a missing quote, or a T that's already
collapsed to the expiry floor all report `status` accordingly with
`edge_pct`/`theoretical_price` left `None`, rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any

from .black_scholes import MIN_T_YEARS, theoretical_price, time_to_expiry_years

DEFAULT_RISK_FREE_RATE = 0.05
DEFAULT_MAX_EDGE_PCT = 0.15
_OCC_SUFFIX = re.compile(r"\d{6}[CP]\d{8}$")


def annotate_buy_decision(decision: Any, snapshot: Any, quote: Any, now_et: datetime,
                          params: dict[str, Any] | None = None) -> None:
    """Attach a best-effort comparison to the option quote used for a buy.

    Missing inputs are recorded as statuses; no diagnostic failure can affect
    the proposed action. The caller must pass the quote already observed by
    the strategy, never fetch a second quote on the audit path.
    """
    if decision.action != "buy" or not decision.symbol:
        return
    params = params or {}
    if params.get("bs_edge_diagnostics_enabled", True) is False:
        return
    if snapshot is None:
        if _OCC_SUFFIX.search(decision.symbol):
            decision.metadata = {**(decision.metadata or {}), "bs_gate_status": "no_snapshot"}
        return
    metadata = dict(decision.metadata or {})
    try:
        row = snapshot.by_symbol(decision.symbol)
        if row is None:
            if _OCC_SUFFIX.search(decision.symbol):
                metadata["bs_gate_status"] = "no_snapshot_row"
                decision.metadata = metadata
            return
        if row.get("Type") not in ("call", "put"):
            return
        metadata.update(
            bs_snapshot_timestamp=snapshot.timestamp,
            bs_snapshot_underlying_price=snapshot.underlying_price,
            bs_quote_timestamp=getattr(quote, "quote_ts", None),
            bs_quote_server_timestamp=getattr(quote, "server_ts", None),
            bs_quote_age_seconds=getattr(quote, "age_seconds", None),
        )
        if quote is None or quote.bid is None or quote.ask is None:
            metadata["bs_gate_status"] = "no_quote"
        else:
            expiration = datetime.strptime(snapshot.expiration, "%Y-%m-%d").date()
            result = evaluate_edge_gate(
                row, snapshot.underlying_price, now_et, expiration,
                (quote.bid + quote.ask) / 2.0,
                max_edge_pct=params.get("bs_max_edge_pct", DEFAULT_MAX_EDGE_PCT),
                risk_free_rate=params.get("bs_risk_free_rate", DEFAULT_RISK_FREE_RATE),
            )
            metadata.update(
                bs_gate_status=result.status,
                bs_theoretical_price=result.theoretical_price,
                bs_quoted_price=result.quoted_price,
                bs_edge_pct=result.edge_pct,
                bs_edge_ok=result.edge_ok,
                bs_iv=result.iv,
                bs_time_to_expiry_years=result.time_to_expiry_years,
            )
    except Exception as exc:
        metadata.update(bs_gate_status="error", bs_diagnostic_error_type=type(exc).__name__)
    decision.metadata = metadata


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
    gate has no "off" reading of its own; the runner decides whether to call
    it based on account parameters.
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
