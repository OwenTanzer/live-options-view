"""G5a, G5b, G5c, D1, D2/D3 exactly as frozen in PROTOCOL.md (commit 0e1d8cc).

    python research/oa241/findings/analysis.py FEATURES_DIR > research/oa241/findings/results.json

FEATURES_DIR is the output of features.py (frozen feature version = its manifest.json).
"""
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.linear_model import LogisticRegression

ET = ZoneInfo("America/New_York")
RNG = np.random.default_rng(20261116)
DISCOVERY = {"2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"}
HOLDOUT_FROM = "2026-11-02"
BOOT = 2000


def load(fdir):
    man = json.loads((fdir / "manifest.json").read_text())
    days = {}
    for day in sorted(man["sessions"]):
        z = np.load(fdir / f"{day}.npz")
        cols = {}
        with (fdir / f"{day}.csv").open() as fh:
            head = fh.readline().strip().split(",")
            rows = [line.rstrip("\n").split(",") for line in fh]
        for i, h in enumerate(head):
            vals = [r[i] for r in rows]
            cols[h] = np.array([np.nan if v == "" else (1.0 if v == "True" else 0.0 if v == "False" else float(v)) for v in vals])
        days[day] = dict(z=z, c=cols, meta=man["sessions"][day])
    return man, days


def minute_of(hhmm):
    h, m = map(int, hhmm.split(":"))
    return (h - 9) * 60 + m - 30


def logret(S, a, b):
    return math.log(S[b] / S[a]) if np.isfinite(S[a]) and np.isfinite(S[b]) and S[a] > 0 and S[b] > 0 else math.nan


def rv(S, a, b):
    r = np.diff(np.log(S[a:b + 1]))
    return float(np.sum(r ** 2)) if np.all(np.isfinite(r)) and len(r) == b - a else math.nan


def ols(X, y):
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def design(rows, keys, days_list, day_fe=True):
    X = [[r[k] for k in keys] for r in rows]
    X = np.array(X, dtype=float)
    if day_fe:
        D = np.array([[1.0 if r["day"] == d else 0.0 for d in days_list] for r in rows])
        return np.hstack([X, D])
    return np.hstack([X, np.ones((len(rows), 1))])


def cluster_boot(rows, stat):
    days = sorted({r["day"] for r in rows})
    by = {d: [r for r in rows if r["day"] == d] for d in days}
    vals = []
    for _ in range(BOOT):
        pick = RNG.choice(days, len(days), replace=True)
        sample, relabel = [], []
        for n, d in enumerate(pick):
            for r in by[d]:
                rr = dict(r)
                rr["day"] = f"{d}#{n}"
                sample.append(rr)
        try:
            v = stat(sample)
        except (np.linalg.LinAlgError, ValueError, ZeroDivisionError):
            continue
        if v is not None and np.isfinite(v):
            vals.append(v)
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))] if vals else [math.nan, math.nan]


# ---------------- G5a ----------------
def g5a_rows(days):
    rows, excluded = [], {"missing_return": 0, "zero_rv": 0}
    for day, d in days.items():
        c = d["c"]
        S, dv = c["qqq_mid"], d["z"]["qqq_dv"]
        for hh in ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30"]:
            t = minute_of(hh)
            y_rv, lag_rv = rv(S, t, t + 30), rv(S, t - 30, t)
            if not (np.isfinite(y_rv) and np.isfinite(lag_rv)):
                excluded["missing_return"] += 1
                continue
            if y_rv == 0 or lag_rv == 0:
                excluded["zero_rv"] += 1
                continue
            v1, v0 = dv[t - 29:t + 1].sum(), dv[t - 59:t - 29].sum()
            feats = dict(G=c["G_within_p100_central"][t] / 1e9, Goi=c["G_oi_unsigned"][t] / 1e9,
                         Glow=c["G_within_p100_low"][t] / 1e9, Ghigh=c["G_within_p100_high"][t] / 1e9,
                         Tlow=c["total_low_p100"][t] / 1e9, Thigh=c["total_high_p100"][t] / 1e9)
            if not all(np.isfinite(list(feats.values()))) or v0 <= 0 or v1 <= 0:
                excluded["missing_return"] += 1
                continue
            tod = t / 390
            rows.append(dict(day=day, t=hh, y=math.log(y_rv), lrv=math.log(lag_rv), lret=logret(S, t - 30, t),
                             ldv=math.log(v1 / v0), tod=tod, tod2=tod * tod,
                             act=math.log1p(c["contracts_30m"][t]), **feats))
    return rows, excluded


