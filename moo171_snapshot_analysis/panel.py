"""Causal panel construction and coverage/exclusion audit for MOO-171.

Ingests raw `intraday/{date}/snapshot_*.csv` rows into two tidy tables --
one option-contract row per (date, snapshot, OptionSymbol), and one
deduplicated spot-price row per (date, snapshot) -- then recomputes interval
volume from consecutive cumulative Volume observations per the issue's
explicit rules (never trust the saved VolDelta; flag rather than guess on
resets, first observations, re-entry, and long gaps).

Nothing here looks at *future* rows relative to any given row -- this module
only orders and diffs each contract's own history forward in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from r2_source import SnapshotSource

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

REGULAR_OPEN = (9, 30)
REGULAR_CLOSE = (16, 0)

# Owen's reported per-session snapshot counts (from collector logs), used
# only to reconcile against -- never as a substitute for the real listing.
REPORTED_SNAPSHOT_COUNTS = {
    "20260910": 595,
    "20260911": 598,
    "20260914": 598,
    "20260915": 598,
    "20260916": 597,
}

OPTION_NUMERIC_FIELDS = [
    "Strike", "DTE", "OpenInterest", "Volume", "VolDelta",
    "Bid", "Mid", "Ask", "Last", "IV", "Delta", "Gamma", "Theta", "Vega",
    "UnderlyingPrice",
]


def parse_snapshot_key(key: str) -> tuple[str, pd.Timestamp, pd.Timestamp]:
    """('20260910', ts_et, ts_utc) from 'intraday/20260910/snapshot_060002893036.csv'.

    The filename time is America/New_York local time-of-day at microsecond
    precision (collector.py: `ts_et.strftime("%H%M%S%f")`), and the
    directory date is that same ET trading date -- never UTC.
    """
    parts = key.split("/")
    yyyymmdd = parts[1]
    fname = parts[2]
    hhmmssffffff = fname.removeprefix("snapshot_").removesuffix(".csv")
    hh, mm, ss, micro = (
        int(hhmmssffffff[0:2]), int(hhmmssffffff[2:4]),
        int(hhmmssffffff[4:6]), int(hhmmssffffff[6:12].ljust(6, "0")),
    )
    y, mo, d = int(yyyymmdd[0:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])
    ts_et = pd.Timestamp(y, mo, d, hh, mm, ss, micro, tz=ET)
    return yyyymmdd, ts_et, ts_et.tz_convert(UTC)


def is_regular_hours(ts_et: pd.Timestamp) -> bool:
    open_t = ts_et.replace(hour=REGULAR_OPEN[0], minute=REGULAR_OPEN[1], second=0, microsecond=0)
    close_t = ts_et.replace(hour=REGULAR_CLOSE[0], minute=REGULAR_CLOSE[1], second=0, microsecond=0)
    return open_t <= ts_et < close_t


@dataclass
class SessionAudit:
    date: str
    listed_objects: int
    excluded_non_snapshot: list[str]
    snapshot_count: int
    reported_count: int | None
    first_ts_et: pd.Timestamp | None
    last_ts_et: pd.Timestamp | None
    premarket_snapshots: int
    regular_hours_snapshots: int
    afterhours_snapshots: int
    gap_seconds: list[float]  # consecutive regular-hours snapshot gaps
    max_gap_seconds: float | None
    distinct_strikes: int
    distinct_contracts: int
    rows_total: int
    field_coverage: dict[str, float]  # field -> fraction non-null & non-empty
    nonfinite_greeks: int
    negative_volumes: int
    negative_open_interest: int
    crossed_quotes: int

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["first_ts_et"] = str(self.first_ts_et) if self.first_ts_et is not None else None
        d["last_ts_et"] = str(self.last_ts_et) if self.last_ts_et is not None else None
        d["gap_seconds"] = None  # summarized separately; raw list omitted from the report dict
        return d


def _coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in OPTION_NUMERIC_FIELDS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_raw_snapshots(
    source: SnapshotSource, dates: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, list[SessionAudit]]:
    """Returns (option_rows, spot_series, per-session audits).

    option_rows: one row per (date, snapshot, OptionSymbol), numeric fields
    coerced (empty-string 'Last' etc. become NaN, not silently 0).
    spot_series: one deduplicated row per (date, snapshot) -- UnderlyingPrice
    is repeated across all 134 option rows in a snapshot and must not be
    treated as 134 independent observations.
    """
    option_frames: list[pd.DataFrame] = []
    spot_frames: list[pd.DataFrame] = []
    audits: list[SessionAudit] = []

    for yyyymmdd in dates:
        all_objects = source.list_all_objects(f"intraday/{yyyymmdd}/")
        snap_objects = source.snapshot_objects(yyyymmdd)
        excluded = sorted(
            o["key"] for o in all_objects
            if o["key"] not in {s["key"] for s in snap_objects}
        )

        session_option_rows = []
        session_spot_rows = []
        snap_meta = []  # (ts_et, regular)

        for obj in snap_objects:
            _, ts_et, ts_utc = parse_snapshot_key(obj["key"])
            regular = is_regular_hours(ts_et)
            snap_meta.append((ts_et, regular))

            rows = source.snapshot_csv_rows(obj["key"])
            if not rows:
                continue

            for r in rows:
                r["date"] = yyyymmdd
                r["snapshot_key"] = obj["key"]
                r["ts_et"] = ts_et
                r["ts_utc"] = ts_utc
                r["regular_hours"] = regular
            session_option_rows.extend(rows)

            spot_val = rows[0].get("UnderlyingPrice")
            session_spot_rows.append({
                "date": yyyymmdd, "snapshot_key": obj["key"],
                "ts_et": ts_et, "ts_utc": ts_utc, "regular_hours": regular,
                "underlying_price": pd.to_numeric(spot_val, errors="coerce"),
            })

        opt_df = pd.DataFrame(session_option_rows)
        if not opt_df.empty:
            opt_df = _coerce_numeric(opt_df)
        option_frames.append(opt_df)
        spot_frames.append(pd.DataFrame(session_spot_rows))

        snap_meta.sort(key=lambda t: t[0])
        regular_ts = [ts for ts, reg in snap_meta if reg]
        gaps = [
            (b - a).total_seconds()
            for a, b in zip(regular_ts[:-1], regular_ts[1:])
        ]

        audits.append(_build_session_audit(
            yyyymmdd, all_objects, excluded, snap_meta, gaps, opt_df,
        ))

    option_rows = pd.concat(option_frames, ignore_index=True) if option_frames else pd.DataFrame()
    spot_series = pd.concat(spot_frames, ignore_index=True) if spot_frames else pd.DataFrame()
    return option_rows, spot_series, audits


def _build_session_audit(
    yyyymmdd: str, all_objects: list[dict], excluded: list[str],
    snap_meta: list[tuple[pd.Timestamp, bool]], gaps: list[float],
    opt_df: pd.DataFrame,
) -> SessionAudit:
    premarket = sum(1 for _, reg in snap_meta if not reg)
    regular = sum(1 for _, reg in snap_meta if reg)

    if opt_df.empty:
        return SessionAudit(
            date=yyyymmdd, listed_objects=len(all_objects), excluded_non_snapshot=excluded,
            snapshot_count=len(snap_meta), reported_count=REPORTED_SNAPSHOT_COUNTS.get(yyyymmdd),
            first_ts_et=snap_meta[0][0] if snap_meta else None,
            last_ts_et=snap_meta[-1][0] if snap_meta else None,
            premarket_snapshots=premarket, regular_hours_snapshots=regular,
            afterhours_snapshots=0, gap_seconds=gaps,
            max_gap_seconds=max(gaps) if gaps else None,
            distinct_strikes=0, distinct_contracts=0, rows_total=0,
            field_coverage={}, nonfinite_greeks=0, negative_volumes=0,
            negative_open_interest=0, crossed_quotes=0,
        )

    coverage = {}
    for col in OPTION_NUMERIC_FIELDS:
        coverage[col] = float(opt_df[col].notna().mean())

    greek_cols = ["Delta", "Gamma", "Theta", "Vega"]
    nonfinite = int((~np.isfinite(opt_df[greek_cols].astype(float))).any(axis=1).sum())
    neg_vol = int((opt_df["Volume"] < 0).sum())
    neg_oi = int((opt_df["OpenInterest"] < 0).sum())
    crossed = int((opt_df["Bid"] > opt_df["Ask"]).sum())

    return SessionAudit(
        date=yyyymmdd, listed_objects=len(all_objects), excluded_non_snapshot=excluded,
        snapshot_count=len(snap_meta), reported_count=REPORTED_SNAPSHOT_COUNTS.get(yyyymmdd),
        first_ts_et=snap_meta[0][0] if snap_meta else None,
        last_ts_et=snap_meta[-1][0] if snap_meta else None,
        premarket_snapshots=premarket, regular_hours_snapshots=regular,
        afterhours_snapshots=0, gap_seconds=gaps,
        max_gap_seconds=max(gaps) if gaps else None,
        distinct_strikes=int(opt_df["Strike"].nunique()),
        distinct_contracts=int(opt_df["OptionSymbol"].nunique()),
        rows_total=int(len(opt_df)),
        field_coverage=coverage, nonfinite_greeks=nonfinite,
        negative_volumes=neg_vol, negative_open_interest=neg_oi,
        crossed_quotes=crossed,
    )


MAX_INTERVAL_SECONDS = 90.0


def recompute_interval_volume(option_rows: pd.DataFrame) -> pd.DataFrame:
    """Adds dV (recomputed interval volume) and a `dv_flag` column.

    dv_flag is one of:
      "ok"               -- consecutive same-contract observation, volume
                             non-decreasing, gap <= MAX_INTERVAL_SECONDS
      "first_observation" -- no prior snapshot of this contract this session
      "re_entry"          -- contract was present earlier, absent from the
                              immediately preceding global snapshot, then
                              reappeared
      "reset_or_decrease"  -- Volume decreased vs the prior observation
      "long_gap"           -- gap to the prior observation exceeds
                              MAX_INTERVAL_SECONDS

    dV is NaN for every flag other than "ok" -- the issue requires excluding
    these from primary interval-activity calculations, not estimating
    through them.
    """
    if option_rows.empty:
        return option_rows.assign(dV=pd.Series(dtype=float), dv_flag=pd.Series(dtype=object))

    df = option_rows.sort_values(["date", "OptionSymbol", "ts_et"]).reset_index(drop=True)

    # Global per-date snapshot ordering, to detect re-entry (a contract
    # missing from the immediately preceding *global* snapshot even though
    # other contracts had one).
    snapshot_order = {
        date: sorted(g["ts_et"].unique())
        for date, g in df.groupby("date")
    }

    dV = np.full(len(df), np.nan)
    flags = np.empty(len(df), dtype=object)

    for (date, symbol), g in df.groupby(["date", "OptionSymbol"]):
        idx = g.index.to_numpy()
        ts = g["ts_et"].to_numpy()
        vol = g["Volume"].to_numpy()
        order = snapshot_order[date]
        order_pos = {t: i for i, t in enumerate(order)}

        for i in range(len(idx)):
            row_idx = idx[i]
            if i == 0:
                flags[row_idx] = "first_observation"
                continue

            gap = (pd.Timestamp(ts[i]) - pd.Timestamp(ts[i - 1])).total_seconds()
            prev_global_pos = order_pos[ts[i - 1]]
            this_global_pos = order_pos[ts[i]]
            contiguous = (this_global_pos - prev_global_pos) == 1

            if not contiguous:
                flags[row_idx] = "re_entry"
                continue
            if gap > MAX_INTERVAL_SECONDS:
                flags[row_idx] = "long_gap"
                continue
            if np.isnan(vol[i]) or np.isnan(vol[i - 1]):
                flags[row_idx] = "long_gap"  # missing volume -- treat as unusable, not a silent zero
                continue
            if vol[i] < vol[i - 1]:
                flags[row_idx] = "reset_or_decrease"
                continue

            dV[row_idx] = vol[i] - vol[i - 1]
            flags[row_idx] = "ok"

    df["dV"] = dV
    df["dv_flag"] = flags
    return df
