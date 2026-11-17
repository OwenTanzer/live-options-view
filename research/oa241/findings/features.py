"""G4: per-minute causal exposure features for every complete Tradier session (PROTOCOL.md).

    python research/oa241/findings/features.py ARCHIVE SNAPSHOTS SESSIONS.json OUTDIR

SESSIONS.json: {date: {"summaries": [{"owner", "status", ...}]}} (read-only R2 listing).
Writes OUTDIR/{date}.npz (minute arrays) and OUTDIR/{date}.csv (G4 aggregate export) plus
OUTDIR/manifest.json with input manifests and output digests (the frozen feature version).
No price outcome is computed here.
"""
import csv
from datetime import datetime, timedelta
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[3]
MAX_SNAP_AGE = 120_000
QUOTE_MAX_AGE = 60_000
N = 391  # minute boundaries 09:30 .. 16:00


def load_recent():
    spec = importlib.util.spec_from_file_location("recent", ROOT / "market_data/tradier/recent.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ms(dt):
    return int(dt.timestamp() * 1000)


def rec_ms(s):
    return ms(datetime.fromisoformat(s))


def snapshots(root, day, symbols):
    """Chain snapshots for the explicit, ordered contract keys ``symbols``.

    Every per-contract array is built by indexing a symbol-keyed row map with that
    ordered list, so column i always belongs to symbols[i]. Returns the snapshot
    tuples and a digest over (file name, sha256) of every snapshot file read.
    """
    assert isinstance(symbols, (list, tuple)), "symbols must be an ordered sequence, never a set"
    keys = set(symbols)
    compact = day.replace("-", "")
    out, digests = [], []
    for p in sorted((root / "intraday" / compact).glob("snapshot_*.csv")):
        digests.append(f"{p.name}:{hashlib.sha256(p.read_bytes()).hexdigest()}")
        h = p.stem.removeprefix("snapshot_")
        t = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), int(h[:2]), int(h[2:4]), int(h[4:6]),
                     int(h[6:12].ljust(6, "0")), tzinfo=ET)
        rows = {r["OptionSymbol"]: r for r in csv.DictReader(p.open(newline="")) if r.get("OptionSymbol") in keys}

        def num(r, k):
            try:
                v = float(r[k])
            except (KeyError, TypeError, ValueError):
                return math.nan
            return v if math.isfinite(v) and v >= 0 else math.nan
        g = [num(rows[s], "Gamma") if s in rows else math.nan for s in symbols]
        d = [abs(float(rows[s]["Delta"])) if s in rows and rows[s].get("Delta") not in (None, "") else math.nan for s in symbols]
        oi = [num(rows[s], "OpenInterest") if s in rows else math.nan for s in symbols]
        spots = {num(r, "UnderlyingPrice") for r in rows.values()}
        spots = {x for x in spots if not math.isnan(x)}
        out.append((ms(t), g, d, oi, spots.pop() if len(spots) == 1 else math.nan))
    return out, hashlib.sha256(chr(10).join(digests).encode()).hexdigest()


def previous_close(root, day):
    d = datetime.fromisoformat(day)
    for back in range(1, 6):
        prev = d - timedelta(days=back)
        if prev.weekday() >= 5:
            continue
        folder = root / "intraday" / prev.strftime("%Y%m%d")
        files = [p for p in sorted(folder.glob("snapshot_*.csv")) if p.stem.removeprefix("snapshot_")[:6] <= "160000"]
        if files:
            for r in csv.DictReader(files[-1].open(newline="")):
                try:
                    return float(r["UnderlyingPrice"]), prev.date().isoformat(), files[-1].name
                except (KeyError, ValueError):
                    continue
    return math.nan, None, None


