#!/usr/bin/env python3
"""Generate N synthetic QQQ 0DTE trading days from real R2 history.

Connects the three real data sources this repo already writes to R2 --
5-min underlying bars, EOD 0DTE chain snapshots, and (where available)
intraday chain snapshots -- through `price_path.py` (underlying path) and
`chain_synth.py` (options chain) into full synthetic sessions in the same
payload shape `intraday/latest.json` uses, so they can be fed straight into
`crassus` strategies via `backtest_bridge.py` with no format translation.

Usage:
    python day_generator.py --n-days 20 --out ./out --seed 7
    python day_generator.py --n-days 5 --mode whole_day --out ./out

Needs R2 credentials the first time (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, optionally R2_BUCKET_NAME) to populate the local
`./r2_cache/`; every run after that works fully offline from cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from r2_sources import R2Source  # noqa: E402
from price_path import split_into_sessions, bootstrap_day, upsample_to_seconds, ET  # noqa: E402
from chain_synth import build_donor, intraday_shape_curve, synth_snapshot_rows  # noqa: E402

STRIKE_WINDOW_DEFAULT = 33  # matches collector.py's live STRIKE_WINDOW


def load_donors(src: R2Source, symbol: str, max_days: int | None = None):
    days = src.all_options_0dte_days(symbol)
    if max_days and len(days) > max_days:
        # Evenly subsample across the full history rather than just the most
        # recent `max_days` -- a donor pool should span regimes (calm/panic,
        # different vol levels), not just whatever happened most recently.
        idx = np.linspace(0, len(days) - 1, max_days).round().astype(int)
        days = [days[i] for i in sorted(set(idx))]
    donors = []
    for d in days:
        cols = src.options_0dte_day(symbol, d)
        if not cols:
            continue
        donor = build_donor(cols, d)
        if donor is not None:
            donors.append(donor)
    return donors


def _row_extrinsic(row: dict, spot: float) -> float | None:
    """One row's own extrinsic (time) value: mid - intrinsic, at the given
    spot. `None` if the row lacks a two-sided quote."""
    if not row.get("Bid") or not row.get("Ask"):
        return None
    strike = float(row["Strike"])
    mid = (float(row["Bid"]) + float(row["Ask"])) / 2.0
    intrinsic = max(0.0, spot - strike) if row.get("Type") == "call" else max(0.0, strike - spot)
    return max(0.0, mid - intrinsic)


def _eod_extrinsic_by_symbol(eod_rows: list[dict]) -> dict[str, float]:
    """Every quoted row's extrinsic value from one day's *last* snapshot --
    the per-symbol denominator every earlier snapshot's ratio is computed
    against (see `load_intraday_curve`)."""
    out = {}
    for r in eod_rows:
        if not r.get("UnderlyingPrice") or not r.get("OptionSymbol"):
            continue
        extrinsic = _row_extrinsic(r, float(r["UnderlyingPrice"]))
        if extrinsic is not None:
            out[r["OptionSymbol"]] = extrinsic
    return out


def load_intraday_curve(src: R2Source, max_days: int = 15, samples_per_day: int = 30, exclude_days: set[str] | None = None):
    """Build the canonical time-of-day OI/spread/volume ramp, and the
    (time x moneyness) premium-decay grid, from real intraday days the
    collector has recorded. Returns None (== "assume already fully in
    place all day") if no intraday history exists yet -- see
    `chain_synth.intraday_shape_curve`'s docstring for why that's the
    honest default rather than inventing a shape.

    A collector day writes ~390 snapshot CSVs (one per minute); fetching
    every one of every available day was tested against the live bucket and
    turned out to mean tens of thousands of individual R2 GETs -- far more
    resolution than a 15-minute-bucket curve needs. `samples_per_day` evenly
    subsamples each day's snapshots instead, and `max_days` evenly
    subsamples *which* days are read when more are available, keeping this
    fast without biasing towards whichever days happen to sort first.

    The premium-decay ratio is computed *per row*, against that same row's
    own symbol's EOD extrinsic value -- not one pooled ATM-straddle scalar
    applied to every strike and side uniformly, which an earlier version
    did (flagged in review: OTM/ITM options don't decay identically to ATM
    ones).

    `exclude_days` (YYYYMMDD strings) removes days from the calibration
    pool entirely before subsampling -- `validate_stats.py` uses this to
    hold one real intraday day out of calibration so it can validate the
    resulting grid against that day's real, never-seen-by-the-model rows
    (an actual out-of-sample check, not comparing the grid to the same rows
    it was fit on).
    """
    days = [d for d in src.intraday_days_available() if d not in (exclude_days or set())]
    if len(days) > max_days:
        idx = np.linspace(0, len(days) - 1, max_days).round().astype(int)
        days = [days[i] for i in sorted(set(idx))]
    samples = []
    premium_decay_samples: list[tuple[float, float, bool, float]] = []
    for yyyymmdd in days:
        keys = src.intraday_snapshots(yyyymmdd)
        if not keys:
            continue
        if len(keys) > samples_per_day:
            idx = np.linspace(0, len(keys) - 1, samples_per_day).round().astype(int)
            keys = [keys[i] for i in sorted(set(idx))]
        parsed_days = []
        for key in keys:
            rows = src.intraday_snapshot_csv(key)
            if not rows:
                continue
            # HHMMSSffffff embedded in the filename, ET (collector's own clock).
            hhmmss = key.rsplit("snapshot_", 1)[-1].split(".")[0][:6]
            t = datetime.strptime(f"{yyyymmdd}{hhmmss}", "%Y%m%d%H%M%S").replace(tzinfo=ET)
            total_oi = sum(float(r.get("OpenInterest") or 0) for r in rows)
            total_vol = sum(float(r.get("Volume") or 0) for r in rows)
            spreads = [
                (float(r["Ask"]) - float(r["Bid"])) / ((float(r["Ask"]) + float(r["Bid"])) / 2.0)
                for r in rows
                if r.get("Bid") and r.get("Ask") and float(r["Bid"]) > 0
            ]
            median_spread = float(np.median(spreads)) if spreads else float("nan")
            parsed_days.append((t, total_oi, total_vol, median_spread, rows))
        if len(parsed_days) < 2:
            continue
        eod_oi = parsed_days[-1][1] or 1.0
        eod_vol = parsed_days[-1][2] or 1.0
        eod_spread = parsed_days[-1][3]
        eod_extrinsic_by_symbol = _eod_extrinsic_by_symbol(parsed_days[-1][4])
        open_t = parsed_days[0][0].replace(hour=9, minute=30, second=0, microsecond=0)
        for t, oi, vol, spread, rows in parsed_days:
            minutes = (t - open_t).total_seconds() / 60.0
            samples.append((minutes, {
                "oi_ratio": oi / eod_oi,
                "volume_ratio": vol / eod_vol,
                "spread_ratio": (spread / eod_spread) if eod_spread and eod_spread == eod_spread else float("nan"),
            }))

            spot = next((float(r["UnderlyingPrice"]) for r in rows if r.get("UnderlyingPrice")), None)
            if spot is None:
                continue
            for r in rows:
                sym = r.get("OptionSymbol")
                eod_extrinsic = eod_extrinsic_by_symbol.get(sym)
                if not eod_extrinsic or eod_extrinsic <= 1e-6:
                    continue
                extrinsic = _row_extrinsic(r, spot)
                if extrinsic is None:
                    continue
                moneyness = float(r["Strike"]) / spot - 1.0
                is_call = r.get("Type") == "call"
                premium_decay_samples.append((minutes, moneyness, is_call, extrinsic / eod_extrinsic))
    return intraday_shape_curve(samples, premium_decay_samples) if samples else None


def generate_day(
    sessions,
    donors,
    intraday_curve,
    rng: np.random.Generator,
    session_date: date,
    mode: str,
    block_size: int,
    strike_window: int,
) -> dict:
    closes, ranges = bootstrap_day(sessions, rng, mode=mode, block_size=block_size)
    timestamps, path = upsample_to_seconds(session_date, closes, ranges, rng, step_s=60)

    donor = donors[rng.integers(0, len(donors))]

    snapshots = []
    for t, spot in zip(timestamps, path):
        rows = synth_snapshot_rows(
            now_et=t,
            spot=float(spot),
            donor=donor,
            strike_window=strike_window,
            intraday_curve=intraday_curve,
            target_total_oi=donor.total_oi,
            target_total_volume=donor.total_volume,
            expiration=session_date,
        )
        snapshots.append({
            "timestamp": t.astimezone(tz=None).isoformat(),
            "snapshot_time": t.strftime("%H:%M ET"),
            "date": session_date.isoformat(),
            "expiration": session_date.isoformat(),
            "underlying_price": float(spot),
            "rows": rows,
            "underlying_market": None,
        })
    return {
        "session_date": session_date.isoformat(),
        "donor_day": donor.day.isoformat(),
        "mode": mode,
        "snapshots": snapshots,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--n-days", type=int, default=10)
    ap.add_argument("--years", default=None, help="comma-separated years of 5-min history to bootstrap from, default: all cached/available")
    ap.add_argument("--mode", choices=["block", "whole_day"], default="block")
    ap.add_argument("--block-size", type=int, default=6, help="bars per block (6 bars = 30min at 5min resolution)")
    ap.add_argument("--strike-window", type=int, default=STRIKE_WINDOW_DEFAULT)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="./out")
    ap.add_argument("--start-date", default=date.today().isoformat(), help="first synthetic session date (labels only)")
    ap.add_argument("--max-donor-days", type=int, default=80, help="cap on real EOD chain days pulled from R2 as smile/OI-shape donors (evenly subsampled if more exist)")
    ap.add_argument("--max-intraday-days", type=int, default=15, help="cap on real intraday collector days used to calibrate the time-of-day ramp")
    ap.add_argument("--intraday-samples-per-day", type=int, default=30, help="snapshots subsampled per intraday day (each day has ~390; this avoids tens of thousands of individual R2 GETs)")
    args = ap.parse_args()

    src = R2Source()
    years = [int(y) for y in args.years.split(",")] if args.years else list(range(2023, date.today().year + 1))
    bars = src.five_min_bars(args.symbol, years)
    if not bars:
        print(f"No 5-min history found for {args.symbol} in years {years} (need R2 creds the first time, or a populated ./r2_cache/)", file=sys.stderr)
        sys.exit(1)
    sessions = split_into_sessions(bars)
    print(f"{len(sessions)} real historical sessions available for bootstrapping")

    donors = load_donors(src, args.symbol, max_days=args.max_donor_days)
    if not donors:
        print(f"No EOD options_0dte chain history found for {args.symbol} -- cannot synthesize a chain, only a price path.", file=sys.stderr)
        sys.exit(1)
    print(f"{len(donors)} real EOD chain snapshots available as smile/OI-shape donors")

    intraday_curve = load_intraday_curve(src, max_days=args.max_intraday_days, samples_per_day=args.intraday_samples_per_day)
    if intraday_curve is None:
        print("No intraday collector history found yet -- time-of-day OI/spread/volume ramp defaults to 'fully in place all day'.")
    else:
        n_samples = sum(v == v for v in intraday_curve["oi_ratio"])
        print(f"Intraday time-of-day shape calibrated from real collector days ({n_samples}/{len(intraday_curve['oi_ratio'])} 15-min buckets have real samples)")

    rng = np.random.default_rng(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(args.start_date)
    written = 0
    d = start
    while written < args.n_days:
        if d.weekday() < 5:  # skip weekends for labeling; holidays aren't modeled
            day = generate_day(sessions, donors, intraday_curve, rng, d, args.mode, args.block_size, args.strike_window)
            path = out_dir / f"synthetic_{d.isoformat()}.json"
            path.write_text(json.dumps(day))
            print(f"-> {path} ({len(day['snapshots'])} snapshots, donor={day['donor_day']})")
            written += 1
        d += timedelta(days=1)


if __name__ == "__main__":
    main()
