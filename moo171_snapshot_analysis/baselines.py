"""Baseline/control features for the toward/away regression per MOO-171 §3:
previous five-minute return, preceding 30-minute sampled volatility, and
time of day. All computed causally -- only snapshots at/before the anchor
are used.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30
PREV_RETURN_WINDOW_MINUTES = 5
VOL_WINDOW_MINUTES = 30
STALENESS_SECONDS = 90
MIN_VOL_OBSERVATIONS = 5


def _nearest_at_or_before(day_spots: pd.DataFrame, target_ts: pd.Timestamp) -> pd.Series | None:
    candidates = day_spots[day_spots["ts_et"] <= target_ts]
    if candidates.empty:
        return None
    row = candidates.loc[candidates["ts_et"].idxmax()]
    if (target_ts - row["ts_et"]).total_seconds() > STALENESS_SECONDS:
        return None
    return row


def previous_return(spot_series: pd.DataFrame, date: str, anchor_ts: pd.Timestamp, anchor_spot: float) -> float:
    """(anchor_spot - spot_5min_before) / spot_5min_before, or NaN if no
    snapshot close enough to the lookback point exists."""
    day_spots = spot_series[(spot_series["date"] == date) & spot_series["regular_hours"]]
    day_spots = day_spots.dropna(subset=["underlying_price"])
    target = anchor_ts - pd.Timedelta(minutes=PREV_RETURN_WINDOW_MINUTES)
    row = _nearest_at_or_before(day_spots, target)
    if row is None:
        return np.nan
    prior_spot = float(row["underlying_price"])
    if prior_spot == 0:
        return np.nan
    return (anchor_spot - prior_spot) / prior_spot


def preceding_volatility(spot_series: pd.DataFrame, date: str, anchor_ts: pd.Timestamp) -> dict:
    """Sample stdev of consecutive log returns over a REQUIRED, actually
    complete VOL_WINDOW_MINUTES before the anchor.

    "Complete" is enforced, not just "however many observations happen to
    fall in the window": the earliest observation used must itself be
    within STALENESS_SECONDS of the window's start, so the computed value
    genuinely covers close to the full 30 minutes rather than a shorter
    span that happens to contain >= MIN_VOL_OBSERVATIONS points. Returns a
    dict with the value AND its diagnostics (`n_obs`, `span_seconds`) so a
    report can state the real coverage instead of assuming "30-minute"
    means what the docstring says without checking.
    """
    day_spots = spot_series[(spot_series["date"] == date) & spot_series["regular_hours"]]
    day_spots = day_spots.dropna(subset=["underlying_price"])
    window_start = anchor_ts - pd.Timedelta(minutes=VOL_WINDOW_MINUTES)
    window = day_spots[(day_spots["ts_et"] >= window_start) & (day_spots["ts_et"] <= anchor_ts)]
    window = window.sort_values("ts_et")

    empty = {"vol_30min": np.nan, "vol_30min_n_obs": len(window), "vol_30min_span_seconds": np.nan}
    if len(window) < MIN_VOL_OBSERVATIONS:
        return empty

    earliest = window["ts_et"].iloc[0]
    if (earliest - window_start).total_seconds() > STALENESS_SECONDS:
        # The window isn't actually complete back to ~30 minutes before the
        # anchor (e.g. near session start) -- report as unavailable rather
        # than silently computing over whatever shorter span exists.
        return empty

    prices = window["underlying_price"].astype(float).to_numpy()
    span_seconds = (window["ts_et"].iloc[-1] - window["ts_et"].iloc[0]).total_seconds()
    if (prices <= 0).any():
        return {"vol_30min": np.nan, "vol_30min_n_obs": len(window), "vol_30min_span_seconds": span_seconds}
    log_returns = np.diff(np.log(prices))
    if len(log_returns) < MIN_VOL_OBSERVATIONS - 1:
        return {"vol_30min": np.nan, "vol_30min_n_obs": len(window), "vol_30min_span_seconds": span_seconds}
    return {
        "vol_30min": float(np.std(log_returns, ddof=1)),
        "vol_30min_n_obs": len(window),
        "vol_30min_span_seconds": span_seconds,
    }


def minutes_since_open(anchor_ts: pd.Timestamp) -> float:
    open_ts = anchor_ts.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
    return (anchor_ts - open_ts).total_seconds() / 60.0


def add_baseline_features(outcome_dataset: pd.DataFrame, spot_series: pd.DataFrame) -> pd.DataFrame:
    df = outcome_dataset.copy()
    # One computation per unique (date, anchor_ts) pair -- not per outcome
    # row -- since all 6 strikes at an anchor share the same baseline.
    unique_anchors = df[["date", "anchor_ts_et", "anchor_spot"]].drop_duplicates()
    unique_anchors = unique_anchors.set_index(["date", "anchor_ts_et"])

    prev_ret = {}
    vol_info = {}
    tod = {}
    for (date, ts), row in unique_anchors.iterrows():
        prev_ret[(date, ts)] = previous_return(spot_series, date, ts, float(row["anchor_spot"]))
        vol_info[(date, ts)] = preceding_volatility(spot_series, date, ts)
        tod[(date, ts)] = minutes_since_open(ts)

    df["prev_5min_return"] = df.apply(lambda r: prev_ret[(r["date"], r["anchor_ts_et"])], axis=1)
    df["vol_30min"] = df.apply(lambda r: vol_info[(r["date"], r["anchor_ts_et"])]["vol_30min"], axis=1)
    df["vol_30min_n_obs"] = df.apply(lambda r: vol_info[(r["date"], r["anchor_ts_et"])]["vol_30min_n_obs"], axis=1)
    df["vol_30min_span_seconds"] = df.apply(
        lambda r: vol_info[(r["date"], r["anchor_ts_et"])]["vol_30min_span_seconds"], axis=1
    )
    df["minutes_since_open"] = df.apply(lambda r: tod[(r["date"], r["anchor_ts_et"])], axis=1)
    return df
