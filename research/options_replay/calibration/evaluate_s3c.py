#!/usr/bin/env python3
"""S3c: fit the combined model on development days and evaluate it per S3C_PROTOCOL.md.

    python research/options_replay/calibration/evaluate_s3c.py --fit
    python research/options_replay/calibration/evaluate_s3c.py --set development --label "..."
    python research/options_replay/calibration/evaluate_s3c.py --set held_out --label "..."

--fit writes calibration/s3c_params.json (fit order: clock weights -> post-close tail ->
volatility response beta -> skew a,b -> level k -> spreads), using development days only.
Evaluation computes the S3b checks (A1-A4, B1, B2) and the S3c checks (S1, S2, C1, P1, U1, Q1,
E1, N1) for the combined model, with the S3b baseline and candidate beside it. Every run is
appended to calibration/s3c_attempts.jsonl. Held-out sessions are the gated collector sessions
from 2026-11-16 on; until 10 per symbol exist, --set held_out refuses to report a result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import archive as A  # noqa: E402
import evaluate as E  # noqa: E402
import pricing as PR  # noqa: E402
import pricing_dev2 as PD  # noqa: E402

PROT = json.loads((HERE / "s3c_protocol.json").read_text())
TOL = PROT["tolerance"]
WINDOWS = {k: (int(a[:2]) * 3600 + int(a[3:]) * 60, int(b[:2]) * 3600 + int(b[3:]) * 60)
           for k, (a, b) in PROT["cells"]["windows"].items()}
HELD_FROM = PROT["split"]["held_out"]["from"]
MIN_SESSIONS = PROT["split"]["held_out"]["min_sessions_per_symbol"]
ANCHOR_T = 9 * 3600 + 45 * 60


def load(cache: Path, symbol: str) -> dict:
    out = {}
    for f in sorted((cache / "calibration_s3c").glob(f"{symbol}_*.json")):
        d = json.loads(f.read_text())
        out[d["day"]] = d
    return out


def split(days: list[str]) -> tuple[list[str], list[str]]:
    return [d for d in days if d < HELD_FROM], [d for d in days if d >= HELD_FROM]


def win(t: int) -> str | None:
    for k, (a, b) in WINDOWS.items():
        if a <= t <= b:
            return k
    return None


pts = E.points
mid = E.mid
sk = E.strike_of
med = E.med


def straddle(p: dict):
    c = E.contracts_for(p)
    cm, pm = mid(p["q"][c[("ATM", "call")]]), mid(p["q"][c[("ATM", "put")]])
    if cm is None or pm is None:
        return None
    return sk(c[("ATM", "call")]), cm + pm


def invert_sigma0(m: PR.SymbolModel, p: dict, t: int, S_ref: float) -> float | None:
    """sigma0 such that the model's ATM straddle equals the recorded one at (p, t)."""
    st_ = straddle(p)
    if not st_:
        return None
    K, target = st_
    f = lambda s: m.mid(p["spot"], K, "C", t, s, S_ref) + m.mid(p["spot"], K, "P", t, s, S_ref) - target  # noqa: E731
    lo, hi = 1e-4, 10.0
    if f(lo) > 0 or f(hi) < 0:
        return None
    for _ in range(70):
        x = 0.5 * (lo + hi)
        lo, hi = (x, hi) if f(x) < 0 else (lo, x)
    return 0.5 * (lo + hi)


def anchor(m, day):
    P = pts(day)
    p = P.get(ANCHOR_T)
    if not p:
        return None, None
    return invert_sigma0(m, p, ANCHOR_T, p["spot"]), p["spot"]


def realized(m, day):
    P = pts(day)
    ss = cov = 0.0
    for t, p in P.items():
        q = P.get(t + 300)
        if q and t + 300 <= PR.CLOSE:
            ss += math.log(q["spot"] / p["spot"]) ** 2
            cov += m.covered(t, t + 300)
    return math.sqrt(ss / cov * PR.YEAR_DAYS) if cov > 0.5 else None


# ---------------------------------------------------------------------------
# Fitting (development only)
# ---------------------------------------------------------------------------