def build(archive, snaproot, day, owner, recent):
    manifest_path = archive / f"moo144/tradier/{day}/manifest-{owner}.json"
    manifest_raw = manifest_path.read_bytes()
    summary = json.loads((manifest_path.parent / f"summary-{owner}.json").read_bytes())
    symbols = sorted(summary["universe"]["option_metadata"])
    meta = summary["universe"]["option_metadata"]
    idx = {s: i for i, s in enumerate(symbols)}
    open_ms = rec_ms(summary["session_open"])
    bounds = open_ms + 60_000 * np.arange(N)
    cum = np.zeros((N, 16, 3))           # B, S, U through boundary (receipt time)
    qmid = np.full(N, np.nan)
    qqq_dv = np.zeros(N)                 # dollar volume received in (b-1, b]
    state = np.zeros((16, 3))
    last_quote = (None, math.nan)
    k = 0
    feed = recent.RecentPrints()

    def advance(upto):
        nonlocal k
        while k < N and bounds[k] < upto:
            cum[k] = state
            qt, qm = last_quote
            qmid[k] = qm if qt is not None and bounds[k] - qt <= QUOTE_MAX_AGE else math.nan
            k += 1
    digest_ok = 0
    for art in sorted(json.loads(manifest_raw)["artifacts"], key=lambda a: a["key"]):
        data = (archive / art["key"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == art["sha256"], art["key"]
        digest_ok += 1
        if "records" not in art:
            continue
        for line in gzip.decompress(data).splitlines():
            e = json.loads(line)
            r = e.get("collector_receipt_timestamp")
            if r is None:
                continue
            rm = rec_ms(r)
            advance(rm)
            if e.get("symbol") == "QQQ":
                if e["type"] == "quote":
                    try:
                        bid, ask = float(e["bid"]), float(e["ask"])
                        if 0 < bid < ask:
                            last_quote = (rm, (bid + ask) / 2)
                    except (KeyError, TypeError, ValueError):
                        pass
                elif e["type"] == "timesale" and not any(e.get(x) for x in ("cancel", "correction", "duplicate_in_run")):
                    b = min(N - 1, max(0, math.ceil((rm - open_ms) / 60_000)))
                    qqq_dv[b] += float(e["last"]) * int(e["size"])
                continue
            before = feed.session.prints if feed.session else 0
            feed.observe(e)
            if feed.session is not None and feed.session.prints > before:
                row = feed.rows[-1]
                state[idx[row["symbol"]], ("buy", "sell", "unknown").index(row["side"])] += row["size"]
    advance(bounds[-1] + 1)
    snaps, snapshot_digest = snapshots(snaproot, day, symbols)
    st = np.array([s[0] for s in snaps])
    gamma = np.full((N, 16), np.nan)
    delta = np.full((N, 16), np.nan)
    oi = np.full((N, 16), np.nan)
    spot = np.full(N, np.nan)
    age = np.full(N, np.nan)
    for i, b in enumerate(bounds):
        j = np.searchsorted(st, b, side="right") - 1
        if j >= 0 and b - st[j] <= MAX_SNAP_AGE:
            _, g, d, o, s = snaps[j]
            gamma[i], delta[i], oi[i], spot[i], age[i] = g, d, o, s, b - st[j]
    j0 = np.searchsorted(st, open_ms, side="right") - 1
    open_oi = np.array(snaps[j0][3]) if j0 >= 0 else np.full(16, np.nan)
    pc, pc_day, pc_file = previous_close(snaproot, day)
    totals = cum[-1].sum(axis=0)
    return dict(
        day=day, owner=owner, manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(), segments_verified=digest_ok,
        symbols=symbols, strikes=[meta[s]["strike"] for s in symbols], types=[meta[s]["option_type"][0].upper() for s in symbols],
        bounds=bounds, cum=cum, qmid=qmid, qqq_dv=qqq_dv, gamma=gamma, delta=delta, oi=oi, spot=spot, snap_age=age,
        open_oi=open_oi, prev_close=pc, prev_close_source=f"{pc_day}/{pc_file}", snapshots=len(snaps),
        snapshot_digest=snapshot_digest,
        accepted_contracts=dict(buy=int(totals[0]), sell=int(totals[1]), unknown=int(totals[2])))


def aggregates(f):
    """G4 export: six flow cases, OI concentration, total endpoints, flags, activity."""
    S = f["spot"][:, None]
    w = 100.0 * f["gamma"] * S ** 2                    # $ per unit return per contract
    B, Sx, U = f["cum"][..., 0], f["cum"][..., 1], f["cum"][..., 2]
    q = {"low": Sx - B - U, "central": Sx - B, "high": Sx - B + U}
    ok = np.isfinite(w)
    cov = ok.sum(axis=1)

    def agg(x):
        v = np.where(ok, x * np.where(ok, w, 0), 0).sum(axis=1)
        return np.where(cov > 0, v, np.nan)
    oi0 = np.broadcast_to(f["open_oi"], w.shape)
    ok_oi = ok & np.isfinite(oi0)
    out = {"t": f["bounds"], "spot": f["spot"], "snapshot_age_ms": f["snap_age"], "covered_contracts": cov,
           "qqq_mid": f["qmid"]}
    for p in (1.0, 0.5):
        for k, v in q.items():
            out[f"G_within_p{int(p*100)}_{k}"] = agg(p * v)
        lo = np.where(ok_oi, (p * q["low"] - oi0) * np.where(ok_oi, w, 0), 0).sum(axis=1)
        hi = np.where(ok_oi, (p * q["high"] + oi0) * np.where(ok_oi, w, 0), 0).sum(axis=1)
        out[f"total_low_p{int(p*100)}"] = np.where(ok_oi.any(axis=1), lo, np.nan)
        out[f"total_high_p{int(p*100)}"] = np.where(ok_oi.any(axis=1), hi, np.nan)
        out[f"total_zero_crossing_p{int(p*100)}"] = (out[f"total_low_p{int(p*100)}"] < 0) & (out[f"total_high_p{int(p*100)}"] > 0)
        out[f"flow_zero_crossing_p{int(p*100)}"] = (out[f"G_within_p{int(p*100)}_low"] < 0) & (out[f"G_within_p{int(p*100)}_high"] > 0)
    out["G_oi_unsigned"] = np.where(ok_oi.any(axis=1), np.where(ok_oi, oi0 * np.where(ok_oi, w, 0), 0).sum(axis=1), np.nan)
    vol = f["cum"].sum(axis=2)                         # contracts per contract, cumulative
    lag = np.vstack([np.zeros((30, 16)), vol[:-30]])
    out["contracts_30m"] = (vol - lag).sum(axis=1)
    out["dollar_delta_30m"] = np.nansum((vol - lag) * 100 * f["delta"] * S, axis=1)
    puts = np.array([t == "P" for t in f["types"]])
    signed = B - Sx                                    # customer-side net buys (B − S)
    slag = np.vstack([np.zeros((30, 16)), signed[:-30]])
    out["signed_put_flow_30m"] = (signed - slag)[:, puts].sum(axis=1)
    out["signed_call_flow_30m"] = (signed - slag)[:, ~puts].sum(axis=1)
    return out


if __name__ == "__main__":
    archive, snaproot, sessions_path, outdir = (Path(a) for a in sys.argv[1:5])
    outdir.mkdir(parents=True, exist_ok=True)
    sessions = json.loads(sessions_path.read_text())
    recent = load_recent()
    manifest = {"protocol_commit": "0e1d8cc", "sessions": {}, "excluded": {}}
    for day, info in sorted(sessions.items()):
        s = (info.get("summaries") or [{}])[0]
        if s.get("status") != "complete" or s.get("partial") or s.get("gap") or s.get("late") or s.get("contracts") != 16:
            manifest["excluded"][day] = s.get("status") or "no summary"
            continue
        f = build(archive, snaproot, day, s["owner"], recent)
        np.savez_compressed(outdir / f"{day}.npz", **{k: v for k, v in f.items() if isinstance(v, np.ndarray)})
        agg = aggregates(f)
        with (outdir / f"{day}.csv").open("w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(agg.keys())
            for row in zip(*agg.values()):
                w.writerow([("" if isinstance(v, float) and math.isnan(v) else v) for v in row])
        manifest["sessions"][day] = {k: f[k] for k in ("owner", "manifest_sha256", "segments_verified", "symbols", "strikes",
                                                        "prev_close", "prev_close_source", "snapshots", "snapshot_digest",
                                                        "accepted_contracts")}
        for ext in ("csv", "npz"):
            manifest["sessions"][day][f"{ext}_sha256"] = hashlib.sha256((outdir / f"{day}.{ext}").read_bytes()).hexdigest()
        print(day, f["accepted_contracts"], f["snapshots"], f["prev_close_source"], flush=True)
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=1, default=float) + "\n")
