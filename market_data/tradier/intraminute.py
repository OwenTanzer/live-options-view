"""Receipt-clock projection of the existing QQQ stream. No provider client."""
import argparse
from collections import defaultdict
from datetime import datetime
import gzip
import json
import math
from pathlib import Path

from .recent import RecentPrints


def millis(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def project(event, signed=None):
    """Public fields only. Keep side timestamps separate from collector receipt."""
    kind = event.get("type")
    if kind == "gap":
        end = millis(event["receipt_timestamp"])
        return dict(kind="gap", at=end, start=end-round(1000*float(event.get("outage_seconds", 0))),
                    reason=event["reason"])
    if event.get("symbol") != "QQQ" and signed is None:
        return None
    at = millis(event["collector_receipt_timestamp"])
    if signed is not None:
        return dict(kind="option", at=at, **signed)
    if kind == "quote":
        bid, ask = float(event["bid"]), float(event["ask"])
        if 0 < bid <= ask and math.isfinite(ask):
            return dict(kind="quote", at=at, bid=bid, ask=ask,
                        bidAt=int(event["biddate"]), askAt=int(event["askdate"]))
    amendment = event.get("cancel") or event.get("correction")
    if kind == "timesale" and (amendment or not event.get("duplicate_in_run")):
        price, size = float(event["last"]), int(event["size"])
        if math.isfinite(price) and (amendment or (price > 0 and size > 0)):
            return dict(kind="stock", at=at, t=int(event["date"]), seq=int(event["seq"]),
                        price=price, size=size, flag=event.get("flag"), cancel=bool(event.get("cancel")),
                        correction=bool(event.get("correction")))
    return None


def safe_project(event, signed=None):
    try:
        return project(event, signed)
    except (TypeError, KeyError, ValueError, OverflowError, OSError):
        return None


class GapState:
    """Carry an already-observed disconnect into a separately readable minute."""
    def __init__(self):
        self.open = False

    def observe(self, row):
        if row["kind"] == "gap":
            self.open = row["reason"] == "stream_disconnect"

    def prefix(self, minute):
        if self.open:
            return [dict(kind="gap", at=minute, start=minute, reason="stream_disconnect")]
        return []


def collect(segments, start, stop):
    """Segments in receipt order, including prior segments for signing warmup."""
    signer = RecentPrints()
    minutes = defaultdict(list)
    seen = set()
    gap = GapState()
    minute = start
    for path in segments:
        with gzip.open(path, "rt") as stream:
            for line in stream:
                event = json.loads(line)
                signed = signer.observe(event)
                row = safe_project(event, signed)
                if not row:
                    continue
                if row["at"] < start:
                    gap.observe(row)
                    continue
                if row["at"] >= stop:
                    continue
                while minute <= row["at"] // 60000 * 60000:
                    prefix = gap.prefix(minute)
                    if prefix:
                        minutes[minute].extend(prefix)
                    minute += 60000
                gap.observe(row)
                if row["kind"] == "stock" and not (row["cancel"] or row["correction"]):
                    identity = (row["seq"], row["cancel"], row["correction"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                minutes[row["at"] // 60000 * 60000].append(row)
    while gap.open and minute < stop:
        minutes[minute].extend(gap.prefix(minute))
        minute += 60000
    return minutes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", required=True, help="inclusive UTC ISO timestamp")
    parser.add_argument("--stop", required=True, help="exclusive UTC ISO timestamp")
    parser.add_argument("segments", type=Path, nargs="+", help="chronological validated archive segments")
    args = parser.parse_args()
    start, stop = millis(args.start), millis(args.stop)
    if start % 60000 or stop % 60000 or stop <= start:
        parser.error("use complete, increasing UTC minute boundaries")
    args.out.mkdir(parents=True, exist_ok=True)
    for minute, rows in collect(args.segments, start, stop).items():
        body = dict(version=1, minute=minute, rows=rows, source="archive")
        (args.out / f"{minute}.json").write_text(json.dumps(body, separators=(",", ":"))+"\n")


if __name__ == "__main__":
    main()