BASE = ["lrv", "lret", "ldv", "tod", "tod2"]
MODELS = {"M0": BASE, "M1": BASE + ["act"], "M2": BASE + ["act", "Goi"], "M3": BASE + ["act", "Goi", "G"]}


def coef(rows, keys, name):
    days_list = sorted({r["day"] for r in rows})
    X, y = design(rows, keys, days_list), np.array([r["y"] for r in rows])
    return float(ols(X, y)[keys.index(name)])


def loso_mse(rows, keys):
    days_list = sorted({r["day"] for r in rows})
    err = []
    for d in days_list:
        tr = [r for r in rows if r["day"] != d]
        te = [r for r in rows if r["day"] == d]
        beta = ols(design(tr, keys, None, day_fe=False), np.array([r["y"] for r in tr]))
        pred = design(te, keys, None, day_fe=False) @ beta
        err += list((np.array([r["y"] for r in te]) - pred) ** 2)
    return float(np.mean(err))


def g5a(days):
    rows, excluded = g5a_rows(days)

    def summary(rs):
        out = dict(n=len(rs), sessions=len({r["day"] for r in rs}))
        if out["sessions"] < 3:
            return out
        out["coef_G_M3"] = coef(rs, MODELS["M3"], "G")
        out["coef_G_M3_ci95"] = cluster_boot(rs, lambda s: coef(s, MODELS["M3"], "G"))
        mse = {m: loso_mse(rs, k) for m, k in MODELS.items()}
        out["loso_mse"] = mse
        out["loso_mse_reduction_M3_vs_M2"] = 1 - mse["M3"] / mse["M2"]
        out["loso_mse_reduction_M2_vs_M1"] = 1 - mse["M2"] / mse["M1"]
        out["loso_mse_reduction_M1_vs_M0"] = 1 - mse["M1"] / mse["M0"]
        out["coef_Goi_M3"] = coef(rs, MODELS["M3"], "Goi")
        out["coef_Goi_M3_ci95"] = cluster_boot(rs, lambda s: coef(s, MODELS["M3"], "Goi"))
        per_day = []
        for d in sorted({r["day"] for r in rs}):
            sub = [r for r in rs if r["day"] == d]
            if len(sub) >= 9:
                X = design(sub, MODELS["M3"], None, day_fe=False)
                per_day.append(float(ols(X, np.array([r["y"] for r in sub]))[MODELS["M3"].index("G")]))
        out["per_session_coef_G_negative_share"] = float(np.mean(np.array(per_day) < 0)) if per_day else None
        out["per_session_fits"] = len(per_day)
        out["G_iqr_bn"] = [float(np.percentile([r["G"] for r in rs], q)) for q in (25, 75)]
        return out
    res = dict(excluded=excluded, all=summary(rows), without_discovery=summary([r for r in rows if r["day"] not in DISCOVERY]))
    # holdout: fit on <= Oct 30, evaluate Nov 2-16 (pooled intercept)
    tr = [r for r in rows if r["day"] < HOLDOUT_FROM]
    te = [r for r in rows if r["day"] >= HOLDOUT_FROM]
    hold = {}
    for m in ("M2", "M3"):
        beta = ols(design(tr, MODELS[m], None, day_fe=False), np.array([r["y"] for r in tr]))
        hold[m] = float(np.mean((np.array([r["y"] for r in te]) - design(te, MODELS[m], None, day_fe=False) @ beta) ** 2))
    res["holdout"] = dict(n=len(te), sessions=len({r["day"] for r in te}), mse=hold, reduction_M3_vs_M2=1 - hold["M3"] / hold["M2"])
    sens = {}
    for alt in ("Glow", "Ghigh", "Tlow", "Thigh"):
        keys = BASE + ["act", "Goi", alt]
        sens[alt] = dict(coef=coef(rows, keys, alt), ci95=cluster_boot(rows, lambda s, k=keys, a=alt: coef(s, k, a)))
    res["scenario_sensitivity"] = sens
    return res


