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

CONTRACT_MULTIPLIER_EVIDENCE = (
    "QQQ 0DTE equity-ETF options carry the standard OCC 100-share-per-"
    "contract deliverable. This is an external fact about the contract, "
    "not something the archive's own fields encode; the archive can only "
    "provide evidence AGAINST the presence of an adjusted (non-standard-"
    "multiplier) contract, which is what run_audit.py's multiplier-"
    "evidence check does by confirming every retained OptionSymbol matches "
    "the plain unadjusted root+YYMMDD+C/P+strike*1000 pattern with zero "
    "symbol_mismatch_rows -- an adjusted contract is conventionally "
    "flagged with a differently-shaped symbol that would fail this check."
)

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


def session_phase(ts_et: pd.Timestamp) -> str:
    """One of "premarket" / "regular" / "afterhours" -- disjoint and
    exhaustive, unlike a single is_regular_hours() bool which conflates
    the other two phases into "not regular"."""
    open_t = ts_et.replace(hour=REGULAR_OPEN[0], minute=REGULAR_OPEN[1], second=0, microsecond=0)
    close_t = ts_et.replace(hour=REGULAR_CLOSE[0], minute=REGULAR_CLOSE[1], second=0, microsecond=0)
    if ts_et < open_t:
        return "premarket"
    if ts_et >= close_t:
        return "afterhours"
    return "regular"


def parse_option_symbol(symbol: str) -> tuple[str, str, str, float] | None:
    """('QQQ', '260910', 'C', 682.0) from 'QQQ260910C00682000', or None if
    the symbol doesn't match the expected OCC-style root+YYMMDD+C/P+strike*1000
    encoding. Used to cross-check the Strike/Type/Expiration columns against
    the symbol itself -- an independent identity check, not a trust in
    either source alone."""
    import re

    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
    if not m:
        return None
    root, yymmdd, type_char, strike_digits = m.groups()
    return root, yymmdd, type_char, int(strike_digits) / 1000.0


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
    duplicate_snapshot_symbol_rows: int  # (ts_et, OptionSymbol) keys appearing more than once
    symbol_mismatch_rows: int  # OptionSymbol-encoded strike/type/expiry disagrees with the row's own columns
    spot_inconsistent_snapshots: int  # snapshots where option rows don't all report the same finite UnderlyingPrice
    contracts_with_oi_change: int  # contracts whose OpenInterest changed at least once intra-session
    contracts_with_oi_baseline: int  # contracts with a usable first-eligible-session OI baseline
    max_repeated_mid_run: int  # longest run of consecutive identical Mid values for any single contract

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
        snap_meta = []  # (ts_et, regular, phase)

        for obj in snap_objects:
            _, ts_et, ts_utc = parse_snapshot_key(obj["key"])
            phase = session_phase(ts_et)
            regular = phase == "regular"
            snap_meta.append((ts_et, regular, phase))

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

            # Spot consistency: every option row in a snapshot repeats the
            # same UnderlyingPrice -- check they actually agree (finite,
            # positive, identical) rather than trusting row[0] alone.
            snapshot_spots = pd.to_numeric(pd.Series([r.get("UnderlyingPrice") for r in rows]), errors="coerce")
            finite_spots = snapshot_spots[np.isfinite(snapshot_spots) & (snapshot_spots > 0)]
            spot_consistent = len(finite_spots.unique()) == 1 and len(finite_spots) == len(snapshot_spots)
            spot_val = finite_spots.iloc[0] if len(finite_spots) else np.nan
            session_spot_rows.append({
                "date": yyyymmdd, "snapshot_key": obj["key"],
                "ts_et": ts_et, "ts_utc": ts_utc, "regular_hours": regular,
                "underlying_price": spot_val, "spot_consistent": spot_consistent,
            })

        opt_df = pd.DataFrame(session_option_rows)
        if not opt_df.empty:
            opt_df = _coerce_numeric(opt_df)
        option_frames.append(opt_df)
        spot_df = pd.DataFrame(session_spot_rows)
        spot_frames.append(spot_df)

        snap_meta.sort(key=lambda t: t[0])
        regular_ts = [ts for ts, reg, _ in snap_meta if reg]
        gaps = [
            (b - a).total_seconds()
            for a, b in zip(regular_ts[:-1], regular_ts[1:])
        ]

        audits.append(_build_session_audit(
            yyyymmdd, all_objects, excluded, snap_meta, gaps, opt_df, spot_df,
        ))

    option_rows = pd.concat(option_frames, ignore_index=True) if option_frames else pd.DataFrame()
    spot_series = pd.concat(spot_frames, ignore_index=True) if spot_frames else pd.DataFrame()
    return option_rows, spot_series, audits