def fit_symbol(days: dict, dev: list[str], weights_override: list[float] | None = None) -> dict:
    # 1. clock weights from development spot returns inside the session
    acc = defaultdict(list)
    for d in dev:
        P = pts(days[d])
        for t, p in P.items():
            q = P.get(t + 300)
            if q and t + 300 <= PR.CLOSE:
                acc[(t - PR.OPEN) // 300].append(math.log(q["spot"] / p["spot"]) ** 2)
    w = [st.mean(acc[b]) if len(acc[b]) >= 5 else None for b in range(PR.BUCKETS)]
    first = next(i for i, x in enumerate(w) if x is not None)
    last = max(i for i, x in enumerate(w) if x is not None)
    w = [w[first] if (x is None and i < first) else w[last] if (x is None and i > last) else x for i, x in enumerate(w)]
    w = [x if x is not None else st.mean([y for y in w[max(0, i - 2):i + 3] if y is not None]) for i, x in enumerate(w)]
    if weights_override is not None:
        w = list(weights_override)
    params = {"weights": w, "tail": 0.0, "beta": 0.0, "skew_a": 0.0, "skew_b": 0.0, "k": None, "spread": {}}

    # 2. post-close tail: model ATM extrinsic at 16:05 (anchored sigma0) = recorded median
    def tail_fit():
        samples = []
        for d in dev:
            P = pts(days[d])
            p = P.get(16 * 3600 + 5 * 60)
            if p and straddle(p):
                K, s = straddle(p)
                samples.append((d, p, K, s - abs(p["spot"] - K)))
        target = st.median(x[3] for x in samples)

        def model_med(tail):
            mm = PR.SymbolModel({**params, "tail": tail})
            vals = []
            for d, p, K, _ in samples:
                s0, ref = anchor(mm, days[d])
                if s0:
                    vals.append(mm.mid(p["spot"], K, "C", p["t"], s0, ref) + mm.mid(p["spot"], K, "P", p["t"], s0, ref)
                                - abs(p["spot"] - K))
            return st.median(vals)
        lo, hi = 0.0, 0.3
        for _ in range(35):
            x = 0.5 * (lo + hi)
            lo, hi = (x, hi) if model_med(x) < target else (lo, x)
        return 0.5 * (lo + hi), target
    params["tail"], params["tail_target_straddle_extrinsic_1605"] = tail_fit()

    # 3. volatility response: slope of d ln(ATM vol) on d ln S since the 09:45 anchor
    m = PR.SymbolModel(params)
    xs, ys = [], []
    for d in dev:
        P = pts(days[d])
        s0, ref = anchor(m, days[d])
        if not s0:
            continue
        for t, p in P.items():
            if t <= ANCHOR_T or t > 15 * 3600 + 45 * 60:
                continue
            s = invert_sigma0(m, p, t, p["spot"])          # beta=0 here, so this is the ATM vol level at t
            if s:
                xs.append(math.log(p["spot"] / ref))
                ys.append(math.log(s / s0))
    params["beta"] = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs) if xs else 0.0
    params["beta_n"] = len(xs)

    # 3b (development revision D2). Time-of-day drift: the median residual log vol per 30-minute
    # bucket after the spot response. Real ATM vol drifts within the session independently of spot.
    m = PR.SymbolModel(params)
    res = defaultdict(list)
    for d in dev:
        P = pts(days[d])
        s0, ref = anchor(m, days[d])
        if not s0:
            continue
        for t, p in P.items():
            if t < ANCHOR_T or t > 16 * 3600 + 10 * 60:
                continue
            s = invert_sigma0(m, p, t, ref)      # sigma0 that reproduces the straddle given the beta response
            if s:
                b = t // 1800 * 1800
                res[f"{b // 3600:02d}:{b % 3600 // 60:02d}"].append(math.log(s / s0))
    params["gamma"] = {k: st.median(v) for k, v in sorted(res.items()) if len(v) >= 20}

    # 4. skew/convexity from out-of-the-money quotes relative to the ATM vol at the same point
    m = PR.SymbolModel(params)
    zs, rs = [], []
    for d in dev:
        for t, p in pts(days[d]).items():
            if t > 15 * 3600 + 45 * 60:
                continue
            satm = invert_sigma0(m, p, t, p["spot"])
            if not satm:
                continue
            tau = m.tau(t)
            for c, q in p["q"].items():
                K = sk(c)
                if (c[0] == "C" and K <= p["spot"]) or (c[0] == "P" and K >= p["spot"]):
                    continue                                   # OTM side only
                mm_ = mid(q)
                if mm_ is None or mm_ < 0.02:
                    continue
                iv = E.implied_sigma(mm_, p["spot"], K, tau, c[0])
                if iv:
                    z = math.log(K / p["spot"]) / (satm * math.sqrt(tau))
                    if abs(z) <= 3:
                        zs.append(m.zc * math.tanh(z / m.zc))     # same saturated variable the model prices with
                        rs.append(iv / satm - 1.0)
    # least squares r = a z + b z^2
    s11 = sum(z * z for z in zs); s12 = sum(z ** 3 for z in zs); s22 = sum(z ** 4 for z in zs)  # noqa: E702
    t1 = sum(z * r for z, r in zip(zs, rs)); t2 = sum(z * z * r for z, r in zip(zs, rs))  # noqa: E702
    det = s11 * s22 - s12 * s12
    params["skew_a"] = (t1 * s22 - t2 * s12) / det if det else 0.0
    params["skew_b"] = (s11 * t2 - s12 * t1) / det if det else 0.0
    params["skew_n"] = len(zs)

    # 5. level: k = median(anchored sigma0 / same-day realized vol)
    m = PR.SymbolModel(params)
    ks = []
    for d in dev:
        s0, _ = anchor(m, days[d])
        rv = realized(m, days[d])
        if s0 and rv:
            ks.append(s0 / rv)
    params["k"] = st.median(ks)

    # 6. spreads: per window, least squares of recorded half-spread on recorded mid
    by = defaultdict(list)
    for d in dev:
        for t, p in pts(days[d]).items():
            name = next((n for n, a, b in PR.SPREAD_WINDOWS if a <= t < b), None)
            for c, q in p["q"].items():
                m_ = mid(q)
                if m_ is not None:
                    by[name].append((m_, (q[1] - q[0]) / 2))
    spread = {}
    for name, xy in by.items():
        n = len(xy)
        mx = sum(x for x, _ in xy) / n
        my = sum(y for _, y in xy) / n
        sxx = sum((x - mx) ** 2 for x, _ in xy)
        h1 = sum((x - mx) * (y - my) for x, y in xy) / sxx if sxx else 0.0
        spread[name] = [max(0.0, my - h1 * mx), max(0.0, h1)]
    params["spread"] = spread
    return params


