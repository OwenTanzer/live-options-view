"""Publish derived minutes after single-run QQQ archive reconciliation."""
import argparse
import gzip
import hashlib
import io
import json
import re

from .intraminute import GapState, millis, safe_project
from .recent import RecentPrints

PREFIX = "order-flow/qqq/intraminute/v1"


def replayable(summary):
    reasons = summary.get("partial_reasons") or []
    return summary.get("status") == "complete" or (
        summary.get("status") == "partial" and bool(reasons)
        and all(re.fullmatch(r"reconnects=[1-9][0-9]*", reason) for reason in reasons)
    )


def publish(client, bucket, summary_key):
    match = re.fullmatch(r"(moo144/tradier/\d{4}-\d{2}-\d{2})/summary-([a-zA-Z0-9-]+)\.json", summary_key)
    if not match:
        raise ValueError("expected a QQQ archive summary")
    summary = json.loads(client.get_object(Bucket=bucket, Key=summary_key)["Body"].read())
    if summary.get("underlying") != "QQQ" or not replayable(summary):
        raise ValueError("requires complete or reconnect-only QQQ archive")
    parts = summary["event_parts"]
    expected = [f"{match[1]}/{match[2]}-part-{i:04d}.ndjson.gz" for i in range(len(parts))]
    if not parts or sorted(p["key"] for p in parts) != expected:
        raise ValueError("multiple owners or incomplete segment sequence; reconcile before publishing")
    start, stop = (millis(summary[name]) for name in ("session_open", "session_close"))
    minute = start
    signer = RecentPrints()
    gap = GapState()
    rows = []
    count = 0
    def flush():
        nonlocal count
        body = json.dumps(dict(version=1, minute=minute, rows=rows, source=summary_key),
                          separators=(",", ":")).encode()
        client.put_object(Bucket=bucket, Key=f"{PREFIX}/{minute}.json", Body=body,
                          ContentType="application/json", CacheControl="no-cache")
        count += 1
    for part in sorted(parts, key=lambda p: p["key"]):
        compressed = client.get_object(Bucket=bucket, Key=part["key"])["Body"].read()
        if hashlib.sha256(compressed).hexdigest() != part["sha256"]:
            raise ValueError("archive digest mismatch: "+part["key"])
        with gzip.open(io.BytesIO(compressed), "rt") as stream:
            for line in stream:
                event = json.loads(line)
                row = safe_project(event, signer.observe(event))
                if not row or not start <= row["at"] < stop:
                    continue
                if row["at"] < minute:
                    raise ValueError("receipt clock crossed a closed minute backwards")
                while row["at"] >= minute + 60000:
                    flush()
                    minute += 60000
                    rows = gap.prefix(minute)
                gap.observe(row)
                rows.append(row)
    while minute < stop:
        flush()
        minute += 60000
        rows = gap.prefix(minute)
    return count


if __name__ == "__main__":
    from ._probe import r2_client
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary_key")
    args = parser.parse_args()
    client, bucket = r2_client()
    print(publish(client, bucket, args.summary_key))
