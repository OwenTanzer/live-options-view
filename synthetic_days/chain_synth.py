"""Synthesize a 0DTE options chain snapshot at a given (time, spot) pair.

Two real calibration sources, each covering what the other can't:

  * `history/{SYMBOL}/options_0dte/{DATE}.json` (many days, EOD only) gives
    the **cross-sectional shape**: relative premium and bid/ask spread by
    moneyness, and how open interest is distributed across strikes --
    separately for calls and puts, since 0DTE skew is asymmetric. "Many
    days" matters here: one day's shape is an anecdote, dozens let us sample
    a real distribution of shapes rather than hard-coding one.

  * `intraday/{YYYYMMDD}/snapshot_*.csv` (few days, ~60s cadence) gives the
    **intraday evolution shape**: what fraction of EOD OI/volume/spread-width
    is already in place at each point in the session. Collector coverage is
    short so far -- this is the piece most starved for data, and
    `day_generator.py` says so rather than pretending otherwise.

Originally this repriced every strike with Black-Scholes off a donor-implied
IV, to keep price/greeks internally consistent when recentering strikes onto
a different day's spot. Dropped after checking the real data against a live
R2 pull: MarketData.app's historical 0DTE endpoint never populates `iv` or
any greek (confirmed null across every sampled day), and inverting IV from
an EOD 0DTE mid price is close to ill-posed anyway -- extrinsic value goes to
zero as time-to-expiry does, so a near-close price barely constrains vol.
Checked every strategy in `crassus/crassus/strategies/` for IV/greek reads:
none of them use IV, Delta, Gamma, Theta, or Vega -- only Strike, Type,
OpenInterest, Bid, Ask. So this resamples real relative premiums
(mid-as-fraction-of-spot) directly by moneyness instead, and leaves
IV/greeks as `None` in synthetic rows rather than manufacturing numbers nothing
downstream reads and that could be quietly wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np


@dataclass
class ChainDonor:
    """One EOD day's chain, reduced to lookup arrays keyed by signed moneyness."""

    day: date
    moneyness: np.ndarray  # (strike/spot - 1), signed
    is_call: np.ndarray
    rel_mid: np.ndarray  # mid / spot -- rescales onto a different day's spot
    extrinsic_rel: np.ndarray  # (mid - intrinsic) / spot -- the time-value part, see synth_snapshot_rows
    rel_spread: np.ndarray  # (ask-bid)/mid
    oi_share: np.ndarray  # this row's OI / total chain OI that day (shape, not level)
    volume_share: np.ndarray
    underlying_price: float
    total_oi: float
    total_volume: float


def _intrinsic_rel(moneyness: np.ndarray, is_call: np.ndarray) -> np.ndarray:
    """Intrinsic value / spot, as an exact function of signed moneyness
    (strike/spot - 1) -- (spot-strike)/spot = -moneyness for a call,
    (strike-spot)/spot = moneyness for a put. No approximation: this is a
    scale-invariant identity, so it holds for the donor's own day and for
    whatever spot the synthetic day recenters onto.
    """
    return np.where(is_call, np.maximum(0.0, -moneyness), np.maximum(0.0, moneyness))


def _relative_moneyness(moneyness: np.ndarray, is_call: np.ndarray) -> np.ndarray:
    """Signed moneyness, reoriented so positive always means OTM and
    negative always means ITM regardless of side -- the dimension that
    actually governs extrinsic-value decay behavior, so calls and puts
    share one bucket axis instead of two mirrored ones."""
    return np.where(is_call, -moneyness, moneyness)


# Bucket edges for `_relative_moneyness`, giving 5 buckets: deep ITM,
# near ITM, ATM, near OTM, deep OTM. Coarse deliberately -- real intraday
# collector coverage (see day_generator.py) is a handful of days at best,
# so a finer grid would mostly be empty cells falling back to the ATM
# column anyway (see `intraday_shape_curve`).
MONEYNESS_BUCKET_EDGES = (-0.02, -0.005, 0.005, 0.02)
N_MONEYNESS_BUCKETS = len(MONEYNESS_BUCKET_EDGES) + 1
ATM_BUCKET_INDEX = int(np.searchsorted(MONEYNESS_BUCKET_EDGES, 0.0))