# ---------------------------------------------------------------------------
# s3c-dev2 (Amendment 1): four declared corrections, development only
# ---------------------------------------------------------------------------

def implied_clock_weights(days: dict, dev: list[str], realized_w: list[float]) -> list[float]:
    """Correction 'clock': weights from the development median decay of ATM straddle-implied remaining
    variance V(t) = (straddle extrinsic / (0.7979 S))^2, per 5-minute bucket 09:45-16:00. Buckets before
    09:45 keep the realized-return shape, scaled to match the first observed implied buckets."""
    dec = defaultdict(list)
    for d in dev:
        P = pts(days[d])
        for t, p in P.items():
            q = P.get(t + 300)
            if not q or t + 300 > PR.CLOSE:
                continue
            a, b = straddle(p), straddle(q)
            if not a or not b:
                continue
            va = ((a[1] - abs(p["spot"] - a[0])) / (0.7979 * p["spot"])) ** 2
            vb = ((b[1] - abs(q["spot"] - b[0])) / (0.7979 * q["spot"])) ** 2
            dec[(t - PR.OPEN) // 300].append(va - vb)
    w = list(realized_w)
    obs = {b: st.median(v) for b, v in dec.items() if len(v) >= 10}
    first = sorted(obs)[:3]
    scale = st.median(obs[b] for b in first) / st.median(realized_w[b] for b in first)
    for b in range(PR.BUCKETS):
        w[b] = max(obs[b], 1e-12) if b in obs else realized_w[b] * scale
    return w


def fit_dev2(days: dict, dev: list[str], frozen: dict) -> dict:
    # clock correction, then the frozen fitting sequence on the new clock (beta, gamma, skew, k, spreads)
    w = implied_clock_weights(days, dev, frozen["weights"])
    p = fit_symbol(days, dev, weights_override=w)
    p["candidate"] = PD.CANDIDATE
    # volatility-response correction: eta from residual log vol vs log(k RV60 / sigma0)
    m = PD.Dev2Model(p)
    xs, ys = [], []
    for d in dev:
        P = pts(days[d])
        s0, ref = anchor(m, days[d])
        if not s0:
            continue
        m.path = {t: q["spot"] for t, q in P.items()}
        for t, q in P.items():
            if t < 10 * 3600 + 45 * 60 or t > 15 * 3600 + 45 * 60:
                continue
            rv = m.rv60(t)
            s_t = invert_sigma0(m, q, t, ref)       # eta = 0 here: sigma0 that reproduces the straddle at t
            if rv and s_t:
                xs.append(math.log(m.k * rv / s0))
                ys.append(math.log(s_t / s0))
    p["eta"] = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs) if xs else 0.0
    p["eta_n"] = len(xs)
    # expiration correction (modelling assumption): fitted post-close horizon H with its tail
    samples = []
    for d in dev:
        P = pts(days[d])
        for hm in (15 * 3600 + 55 * 60, 16 * 3600 + 5 * 60, 16 * 3600 + 10 * 60):
            q = P.get(hm)
            if q and straddle(q):
                K, sv = straddle(q)
                samples.append((d, hm, q, K, sv - abs(q["spot"] - K)))
    targets = {hm: st.median(x[4] for x in samples if x[1] == hm) for hm in {x[1] for x in samples}}
    best = None
    for H in range(16 * 3600 + 15 * 60, 18 * 3600 + 31 * 60, 900):
        def med_at(tail):
            mm = PD.Dev2Model({**p, "tail": tail, "post_close_horizon_s": H, "eta": 0.0})
            out = defaultdict(list)
            for d, hm, q, K, _ in samples:
                s0, ref = anchor(mm, days[d])
                if s0:
                    out[hm].append(mm.mid(q["spot"], K, "C", hm, s0, ref) + mm.mid(q["spot"], K, "P", hm, s0, ref)
                                   - abs(q["spot"] - K))
            return {k: st.median(v) for k, v in out.items()}
        lo, hi = 0.0, 0.3
        for _ in range(25):
            x = 0.5 * (lo + hi)
            lo, hi = (x, hi) if med_at(x)[16 * 3600 + 5 * 60] < targets[16 * 3600 + 5 * 60] else (lo, x)
        tail = 0.5 * (lo + hi)
        meds = med_at(tail)
        err = sum(abs(meds[k] - targets[k]) for k in targets)
        if best is None or err < best[0]:
            best = (err, H, tail)
    p["post_close_horizon_s"], p["tail"] = best[1], best[2]
    p["post_close_note"] = "modelling assumption: linear decay to a fitted horizon; not a settlement rule"
    # spreads correction: ATM vs non-ATM per window
    for label, cond in (("spread_atm", lambda K, S: abs(K - S) <= 0.5), ("spread_otm", lambda K, S: abs(K - S) > 0.5)):
        by = defaultdict(list)
        for d in dev:
            for t, q in pts(days[d]).items():
                name = next((n for n, a, b in PR.SPREAD_WINDOWS if a <= t < b), None)
                for c, qq in q["q"].items():
                    m_ = mid(qq)
                    if m_ is not None and cond(sk(c), q["spot"]):
                        by[name].append((m_, (qq[1] - qq[0]) / 2))
        tab = {}
        for name, xy in by.items():
            n = len(xy)
            mx = sum(x for x, _ in xy) / n
            my = sum(y for _, y in xy) / n
            sxx = sum((x - mx) ** 2 for x, _ in xy)
            h1 = sum((x - mx) * (y - my) for x, y in xy) / sxx if sxx else 0.0
            tab[name] = [max(0.0, my - max(0.0, h1) * mx), max(0.0, h1)]
        p[label] = tab
    return p


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

