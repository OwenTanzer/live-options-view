#!/usr/bin/env python3
"""S3b evaluation per PROTOCOL.md: fit on development days, evaluate once on held-out days.

    python research/options_replay/calibration/evaluate.py --model candidate --label first-evaluation
    python research/options_replay/calibration/evaluate.py --model candidate+skew --label correction-1

Every invocation appends one line to calibration/attempts.jsonl (committed), so every
held-out evaluation -- passes and misses -- stays on record. Tolerances are read from
protocol.json and never passed on the command line.

Models
  baseline     Black-Scholes r=0, constant sigma, uniform session clock, no skew
  candidate    baseline + business-time clock (variance weights per 5-min bucket from
               development spot returns)
  sim-v1       the S3a generator's donor pricing at the same spot/time (reference only)
  ...+<corr>   candidate plus a named correction (see CORRECTIONS), fitted on development only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import archive as A  # noqa: E402

PROTOCOL = json.loads((HERE / "protocol.json").read_text())
TOL = PROTOCOL["tolerance"]
CLOSE = 16 * 3600
OPEN = 9 * 3600 + 30 * 60
BUCKETS = 78                                     # 5-minute buckets 09:30..16:00
YEAR_DAYS = 252.0
WINDOWS = {k: (int(a[:2]) * 3600 + int(a[3:]) * 60, int(b[:2]) * 3600 + int(b[3:]) * 60)
           for k, (a, b) in PROTOCOL["cells"]["windows"].items()}
EXPLORATORY = set(PROTOCOL["exploratory_days"])


# ---------------------------------------------------------------------------
# Black-Scholes (r = 0) with an explicit variance clock
# ---------------------------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S: float, K: float, sigma: float, tau: float, side: str) -> float:
    """Price with tau in years of *variance time* (sigma^2 * tau = remaining variance)."""
    intrinsic = max(0.0, S - K) if side == "C" else max(0.0, K - S)
    if tau <= 0 or sigma <= 0:
        return intrinsic
    v = sigma * math.sqrt(tau)
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if side == "C":
        return S * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - S * _ncdf(-d1)


def implied_sigma(target: float, S: float, K: float, tau: float, side: str) -> float | None:
    lo, hi = 1e-4, 10.0
    if target <= bs(S, K, lo, tau, side) or target >= bs(S, K, hi, tau, side):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs(S, K, mid, tau, side) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class Clock:
    """Remaining session variance time in years: sum of bucket weights / 252."""

    def __init__(self, weights: list[float], tail: float = 0.0):
        tot = sum(weights)
        self.w = [x / tot for x in weights]
        self.tail = tail          # variance still to come after 16:00, as a share of a day

    def tau(self, t: float) -> float:
        if t >= CLOSE:
            return self.tail / YEAR_DAYS
        x = (max(t, OPEN) - OPEN) / 300.0
        b = int(x)
        rem = (1.0 - (x - b)) * self.w[b] + sum(self.w[b + 1:])
        return (rem + self.tail) / YEAR_DAYS

    def covered(self, t0: float, t1: float) -> float:
        """Share of a full day's variance between t0 and t1 (for realized-vol scaling)."""
        return (self.tau(t0) - self.tau(t1)) * YEAR_DAYS

    def with_tail(self, tail: float) -> "Clock":
        c = Clock(self.w, tail)
        return c


UNIFORM = Clock([1.0] * BUCKETS)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load(cache: Path, symbol: str) -> dict[str, dict]:
    out = {}
    for f in sorted((cache / "calibration").glob(f"{symbol}_*.json")):
        d = json.loads(f.read_text())
        out[d["day"]] = d
    return out


def split(symbol: str, days: list[str]) -> tuple[list[str], list[str], list[str]]:
    sp = PROTOCOL["symbols"][symbol]
    dev = [d for d in days if sp["development"][0] <= d <= sp["development"][1]]
    held = [d for d in days if sp["held_out"][0] <= d <= sp["held_out"][1] and d not in EXPLORATORY]
    expl = [d for d in days if d in EXPLORATORY and sp["held_out"][0] <= d <= sp["held_out"][1]]
    return dev, held, expl


