#!/usr/bin/env python3
"""D4 M3/M4 coverage assembly for the fixed S-elig set (read-only; NO outcomes).

Preparation only. For the 46 coverage-eligible sessions fixed in
STEP4_PLAN_v6.md (2026-08-31 .. 2026-11-09, exclusions intact) it:

1. LISTs every collector chain snapshot key per session and downloads those
   needed (read-only GETs; nothing is written to R2), verifying size and the
   single-part MD5 ETag and recording SHA-256;
2. maps every selected replay cycle (market-hours ledger records in S-elig) to
   the snapshot it used: by the recorded lineage key (from 2026-10-05), else by
   matching `market_snapshot_timestamp` to a listed key's timestamp within a
   tolerance derived from, and cross-validated on, the lineage-keyed records;
   missing and ambiguous identities are kept as explicit gaps;
3. checks strategy-input and anchor coverage on every selected cycle;
4. assembles each session's full-chain snapshot path (per-snapshot summary;
   raw files stay in the private cache) and QQQ 5-minute bars from the
   snapshots' own spot samples, including zero-trade sessions. Nothing is
   imputed: an interval without samples is listed, not filled.

It computes no arm, P&L, parity or evaluator result. Snapshots carry no
executable quote time and no venue fees.

    python research/oa236_protection/d4_coverage.py \\
      --inventory d4_inventory_330b305.json \\
      --ledger crassus-ledger/chej-export/oiskew_decisions_2026-08-31_to_09-02.jsonl \\
      --ledger crassus-ledger/chej-export/oiskew_decisions_railway_2026-09-03_to_11-09.jsonl \\
      --r2-env crassus-ledger/.env --cache D:/d4-coverage-cache --out D:/d4-coverage-cache/out
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCHEMA = "d4_coverage.v1"
FIXED_ELIGIBLE = 46
FIXED_EXCLUDED = {"2026-08-31", "2026-11-04", "2026-11-05", "2026-11-06"}
DOWNLOAD_WINDOW_ET = (time(9, 15), time(16, 15))   # covers the cycles' snapshots around the open/close
RTH = (time(9, 30), time(16, 0))
INPUT_FIELDS = ("imbalance_ratio", "session_start_imbalance", "delta_from_session_start",
                "band_strikes", "near_money_call_oi", "near_money_put_oi")
KEY_RE = re.compile(r"snapshot_(\d{2})(\d{2})(\d{2})(\d{6})\.csv$")


def ts(s):
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def key_time(key):
    """The ET wall-clock time encoded in a snapshot key, as UTC."""
    m = KEY_RE.search(key)
    if not m:
        return None
    day = key.split("/")[1]
    h, mi, s, us = (int(x) for x in m.groups())
    return datetime(int(day[:4]), int(day[4:6]), int(day[6:]), h, mi, s, us, tzinfo=ET).astimezone(timezone.utc)


def stats(v, digits=6):
    v = sorted(x for x in v if x is not None)
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": round(v[0], digits), "median": round(median(v), digits),
            "p99": round(v[min(len(v) - 1, int(0.99 * (len(v) - 1)))], digits), "max": round(v[-1], digits)}


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class R2:
    """Read-only client: list_objects_v2 and get_object only."""

    def __init__(self, env_path):
        import boto3
        env = dict(line.strip().split("=", 1) for line in open(env_path, encoding="utf-8") if "=" in line)
        self.bucket = env["R2_BUCKET_NAME"]
        self.s3 = boto3.client("s3", endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                               aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                               aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"])

    def list_day(self, day):
        out, token = [], None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=f"intraday/{day}/snapshot_", MaxKeys=1000)
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [{"key": o["Key"], "size": o["Size"], "etag": o["ETag"].strip('"'),
                     "last_modified": o["LastModified"].isoformat()} for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]

    def get(self, key, attempts=4):
        for i in range(attempts):
            try:
                return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except Exception:  # noqa: BLE001 - retried, then reported
                if i == attempts - 1:
                    raise
                _time.sleep(1.5 * (i + 1))


def download(r2, obj, cache):
    path = Path(cache) / obj["key"]
    status = "cached"
    if not (path.exists() and path.stat().st_size == obj["size"]):
        try:
            body = r2.get(obj["key"])
        except Exception as exc:  # noqa: BLE001
            return {**obj, "status": "get_failed", "error": type(exc).__name__}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        status = "downloaded"
    body = path.read_bytes()
    md5 = hashlib.md5(body).hexdigest()
    multipart = "-" in obj["etag"]
    return {**obj, "status": status, "size_ok": len(body) == obj["size"],
            "md5_ok": None if multipart else md5 == obj["etag"], "sha256": hashlib.sha256(body).hexdigest()}


def summarize_snapshot(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    spot = None
    if rows:
        try:
            spot = float(rows[0]["UnderlyingPrice"])
        except (KeyError, TypeError, ValueError):
            spot = None
    bid_ask = sum(1 for r in rows if r.get("Bid") not in (None, "") and r.get("Ask") not in (None, ""))
    return {"rows": len(rows), "symbols": len({r.get("OptionSymbol") for r in rows}),
            "bid_ask_share": round(bid_ask / len(rows), 4) if rows else None, "spot_first_row": spot}


def bars_for(session, snaps):
    """5-minute bars from snapshot spot samples; empty intervals listed, never filled."""
    d = date.fromisoformat(session)
    open_ = datetime.combine(d, RTH[0], ET).astimezone(timezone.utc)
    samples = sorted((s["t"], s["spot"]) for s in snaps if s["spot"] is not None)
    bars, missing = [], []
    for i in range(78):
        start, end = open_ + timedelta(minutes=5 * i), open_ + timedelta(minutes=5 * (i + 1))
        px = [(t, p) for t, p in samples if start <= t < end]
        if not px:
            missing.append(start.isoformat())
            continue
        bars.append({"session": session, "event_start": start.isoformat(), "event_end": end.isoformat(),
                     "open": px[0][1], "high": max(p for _, p in px), "low": min(p for _, p in px),
                     "close": px[-1][1], "samples": len(px), "first_sample": px[0][0].isoformat(),
                     "last_sample": px[-1][0].isoformat()})
    return bars, missing


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True, help="d4_inventory v2 output (fixes S-elig)")
    ap.add_argument("--ledger", action="append", required=True)
    ap.add_argument("--r2-env", required=True, help="local .env with read credentials (never printed)")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    inv = json.loads(Path(a.inventory).read_text())
    sessions = [r["session"] for r in inv["session_denominator"]["sessions"] if r["eligible"]]
    excluded = {r["session"] for r in inv["session_denominator"]["sessions"] if not r["eligible"]}
    if len(sessions) != FIXED_ELIGIBLE or excluded != FIXED_EXCLUDED:
        raise SystemExit(f"S-elig differs from the fixed set: {len(sessions)} eligible, excluded {sorted(excluded)}")
    elig = set(sessions)

    # -- selected replay cycles: market-hours ledger records on S-elig sessions --
    cycles = []
    for f in a.ledger:
        for line in open(f, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            t = ts(r["timestamp_utc"])
            te = t.astimezone(ET)
            if te.date().isoformat() not in elig or not (RTH[0] <= te.time() <= RTH[1]):
                continue
            m = (r.get("decision") or {}).get("metadata") or {}
            lin = r.get("market_snapshot_lineage") or {}
            cycles.append({
                "session": te.date().isoformat(), "decision_utc": t.isoformat(), "decision_id": r.get("decision_id"),
                "run_id": r.get("run_id"), "outcome_class": r.get("outcome_class"),
                "holding": "Already holding" in (r.get("reason") or ""),
                "lineage_key": lin.get("snapshot_key") if lin.get("status") == "valid" else None,
                "lineage_status": lin.get("status"),
                "market_snapshot_timestamp": r.get("market_snapshot_timestamp"),
                "inputs_present": {k: m.get(k) is not None for k in INPUT_FIELDS},
            })
    cycles.sort(key=lambda c: c["decision_utc"])

    # -- list every snapshot key per session (read-only) --------------------------
    r2 = R2(a.r2_env)
    listing = {}
    for s in sessions:
        listing[s] = r2.list_day(s.replace("-", ""))
    by_key = {o["key"]: o for v in listing.values() for o in v}

    # -- empirical timestamp tolerance from lineage-keyed cycles ------------------
    deltas = []
    for c in cycles:
        if c["lineage_key"] and c["market_snapshot_timestamp"]:
            kt = key_time(c["lineage_key"])
            if kt is not None:
                deltas.append(abs((kt - ts(c["market_snapshot_timestamp"])).total_seconds()))
    max_lineage_delta = max(deltas) if deltas else None
    tolerance_s = 0.001  # 1 ms; must exceed every observed lineage delta (checked below)
    if max_lineage_delta is None or max_lineage_delta >= tolerance_s:
        raise SystemExit(f"lineage key/timestamp deltas reach {max_lineage_delta}s; 1 ms tolerance not supported")
    key_times = {s: sorted((key_time(o["key"]), o["key"]) for o in listing[s] if key_time(o["key"])) for s in sessions}

    def match_by_time(session_hint, mst):
        t = ts(mst)
        day = t.astimezone(ET).date().isoformat()
        pool = key_times.get(day)
        if pool is None:
            # a cycle may use a snapshot from another day (never past 2026-11-09 here)
            listed = r2.list_day(day.replace("-", ""))
            pool = sorted((key_time(o["key"]), o["key"]) for o in listed if key_time(o["key"]))
            key_times[day] = pool
            for o in listed:
                by_key.setdefault(o["key"], o)
        hits = [(abs((kt - t).total_seconds()), k) for kt, k in pool if abs((kt - t).total_seconds()) <= tolerance_s]
        return hits

    crosscheck = {"checked": 0, "agree": 0, "disagree": []}
    for c in cycles:
        if c["lineage_key"]:
            c["identity_method"] = "lineage_key"
            c["snapshot_key"] = c["lineage_key"]
            if c["market_snapshot_timestamp"]:
                hits = match_by_time(c["session"], c["market_snapshot_timestamp"])
                crosscheck["checked"] += 1
                if len(hits) == 1 and hits[0][1] == c["lineage_key"]:
                    crosscheck["agree"] += 1
                else:
                    crosscheck["disagree"].append([c["decision_id"], c["lineage_key"], [h[1] for h in hits]])
        elif c["market_snapshot_timestamp"]:
            hits = match_by_time(c["session"], c["market_snapshot_timestamp"])
            if len(hits) == 1:
                c["identity_method"], c["snapshot_key"] = "timestamp_match", hits[0][1]
                c["match_delta_s"] = round(hits[0][0], 9)
            elif not hits:
                c["identity_method"], c["snapshot_key"] = "timestamp_no_listed_key", None
            else:
                c["identity_method"], c["snapshot_key"] = "timestamp_ambiguous", None
                c["candidates"] = [h[1] for h in hits]
        else:
            c["identity_method"], c["snapshot_key"] = "unidentified", None
        c["key_listed"] = c["snapshot_key"] in by_key if c["snapshot_key"] else None

    # -- download: session window + every identified key ------------------------
    want = {}
    for s in sessions:
        for o in listing[s]:
            kt = key_time(o["key"])
            if kt and DOWNLOAD_WINDOW_ET[0] <= kt.astimezone(ET).time() <= DOWNLOAD_WINDOW_ET[1]:
                want[o["key"]] = o
    for c in cycles:
        if c["snapshot_key"] and c["snapshot_key"] in by_key:
            want[c["snapshot_key"]] = by_key[c["snapshot_key"]]
    with ThreadPoolExecutor(a.workers) as pool:
        fetched = list(pool.map(lambda o: download(r2, o, a.cache), want.values()))
    fetched_by_key = {f["key"]: f for f in fetched}
    for c in cycles:
        f = fetched_by_key.get(c["snapshot_key"]) if c["snapshot_key"] else None
        c["snapshot_sha256"] = f.get("sha256") if f else None
        c["snapshot_retrieved"] = bool(f and f.get("sha256") and f.get("size_ok") and f.get("md5_ok") is not False)

    # -- per-snapshot summaries, paths and bars ------------------------------------
    snaps_by_session = collections.defaultdict(list)
    snapshot_rows = []
    for f in sorted(fetched, key=lambda x: x["key"]):
        row = {k: f.get(k) for k in ("key", "size", "etag", "last_modified", "status", "size_ok", "md5_ok", "sha256", "error")}
        kt = key_time(f["key"])
        row["t"] = kt.isoformat() if kt else None
        if f.get("sha256"):
            row.update(summarize_snapshot(Path(a.cache) / f["key"]))
        snapshot_rows.append(row)
        sess = kt.astimezone(ET).date().isoformat() if kt else None
        if sess in elig and f.get("sha256"):
            snaps_by_session[sess].append({"t": kt, "spot": row.get("spot_first_row")})
    all_bars, session_report = [], []
    for s in sessions:
        bars, missing = bars_for(s, snaps_by_session.get(s, []))
        all_bars += bars
        rth_snaps = [x for x in snaps_by_session.get(s, []) if RTH[0] <= x["t"].astimezone(ET).time() <= RTH[1]]
        times = sorted(x["t"] for x in rth_snaps)
        gaps = [(b - a_).total_seconds() for a_, b in zip(times, times[1:])]
        sc = [c for c in cycles if c["session"] == s]
        session_report.append({
            "session": s, "listed_keys": len(listing[s]), "rth_snapshots_retrieved": len(rth_snaps),
            "same_second_duplicate_keys": len(times) - len({t.replace(microsecond=0) for t in times}),
            "max_snapshot_gap_s": round(max(gaps), 1) if gaps else None,
            "bars_built": len(bars), "bar_intervals_without_samples": missing,
            "cycles": len(sc), "cycles_identified_and_retrieved": sum(c["snapshot_retrieved"] for c in sc),
            "cycles_with_all_inputs": sum(all(c["inputs_present"].values()) for c in sc),
        })

    # -- coverage report -------------------------------------------------------------
    by_method = collections.Counter(c["identity_method"] for c in cycles)
    by_outcome_inputs = collections.defaultdict(lambda: [0, 0])
    for c in cycles:
        by_outcome_inputs[c["outcome_class"]][0] += 1
        by_outcome_inputs[c["outcome_class"]][1] += all(c["inputs_present"].values())
    run_ids = collections.defaultdict(set)
    for c in cycles:
        run_ids[c["session"]].add(c["run_id"])
    report = {
        "schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "population": {"eligible_sessions": len(sessions), "excluded_fixed": sorted(FIXED_EXCLUDED),
                       "window": [sessions[0], sessions[-1]], "nov_10_14": "excluded, not listed or read"},
        "cycles": {"selected_market_hours_cycles": len(cycles), "holding_cycles": sum(c["holding"] for c in cycles),
                   "identity_method": dict(by_method),
                   "identified_and_retrieved": sum(c["snapshot_retrieved"] for c in cycles),
                   "gaps": [[c["decision_id"], c["decision_utc"], c["identity_method"], c.get("candidates")]
                            for c in cycles if not c["snapshot_retrieved"]]},
        "timestamp_match_evidence": {"tolerance_s": tolerance_s,
                                     "lineage_key_vs_timestamp_abs_delta_s": stats(deltas, 9),
                                     "timestamp_rule_vs_lineage_crosscheck": {**crosscheck,
                                                                              "disagree": crosscheck["disagree"][:50],
                                                                              "disagree_count": len(crosscheck["disagree"])},
                                     "timestamp_matched_delta_s": stats(
                                         [c["match_delta_s"] for c in cycles if c.get("match_delta_s") is not None], 9)},
        "inputs": {"fields": list(INPUT_FIELDS),
                   "per_field_present": {k: sum(c["inputs_present"][k] for c in cycles) for k in INPUT_FIELDS},
                   "all_present_by_outcome_class": {k: {"cycles": v[0], "all_inputs": v[1]}
                                                    for k, v in sorted(by_outcome_inputs.items())},
                   "anchor_field": "session_start_imbalance",
                   "sessions_with_multiple_run_ids": {s: len(v) for s, v in sorted(run_ids.items()) if len(v) > 1}},
        "snapshots": {"listed": sum(len(v) for v in listing.values()), "requested": len(want),
                      "retrieved": sum(1 for f in fetched if f.get("sha256")),
                      "get_failed": [[f["key"], f.get("error")] for f in fetched if not f.get("sha256")],
                      "size_mismatch": sum(1 for f in fetched if f.get("sha256") and not f.get("size_ok")),
                      "md5_mismatch": sum(1 for f in fetched if f.get("md5_ok") is False),
                      "multipart_unverified": sum(1 for f in fetched if f.get("md5_ok") is None and f.get("sha256")),
                      "bytes": sum(f["size"] for f in fetched if f.get("sha256"))},
        "bars": {"built": len(all_bars), "expected": 78 * len(sessions),
                 "intervals_without_samples": sum(len(r["bar_intervals_without_samples"]) for r in session_report),
                 "method": "5-minute bars from each snapshot's first-row UnderlyingPrice; no imputation"},
        "not_recoverable": ["executable quote time (snapshots carry no per-row quote time)", "real-venue fees"],
        "sessions": session_report,
    }

    def write_jsonl(name, rows):
        p = out / name
        with open(p, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
        return p

    files = [write_jsonl("cycle_manifest.jsonl", cycles), write_jsonl("snapshots.jsonl", snapshot_rows),
             write_jsonl("bars_5m.jsonl", all_bars)]
    rp = out / "coverage_report.json"
    rp.write_text(json.dumps(report, indent=2, default=str))
    files.append(rp)
    (out / "SHA256SUMS.txt").write_text("".join(f"{sha256_file(p)}  {p.name}\n" for p in files))
    print(json.dumps({k: report[k] for k in ("population", "cycles", "timestamp_match_evidence", "snapshots", "bars")},
                     indent=2, default=str)[:6000])


if __name__ == "__main__":
    main()
