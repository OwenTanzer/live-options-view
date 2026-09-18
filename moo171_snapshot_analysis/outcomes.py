"""Anchor selection and the toward/away outcome per MOO-171 §3.

At non-overlapping five-minute anchors within regular trading hours, select
the three nearest observed strikes strictly above and three strictly below
current spot using only information available at the anchor snapshot. The
outcome window is fixed once selected -- strikes are never recentered after
observing the move.

    toward[k,t] = (abs(S[t] - k) - abs(S[t+h] - k)) / S[t]

Positive means closer to k at the outcome snapshot; negative means farther.
This does not observe intraminute touches/crossings and does not establish
sustained pinning -- it is one endpoint-to-endpoint distance comparison.
"""

from __future__ import annotations

import pandas as pd

ANCHOR_INTERVAL_MINUTES = 5
OUTCOME_HORIZON_MINUTES = 5
MAX_STALENESS_SECONDS = 90
STRIKES_PER_SIDE = 3


def _nearest_valid_snapshot(
    spot_series: pd.DataFrame, date: str, target_ts: pd.Timestamp, direction: str,
) -> pd.Series | None:
    """The latest regular-hours snapshot at/before target_ts (direction="before")
    or the first regular-hours snapshot at/after it (direction="after"), if
    within MAX_STALENESS_SECONDS of target_ts. None if no such snapshot exists."""
    day = spot_series[(spot_series["date"] == date) & spot_series["regular_hours"]]
    day = day.dropna(subset=["underlying_price"])
    if day.empty:
        return None

    if direction == "before":
        candidates = day[day["ts_et"] <= target_ts]
        if candidates.empty:
            return None
        row = candidates.loc[candidates["ts_et"].idxmax()]
        staleness = (target_ts - row["ts_et"]).total_seconds()
    else:
        candidates = day[day["ts_et"] >= target_ts]
        if candidates.empty:
            return None
        row = candidates.loc[candidates["ts_et"].idxmin()]
        staleness = (row["ts_et"] - target_ts).total_seconds()

    if staleness > MAX_STALENESS_SECONDS:
        return None
    return row


def generate_anchors(spot_series: pd.DataFrame) -> pd.DataFrame:
    """Non-overlapping 5-minute anchor points per date, each resolved to the
    actual snapshot used (may be None if no snapshot is close enough to a
    given bin boundary -- those bins are simply absent from the result,
    per the issue's 'record fewer available candidates' instruction)."""
    rows = []
    for date, group in spot_series.groupby("date"):
        regular = group[group["regular_hours"]]
        if regular.empty:
            continue
        session_start = regular["ts_et"].min().floor("min")
        session_end = regular["ts_et"].max()
        bin_starts = pd.date_range(
            session_start, session_end, freq=f"{ANCHOR_INTERVAL_MINUTES}min", tz=session_start.tz,
        )
        for bin_start in bin_starts:
            anchor_row = _nearest_valid_snapshot(spot_series, date, bin_start, "before")
            if anchor_row is None:
                continue
            outcome_target = anchor_row["ts_et"] + pd.Timedelta(minutes=OUTCOME_HORIZON_MINUTES)
            outcome_row = _nearest_valid_snapshot(spot_series, date, outcome_target, "after")
            if outcome_row is None:
                continue
            rows.append({
                "date": date,
                "anchor_bin": bin_start,
                "anchor_snapshot_key": anchor_row["snapshot_key"],
                "anchor_ts_et": anchor_row["ts_et"],
                "anchor_spot": anchor_row["underlying_price"],
                "outcome_snapshot_key": outcome_row["snapshot_key"],
                "outcome_ts_et": outcome_row["ts_et"],
                "outcome_spot": outcome_row["underlying_price"],
                "realized_horizon_seconds": (outcome_row["ts_et"] - anchor_row["ts_et"]).total_seconds(),
            })
    return pd.DataFrame(rows)


def select_strikes_for_anchor(measures_at_snapshot: pd.DataFrame, spot: float) -> pd.DataFrame:
    """The 3 nearest strikes strictly above and 3 strictly below `spot`,
    from rows already computed for this exact anchor snapshot. Fewer than 3
    on a side is returned as-is (not padded or substituted)."""
    valid = measures_at_snapshot.dropna(subset=["C", "A"])
    above = valid[valid["Strike"].astype(float) > spot].sort_values("Strike").head(STRIKES_PER_SIDE)
    below = valid[valid["Strike"].astype(float) < spot].sort_values("Strike", ascending=False).head(STRIKES_PER_SIDE)
    return pd.concat([above, below], ignore_index=True)


def toward(spot_t: float, spot_t_plus_h: float, strike: float) -> float:
    return (abs(spot_t - strike) - abs(spot_t_plus_h - strike)) / spot_t


def build_outcome_dataset(anchors: pd.DataFrame, measures: pd.DataFrame) -> pd.DataFrame:
    """One row per (anchor, selected strike): features at the anchor
    snapshot plus the realized toward/away outcome. Only anchors with a
    resolved outcome snapshot (see generate_anchors) are included."""
    rows = []
    for _, anchor in anchors.iterrows():
        snap_measures = measures[
            (measures["date"] == anchor["date"])
            & (measures["snapshot_key"] == anchor["anchor_snapshot_key"])
        ]
        selected = select_strikes_for_anchor(snap_measures, float(anchor["anchor_spot"]))
        for _, strike_row in selected.iterrows():
            k = float(strike_row["Strike"])
            rows.append({
                "date": anchor["date"],
                "anchor_bin": anchor["anchor_bin"],
                "anchor_ts_et": anchor["anchor_ts_et"],
                "strike": k,
                "side": strike_row["side"],
                "distance": strike_row["distance"],
                "abs_distance": abs(float(strike_row["distance"])),
                "C": strike_row["C"],
                "A": strike_row["A"],
                "gamma_alone": strike_row["total_gamma"],
                "unweighted_oi": strike_row["total_oi"],
                "unweighted_activity": strike_row["total_dv"],
                "anchor_spot": anchor["anchor_spot"],
                "outcome_spot": anchor["outcome_spot"],
                "realized_horizon_seconds": anchor["realized_horizon_seconds"],
                "toward": toward(float(anchor["anchor_spot"]), float(anchor["outcome_spot"]), k),
            })
    return pd.DataFrame(rows)
