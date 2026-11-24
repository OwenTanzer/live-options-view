"""Import a Crypto Backend Notion snapshot and replay its manifest-selected series."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

from .one_clock import instant_ms, observation, select_as_of

INTERVALS = {"daily": ("1d", 86400000), "1hour": ("1h", 3600000), "1min": ("1m", 60000)}
MAX_MEMBER = 128 * 1024 * 1024


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def read_member(archive, name):
    member = archive.getinfo(name)
    if member.file_size > MAX_MEMBER:
        raise ValueError(f"Oversized archive member: {name}")
    return json.loads(archive.read(member))


def decode(raw, provider):
    if provider == "coingecko":
        stamp = raw["timestamp_ms"]
        if stamp != instant_ms(raw["date"]):
            raise ValueError("Price sample timestamps disagree")
        value = {"price": raw["price"]}
    elif provider == "coinbase":
        seconds, low, high, opened, close, volume = raw
        stamp = seconds * 1000
        value = dict(open=opened, high=high, low=low, close=close, volume=volume)
    elif provider == "tiingo":
        stamp = instant_ms(raw["date"])
        value = {key: raw[key] for key in ("open", "high", "low", "close", "volume")}
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("Invalid provider timestamp")
    for key, number in value.items():
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0 or (key != "volume" and number == 0):
            raise ValueError(f"Invalid {key} at {stamp}")
    if "close" in value and not (value["low"] <= min(value["open"], value["close"])
                                 <= max(value["open"], value["close"]) <= value["high"]):
        raise ValueError(f"Inconsistent candle at {stamp}")
    return stamp, value


def ingest(archive, db, digest):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("Duplicate archive member")
    manifest = read_member(archive, "manifest.json")
    coverage = []
    started = instant_ms(manifest["started"])
    updated = instant_ms(manifest["updated"])
    if started > updated:
        raise ValueError("Archive start follows its update")
    if set(manifest["coins"]) != set(manifest["coverage"]):
        raise ValueError("Manifest membership mismatch")
    db.execute("""CREATE TABLE prices(symbol TEXT, interval TEXT, ts INTEGER, event INTEGER,
               value TEXT, PRIMARY KEY(symbol,interval,ts)) WITHOUT ROWID""")
    for symbol, series in sorted(manifest["coverage"].items()):
        if not re.fullmatch(r"[A-Z0-9_]{1,32}", symbol):
            raise ValueError("Invalid symbol")
        for label, info in sorted(series.items()):
            interval, width = INTERVALS[label]
            provider = info["source"]
            member = f"data/{provider}/{symbol}_{label}.json"
            raw = read_member(archive, member)
            if len(raw) != info["bars"] or not raw:
                raise ValueError(f"Invalid manifest count for {member}")
            kind = "price_sample" if provider == "coingecko" else "candle"
            cutoff = instant_ms(info["updated"])
            if not started <= cutoff <= updated:
                raise ValueError(f"Invalid capture time for {member}")
            # Paged downloads have no per-row fetch time. Only a bar that
            # closed before this job started is certainly complete.
            if kind == "candle":
                cutoff = started
            rows = []
            times = set()
            for item in raw:
                ts, value = decode(item, provider)
                if ts in times:
                    raise ValueError(f"Duplicate timestamp in {member}")
                times.add(ts)
                event = ts if kind == "price_sample" else ts + width
                if event <= cutoff:
                    rows.append((symbol, interval, ts, event, json.dumps(value, separators=(",", ":"))))
            if min(times) != instant_ms(info["first"]) or max(times) != instant_ms(info["last"]):
                raise ValueError(f"Manifest date range mismatch for {member}")
            db.executemany("INSERT INTO prices VALUES (?,?,?,?,?)", rows)
            stamps = [row[2] for row in rows]
            coverage.append(dict(symbol=symbol, interval=interval, provider=provider, kind=kind,
                                 count=len(rows), raw_count=len(raw), member=member,
                                 first=iso(min(stamps)) if stamps else None,
                                 last=iso(max(stamps)) if stamps else None))
    available = iso(int(datetime.now(timezone.utc).timestamp()*1000))
    if instant_ms(manifest["updated"]) > instant_ms(available):
        raise ValueError("Future archive manifest")
    metadata = dict(available_at=available, manifest_updated=manifest["updated"],
                    archive_sha256=digest, coverage=coverage)
    db.execute("CREATE TABLE receipt(json TEXT NOT NULL)")
    db.execute("INSERT INTO receipt VALUES (?)", (json.dumps(metadata),))


def install(source, root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".import-lock").open("a") as lock, source.open("rb") as stream:
        fcntl.flock(lock, fcntl.LOCK_EX)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        target = root / (digest + ".sqlite3")
        if target.exists():
            return target
        stream.seek(0)
        fd, temp = tempfile.mkstemp(dir=root)
        os.close(fd)
        try:
            with zipfile.ZipFile(stream) as archive, closing(sqlite3.connect(temp)) as db, db:
                ingest(archive, db, digest)
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
    return target


def read_rows(db, metadata, symbol, interval, cutoff, limit):
    if not 1 <= limit <= 1000000:
        raise ValueError("limit must be 1 through 1000000")
    if instant_ms(metadata["available_at"]) > cutoff:
        return []
    info = next((s for s in metadata["coverage"] if (s["symbol"], s["interval"]) == (symbol, interval)), None)
    if not info:
        return []
    evidence = metadata["archive_sha256"] + "/" + info["member"]
    bars = db.execute("""SELECT ts,event,value FROM prices WHERE symbol=? AND interval=? AND event<=?
                         ORDER BY ts DESC LIMIT ?""", (symbol, interval, cutoff, limit))
    items = [observation(dict(source="crypto_candle_cache", session="continuous", instrument=symbol,
             id=f"{symbol}|{interval}|{ts}", revision=1, origin="recorded", valueAt=ts, eventAt=event,
             availableAt=metadata["available_at"], evidence=evidence, provider=info["provider"],
             kind=info["kind"], interval=interval, value=json.loads(value))) for ts,event,value in bars]
    return select_as_of(items, dict(instrument=symbol, session="continuous", cutoff=cutoff))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("import")
    setup.add_argument("zip", type=Path)
    setup.add_argument("root", type=Path)
    read = commands.add_parser("read")
    read.add_argument("database", type=Path)
    read.add_argument("symbol")
    read.add_argument("interval", choices=["1m", "1h", "1d"])
    read.add_argument("--as-of", required=True)
    read.add_argument("--limit", type=int, default=100)
    panel = commands.add_parser("panel")
    panel.add_argument("database", type=Path)
    args = parser.parse_args()
    if args.action == "import":
        print(install(args.zip, args.root))
        return
    with closing(sqlite3.connect(args.database.resolve().as_uri()+"?mode=ro", uri=True)) as db:
        metadata = json.loads(db.execute("SELECT json FROM receipt").fetchone()[0])
        if args.action == "read":
            result = read_rows(db, metadata, args.symbol, args.interval, instant_ms(args.as_of), args.limit)
        else:
            result = dict(metadata, btc_hourly=read_rows(db, metadata, "BTC", "1h",
                          instant_ms(metadata["available_at"]), 24))
        print(json.dumps(result, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