# ---------------- G5b ----------------
def g5b(days, anchors):
    rows = []
    for day, d in days.items():
        c, pc = d["c"], d["meta"]["prev_close"]
        S = c["qqq_mid"]
        for hh in anchors:
            t = minute_of(hh)
            h = min(t + 60, 390)
            if not (np.isfinite(S[t]) and np.isfinite(S[h]) and pc and np.isfinite(pc)):
                continue
            G, Goi = c["G_within_p100_central"][t] / 1e9, c["G_oi_unsigned"][t] / 1e9
            if not (np.isfinite(G) and np.isfinite(Goi)):
                continue
            sig = np.sign(math.log(S[t] / pc))
            tod = t / 390
            rows.append(dict(day=day, t=hh, y=float(sig * (S[h] / S[t] - 1)), G=G, Goi=Goi, tod=tod, tod2=tod * tod))
    keys = ["G", "Goi", "tod", "tod2"]

    def b(rs):
        return float(ols(design(rs, keys, None, day_fe=False), np.array([r["y"] for r in rs]))[0])
    out = dict(n=len(rows), sessions=len({r["day"] for r in rows}))
    if len(rows) < 10:
        return out
    out["b_G"] = b(rows)
    out["b_G_ci95"] = cluster_boot(rows, b)
    q = np.percentile([r["G"] for r in rows], [25, 50, 75])
    out["G_iqr_bn"] = [float(q[0]), float(q[2])]
    out["effect_iqr_pct"] = float(out["b_G"] * (q[2] - q[0]) * 100)
    bins = np.digitize([r["G"] for r in rows], q)
    out["mean_M_by_quartile_pct"] = [dict(quartile=i + 1, n=int(np.sum(bins == i)),
                                          mean=float(np.mean([r["y"] for r, bb in zip(rows, bins) if bb == i]) * 100))
                                     for i in range(4)]
    nd = [r for r in rows if r["day"] not in DISCOVERY]
    out["without_discovery"] = dict(n=len(nd), b_G=b(nd), ci95=cluster_boot(nd, b))
    return out


# ---------------- G5c ----------------
def strike_series(d):
    z = d["z"]
    S = z["spot"][:, None]
    w = 100.0 * z["gamma"] * S ** 2
    q = z["cum"][..., 1] - z["cum"][..., 0]
    gq = np.where(np.isfinite(w), q * w, np.nan)
    goi = np.where(np.isfinite(w), z["open_oi"][None, :] * w, np.nan)
    strikes = np.array(d["meta"]["strikes"])
    ks = sorted(set(strikes))
    G = np.stack([np.nansum(np.where(strikes == k, gq, 0), axis=1) for k in ks], axis=1)
    Goi = np.stack([np.nansum(np.where(strikes == k, goi, 0), axis=1) for k in ks], axis=1)
    covered = np.stack([np.any(np.isfinite(gq[:, strikes == k]), axis=1) for k in ks], axis=1)
    return np.array(ks), np.where(covered, G, np.nan), np.where(covered, Goi, np.nan)


def g5c(days):
    events = {"signed": [], "unsigned": []}
    unusable = {"signed": 0, "unsigned": 0}
    for day, d in days.items():
        S = d["c"]["qqq_mid"]
        ks, G, Goi = strike_series(d)
        for t in range(minute_of("09:45"), minute_of("15:50") + 1, 5):
            if not (np.isfinite(S[t]) and np.isfinite(S[t + 5])):
                continue
            dist = np.abs(S[t] - ks)
            for kind, X in (("signed", G), ("unsigned", Goi)):
                elig = (dist <= 2.0) & np.isfinite(X[t])
                if not elig.any():
                    continue
                i = int(np.argmax(np.where(elig, np.abs(X[t]), -np.inf)))
                cand = [j for j in range(len(ks)) if j != i and abs(dist[j] - dist[i]) <= 0.5]
                if not cand:
                    unusable[kind] += 1
                    continue
                j = min(cand, key=lambda j: abs(dist[j] - dist[i]))
                D = lambda k: abs(S[t + 5] - k) - abs(S[t] - k)  # noqa: E731
                events[kind].append(dict(day=day, delta=float(D(ks[i]) - D(ks[j])), sign=float(np.sign(X[t, i]))))
    out = {}
    for kind, ev in events.items():
        def stat(rs, s=None):
            sel = [r["delta"] for r in rs if s is None or r["sign"] == s]
            return float(np.mean(sel)) if sel else math.nan

        def contrast(rs):
            return stat(rs, 1.0) - stat(rs, -1.0)
        res = dict(events=len(ev), unusable=unusable[kind], sessions=len({e["day"] for e in ev}),
                   mean_delta_all=stat(ev), mean_delta_all_ci95=cluster_boot(ev, stat))
        if kind == "signed":
            res.update(long_gamma=dict(n=sum(e["sign"] > 0 for e in ev), mean=stat(ev, 1.0), ci95=cluster_boot(ev, lambda r: stat(r, 1.0))),
                       short_gamma=dict(n=sum(e["sign"] < 0 for e in ev), mean=stat(ev, -1.0), ci95=cluster_boot(ev, lambda r: stat(r, -1.0))),
                       contrast=contrast(ev), contrast_ci95=cluster_boot(ev, contrast))
            nd = [e for e in ev if e["day"] not in DISCOVERY]
            res["without_discovery"] = dict(n=len(nd), contrast=contrast(nd), ci95=cluster_boot(nd, contrast))
        out[kind] = res
    return out


