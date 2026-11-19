"""Depth of the chain-snapshot archive for a snapshot-only 0DTE follow-up. Read-only; inputs only.

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
    python research/oa241/strike_test/snapshot_depth.py > research/oa241/strike_test/snapshot_depth.json

For each intraday/{YYYYMMDD}/ day: the number of regular-session snapshots and the largest gap between
them; and, in the snapshot nearest 12:00 ET, the same-day-expiry rows, distinct strikes, rows with
positive settled OpenInterest and rows with a numeric Gamma. It reads no price column.
"""
import csv
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import re
import sys

BUCKET = os.environ.get("R2_BUCKET_NAME", "qqq-options-chain-data")


def client():
    import boto3
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


def secs(key):
    h = re.search(r"snapshot_(\d{6})", key)
    return None if not h else int(h[1][:2]) * 3600 + int(h[1][2:4]) * 60 + int(h[1][4:6])


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day(s3, d):
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"intraday/{d}/")
            for o in p.get("Contents", [])]
    rth = sorted((secs(k), k) for k in keys if secs(k) is not None and 9.5 * 3600 <= secs(k) <= 16 * 3600)
    out = dict(day=d, rth=len(rth))
    if not rth:
        return out
    ts = [t for t, _ in rth]
    out["max_gap_s"] = max(b - a for a, b in zip([34200] + ts, ts + [57600]))
    mid = min(rth, key=lambda x: abs(x[0] - 12 * 3600))[1]
    iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    body = s3.get_object(Bucket=BUCKET, Key=mid)["Body"].read().decode("utf-8", "replace")
    rows = [r for r in csv.DictReader(io.StringIO(body)) if r.get("Expiration") == iso]
    out.update(odte_rows=len(rows), odte_strikes=len({r["Strike"] for r in rows}),
               oi_ok=sum((num(r.get("OpenInterest")) or 0) > 0 for r in rows),
               gamma_ok=sum(num(r.get("Gamma")) is not None for r in rows))
    return out


def main():
    s3 = client()
    days = []
    for p in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="intraday/", Delimiter="/"):
        days += [c["Prefix"].split("/")[1] for c in p.get("CommonPrefixes", [])]
    days = [d for d in days if re.fullmatch(r"\d{8}", d)]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda d: day(s3, d), days))
    trading = [r for r in res if r.get("odte_rows")]
    summary = dict(days_listed=len(res), days_with_0dte=len(trading),
                   before_2026_09_21=sum(r["day"] < "20260921" for r in trading),
                   min_0dte_strikes=min(r["odte_strikes"] for r in trading),
                   days_with_gap_over_120s=[r["day"] for r in trading if r["max_gap_s"] > 120])
    json.dump(dict(summary=summary, days=res), sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