def points(day: dict) -> dict[int, dict]:
    return {p["t"]: p for p in day["points"] if "drop" not in p}


def mid(q: list) -> float | None:
    b, a = q
    if b is None or a is None or a < b or a <= 0:
        return None
    return (b + a) / 2.0


def strike_of(c: str) -> float:
    return float(c[1:])


def contracts_for(p: dict) -> dict[tuple[str, str], str]:
    """{(moneyness, side): contract} for ATM and 1-OTM (next strike away from spot)."""
    strikes = sorted({strike_of(c) for c in p["q"]})
    atm = min(strikes, key=lambda k: abs(k - p["spot"]))
    i = strikes.index(atm)
    out = {("ATM", "call"): "C" + A._fmt_strike(atm), ("ATM", "put"): "P" + A._fmt_strike(atm)}
    if i + 1 < len(strikes):
        out[("1-OTM", "call")] = "C" + A._fmt_strike(strikes[i + 1])
    if i - 1 >= 0:
        out[("1-OTM", "put")] = "P" + A._fmt_strike(strikes[i - 1])
    return out


def window_of(t: int) -> str | None:
    for k, (a, b) in WINDOWS.items():
        if a <= t <= b:
            return k
    return None


# ---------------------------------------------------------------------------
# Fitting (development only)
# ---------------------------------------------------------------------------

