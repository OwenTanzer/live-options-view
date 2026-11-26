"""Display-only QQQ minute bars from captured, range-eligible regular trades.

Run from this directory with a verified Tradier session manifest key and a chart
cache directory. The Trainer never downloads a tape on an interactive request.
"""
import argparse
import csv
import io
import gzip
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import archive

ET = ZoneInfo("America/New_York")


def read_bars(cache, symbol, day):
    for name, interval in (("chart_minutes", 60), ("chart_five_minutes", 300)):
        rows = read_file(cache / name / symbol / (day + ".json"), symbol, day, interval)
        if rows:
            return interval, rows
    return 0, []


def read_file(path, symbol, day, interval):
    try:
        doc = json.loads(path.read_bytes())
        if doc["symbol"] != symbol or doc["day"] != day:
            return []
        rows = doc["bars"]
        previous = -1
        for end, available, opened, high, low, closed in rows:
            if not (isinstance(end, int) and previous < end <= 86400 and
                    end % interval == 0 and end <= available <= 86400 and
                    all(math.isfinite(x) for x in (opened, high, low, closed)) and
                    0 < low <= min(opened, closed) <= max(opened, closed) <= high):
                return []
            previous = end
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        return []


def install(cache, directory, symbol, day, source, raw, rows):
    if not rows:
        return 0
    root = cache / directory / symbol
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as tmp:
        json.dump(dict(symbol=symbol, day=day, source=source,
                       sha256=hashlib.sha256(raw).hexdigest(), bars=rows), tmp)
    os.replace(tmp.name, root / (day + ".json"))
    return len(rows)


def import_five(client, bucket, key, cache):
    day = datetime.strptime(Path(key).stem[-8:], "%Y%m%d").date().isoformat()
    raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    bars = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        start = datetime.fromisoformat(row["datetime"])
        if start.tzinfo:
            start = start.astimezone(ET)
        t = 3600 * start.hour + start.minute * 60 + start.second
        if start.date().isoformat() == day and 34200 <= t < 57600 and t % 300 == 0:
            end = t + 300
            bars.append([end, end, *[float(row[k]) for k in ("open", "high", "low", "close")]])
    return install(cache, "chart_five_minutes", "QQQ", day, key, raw, bars)


def build(client, bucket, manifest_key, cache):
    day = manifest_key.split("/")[2]
    midnight = datetime.fromisoformat(day).replace(tzinfo=ET).timestamp()
    def fetch(key):
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    raw = fetch(manifest_key)
    manifest = json.loads(raw)
    groups = {}
    for part in sorted(manifest["artifacts"], key=lambda p: p["key"]):
        if "records" not in part:
            continue
        blob = fetch(part["key"])
        if hashlib.sha256(blob).hexdigest() != part["sha256"]:
            raise ValueError("Capture checksum mismatch")
        for line in gzip.decompress(blob).splitlines():
            if b'"QQQ"' not in line or b'timesale' not in line:
                continue
            trade = json.loads(line)
            flags = set((trade.get("flag") or "").strip())
            if (trade.get("type") != "timesale" or trade.get("symbol") != "QQQ"
                    or trade.get("session") != "normal" or not flags
                    or flags - set("@ABDFKLOSXY156 ")
                    or any(trade.get(k) for k in ("cancel","correction","duplicate_in_run"))):
                continue
            ts = int(trade["date"])/1000 - midnight
            arrival = datetime.fromisoformat(trade["collector_receipt_timestamp"]).timestamp() - midnight
            price = float(trade["last"])
            if not (34200 <= ts < 57600 and arrival - ts <= 5 and math.isfinite(price) and price > 0):
                continue
            end = (int(ts)//60 + 1)*60
            key = (ts, int(trade.get("seq") or 0))
            bar = groups.setdefault(end, [end, end, price, price, price, price, key, key])
            bar[1] = max(bar[1], math.ceil(arrival))
            bar[3] = max(bar[3], price)
            bar[4] = min(bar[4], price)
            if key < bar[6]:
                bar[2], bar[6] = price, key
            if key >= bar[7]:
                bar[5], bar[7] = price, key
    rows = [groups[k][:6] for k in sorted(groups)]
    return install(cache, "chart_minutes", "QQQ", day, manifest_key, raw, rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("cache",type=Path)
    args=parser.parse_args()
    arc=archive.Archive(args.cache)
    builder = import_five if args.manifest.startswith("prices/qqq_intraday_") else build
    print(builder(arc.s3, arc.bucket, args.manifest, args.cache))
