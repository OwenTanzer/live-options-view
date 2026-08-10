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
    rel_spread: np.ndarray  # (ask-bid)/mid
    oi_share: np.ndarray  # this row's OI / total chain OI that day (shape, not level)
    volume_share: np.ndarray
    underlying_price: float
    total_oi: float
    total_volume: float


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

    return ChainDonor(
        day=day,
        moneyness=moneyness[valid],
        is_call=is_call[valid],
        rel_mid=rel_mid[valid],
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


def intraday_shape_curve(intraday_rows_by_time: list[tuple[float, dict[str, float]]]) -> dict[str, np.ndarray]:
    """Reduce several real intraday days into one canonical time-of-day ramp.

    `intraday_rows_by_time`: list of (minutes_since_open, {"oi_ratio":...,
    "spread_ratio":..., "volume_ratio":...}) samples pooled across every
    available real intraday day (each day's own ratios are computed against
    *that day's own* EOD values before pooling, so days with different
    absolute OI/volume levels still contribute comparably-shaped samples).

    Returns a monotonic lookup table (minutes-since-open -> ratio) built by
    binning into 15-minute buckets and taking the median, then
    forward/back-filling gaps -- collector coverage is sparse enough that
    some buckets may have zero samples.
    """
    buckets: dict[int, dict[str, list[float]]] = {}
    for minutes, ratios in intraday_rows_by_time:
        b = int(minutes // 15)
        slot = buckets.setdefault(b, {"oi_ratio": [], "spread_ratio": [], "volume_ratio": []})
        for k, v in ratios.items():
            if v == v:  # not NaN
                slot[k].append(v)

    n_buckets = (390 // 15) + 1
    curve = {"oi_ratio": np.full(n_buckets, np.nan), "spread_ratio": np.full(n_buckets, np.nan), "volume_ratio": np.full(n_buckets, np.nan)}
    for b, slot in buckets.items():
        if 0 <= b < n_buckets:
            for k, vals in slot.items():
                if vals:
                    curve[k][b] = float(np.median(vals))

    # Forward/back fill, then fall back to a flat "already fully in place"
    # default (1.0) if no real intraday data exists for that ratio at all --
    # this is the honest degrade-to-neutral path when collector coverage is
    # too short to say anything about time-of-day shape yet.
    for k in curve:
        arr = curve[k]
        last = 1.0
        for i in range(len(arr)):
            if arr[i] == arr[i]:
                last = arr[i]
            else:
                arr[i] = last
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

    rel_mid = _lookup_nearest(donor.moneyness, donor.is_call, donor.rel_mid, target_moneyness, all_is_call)
    rel_mid = np.clip(np.nan_to_num(rel_mid, nan=np.nanmedian(donor.rel_mid)), 1e-4, None)
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
    else:
        oi_ratio = spread_ratio = vol_ratio = 1.0

    mid = np.maximum(rel_mid * spot, 0.01)
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
