"""Coverage check for the frozen strike approach/retreat test (SPEC.md). Read-only; inputs only.

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
    python research/oa241/strike_test/coverage.py CACHE_DIR > research/oa241/strike_test/coverage.json

For each candidate session never used in an outcome analysis, it reads the Tradier run summary and
universe (prints, preceding-quote ages, late start, reconnect gaps) and every regular-session chain
snapshot (same-day gamma and spot for all 16 universe contracts, gaps over 120 s). It computes no
QQQ return, distance change or any other outcome. Snapshot bodies are cached under CACHE_DIR.
"""
import csv
from concurrent.futures import ThreadPoolExecutor
import io
import json
import math
import os
from pathlib import Path
import re
import sys

BUCKET = os.environ.get("R2_BUCKET_NAME", "qqq-options-chain-data")
# Never used in any outcome analysis (results.json lists them as excluded) or recorded after it.
CANDIDATES = ["2026-09-21", "2026-09-22", "2026-09-24", "2026-09-28", "2026-09-30", "2026-10-29",
              "2026-11-17", "2026-11-18"]
GAP_S = 120


def client():
    import boto3
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


def keys(s3, prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def fin(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def secs(key):
    h = re.search(r"snapshot_(\d{6})", key).group(1)
    return int(h[:2]) * 3600 + int(h[2:4]) * 60 + int(h[4:6])


def clock(s):
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def session(s3, cache, day):
    tk = keys(s3, f"moo144/tradier/{day}/")
    get = lambda k: json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())  # noqa: E731
    summ = get(next(k for k in tk if "/summary-" in k))
    uni = get(next(k for k in tk if k.endswith("universe.json")))
    syms = [x for x in uni["symbols"] if x != "QQQ"]
    per = summ.get("timesale_counts_by_symbol") or {}
    pq = summ.get("preceding_quote_age_ms") or {}

    def body(k):
        p = cache / k
        if not (p.exists() and p.stat().st_size):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
        return p.read_bytes()
    snaps = sorted((k for k in keys(s3, f"intraday/{day.replace('-', '')}/") if re.search(r"snapshot_\d+\.csv$", k)
                    and 9.5 * 3600 - GAP_S <= secs(k) <= 16 * 3600), key=secs)
    with ThreadPoolExecutor(8) as ex:
        bodies = list(ex.map(body, snaps))
    good = []
    for k, b in zip(snaps, bodies):
        rows = {r["OptionSymbol"]: r for r in csv.DictReader(io.StringIO(b.decode("utf-8", "replace")))
                if r.get("Expiration") == day}
        spots = {fin(r.get("UnderlyingPrice")) for r in rows.values()} - {None}
        if all(x in rows and fin(rows[x].get("Gamma")) is not None for x in syms) and len(spots) == 1:
            good.append(secs(k))
    edges = [int(9.5 * 3600)] + [t for t in good if t >= 9.5 * 3600] + [16 * 3600]
    return dict(
        status=summ.get("status"), partial_reasons=summ.get("partial_reasons"),
        late_start_s=round(summ.get("effective_late_start_seconds") or 0, 1),
        reconnects=summ.get("reconnects"), stream_gap_s=summ.get("gap_seconds"),
        strikes=uni["universe"].get("strikes"), contracts=len(syms),
        contracts_with_prints=sum(1 for x in syms if (per.get(x) or 0) > 0),
        min_prints_per_contract=min((per.get(x) or 0) for x in syms) if per else None,
        timesales=(summ.get("event_counts") or {}).get("timesale"), quotes=(summ.get("event_counts") or {}).get("quote"),
        preceding_quote_age_ms={k: pq.get(k) for k in ("sampled_median", "sampled_p95", "max")},
        rth_snapshots=len(snaps), snapshots_with_all_gamma_and_spot=len(good),
        snapshot_gaps_over_120s=[(clock(a), clock(b), b - a) for a, b in zip(edges, edges[1:]) if b - a > GAP_S])


def main():
    cache = Path(sys.argv[1])
    s3 = client()
    json.dump({d: session(s3, cache, d) for d in CANDIDATES}, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
