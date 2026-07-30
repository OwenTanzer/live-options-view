"""VWAP/RVOL confirmation gate for QQQ options strategies.

Separate from `momentum.py` on purpose: momentum's math accumulates its own
rolling price history in-process (`PriceHistoryTracker`) because the market
layer only ever hands a strategy one snapshot at a time. VWAP/RVOL need no
accumulation of their own here -- the collector already computes and
delivers them whole, as `MarketSnapshot.underlying_market` (see
`crassus/crassus/market.py` and `docs/plans/2026-07-vwap-rvol.md`). This
module's only job is to *evaluate* those already-computed fields against a
strategy's thresholds -- keeps the "pure math separate from decision logic"
split intact per concern, rather than overloading one file with two
independent signal types.

`evaluate_gate` never fabricates a verdict from untrustworthy data: when
`underlying_market` is missing or its `rvol_status` isn't `"ok"`, the gate's
`status` pass-through tells the caller exactly that, with `vwap_agrees`/
`rvol_participation_ok` left `None` rather than guessed. It is the caller's
job (see `strategies/momentum_qqq.py`) to treat that the same way an absent
or stale market observation is already treated -- retain a held position,
don't act on nothing -- not the same as a genuine unsupported/neutral read.
"""

from __future__ import annotations

from dataclasses import dataclass

from .market import UnderlyingMarket


@dataclass(frozen=True)
class VwapRvolGate:
    """The result of `evaluate_gate`."""

    status: str  # "no_data" | "insufficient_history" | "ok"
    vwap_agrees: bool | None
    rvol_participation_ok: bool | None
    vwap: float | None
    price_vs_vwap_pct: float | None
    rvol_multiple: float | None
    freshness: str


def evaluate_gate(
    underlying_market: UnderlyingMarket | None,
    direction: str,
    *,
    rvol_floor: float | None,
    require_vwap_agreement: bool,
) -> VwapRvolGate:
    """`direction` is `"up"` or `"down"` -- the direction a momentum signal
    currently supports (see `momentum_qqq._decide_core`'s `direction` local).

    `rvol_floor=None` disables the RVOL check (always considered satisfied);
    `require_vwap_agreement=False` disables the VWAP check the same way. If
    both are disabled, callers shouldn't be calling this at all -- see
    `momentum_qqq.py`'s guard before invoking it.
    """
    if underlying_market is None:
        return VwapRvolGate(
            status="no_data", vwap_agrees=None, rvol_participation_ok=None,
            vwap=None, price_vs_vwap_pct=None, rvol_multiple=None, freshness="stale",
        )

    if underlying_market.rvol_status != "ok":
        return VwapRvolGate(
            status=underlying_market.rvol_status,
            vwap_agrees=None, rvol_participation_ok=None,
            vwap=underlying_market.vwap, price_vs_vwap_pct=underlying_market.price_vs_vwap_pct,
            rvol_multiple=None, freshness=underlying_market.freshness,
        )

    vwap_agrees: bool | None = None
    if require_vwap_agreement:
        if underlying_market.price_vs_vwap_pct is None:
            vwap_agrees = None
        elif direction == "up":
            vwap_agrees = underlying_market.price_vs_vwap_pct > 0
        else:
            vwap_agrees = underlying_market.price_vs_vwap_pct < 0

    rvol_participation_ok: bool | None = None
    if rvol_floor is not None:
        rvol_participation_ok = (
            underlying_market.rvol_multiple is not None
            and underlying_market.rvol_multiple >= rvol_floor
        )

    return VwapRvolGate(
        status="ok",
        vwap_agrees=vwap_agrees,
        rvol_participation_ok=rvol_participation_ok,
        vwap=underlying_market.vwap,
        price_vs_vwap_pct=underlying_market.price_vs_vwap_pct,
        rvol_multiple=underlying_market.rvol_multiple,
        freshness=underlying_market.freshness,
    )