class SimV1Model:
    """sim-v1 (S3a donor pricing) behind the evaluator's model interface, with its own generated
    bid/ask. Its quotes do not depend on sigma0, so conditions A and B coincide for it."""
    fixed = True

    def __init__(self, cache: Path, symbol: str, day: str):
        import hashlib as _h
        import simulate as SIM
        from chain_synth import synth_snapshot_rows
        from price_path import ET
        self.SIM, self.rows_fn, self.ET = SIM, synth_snapshot_rows, ET
        _, donors, self.curve = SIM.Simulator(cache)._load_inputs()
        self.donor = donors[int(_h.sha256(f"{symbol}{day}".encode()).hexdigest(), 16) % len(donors)]
        self.memo = {}
        self.clock = PR.SymbolModel({"weights": [1.0] * PR.BUCKETS})

    def tau(self, t):
        return self.clock.tau(t)

    def covered(self, a, b):
        return self.clock.covered(a, b)

    def _board(self, S, t):
        key = (round(S, 4), t)
        if key not in self.memo:
            from datetime import datetime as dt
            tt = min(t, PR.CLOSE - 60)
            now = dt(2000, 1, 3, tt // 3600, tt % 3600 // 60, tzinfo=self.ET)
            rows = self.rows_fn(now, S, self.donor, self.SIM.STRIKE_WINDOW, self.curve, self.donor.total_oi,
                                self.donor.total_volume, self.SIM.LABEL_DATE)
            self.memo[key] = {("C" if r["Type"] == "call" else "P") + A._fmt_strike(r["Strike"]): (r["Bid"], r["Ask"])
                              for r in rows}
        return self.memo[key]

    def quote(self, S, K, side, t, sigma0, S_ref):
        b, a = self._board(S, t).get(side + A._fmt_strike(K), (0.0, 0.01))
        return b, a, (b + a) / 2

    def mid(self, S, K, side, t, sigma0, S_ref):
        return self.quote(S, K, side, t, sigma0, S_ref)[2]


def baseline_params(days: dict, dev: list[str]) -> dict:
    """Elementary baseline in the same interface: uniform clock, flat sigma, no response, no skew,
    one constant half-spread per symbol (development median), k fitted on development."""
    hs = [(q[1] - q[0]) / 2 for d in dev for p in pts(days[d]).values() for q in p["q"].values() if mid(q) is not None]
    p = {"weights": [1.0] * PR.BUCKETS, "tail": 0.0, "beta": 0.0, "skew_a": 0.0, "skew_b": 0.0, "gamma": {},
         "spread": {n: [st.median(hs), 0.0] for n, _, _ in PR.SPREAD_WINDOWS}}
    m = PR.SymbolModel(p)
    ks = []
    for d in dev:
        s0, _ = anchor(m, days[d])
        rv = realized(m, days[d])
        if s0 and rv:
            ks.append(s0 / rv)
    p["k"] = st.median(ks)
    return p


def evaluate(params_sym, days: dict, which: list[str], factory=None, model_cls=None) -> dict:
    m = (model_cls or PR.SymbolModel)(params_sym) if params_sym is not None else None
    cells = defaultdict(lambda: defaultdict(list))
    invariants = {"quotes": 0, "crossed": 0, "negative_bid": 0, "off_grid": 0, "convexity_violations": 0,
                  "max_parity_abs": 0.0, "snapshots": 0}
    endpoint = defaultdict(list)
    for d in which:
        day = days[d]
        P = pts(day)
        if factory is not None:
            m = factory(d)
        if hasattr(m, "path"):
            m.path = {t: q["spot"] for t, q in P.items()}
        if getattr(m, "fixed", False):
            first = P.get(ANCHOR_T)
            if not first:
                continue
            s0A = s0B = 1.0
            ref = first["spot"]
        else:
            s0A, ref = anchor(m, day)
            rv = realized(m, day)
            if not s0A or not rv:
                continue
            s0B = m.k * rv
        for t, p in P.items():
            S = p["spot"]
            # invariants over a +-10 strike ladder
            if t % 900 == 0:
                invariants["snapshots"] += 1
                Ks = [round(S) + i for i in range(-10, 11)]
                calls = [m.quote(S, K, "C", t, s0B, ref) for K in Ks]
                puts = [m.quote(S, K, "P", t, s0B, ref) for K in Ks]
                for b, a, _ in calls + puts:
                    invariants["quotes"] += 1
                    invariants["crossed"] += a <= b
                    invariants["negative_bid"] += b < 0
                    invariants["off_grid"] += (abs(round(b * 100) - b * 100) > 1e-6) or (abs(round(a * 100) - a * 100) > 1e-6)
                cm = [x[2] for x in calls]
                pm = [x[2] for x in puts]
                for i in range(len(Ks)):
                    invariants["max_parity_abs"] = max(invariants["max_parity_abs"], abs(cm[i] - pm[i] - (S - Ks[i])))
                for arr, sign in ((cm, -1), (pm, 1)):
                    for i in range(len(Ks) - 1):
                        if sign * (arr[i + 1] - arr[i]) < -1e-9:
                            invariants["convexity_violations"] += 1
                    for i in range(1, len(Ks) - 1):
                        if arr[i - 1] - 2 * arr[i] + arr[i + 1] < -1e-9:
                            invariants["convexity_violations"] += 1
            w = win(t)
            if not w:
                continue
            for (mny, side), c in E.contracts_for(p).items():
                rq = p["q"][c]
                rec = mid(rq)
                if rec is None:
                    continue
                K = sk(c)
                key = (mny, side, w)
                pa = m.mid(S, K, c[0], t, s0A, ref)
                bB, aB, pb = m.quote(S, K, c[0], t, s0B, ref)
                cells[key]["eA"].append((pa - rec) * 100)
                cells[key]["ratioB"].append(pb / rec if rec > 0 else float("nan"))
                cells[key]["spread_err"].append(abs((aB - bB) / 2 - (rq[1] - rq[0]) / 2))
                if w == "endpoint" and mny == "ATM":
                    endpoint[t].append((pa - rec) * 100)
                later = P.get(t + 1800)
                if later and c in later["q"] and mid(later["q"][c]) is not None:
                    rq2 = later["q"][c]
                    rec2 = mid(rq2)
                    pa2 = m.mid(later["spot"], K, c[0], t + 1800, s0A, ref)
                    b2, a2, pb2 = m.quote(later["spot"], K, c[0], t + 1800, s0B, ref)
                    cells[key]["holdA"].append(((pa2 - pa) - (rec2 - rec)) * 100)
                    cells[key]["holdB"].append(((pb2 - pb) - (rec2 - rec)) * 100)
                    cells[key]["netN"].append(((b2 - aB) - (rq2[0] - rq[1])) * 100)
                q5 = P.get(t + 300)
                if q5 and c in q5["q"] and mid(q5["q"][c]) is not None and abs(q5["spot"] - S) >= S * TOL["U1_min_move_pct"] / 100:
                    rd = (mid(q5["q"][c]) - rec) / (q5["spot"] - S)
                    md = (m.mid(q5["spot"], K, c[0], t + 300, s0A, ref) - pa) / (q5["spot"] - S)
                    if abs(rd) > 1e-6:
                        cells[key]["delta_ratio"].append(md / rd)
                q15 = P.get(t + 900)
                if q15 and abs(q15["spot"] - S) <= S * 0.02 / 100 and c in q15["q"] and mid(q15["q"][c]) is not None:
                    intr = lambda x: max(0.0, x - K) if c[0] == "C" else max(0.0, K - x)  # noqa: E731
                    rec_dx = (mid(q15["q"][c]) - intr(q15["spot"])) - (rec - intr(S))
                    m2 = m.mid(q15["spot"], K, c[0], t + 900, s0A, ref)
                    mod_dx = (m2 - intr(q15["spot"])) - (pa - intr(S))
                    cells[key]["quietA"].append((mod_dx - rec_dx) * 100)
                    # IV movement of the recorded ATM level between the two times
                    sv1 = invert_sigma0(m, p, t, ref)
                    sv2 = invert_sigma0(m, q15, t + 900, ref)
                    if sv1 and sv2:
                        vol_part = m.mid(q15["spot"], K, c[0], t + 900, sv2, ref) - m.mid(q15["spot"], K, c[0], t + 900, sv1, ref)
                        cells[key]["thetaQ"].append((mod_dx - (rec_dx - vol_part)) * 100)
    out = {}
    for (mny, side, w), c in sorted(cells.items()):
        r = {"n": len(c["eA"]), "A1": med([abs(x) for x in c["eA"]]), "A2_signed": med(c["eA"]),
             "A3": med([abs(x) for x in c["holdA"]]), "A4": med([abs(x) for x in c["quietA"]]),
             "B1": med(c["ratioB"]), "B2": med([abs(x) for x in c["holdB"]]),
             "S1": med(c["spread_err"]), "U1": med(c["delta_ratio"]), "Q1": med([abs(x) for x in c["thetaQ"]]),
             "N1": med([abs(x) for x in c["netN"]]), "n_hold": len(c["holdA"]), "n_quiet": len(c["quietA"]),
             "n_delta": len(c["delta_ratio"])}
        checks = {
            "S1": r["S1"] is not None and r["S1"] <= TOL["S1_median_abs_half_spread_usd_per_share"] + 1e-9,
            "U1": r["U1"] is not None and TOL["U1_median_delta_ratio_band"][0] <= r["U1"] <= TOL["U1_median_delta_ratio_band"][1]}
        if w != "endpoint":   # D3: a 30-minute hold opened after 15:50 cannot finish before the 16:15 close
            checks["N1"] = r["N1"] is not None and r["N1"] <= TOL["N1_median_abs_net_profit_usd"]
        if w != "endpoint":
            checks.update({
                "A1": r["A1"] is not None and r["A1"] <= TOL["A1_median_abs_level_usd"],
                "A2": r["A2_signed"] is not None and abs(r["A2_signed"]) <= TOL["A2_abs_median_bias_usd"],
                "A3": r["A3"] is not None and r["A3"] <= TOL["A3_median_abs_hold_pnl_usd"],
                "A4": r["A4"] is not None and r["A4"] <= TOL["A4_median_abs_quiet_change_usd"],
                "B1": r["B1"] is not None and TOL["B1_median_ratio_band"][0] <= r["B1"] <= TOL["B1_median_ratio_band"][1],
                "B2": r["B2"] is not None and r["B2"] <= TOL["B2_median_abs_hold_pnl_usd"],
                "Q1": r["Q1"] is not None and r["Q1"] <= TOL["Q1_median_abs_theta_only_change_usd"]})
        r["checks"] = checks
        r["verdict"] = "insufficient" if r["n"] < TOL["min_samples_per_cell_window"] else (
            "pass" if all(checks.values()) else "miss")
        out[f"{mny}|{side}|{w}"] = r
    e1 = {}
    for t, errs in sorted(endpoint.items()):
        hm = f"{t // 3600:02d}:{t % 3600 // 60:02d}"
        if hm in TOL["E1_times"]:
            v = med([abs(x) for x in errs])
            e1[hm] = {"n": len(errs), "median_abs_usd": v, "pass": v is not None and v <= TOL["E1_median_abs_level_usd"]}
    inv = dict(invariants)
    inv["S2_pass"] = inv["crossed"] == 0 and inv["negative_bid"] == 0 and inv["off_grid"] == 0
    inv["C1_pass"] = inv["convexity_violations"] == 0
    inv["P1_pass"] = inv["max_parity_abs"] <= TOL["P1_max_abs_parity_usd_per_share"]
    return {"cells": out, "E1": e1, "invariants": inv,
            "summary": {v: sum(1 for c in out.values() if c["verdict"] == v) for v in ("pass", "miss", "insufficient")}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--set", choices=["development", "held_out"], default=None)
    ap.add_argument("--label", default=None)
    ap.add_argument("--params", default=str(PR.PARAMS_PATH))
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--out", default=None)
    ap.add_argument("--model", choices=["combined", "baseline", "sim-v1", "dev2"], default="combined")
    ap.add_argument("--fit-dev2", action="store_true", help="fit the s3c-dev2 development candidate (Amendment 1)")
    args = ap.parse_args()
    cache = Path(args.cache)
    if args.fit_dev2:
        frozen = PR.load_params(args.params)
        out = {"candidate": PD.CANDIDATE, "protocol": "S3C_PROTOCOL.md Amendment 1", "base_frozen_params_sha256":
               hashlib.sha256(Path(args.params).read_bytes()).hexdigest(),
               "fitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": {}}
        for symbol in ("QQQ", "SPY"):
            days = load(cache, symbol)
            dev, _ = split(sorted(days))           # forward sessions (>= 2026-11-16) are never read here
            out["symbols"][symbol] = {**fit_dev2(days, dev, frozen["symbols"][symbol]), "development_days": len(dev),
                                      "development_range": [dev[0], dev[-1]]}
        PD.DEV2_PARAMS_PATH.write_text(json.dumps(out, indent=1))
        for sy, p in out["symbols"].items():
            print(sy, {k: (round(v, 5) if isinstance(v, float) else v) for k, v in p.items()
                       if k in ("eta", "eta_n", "tail", "post_close_horizon_s", "beta", "k", "skew_a", "skew_b")})
        return 0
    if args.fit:
        params = {"generator_version": PROT["generator_version"], "protocol": "S3C_PROTOCOL.md",
                  "fitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": {}}
        for symbol in ("QQQ", "SPY"):
            days = load(cache, symbol)
            dev, _ = split(sorted(days))
            params["symbols"][symbol] = {**fit_symbol(days, dev), "development_days": len(dev),
                                         "development_range": [dev[0], dev[-1]]}
        Path(args.params).write_text(json.dumps(params, indent=1))
        for s, p in params["symbols"].items():
            print(s, {k: (round(v, 5) if isinstance(v, float) else v) for k, v in p.items() if k not in ("weights",)})
        return 0
    if not args.set or not args.label:
        raise SystemExit("--set and --label are required for an evaluation run")
    params = PR.load_params(args.params)
    report = {"set": args.set, "label": args.label, "model": args.model, "params_sha256": hashlib.sha256(Path(args.params).read_bytes()).hexdigest(),
              "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": {}}
    for symbol in ("QQQ", "SPY"):
        days = load(cache, symbol)
        dev, held = split(sorted(days))
        which = dev if args.set == "development" else held
        if args.set == "held_out" and len(held) < MIN_SESSIONS:
            report["symbols"][symbol] = {"held_out_sessions": len(held), "status":
                                         f"not evaluable: {len(held)} of {MIN_SESSIONS} required held-out sessions exist"}
            continue
        if args.model == "combined":
            r = evaluate(params["symbols"][symbol], days, which)
        elif args.model == "dev2":
            r = evaluate(PD.load_dev2()["symbols"][symbol], days, which, model_cls=PD.Dev2Model)
        elif args.model == "baseline":
            r = evaluate(baseline_params(days, dev), days, which)
        else:
            r = evaluate(None, days, which, factory=lambda d, s=symbol: SimV1Model(cache, s, d))
        r["days"] = len(which)
        report["symbols"][symbol] = r
    text = json.dumps(report, indent=1, default=lambda o: None)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    with (HERE / "s3c_attempts.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_at": report["run_at"], "set": args.set, "label": args.label, "model": args.model,
                             "params_sha256": report["params_sha256"],
                             "summary": {s: r.get("summary", r.get("status")) for s, r in report["symbols"].items()},
                             "report_sha256": hashlib.sha256(text.encode()).hexdigest()}) + "\n")
    f = lambda x, d=2: "-" if x is None else f"{x:.{d}f}"  # noqa: E731
    for s, r in report["symbols"].items():
        if "cells" not in r:
            print(f"== {s}: {r['status']}")
            continue
        inv = r["invariants"]
        print(f"== {s} [{args.set}, {r['days']} days] {r['summary']}  S2={inv['S2_pass']} C1={inv['C1_pass']} "
              f"(viol {inv['convexity_violations']}) P1={inv['P1_pass']} (max {inv['max_parity_abs']:.2e})  "
              f"E1={ {k: (round(v['median_abs_usd'], 2), v['pass']) for k, v in r['E1'].items()} }")
        for name, c in r["cells"].items():
            miss = [k for k, v in c["checks"].items() if not v]
            print(f"  {name:24s} n={c['n']:5d} {c['verdict']:5s} A1={f(c['A1'])} A2={f(c['A2_signed'])} A3={f(c['A3'])} "
                  f"A4={f(c['A4'])} Q1={f(c['Q1'])} B1={f(c['B1'], 3)} B2={f(c['B2'])} S1={f(c['S1'], 3)} "
                  f"U1={f(c['U1'], 3)} N1={f(c['N1'])} miss={miss}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