# ---------------- D1 ----------------
def d1(days):
    out = {}
    for day in ("2026-10-07", "2026-10-08"):
        if day not in days:
            out[day] = "session not in the complete sample"
            continue
        c = days[day]["c"]
        S = c["qqq_mid"]
        drops = [(logret(S, t, t + 15), t) for t in range(0, 375) if np.isfinite(logret(S, t, t + 15))]
        worst, onset = min(drops)
        def at(t):
            return dict(time=f"{9 + (30 + t) // 60:02d}:{(30 + t) % 60:02d}", spot=None if not np.isfinite(S[t]) else round(float(S[t]), 3),
                        put_flow_30m=c["signed_put_flow_30m"][t], call_flow_30m=c["signed_call_flow_30m"][t],
                        contracts_30m=c["contracts_30m"][t], G_within_bn=round(float(c["G_within_p100_central"][t] / 1e9), 3))
        timeline = [at(t) for t in range(0, 391, 15)]
        pre = [at(t) for t in range(max(0, onset - 30), onset + 16, 5)]
        base = np.nanmedian(np.abs(np.diff(c["signed_put_flow_30m"][max(0, onset - 120):max(1, onset - 30)])))
        lead = [t for t in range(max(0, onset - 30), onset - 4)
                if abs(c["signed_put_flow_30m"][t] - c["signed_put_flow_30m"][t - 5]) > 3 * max(base, 1)]
        out[day] = dict(largest_15min_drop_pct=round(worst * 100, 3), onset=at(onset)["time"], timeline_15min=timeline,
                        around_onset_5min=pre,
                        put_flow_jump_at_least_5min_before_onset=[at(t)["time"] for t in lead],
                        cannot_resolve=["16 contracts at 8 strikes only (no 748-750 puts unless selected)",
                                        "participant identity and opening/closing status", "aggressor side is inferred",
                                        "two selected days cannot establish predictive validity"])
        out[day]["universe_strikes"] = sorted(set(days[day]["meta"]["strikes"]))
    return out


# ---------------- D2/D3 ----------------
def d_rows(days):
    rows = []
    for day, d in days.items():
        c, z = d["c"], d["z"]
        S = c["qqq_mid"]
        strikes = np.array(d["meta"]["strikes"])
        puts = np.array([s[9] == "P" for s in d["meta"]["symbols"]])
        for t in range(minute_of("10:00"), minute_of("15:40") + 1, 5):
            f15, b15, b5, b30 = logret(S, t, t + 15), logret(S, t - 15, t), logret(S, t - 5, t), logret(S, t - 30, t)
            r30 = rv(S, t - 30, t)
            if not all(np.isfinite([f15, b15, b5, b30, r30])) or r30 <= 0:
                continue
            w = 100.0 * z["gamma"][t] * z["spot"][t] ** 2
            goi = z["open_oi"] * w
            tot = np.nansum(goi)
            below = np.nansum(np.where(puts & (strikes < S[t]), goi, 0))
            G = c["G_within_p100_central"][t]
            if not (np.isfinite(G) and tot > 0):
                continue
            tod = t / 390
            rows.append(dict(day=day, y=int(f15 <= -0.0025 and f15 < b15), b5=b5, b15=b15, b30=b30, lrv=math.log(r30),
                             tod=tod, tod2=tod * tod, act=math.log1p(c["contracts_30m"][t]), put_below=below / tot,
                             pflow=c["signed_put_flow_30m"][t], cflow=c["signed_call_flow_30m"][t], G=G / 1e9))
    return rows