def _build_session_audit(
    yyyymmdd: str, all_objects: list[dict], excluded: list[str],
    snap_meta: list[tuple[pd.Timestamp, bool, str]], gaps: list[float],
    opt_df: pd.DataFrame, spot_df: pd.DataFrame,
) -> SessionAudit:
    premarket = sum(1 for _, _, phase in snap_meta if phase == "premarket")
    regular = sum(1 for _, _, phase in snap_meta if phase == "regular")
    afterhours = sum(1 for _, _, phase in snap_meta if phase == "afterhours")
    spot_inconsistent = int((~spot_df["spot_consistent"]).sum()) if not spot_df.empty else 0

    if opt_df.empty:
        return SessionAudit(
            date=yyyymmdd, listed_objects=len(all_objects), excluded_non_snapshot=excluded,
            snapshot_count=len(snap_meta), reported_count=REPORTED_SNAPSHOT_COUNTS.get(yyyymmdd),
            first_ts_et=snap_meta[0][0] if snap_meta else None,
            last_ts_et=snap_meta[-1][0] if snap_meta else None,
            premarket_snapshots=premarket, regular_hours_snapshots=regular,
            afterhours_snapshots=afterhours, gap_seconds=gaps,
            max_gap_seconds=max(gaps) if gaps else None,
            distinct_strikes=0, distinct_contracts=0, rows_total=0,
            field_coverage={}, nonfinite_greeks=0, negative_volumes=0,
            negative_open_interest=0, crossed_quotes=0,
            duplicate_snapshot_symbol_rows=0, symbol_mismatch_rows=0,
            spot_inconsistent_snapshots=spot_inconsistent,
            contracts_with_oi_change=0, contracts_with_oi_baseline=0,
            max_repeated_mid_run=0,
        )

    coverage = {}
    for col in OPTION_NUMERIC_FIELDS:
        coverage[col] = float(opt_df[col].notna().mean())

    greek_cols = ["Delta", "Gamma", "Theta", "Vega"]
    nonfinite = int((~np.isfinite(opt_df[greek_cols].astype(float))).any(axis=1).sum())
    neg_vol = int((opt_df["Volume"] < 0).sum())
    neg_oi = int((opt_df["OpenInterest"] < 0).sum())
    crossed = int((opt_df["Bid"] > opt_df["Ask"]).sum())

    # (snapshot timestamp, OptionSymbol) uniqueness.
    dup_mask = opt_df.duplicated(subset=["ts_et", "OptionSymbol"], keep=False)
    duplicate_rows = int(dup_mask.sum())

    # Symbol-encoded strike/type/expiry vs. the row's own columns -- an
    # independent identity check (also documents the strike*1000/multiplier
    # encoding convention, rather than asserting it only in a comment).
    parsed = opt_df["OptionSymbol"].map(parse_option_symbol)
    symbol_mismatches = 0
    for row_type, row_strike, row_exp, p in zip(opt_df["Type"], opt_df["Strike"], opt_df["Expiration"], parsed):
        if p is None:
            symbol_mismatches += 1
            continue
        _root, yymmdd, type_char, strike = p
        expected_type = "call" if type_char == "C" else "put"
        exp_yymmdd = pd.to_datetime(row_exp).strftime("%y%m%d") if pd.notna(row_exp) else None
        if row_type != expected_type or abs(float(row_strike) - strike) > 1e-6 or yymmdd != exp_yymmdd:
            symbol_mismatches += 1

    # Per-contract OI stability: does reported OpenInterest ever change
    # within the (regular-hours) session, and is there a usable
    # first-eligible-session baseline to compare later readings against.
    regular_df = opt_df[opt_df["regular_hours"]].sort_values("ts_et")
    oi_change_contracts = 0
    oi_baseline_contracts = 0
    max_repeated_mid_run = 0
    for _symbol, g in regular_df.groupby("OptionSymbol"):
        oi_vals = g["OpenInterest"].dropna()
        if len(oi_vals) and oi_vals.nunique() > 1:
            oi_change_contracts += 1
        if len(oi_vals):
            oi_baseline_contracts += 1  # first-eligible-session value exists and is usable as a baseline
        mid_vals = g["Mid"].to_numpy()
        if len(mid_vals):
            run = 1
            best = 1
            for i in range(1, len(mid_vals)):
                if mid_vals[i] == mid_vals[i - 1] and not np.isnan(mid_vals[i]):
                    run += 1
                    best = max(best, run)
                else:
                    run = 1
            max_repeated_mid_run = max(max_repeated_mid_run, best)

    return SessionAudit(
        date=yyyymmdd, listed_objects=len(all_objects), excluded_non_snapshot=excluded,
        snapshot_count=len(snap_meta), reported_count=REPORTED_SNAPSHOT_COUNTS.get(yyyymmdd),
        first_ts_et=snap_meta[0][0] if snap_meta else None,
        last_ts_et=snap_meta[-1][0] if snap_meta else None,
        premarket_snapshots=premarket, regular_hours_snapshots=regular,
        afterhours_snapshots=afterhours, gap_seconds=gaps,
        max_gap_seconds=max(gaps) if gaps else None,
        distinct_strikes=int(opt_df["Strike"].nunique()),
        distinct_contracts=int(opt_df["OptionSymbol"].nunique()),
        rows_total=int(len(opt_df)),
        field_coverage=coverage, nonfinite_greeks=nonfinite,
        negative_volumes=neg_vol, negative_open_interest=neg_oi,
        crossed_quotes=crossed,
        duplicate_snapshot_symbol_rows=duplicate_rows,
        symbol_mismatch_rows=symbol_mismatches,
        spot_inconsistent_snapshots=spot_inconsistent,
        contracts_with_oi_change=oi_change_contracts,
        contracts_with_oi_baseline=oi_baseline_contracts,
        max_repeated_mid_run=max_repeated_mid_run,
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
        return option_rows.assign(
            dV=pd.Series(dtype=float), dv_flag=pd.Series(dtype=object),
            interval_seconds=pd.Series(dtype=float),
        )

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
    interval_seconds = np.full(len(df), np.nan)

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
            interval_seconds[row_idx] = gap
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
    df["interval_seconds"] = interval_seconds
    return df
