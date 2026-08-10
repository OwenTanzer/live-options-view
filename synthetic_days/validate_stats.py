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

    print_report(real_stats, synth_stats, real_ranges, synth_ranges, real_rate, synth_rate)


if __name__ == "__main__":
    main()