def build_donor(chain_cols: dict, day: date) -> ChainDonor | None:
    """`chain_cols` is the column-oriented payload from `options_0dte_day`."""
    n = len(chain_cols.get("strike", []))
    if n == 0:
        return None
    strike = np.array(chain_cols["strike"], dtype=float)
    side = np.array(chain_cols["side"])
    bid = np.array([x if x is not None else np.nan for x in chain_cols.get("bid", [None] * n)], dtype=float)
    ask = np.array([x if x is not None else np.nan for x in chain_cols.get("ask", [None] * n)], dtype=float)
    oi = np.array([x if x is not None else 0.0 for x in chain_cols.get("openInterest", [0.0] * n)], dtype=float)
    vol = np.array([x if x is not None else 0.0 for x in chain_cols.get("volume", [0.0] * n)], dtype=float)
    underlying = chain_cols.get("underlyingPrice", [None] * n)
    spot = next((u for u in underlying if u), None)
    if not spot:
        return None

    mid = np.where(np.isnan(bid) | np.isnan(ask), np.nan, (bid + ask) / 2.0)
    rel_mid = mid / spot
    rel_spread = np.where((mid > 0) & ~np.isnan(mid), (ask - bid) / mid, np.nan)
    moneyness = strike / spot - 1.0
    is_call = np.array([s == "call" for s in side])

    total_oi = float(np.nansum(oi)) or 1.0
    total_vol = float(np.nansum(vol)) or 1.0

    valid = ~np.isnan(rel_mid) & (rel_mid > 0) & ~np.isnan(rel_spread)
    if valid.sum() < 5:
        return None

    extrinsic_rel = np.maximum(rel_mid - _intrinsic_rel(moneyness, is_call), 0.0)

    return ChainDonor(
        day=day,
        moneyness=moneyness[valid],
        is_call=is_call[valid],
        rel_mid=rel_mid[valid],
        extrinsic_rel=extrinsic_rel[valid],
        rel_spread=rel_spread[valid],
        oi_share=(oi[valid] / total_oi),
        volume_share=(vol[valid] / total_vol),
        underlying_price=float(spot),
        total_oi=total_oi,
        total_volume=total_vol,
    )


def _lookup_nearest(donor_moneyness: np.ndarray, donor_is_call: np.ndarray, donor_values: np.ndarray, target_moneyness: np.ndarray, target_is_call: np.ndarray) -> np.ndarray:
    """Nearest-moneyness lookup within the matching option side."""
    out = np.empty(len(target_moneyness))
    for side_flag in (True, False):
        side_mask_donor = donor_is_call == side_flag
        side_mask_target = target_is_call == side_flag
        if not side_mask_donor.any() or not side_mask_target.any():
            out[side_mask_target] = np.nan
            continue
        dm = donor_moneyness[side_mask_donor]
        dv = donor_values[side_mask_donor]
        order = np.argsort(dm)
        dm, dv = dm[order], dv[order]
        idx = np.searchsorted(dm, target_moneyness[side_mask_target])
        idx = np.clip(idx, 0, len(dm) - 1)
        idx_lo = np.clip(idx - 1, 0, len(dm) - 1)
        pick_lo = np.abs(dm[idx_lo] - target_moneyness[side_mask_target]) < np.abs(dm[idx] - target_moneyness[side_mask_target])
        chosen = np.where(pick_lo, idx_lo, idx)
        out[side_mask_target] = dv[chosen]
    return out


def _backward_then_forward_fill(arr: np.ndarray, default: float | None = 1.0) -> None:
    """In place. Backward-fill first (a leading gap inherits the nearest
    *real future* value), then forward-fill (a trailing gap inherits the
    nearest real past value). An earlier version forward-filled only,
    seeded from a hard-coded 1.0 -- so any leading gap got stuck at 1.0
    regardless of what the real data showed a bit later, instead of
    inheriting the nearest real value. Flagged in review: for a decay curve
    like premium_decay_ratio, the leading buckets are exactly the ones
    covering the most active part of the session, so this could materially
    underprice opening options. `default` seeds the *remaining* gaps only if
    the array has no real values at all; pass `None` to leave it all-NaN in
    that case instead (used for the moneyness grid, where the caller has a
    better fallback than a flat constant -- see `intraday_shape_curve`).
    """
    last = None
    for i in range(len(arr) - 1, -1, -1):
        if arr[i] == arr[i]:
            last = arr[i]
        elif last is not None:
            arr[i] = last
    last = default
    for i in range(len(arr)):
        if arr[i] == arr[i]:
            last = arr[i]
        elif last is not None:
            arr[i] = last