def fit_business_weights(days: dict[str, dict], dev: list[str]) -> list[float]:
    """Mean squared 5-min log return per bucket on development days. The grid covers 09:45-15:55;
    buckets outside it (09:30-09:45 opening, 15:55-16:00) take their nearest covered neighbour's
    weight -- stated, since the grid cannot observe them."""
    acc = defaultdict(list)
    for d in dev:
        pts = points(days[d])
        for t, p in pts.items():
            q = pts.get(t + 300)
            if q:
                acc[(t - OPEN) // 300].append(math.log(q["spot"] / p["spot"]) ** 2)
    w = [st.mean(acc[b]) if len(acc[b]) >= 5 else None for b in range(BUCKETS)]
    first = next(i for i, x in enumerate(w) if x is not None)
    last = max(i for i, x in enumerate(w) if x is not None)
    for i in range(BUCKETS):
        if w[i] is None:
            w[i] = w[first] if i < first else w[last] if i > last else st.mean(
                [x for x in w[max(0, i - 2):i + 3] if x is not None])
    return w


def anchor_sigma(day: dict, clock: Clock) -> float | None:
    """Condition A: ATM implied vol from the recorded 09:45 straddle (first kept point <= 10:00)."""
    for t in sorted(points(day)):
        if t > 10 * 3600:
            return None
        p = points(day)[t]
        c = contracts_for(p)
        cm, pm = mid(p["q"][c[("ATM", "call")]]), mid(p["q"][c[("ATM", "put")]])
        if cm is None or pm is None:
            continue
        K = strike_of(c[("ATM", "call")])
        lo, hi, tau = 1e-4, 10.0, clock.tau(t)
        target = cm + pm
        f = lambda s: bs(p["spot"], K, s, tau, "C") + bs(p["spot"], K, s, tau, "P") - target  # noqa: E731
        if f(lo) > 0 or f(hi) < 0:
            continue
        for _ in range(80):
            m = 0.5 * (lo + hi)
            lo, hi = (m, hi) if f(m) < 0 else (lo, m)
        return 0.5 * (lo + hi)
    return None


def realized_sigma(day: dict, clock: Clock) -> float | None:
    pts = points(day)
    ss, cov = 0.0, 0.0
    for t, p in pts.items():
        q = pts.get(t + 300)
        if q:
            ss += math.log(q["spot"] / p["spot"]) ** 2
            cov += clock.covered(t, t + 300)
    if cov < 0.5:
        return None
    return math.sqrt(ss / cov * YEAR_DAYS)


def fit_k(days, dev, clock) -> float | None:
    r = []
    for d in dev:
        a, rv = anchor_sigma(days[d], clock), realized_sigma(days[d], clock)
        if a and rv:
            r.append(a / rv)
    return st.median(r) if r else None


# ---------------------------------------------------------------------------
# Corrections (each one named change, fitted on development only)
# ---------------------------------------------------------------------------

def fit_skew(days, dev, clock) -> dict:
    """Correction 'skew': multiplicative sigma adjustment per (moneyness, side) = median on
    development of (implied vol of that contract) / (anchored ATM vol)."""
    acc = defaultdict(list)
    for d in dev:
        sa = anchor_sigma(days[d], clock)
        if not sa:
            continue
        for t, p in points(days[d]).items():
            for (mny, side), c in contracts_for(p).items():
                m = mid(p["q"][c])
                if m is None:
                    continue
                iv = implied_sigma(m, p["spot"], strike_of(c), clock.tau(t), c[0])
                if iv:
                    acc[(mny, side)].append(iv / sa)
    return {f"{k[0]}|{k[1]}": st.median(v) for k, v in acc.items() if len(v) >= 30}


def fit_closetail(days, dev, clock) -> float:
    """Correction 'closetail': variance after 16:00 (share of a day) such that the anchored model's
    median ATM extrinsic at 15:55 equals the recorded median on development days. Recorded 0DTE
    ATM premium does not converge to ~0 by 15:55 (it holds ~$37/contract on development days),
    consistent with the post-close exercise window; a session-only clock cannot represent it."""
    samples = []
    for d in dev:
        day = days[d]
        p = points(day).get(15 * 3600 + 55 * 60)
        if not p:
            continue
        for side in ("call", "put"):
            c = contracts_for(p)[("ATM", side)]
            m = mid(p["q"][c])
            if m is None:
                continue
            K = strike_of(c)
            intr = max(0.0, p["spot"] - K) if c[0] == "C" else max(0.0, K - p["spot"])
            samples.append((d, p["spot"], c, m - intr))
    target = st.median(x[3] for x in samples)

    def model_median(tail):
        ck = clock.with_tail(tail)
        vals = []
        for d, S, c, _ in samples:
            sa = anchor_sigma(days[d], ck)
            if sa:
                K = strike_of(c)
                intr = max(0.0, S - K) if c[0] == "C" else max(0.0, K - S)
                vals.append(bs(S, K, sa, ck.tau(15 * 3600 + 55 * 60), c[0]) - intr)
        return st.median(vals)
    lo, hi = 0.0, 0.2
    for _ in range(40):
        m = 0.5 * (lo + hi)
        lo, hi = (m, hi) if model_median(m) < target else (lo, m)
    return 0.5 * (lo + hi)


CORRECTIONS = {"skew": fit_skew, "closetail": fit_closetail}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

class Model:
    def __init__(self, name, clock, k, skew=None, sim=None):
        self.name, self.clock, self.k, self.skew, self.sim = name, clock, k, skew or {}, sim

    def price(self, S, contract, t, sigma, mny, side):
        if self.sim is not None:
            return self.sim(S, contract, t)
        s = sigma * self.skew.get(f"{mny}|{side}", 1.0)
        return bs(S, strike_of(contract), s, self.clock.tau(t), contract[0])


def sim_v1_pricer(cache: Path, day_key: str):
    """Reference: the S3a generator's donor pricing at the actual spot/time."""
    import numpy as np
    import simulate as SIM
    from chain_synth import synth_snapshot_rows
    from datetime import datetime as dt
    from price_path import ET
    sim = SIM.Simulator(cache)
    _, donors, curve = sim._load_inputs()
    donor = donors[int(hashlib.sha256(day_key.encode()).hexdigest(), 16) % len(donors)]
    memo = {}

    def price(S, contract, t):
        key = (round(S, 4), t)
        if key not in memo:
            now = dt(2000, 1, 3, t // 3600, t % 3600 // 60, tzinfo=ET)
            rows = synth_snapshot_rows(now, S, donor, SIM.STRIKE_WINDOW, curve, donor.total_oi,
                                       donor.total_volume, SIM.LABEL_DATE)
            memo[key] = {("C" if r["Type"] == "call" else "P") + A._fmt_strike(r["Strike"]): (r["Bid"] + r["Ask"]) / 2
                         for r in rows}
        return memo[key].get(contract)
    _ = np  # numpy imported for chain_synth
    return price


def evaluate_days(model: Model, days: dict, which: list[str], symbol: str, cache: Path) -> dict:
    cells = defaultdict(lambda: defaultdict(list))
    quiet = defaultdict(lambda: defaultdict(list))
    conv = defaultdict(list)
    for d in which:
        day = days[d]
        pts = points(day)
        sA = anchor_sigma(day, model.clock)
        rv = realized_sigma(day, model.clock)
        sB = model.k * rv if (model.k and rv) else None
        if model.sim is None and (sA is None or sB is None):
            continue
        pricer = model
        if model.name == "sim-v1":
            pricer = Model("sim-v1", UNIFORM, None, sim=sim_v1_pricer(cache, f"{symbol}{d}"))
        for t, p in pts.items():
            w = window_of(t)
            for (mny, side), c in contracts_for(p).items():
                rec = mid(p["q"][c])
                if rec is None:
                    continue
                key = (mny, side, w)
                pa = pricer.price(p["spot"], c, t, sA or 0, mny, side)
                pb = pricer.price(p["spot"], c, t, sB or 0, mny, side) if model.sim is None else pa
                if pa is None:
                    continue
                if w:
                    cells[key]["eA"].append((pa - rec) * 100)
                    cells[key]["ratioB"].append(pb / rec if rec > 0 else float("nan"))
                    cells[key]["rec"].append(rec * 100)
                    later = pts.get(t + 30 * 60)
                    if later and c in later["q"] and mid(later["q"][c]) is not None:
                        rec2 = mid(later["q"][c])
                        pa2 = pricer.price(later["spot"], c, t + 1800, sA or 0, mny, side)
                        pb2 = pricer.price(later["spot"], c, t + 1800, sB or 0, mny, side) if model.sim is None else pa2
                        if pa2 is not None:
                            cells[key]["holdA"].append(((pa2 - pa) - (rec2 - rec)) * 100)
                            cells[key]["holdB"].append(((pb2 - pb) - (rec2 - rec)) * 100)
                            cells[key]["hold_rec"].append((rec2 - rec) * 100)
                    q15 = pts.get(t + 15 * 60)
                    if q15 and abs(q15["spot"] - p["spot"]) <= p["spot"] * PROTOCOL["quiet_spot"]["max_abs_move_pct"] / 100 \
                            and c in q15["q"] and mid(q15["q"][c]) is not None:
                        K = strike_of(c)
                        intr = lambda S: max(0.0, S - K) if c[0] == "C" else max(0.0, K - S)  # noqa: E731
                        rec_dx = (mid(q15["q"][c]) - intr(q15["spot"])) - (rec - intr(p["spot"]))
                        m2 = pricer.price(q15["spot"], c, t + 900, sA or 0, mny, side)
                        fixed = pricer.price(p["spot"], c, t + 900, sA or 0, mny, side)   # fixed-spot / less-time
                        if m2 is not None and fixed is not None:
                            mod_dx = (m2 - intr(q15["spot"])) - (pa - intr(p["spot"]))
                            fix_dx = (fixed - intr(p["spot"])) - (pa - intr(p["spot"]))
                            half = ((p["q"][c][1] - p["q"][c][0]) + (q15["q"][c][1] - q15["q"][c][0])) / 4
                            cells[key]["quietA"].append((mod_dx - rec_dx) * 100)
                            quiet[(mny, w)]["rec_dx"].append(rec_dx * 100)
                            quiet[(mny, w)]["model_dx"].append(mod_dx * 100)
                            quiet[(mny, w)]["fixed_spot_dx"].append(fix_dx * 100)
                            quiet[(mny, w)]["within_half_spread"].append(abs(rec_dx) <= half)
                            if model.sim is None and mny == "ATM":
                                ivs = []
                                for (pt, tt) in ((p, t), (q15, t + 900)):
                                    cc = contracts_for(pt)
                                    cm, pm = mid(pt["q"][cc[("ATM", "call")]]), mid(pt["q"][cc[("ATM", "put")]])
                                    Kc = strike_of(cc[("ATM", "call")])
                                    ivs.append(implied_sigma(cm, pt["spot"], Kc, model.clock.tau(tt), "C") if cm else None)
                                if None not in ivs:
                                    vol_part = (bs(q15["spot"], K, ivs[1], model.clock.tau(t + 900), c[0])
                                                - bs(q15["spot"], K, ivs[0], model.clock.tau(t + 900), c[0])) * 100
                                    quiet[(mny, w)]["vol_part"].append(vol_part)
                if t in (15 * 3600 + 45 * 60, 15 * 3600 + 55 * 60) and mny == "ATM":
                    K = strike_of(c)
                    intr = max(0.0, p["spot"] - K) if c[0] == "C" else max(0.0, K - p["spot"])
                    conv[(t, side)].append(((rec - intr) * 100, (pa - intr) * 100))
    return {"cells": cells, "quiet": quiet, "conv": conv}


def med(x):
    x = [v for v in x if v == v]
    return st.median(x) if x else None


def judge(c: dict) -> dict:
    n = len(c["eA"])
    out = {"n": n, "n_hold": len(c["holdA"]), "n_quiet": len(c["quietA"]),
           "recorded_mid_usd_median": med(c["rec"]),
           "A1": med([abs(e) for e in c["eA"]]), "A2": abs(med(c["eA"])) if c["eA"] else None,
           "A2_signed": med(c["eA"]), "A3": med([abs(e) for e in c["holdA"]]),
           "A4": med([abs(e) for e in c["quietA"]]), "B1": med(c["ratioB"]),
           "B2": med([abs(e) for e in c["holdB"]])}
    if n < TOL["min_samples_per_cell_window"]:
        out["verdict"] = "insufficient"
        return out
    checks = {"A1": out["A1"] is not None and out["A1"] <= TOL["A1_median_abs_level_usd"],
              "A2": out["A2"] is not None and out["A2"] <= TOL["A2_abs_median_bias_usd"],
              "A3": out["A3"] is not None and out["A3"] <= TOL["A3_median_abs_hold_pnl_usd"],
              "A4": out["A4"] is not None and out["A4"] <= TOL["A4_median_abs_quiet_change_usd"],
              "B1": out["B1"] is not None and TOL["B1_median_ratio_band"][0] <= out["B1"] <= TOL["B1_median_ratio_band"][1],
              "B2": out["B2"] is not None and out["B2"] <= TOL["B2_median_abs_hold_pnl_usd"]}
    out["checks"] = checks
    out["verdict"] = "pass" if all(checks.values()) else "miss"
    return out


def build_model(name: str, days: dict, dev: list[str], cache: Path) -> tuple[Model, dict]:
    parts = name.split("+")
    base = parts[0]
    fitted = {}
    if base == "sim-v1":
        return Model("sim-v1", UNIFORM, None, sim=lambda *a: None), fitted
    if base == "baseline":
        clock = UNIFORM
    elif base == "candidate":
        w = fit_business_weights(days, dev)
        clock = Clock(w)
        fitted["business_weights_rel"] = [round(x / (sum(w) / BUCKETS), 3) for x in w]
    else:
        raise SystemExit(f"unknown model {base}")
    k = fit_k(days, dev, clock)
    fitted["k_implied_over_realized"] = k
    skew = None
    for corr in parts[1:]:
        if corr not in CORRECTIONS:
            raise SystemExit(f"unknown correction {corr}")
        val = CORRECTIONS[corr](days, dev, clock)
        fitted[f"correction_{corr}"] = val
        if corr == "closetail":
            clock = clock.with_tail(val)
            k = fit_k(days, dev, clock)                  # k refitted on development with the new clock
            fitted["k_implied_over_realized"] = k
        elif corr == "skew":
            skew = val
    return Model(name, clock, k, skew), fitted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="candidate")
    ap.add_argument("--label", required=True, help="why this held-out evaluation is being run")
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--symbols", default="QQQ,SPY")
    ap.add_argument("--set", choices=["held_out", "development", "exploratory"], default="held_out")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cache = Path(args.cache)
    report = {"model": args.model, "label": args.label, "set": args.set,
              "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": {}}
    for symbol in args.symbols.split(","):
        days = load(cache, symbol)
        dev, held, expl = split(symbol, sorted(days))
        which = {"held_out": held, "development": dev, "exploratory": expl}[args.set]
        model, fitted = build_model(args.model, days, dev, cache)
        res = evaluate_days(model, days, which, symbol, cache)
        cells = {}
        for (mny, side, w), c in sorted(res["cells"].items()):
            cells[f"{mny}|{side}|{w}"] = judge(c)
        quiet = {f"{m}|{w}": {"n": len(q["rec_dx"]), "recorded_dx_median": med(q["rec_dx"]),
                              "model_dx_median": med(q["model_dx"]), "fixed_spot_less_time_dx_median": med(q["fixed_spot_dx"]),
                              "vol_part_median": med(q.get("vol_part", [])),
                              "share_within_half_spread": (sum(q["within_half_spread"]) / len(q["within_half_spread"]))
                              if q["within_half_spread"] else None}
                 for (m, w), q in sorted(res["quiet"].items())}
        conv = {f"{t // 3600:02d}:{t % 3600 // 60:02d}|{side}": {"n": len(v), "recorded_extrinsic_median": med([a for a, _ in v]),
                                                                 "model_extrinsic_median": med([b for _, b in v])}
                for (t, side), v in sorted(res["conv"].items())}
        report["symbols"][symbol] = {
            "development_days": len(dev), "evaluated_days": which, "fitted_on_development": fitted,
            "cells": cells, "quiet_spot": quiet, "expiration_convergence": conv,
            "unsupported": ["next-session expiry: " + PROTOCOL["unsupported"]["next-session"]],
            "summary": {v: sum(1 for c in cells.values() if c["verdict"] == v) for v in ("pass", "miss", "insufficient")}}
    text = json.dumps(report, indent=1, default=lambda o: None)
    if args.out:
        Path(args.out).write_text(text)
    with (HERE / "attempts.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_at": report["run_at"], "model": args.model, "label": args.label, "set": args.set,
                             "summary": {s: r["summary"] for s, r in report["symbols"].items()},
                             "report_sha256": hashlib.sha256(text.encode()).hexdigest()}) + "\n")
    for s, r in report["symbols"].items():
        print(f"== {s} [{args.model} on {args.set}, {len(r['evaluated_days'])} days] {r['summary']}  fitted k={r['fitted_on_development'].get('k_implied_over_realized')}")
        for name, c in r["cells"].items():
            f = lambda x, d=2: "-" if x is None else f"{x:.{d}f}"  # noqa: E731
            print(f"  {name:22s} n={c['n']:5d} {c['verdict']:12s} A1={f(c['A1'])} A2={f(c['A2_signed'])} A3={f(c['A3'])} "
                  f"A4={f(c['A4'])} B1={f(c['B1'], 3)} B2={f(c['B2'])} rec_mid=${f(c['recorded_mid_usd_median'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
