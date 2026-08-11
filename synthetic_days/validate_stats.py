#!/usr/bin/env python3
"""Prove synthetic days match real days statistically, rather than assume it.

Compares real 5-min bars (from `history/QQQ/5min/`) against generated
`out/synthetic_*.json` days on exactly the properties `price_path.py`'s
design claims to preserve:

  * return distribution        -- mean, std, skew, kurtosis of 5-min log returns
  * volatility clustering      -- ACF of squared returns at lag 1-5 (should be
                                   positive and decaying, the classic GARCH
                                   signature; a naive iid simulator gives ~0)
  * return autocorrelation     -- ACF of raw returns at lag 1-5 (should be
                                   small, same as real markets)
  * daily range                -- distribution of (high-low)/open per day
  * canopus_down_day_14's own signal rate -- fraction of days where QQQ is
    down >=0.25% from the 9:30 reference by 2:45pm ET, real vs synthetic.
    This is the one number that most directly answers "does this generator
    produce a realistic rate of the exact condition the strategy trades on."

`check_acceptance` turns these into an actual pass/fail gate (exit code 1
on any failure), not just numbers for a human to eyeball -- flagged in
review as necessary for this to run in CI at all. See that function's
docstring for why the thresholds are loose, directional-correctness bounds
rather than tight statistical parity.

Usage:
    python validate_stats.py --days ./out
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from r2_sources import R2Source  # noqa: E402
from price_path import split_into_sessions, _log_returns  # noqa: E402
from chain_synth import (  # noqa: E402
    MONEYNESS_BUCKET_EDGES,
    N_MONEYNESS_BUCKETS,
    ATM_BUCKET_INDEX,
    _relative_moneyness,
)
from day_generator import load_intraday_curve, _row_extrinsic, _eod_extrinsic_by_symbol  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

ET = ZoneInfo("America/New_York")


def acf(x: np.ndarray, lag: int) -> float:
    x = x - x.mean()
    num = np.sum(x[:-lag] * x[lag:])
    den = np.sum(x * x)
    return float(num / den) if den else 0.0


def describe_returns(all_returns: list[np.ndarray], label: str) -> dict:
    pooled = np.concatenate(all_returns) if all_returns else np.array([])
    sq = pooled**2
    stats = {
        "label": label,
        "n_days": len(all_returns),
        "n_bars": len(pooled),
        "mean": float(pooled.mean()) if len(pooled) else float("nan"),
        "std": float(pooled.std()) if len(pooled) else float("nan"),
        "skew": float(((pooled - pooled.mean())**3).mean() / pooled.std()**3) if len(pooled) else float("nan"),
        "kurtosis": float(((pooled - pooled.mean())**4).mean() / pooled.std()**4) if len(pooled) else float("nan"),
        "acf_returns_lag1-5": [round(acf(pooled, k), 4) for k in range(1, 6)],
        "acf_sq_returns_lag1-5": [round(acf(sq, k), 4) for k in range(1, 6)],
    }
    return stats


def daily_ranges(sessions_or_days: list) -> np.ndarray:
    out = []
    for s in sessions_or_days:
        if hasattr(s, "h"):  # real DayBars
            o, h, l = s.o[0], s.h.max(), s.l.min()
        else:  # synthetic day dict's underlying_price series
            prices = [snap["underlying_price"] for snap in s["snapshots"]]
            o, h, l = prices[0], max(prices), min(prices)
        out.append((h - l) / o if o else float("nan"))
    return np.array(out)


def canopus_signal_rate_real(sessions) -> float:
    """Down >=0.25% from 9:30 open-proxy to the 2:45pm bar, real 5-min bars.
    2:45pm ET is bar index (14*60+45 - 9*60-30)/5 = 63 into the session."""
    idx_245 = int((14 * 60 + 45 - (9 * 60 + 30)) / 5)
    hits = 0
    for s in sessions:
        if idx_245 >= len(s.c):
            continue
        ref = s.o[0]
        px_245 = s.c[idx_245]
        if (px_245 - ref) / ref <= -0.0025:
            hits += 1
    return hits / len(sessions) if sessions else float("nan")


def canopus_signal_rate_synthetic(days: list[dict]) -> float:
    hits = 0
    for day in days:
        snaps = day["snapshots"]
        ref = snaps[0]["underlying_price"]
        target = None
        for snap in snaps:
            hhmm = snap["snapshot_time"].split(" ")[0]
            if hhmm >= "14:45":
                target = snap["underlying_price"]
                break
        if target is None:
            continue
        if (target - ref) / ref <= -0.0025:
            hits += 1
    return hits / len(days) if days else float("nan")


def _bucket_option_ratios(rows_by_snapshot: list[tuple[float, list[dict]]], eod_extrinsic_by_symbol: dict[str, float]) -> np.ndarray:
    """One real intraday day's rows -> a (time_bucket x moneyness_bucket)
    grid of median premium_decay ratios, same bucketing
    `chain_synth.intraday_shape_curve` uses for the calibration grid, so the
    two are directly comparable cell-for-cell.
    """
    n_buckets = (390 // 15) + 1
    samples: dict[tuple[int, int], list[float]] = {}
    for minutes, rows in rows_by_snapshot:
        spot = next((float(r["UnderlyingPrice"]) for r in rows if r.get("UnderlyingPrice")), None)
        if spot is None:
            continue
        b = int(minutes // 15)
        if not (0 <= b < n_buckets):
            continue
        for r in rows:
            eod_extrinsic = eod_extrinsic_by_symbol.get(r.get("OptionSymbol"))
            if not eod_extrinsic or eod_extrinsic <= 1e-6:
                continue
            extrinsic = _row_extrinsic(r, spot)
            if extrinsic is None:
                continue
            moneyness = float(r["Strike"]) / spot - 1.0
            is_call = r.get("Type") == "call"
            rel_m = _relative_moneyness(np.array([moneyness]), np.array([is_call]))[0]
            m_bucket = int(np.searchsorted(MONEYNESS_BUCKET_EDGES, rel_m))
            samples.setdefault((b, m_bucket), []).append(extrinsic / eod_extrinsic)

    grid = np.full((n_buckets, N_MONEYNESS_BUCKETS), np.nan)
    for (b, m_bucket), vals in samples.items():
        grid[b, m_bucket] = float(np.median(vals))
    return grid


def held_out_option_path_check(src: R2Source, max_calibration_days: int = 15, samples_per_day: int = 30) -> dict | None:
    """Out-of-sample check for `chain_synth`'s moneyness/time-conditional
    premium-decay grid: hold one real intraday day *out* of calibration,
    rebuild the grid from every other real intraday day, then compare that
    grid's predictions against the held-out day's own real, never-seen rows
    -- a held contract's price path followed across (time, moneyness)
    buckets, exactly the dimension the strategy P&L in `backtest_bridge.py`
    depends on.

    Calibrating the grid from real rows and then only comparing it back
    against rows it was fit on (what `validate_stats.py` did before this)
    is not a validation -- flagged in review. Returns `None` (skip, not
    fail) if fewer than 2 real intraday days are available, since a
    held-out check needs at least one day excluded from calibration and one
    day to validate against.

    Only compares cells the calibration grid was actually fit on real data
    for (`premium_decay_grid_sampled_mask`), not cells that fell back to a
    flat 1.0 or a borrowed ATM-column value for lack of any real
    calibration sample -- comparing the held-out day against an explicit
    "no real data here" placeholder measures how wrong the placeholder is,
    not whether the model generalizes.
    """
    days = src.intraday_days_available()
    if len(days) < 2:
        return None
    held_out_day = days[-1]

    curve = load_intraday_curve(src, max_days=max_calibration_days, samples_per_day=samples_per_day, exclude_days={held_out_day})
    if curve is None:
        return None
    calibrated_grid = curve["premium_decay_grid"]
    calibrated_sampled_mask = curve["premium_decay_grid_sampled_mask"]

    keys = src.intraday_snapshots(held_out_day)
    if len(keys) > samples_per_day:
        idx = np.linspace(0, len(keys) - 1, samples_per_day).round().astype(int)
        keys = [keys[i] for i in sorted(set(idx))]
    parsed = []
    for key in keys:
        rows = src.intraday_snapshot_csv(key)
        if not rows:
            continue
        hhmmss = key.rsplit("snapshot_", 1)[-1].split(".")[0][:6]
        t = datetime.strptime(f"{held_out_day}{hhmmss}", "%Y%m%d%H%M%S").replace(tzinfo=ET)
        parsed.append((t, rows))
    if len(parsed) < 2:
        return None

    open_t = parsed[0][0].replace(hour=9, minute=30, second=0, microsecond=0)
    eod_extrinsic_by_symbol = _eod_extrinsic_by_symbol(parsed[-1][1])
    rows_by_snapshot = [((t - open_t).total_seconds() / 60.0, rows) for t, rows in parsed]
    real_holdout_grid = _bucket_option_ratios(rows_by_snapshot, eod_extrinsic_by_symbol)

    matched = ~np.isnan(real_holdout_grid) & (real_holdout_grid > 0) & (calibrated_grid > 0) & calibrated_sampled_mask
    n_matched = int(matched.sum())
    if n_matched == 0:
        return None
    # log-ratio, not a raw absolute difference: these are decay *ratios*
    # spanning roughly 1x-20x (see chain_synth.py/README), a multiplicative
    # quantity, so a fixed absolute-difference bound would be meaningless at
    # one end of the range and impossibly tight at the other.
    log_ratio_err = np.abs(np.log2(real_holdout_grid[matched] / calibrated_grid[matched]))

    atm_real = real_holdout_grid[:, ATM_BUCKET_INDEX]
    atm_calibrated = calibrated_grid[:, ATM_BUCKET_INDEX]
    atm_buckets_with_real_samples = [
        (b, float(atm_real[b]), float(atm_calibrated[b])) for b in range(len(atm_real)) if atm_real[b] == atm_real[b]
    ]

    return {
        "held_out_day": held_out_day,
        "n_matched_cells": n_matched,
        "median_log2_ratio_error": float(np.median(log_ratio_err)),
        "max_log2_ratio_error": float(np.max(log_ratio_err)),
        "atm_bucket_path": atm_buckets_with_real_samples,  # the "held contract followed across time buckets" trace
    }


def check_acceptance(
    real_stats: dict, synth_stats: dict,
    real_ranges: np.ndarray, synth_ranges: np.ndarray,
    real_rate: float, synth_rate: float,
    option_path_check: dict | None = None,
) -> bool:
    """Pass/fail gate, not just diagnostics -- flagged in review: printing
    numbers for a human to eyeball doesn't catch a regression in CI. Returns
    True iff every check passes.

    Thresholds are deliberately loose, directional-correctness bounds, not
    tight statistical parity: they're checking "does this still look like a
    real market and not an iid simulator," not "does it match real stats to
    two decimal places." Two reasons for looseness: (1) small synthetic-day
    counts (5 in a quick run, or a handful in a CI fixture) carry real
    sampling noise on their own statistics, and (2) the whole point of the
    generator is *novel* days, not reproductions of real ones, so some
    drift from the specific real sample is expected and healthy. A tight
    threshold here would make CI flaky on nothing but sampling variance.

    The *ratio* checks above (synthetic vs real) are necessary but not
    sufficient: a corrupted or unrealistic "real" fixture would still pass
    them as long as the synthetic side drifted along with it -- exactly
    what happened before this fix, when the checked-in "real" 5-min-bar
    fixture had a 1.99% per-bar standard deviation and a 21.7% average
    daily range (both roughly 10-20x anything QQQ has ever actually done
    intraday) and every check here still reported PASS. The absolute bounds
    below apply directly to the real side too, so a bad fixture fails loudly
    instead of dragging a correct generator down with it in a ratio check.
    """
    passed, failed = 0, 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))

    print("\n=== Acceptance thresholds ===")

    std_ratio = (synth_stats["std"] / real_stats["std"]) if real_stats["std"] else float("nan")
    check("synthetic std is within 0.5x-1.75x of real std", 0.5 <= std_ratio <= 1.75, f"ratio={std_ratio:.2f}")

    check(
        "synthetic kurtosis > 3.5 (fat tails present -- an iid/normal simulator would sit near 3.0)",
        synth_stats["kurtosis"] > 3.5, f"kurtosis={synth_stats['kurtosis']:.2f}",
    )

    acf_sq_lag1 = synth_stats["acf_sq_returns_lag1-5"][0]
    check(
        "synthetic ACF(returns^2) lag1 > 0.03 (positive volatility clustering -- iid noise sits near 0)",
        acf_sq_lag1 > 0.03, f"acf_sq_lag1={acf_sq_lag1:.3f}",
    )

    real_range_mean, synth_range_mean = np.nanmean(real_ranges), np.nanmean(synth_ranges)
    range_ratio = (synth_range_mean / real_range_mean) if real_range_mean else float("nan")
    check("synthetic daily range mean is within 0.4x-2.5x of real", 0.4 <= range_ratio <= 2.5, f"ratio={range_ratio:.2f}")

    rate_gap = abs(synth_rate - real_rate) if real_rate == real_rate and synth_rate == synth_rate else float("nan")
    check(
        "canopus_down_day_14 signal rate is within 30 percentage points of real (loose: small "
        "synthetic-day counts make this the noisiest check here)",
        rate_gap == rate_gap and rate_gap <= 0.30,
        f"real={real_rate:.1%} synthetic={synth_rate:.1%} gap={rate_gap:.1%}" if rate_gap == rate_gap else "n/a",
    )

    # -- Absolute sanity bounds: guard the fixture itself, not just the
    # synthetic/real ratio (see this function's docstring). Bounds are QQQ-
    # scale but generous -- real 5-min bar std is typically 0.03%-0.3%, real
    # single-day range is typically 0.3%-4%; these bounds are wide enough to
    # tolerate a genuinely volatile real session without being wide enough
    # to admit the 1.99%-std / 21.7%-range fixture that motivated this check.
    check(
        "real fixture's 5-min-bar std is within a sane absolute range for QQQ (0.02%-0.6%)",
        0.0002 <= real_stats["std"] <= 0.006, f"real_std={real_stats['std']:.5f}",
    )
    check(
        "synthetic 5-min-bar std is within a sane absolute range for QQQ (0.02%-0.6%)",
        0.0002 <= synth_stats["std"] <= 0.006, f"synth_std={synth_stats['std']:.5f}",
    )
    check(
        "real fixture's average daily range is within a sane absolute bound for QQQ (0.2%-8%)",
        0.002 <= real_range_mean <= 0.08, f"real_range_mean={real_range_mean:.4f}",
    )
    check(
        "synthetic average daily range is within a sane absolute bound for QQQ (0.2%-8%)",
        0.002 <= synth_range_mean <= 0.08, f"synth_range_mean={synth_range_mean:.4f}",
    )

    # -- Option-path acceptance: held-out real intraday day vs the
    # moneyness/time-conditional premium-decay grid calibrated without it
    # (see held_out_option_path_check). Skipped, not failed, when fewer than
    # 2 real intraday days are available to hold one out -- an absent check
    # is reported as such below, not silently treated as a pass.
    if option_path_check is None:
        print("  [SKIP] option-path out-of-sample check -- need >=2 real intraday days (only had fewer)")
    else:
        opc = option_path_check
        # Bounds expressed as a real/calibrated multiplicative factor
        # (2**log2_error), matching the ~1x-8x dynamic range chain_synth.py's
        # own docs and README already document for this decay grid -- not
        # fitted to any one held-out day's specific result.
        check(
            f"held-out day {opc['held_out_day']}: option-path grid median real-vs-calibrated factor <= 3x "
            f"(matched {opc['n_matched_cells']} real cells)",
            opc["median_log2_ratio_error"] <= np.log2(3), f"factor={2**opc['median_log2_ratio_error']:.2f}x",
        )
        check(
            f"held-out day {opc['held_out_day']}: option-path grid max real-vs-calibrated factor <= 16x "
            f"(deliberately wider than the median check -- a single worst cell is the thinnest-sample "
            f"signal in this whole gate with only a handful of real intraday days to calibrate from; "
            f"the median check above is the one that actually reflects typical backtest fidelity)",
            opc["max_log2_ratio_error"] <= np.log2(16), f"factor={2**opc['max_log2_ratio_error']:.2f}x",
        )
        if opc["atm_bucket_path"]:
            print("  ATM bucket, held contract followed across time buckets (real vs grid calibrated without this day):")
            for b, real_ratio, calibrated_ratio in opc["atm_bucket_path"]:
                minutes = b * 15
                print(f"    t+{minutes:>3}min: real={real_ratio:.3f}  calibrated_grid={calibrated_ratio:.3f}")

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


def print_report(real: dict, synth: dict, real_ranges: np.ndarray, synth_ranges: np.ndarray, real_rate: float, synth_rate: float):
    def row(label, r, s):
        print(f"  {label:<28} real={r!s:<28} synthetic={s!s}")

    print("\n=== Return distribution (pooled 5-min log returns) ===")
    row("n_days", real["n_days"], synth["n_days"])
    row("mean", f"{real['mean']:.6f}", f"{synth['mean']:.6f}")
    row("std", f"{real['std']:.6f}", f"{synth['std']:.6f}")
    row("skew", f"{real['skew']:.3f}", f"{synth['skew']:.3f}")
    row("kurtosis (fat tails > 3)", f"{real['kurtosis']:.3f}", f"{synth['kurtosis']:.3f}")

    print("\n=== Volatility clustering: ACF(returns^2), lag 1-5 ===")
    print("  (real markets: positive, slowly decaying. iid noise: ~0. This is the key check.)")
    row("real", real["acf_sq_returns_lag1-5"], "")
    row("synthetic", "", synth["acf_sq_returns_lag1-5"])

    print("\n=== Return autocorrelation: ACF(returns), lag 1-5 ===")
    row("real", real["acf_returns_lag1-5"], "")
    row("synthetic", "", synth["acf_returns_lag1-5"])

    print("\n=== Daily range distribution: (high-low)/open ===")
    row("mean", f"{np.nanmean(real_ranges):.4f}", f"{np.nanmean(synth_ranges):.4f}")
    row("p10 / p50 / p90", np.round(np.nanpercentile(real_ranges, [10, 50, 90]), 4).tolist(), np.round(np.nanpercentile(synth_ranges, [10, 50, 90]), 4).tolist())

    print("\n=== canopus_down_day_14 signal rate (down >=0.25% by 2:45pm ET) ===")
    row("qualifying-day fraction", f"{real_rate:.1%}", f"{synth_rate:.1%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--years", default=None)
    ap.add_argument("--days", default="./out")
    args = ap.parse_args()

    src = R2Source()
    years = [int(y) for y in args.years.split(",")] if args.years else list(range(2023, 2027))
    bars = src.five_min_bars(args.symbol, years)
    sessions = split_into_sessions(bars)
    if not sessions:
        print("No real 5-min history available (need R2 creds or a populated ./r2_cache/).", file=sys.stderr)
        sys.exit(1)

    synth_files = sorted(Path(args.days).glob("synthetic_*.json"))
    if not synth_files:
        print(f"No synthetic_*.json files in {args.days} -- run day_generator.py first.", file=sys.stderr)
        sys.exit(1)
    synth_days = [json.loads(p.read_text()) for p in synth_files]

    real_returns = [_log_returns(s) for s in sessions]
    synth_returns = []
    for day in synth_days:
        prices = np.array([snap["underlying_price"] for snap in day["snapshots"]])
        # `day_generator.py` upsamples to a 60s cadence (step_s=60, bar_s=300
        # -> 5 sub-steps per 5-min bar), and `upsample_to_seconds`'s Brownian
        # bridge is pinned to land exactly on the bootstrapped 5-min close at
        # the last sub-step of each bar -- so `prices[4::5]` recovers exactly
        # those 5-min closes. Comparing those to real 5-min bars, instead of
        # diffing the raw 1-min path against 5-min real bars, matters: return
        # variance scales with the sampling interval under a random walk, so
        # a 1-min-vs-5-min comparison structurally understates synthetic std/
        # kurtosis/ACF regardless of how good the generator is. Caught in
        # real PR review (see #65) -- this was silently wrong before.
        five_min_closes = prices[4::5]
        synth_returns.append(np.diff(np.log(five_min_closes)))

    real_stats = describe_returns(real_returns, "real")
    synth_stats = describe_returns(synth_returns, "synthetic")
    real_ranges = daily_ranges(sessions)
    synth_ranges = daily_ranges(synth_days)
    real_rate = canopus_signal_rate_real(sessions)
    synth_rate = canopus_signal_rate_synthetic(synth_days)
    option_path_check = held_out_option_path_check(src)

    print_report(real_stats, synth_stats, real_ranges, synth_ranges, real_rate, synth_rate)
    ok = check_acceptance(real_stats, synth_stats, real_ranges, synth_ranges, real_rate, synth_rate, option_path_check)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