def intraday_shape_curve(
    intraday_rows_by_time: list[tuple[float, dict[str, float]]],
    premium_decay_samples: list[tuple[float, float, bool, float]] | None = None,
) -> dict[str, Any]:
    """Reduce several real intraday days into one canonical time-of-day ramp.

    `intraday_rows_by_time`: list of (minutes_since_open, {"oi_ratio":...,
    "spread_ratio":..., "volume_ratio":...}) samples pooled across every
    available real intraday day (each day's own ratios are computed against
    *that day's own* EOD values before pooling, so days with different
    absolute OI/volume levels still contribute comparably-shaped samples).
    Binned into 15-minute buckets, median per bucket, gaps filled per
    `_backward_then_forward_fill`.

    `premium_decay_samples`: list of (minutes_since_open, moneyness, is_call,
    ratio) -- one entry *per real intraday chain row*, pooled the same way,
    each row's ratio computed against that same row's own symbol's EOD
    extrinsic value. An earlier version instead computed one pooled
    ATM-straddle ratio and applied it to every strike and side uniformly --
    flagged in review: OTM and ITM options don't decay identically to ATM
    ones, so `synth_snapshot_rows` needs a moneyness-conditional lookup, not
    one scalar. Binned into (15-minute bucket x moneyness bucket, see
    `MONEYNESS_BUCKET_EDGES`) and returned as `"premium_decay_grid"`, a 2D
    array. A moneyness bucket with no samples anywhere falls back to the ATM
    bucket's own (filled) curve as the best available proxy for decay
    *shape* at other moneyness levels, before falling back further to a
    flat 1.0 if even the ATM bucket has nothing.
    """
    keys = ("oi_ratio", "spread_ratio", "volume_ratio")
    buckets: dict[int, dict[str, list[float]]] = {}
    for minutes, ratios in intraday_rows_by_time:
        b = int(minutes // 15)
        slot = buckets.setdefault(b, {k: [] for k in keys})
        for k, v in ratios.items():
            if v == v:  # not NaN
                slot[k].append(v)

    n_buckets = (390 // 15) + 1
    curve: dict[str, Any] = {k: np.full(n_buckets, np.nan) for k in keys}
    for b, slot in buckets.items():
        if 0 <= b < n_buckets:
            for k, vals in slot.items():
                if vals:
                    curve[k][b] = float(np.median(vals))
    for k in curve:
        _backward_then_forward_fill(curve[k], default=1.0)

    grid = np.full((n_buckets, N_MONEYNESS_BUCKETS), np.nan)
    grid_samples: dict[tuple[int, int], list[float]] = {}
    for minutes, moneyness, is_call, ratio in (premium_decay_samples or []):
        if ratio != ratio:
            continue
        b = int(minutes // 15)
        if not (0 <= b < n_buckets):
            continue
        rel_m = -moneyness if is_call else moneyness
        m_bucket = int(np.searchsorted(MONEYNESS_BUCKET_EDGES, rel_m))
        grid_samples.setdefault((b, m_bucket), []).append(ratio)
    for (b, m_bucket), vals in grid_samples.items():
        grid[b, m_bucket] = float(np.median(vals))

    for m_bucket in range(N_MONEYNESS_BUCKETS):
        _backward_then_forward_fill(grid[:, m_bucket], default=None)

    atm_column = grid[:, ATM_BUCKET_INDEX].copy()
    if np.all(np.isnan(atm_column)):
        atm_column = np.ones(n_buckets)
    for m_bucket in range(N_MONEYNESS_BUCKETS):
        if np.all(np.isnan(grid[:, m_bucket])):
            grid[:, m_bucket] = atm_column

    curve["premium_decay_grid"] = grid
    return curve


def synth_snapshot_rows(
    now_et: datetime,
    spot: float,
    donor: ChainDonor,
    strike_window: int,
    intraday_curve: dict[str, np.ndarray] | None,
    target_total_oi: float,
    target_total_volume: float,
    expiration: date,
) -> list[dict[str, Any]]:
    """One synthetic snapshot's `rows`, in the exact schema
    `MarketSnapshot`/the collector's CSV expect (Strike, Type, OptionSymbol,
    OpenInterest, Bid, Ask, Mid, ...). IV/Delta/Gamma/Theta/Vega are `None`
    -- see this module's docstring for why they aren't synthesized.
    """
    center = round(spot)
    strikes = np.arange(center - strike_window, center + strike_window + 1, dtype=float)
    n = len(strikes)
    all_strikes = np.tile(strikes, 2)
    all_is_call = np.array([True] * n + [False] * n)
    target_moneyness = all_strikes / spot - 1.0

    extrinsic_rel = _lookup_nearest(donor.moneyness, donor.is_call, donor.extrinsic_rel, target_moneyness, all_is_call)
    extrinsic_rel = np.clip(np.nan_to_num(extrinsic_rel, nan=np.nanmedian(donor.extrinsic_rel)), 0.0, None)
    rel_spread = _lookup_nearest(donor.moneyness, donor.is_call, donor.rel_spread, target_moneyness, all_is_call)
    rel_spread = np.clip(np.nan_to_num(rel_spread, nan=np.nanmedian(donor.rel_spread)), 0.001, 1.0)
    oi_share = _lookup_nearest(donor.moneyness, donor.is_call, donor.oi_share, target_moneyness, all_is_call)
    oi_share = np.nan_to_num(oi_share, nan=0.0)
    vol_share = _lookup_nearest(donor.moneyness, donor.is_call, donor.volume_share, target_moneyness, all_is_call)
    vol_share = np.nan_to_num(vol_share, nan=0.0)

    minutes_since_open = max(0.0, (now_et.hour - 9) * 60 + (now_et.minute - 30) + now_et.second / 60.0)
    if intraday_curve is not None:
        b = min(int(minutes_since_open // 15), len(intraday_curve["oi_ratio"]) - 1)
        oi_ratio, spread_ratio, vol_ratio = intraday_curve["oi_ratio"][b], intraday_curve["spread_ratio"][b], intraday_curve["volume_ratio"][b]
        # Per-row, not one scalar for the whole snapshot: OTM/ITM options
        # don't decay identically to ATM ones (see chain_synth.intraday_shape_curve).
        grid = intraday_curve["premium_decay_grid"]
        gb = min(b, grid.shape[0] - 1)
        rel_moneyness = _relative_moneyness(target_moneyness, all_is_call)
        m_bucket = np.clip(np.searchsorted(MONEYNESS_BUCKET_EDGES, rel_moneyness), 0, N_MONEYNESS_BUCKETS - 1)
        premium_decay_ratio = grid[gb, m_bucket]
    else:
        oi_ratio = spread_ratio = vol_ratio = 1.0
        premium_decay_ratio = 1.0

    # Price = intrinsic (exact function of moneyness, no donor needed) +
    # extrinsic (the donor's EOD time-value shape by moneyness, scaled up by
    # the real intraday decay curve -- extrinsic value is largest in the
    # morning and bleeds off to the donor's EOD level by the close, not flat
    # all day).
    intrinsic_rel = _intrinsic_rel(target_moneyness, all_is_call)
    mid = np.maximum(intrinsic_rel * spot + extrinsic_rel * spot * premium_decay_ratio, 0.01)
    half_spread = mid * rel_spread * spread_ratio / 2.0
    bid = np.maximum(mid - half_spread, 0.0)
    ask = mid + half_spread

    oi = np.round(oi_share * target_total_oi * oi_ratio)
    volume = np.round(vol_share * target_total_volume * vol_ratio)

    rows = []
    exp_str = expiration.isoformat()
    for i in range(len(all_strikes)):
        strike = float(all_strikes[i])
        is_call = bool(all_is_call[i])
        occ_type = "C" if is_call else "P"
        occ_strike = f"{round(strike * 1000):08d}"
        symbol = f"QQQ{expiration.strftime('%y%m%d')}{occ_type}{occ_strike}"
        rows.append({
            "TradeDate": now_et.date().isoformat(),
            "Expiration": exp_str,
            "Strike": strike,
            "Type": "call" if is_call else "put",
            "OptionSymbol": symbol,
            "DTE": 0,
            "OpenInterest": float(oi[i]),
            "Volume": float(volume[i]),
            "VolDelta": 0.0,  # synthetic snapshots are independent draws, not a running series
            "Bid": round(float(bid[i]), 2),
            "Mid": round(float(mid[i]), 4),
            "Ask": round(float(ask[i]), 2),
            "Last": round(float(mid[i]), 2),
            "IV": None,
            "Delta": None,
            "Gamma": None,
            "Theta": None,
            "Vega": None,
            "UnderlyingPrice": spot,
        })
    return rows