D_BASE = ["b5", "b15", "b30", "lrv", "tod", "tod2", "act"]
D_AUG = D_BASE + ["put_below", "pflow", "cflow", "G"]


def d23(days):
    rows = d_rows(days)
    dev = [r for r in rows if r["day"] < HOLDOUT_FROM]
    hold = [r for r in rows if r["day"] >= HOLDOUT_FROM]

    def fit(rs, keys):
        X = np.array([[r[k] for k in keys] for r in rs])
        mu, sd = X.mean(0), X.std(0) + 1e-12
        m = LogisticRegression(C=1.0, max_iter=2000).fit((X - mu) / sd, [r["y"] for r in rs])
        return lambda q: m.predict_proba((np.array([[r[k] for k in keys] for r in q]) - mu) / sd)[:, 1]
    out = dict(dev_anchors=len(dev), dev_sessions=len({r["day"] for r in dev}), dev_positives=sum(r["y"] for r in dev),
               holdout_anchors=len(hold), holdout_sessions=len({r["day"] for r in hold}), holdout_positives=sum(r["y"] for r in hold))
    if not dev or not hold or len({r["y"] for r in dev}) < 2:
        out["verdict_reason"] = "no usable development events"
        return out
    pb, pa = fit(dev, D_BASE), fit(dev, D_AUG)
    yb, ya = pb(hold), pa(hold)
    y = np.array([r["y"] for r in hold])
    out["brier_base"] = float(np.mean((yb - y) ** 2))
    out["brier_aug"] = float(np.mean((ya - y) ** 2))
    days_h = sorted({r["day"] for r in hold})
    diffs = []
    for d in days_h:
        m = np.array([r["day"] == d for r in hold])
        diffs.append(float(np.mean((ya[m] - y[m]) ** 2) - np.mean((yb[m] - y[m]) ** 2)))
    diffs = np.array(diffs)
    out["paired_daily_diff_mean"] = float(diffs.mean())
    boots = [RNG.choice(diffs, len(diffs)).mean() for _ in range(BOOT)]
    out["paired_daily_diff_ci95"] = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
    out["useful_threshold"] = -0.05 * out["brier_base"]
    # threshold maximizing development F1 (in-sample on dev), then holdout precision/recall
    pdv = pa(dev)
    ydv = np.array([r["y"] for r in dev])
    best = max(np.linspace(0.02, 0.8, 79), key=lambda th: f1(ydv, pdv >= th))
    alerts = ya >= best
    out["threshold"] = float(best)
    out["holdout_precision"] = float(y[alerts].mean()) if alerts.any() else None
    out["holdout_recall"] = float(alerts[y == 1].mean()) if (y == 1).any() else None
    out["false_alerts_per_session"] = float(np.sum(alerts & (y == 0)) / len(days_h))
    # power memo from development: daily paired SD under walk-forward is unavailable; use holdout daily SD
    sd = float(diffs.std(ddof=1)) if len(diffs) > 1 else math.nan
    out["daily_diff_sd"] = sd
    out["event_rate_dev"] = float(np.mean(ydv))
    out["required_sessions"] = {f"d={dd}": math.ceil(7.85 / dd ** 2) for dd in (0.3, 0.2, 0.1)}
    out["observed_d"] = float(abs(diffs.mean()) / sd) if sd and np.isfinite(sd) and sd > 0 else None
    return out


def f1(y, a):
    tp = np.sum(a & (y == 1))
    return 0 if tp == 0 else 2 * tp / (np.sum(a) + np.sum(y == 1))


if __name__ == "__main__":
    fdir = Path(sys.argv[1])
    man, days = load(fdir)
    res = dict(protocol_commit="0e1d8cc", feature_manifest_sha256=__import__("hashlib").sha256((fdir / "manifest.json").read_bytes()).hexdigest(),
               sessions=sorted(days), excluded_sessions=man["excluded"], discovery=sorted(DISCOVERY), holdout_from=HOLDOUT_FROM,
               G5a=g5a(days),
               G5b=dict(primary=g5b(days, ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30"]),
                        hourly_nonoverlapping=g5b(days, ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00"])),
               G5c=g5c(days), D1=d1(days), D3=d23(days))
    print(json.dumps(res, indent=1, default=float))
