"""Minute capture and conditional publication. Publish is an explicit subcommand."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import subprocess

from .panel_prices import Capture, collect, crypto_time, utcnow, ET
from .crypto_collector.publish import _error_code
from .crypto_collector import spot_receipts

MAX_BYTES = 128 * 1024
FEEDS = ("crypto", "nasdaq")


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":")))
    temp.replace(path)


def after(header, now):
    try:
        seconds = int(header)
        return now + timedelta(seconds=max(60, seconds))
    except (ValueError, TypeError, OverflowError):
        try:
            return max(now + timedelta(seconds=60), parsedate_to_datetime(header))
        except (ValueError, TypeError, OverflowError):
            return now + timedelta(seconds=60)


class StatefulCapture(Capture):
    def __init__(self, state):
        super().__init__()
        self.path = state
        now = utcnow()
        self.until = json.loads(state.read_bytes()) if state.exists() else {}
        self.until = {k: v for k, v in self.until.items() if crypto_time(v) > now}
        self.backoff.update(self.until)

    def get(self, provider, url, params=None):
        result = super().get(provider, url, params)
        with self.lock:
            header = self.backoff.get(provider)
            if header and provider not in self.until:
                self.until[provider] = after(header, utcnow()).isoformat()
                atomic(self.path, self.until)
        return result


def capture(feed, baseline_path, state):
    baseline = json.loads(baseline_path.read_bytes())
    baseline_at = baseline.get("constructed_at") or baseline.get("as_of")
    now = utcnow()
    stamp = crypto_time(baseline_at)
    if not stamp or not 0 <= (now - stamp).total_seconds() <= 10800:
        raise ValueError("Hourly enrichment missing, future-dated or older than three hours")
    network = StatefulCapture(state)
    network.verify_session_quotes = feed == "nasdaq"
    prices = collect(feed, baseline, network)
    del prices["enrichment"]
    prices.update(schema_version="panel_minute.v1", baseline_at=baseline_at, started_at=now.isoformat())
    return prices


def regular_session(calendar, now):
    day = now.astimezone(ET).date().isoformat()
    if not calendar["start"] <= day <= calendar["end"]:
        raise ValueError("Exchange calendar coverage expired")
    hours = calendar["sessions"].get(day)
    return bool(hours and crypto_time(hours[0]) <= now < crypto_time(hours[1]))


def encode(doc):
    body = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > MAX_BYTES:
        raise ValueError("Oversize minute document")
    return body


def observation_floor(row):
    observed = crypto_time(row.get("provider_observed_at"))
    if observed:
        return observed
    try:
        return datetime.strptime(row["provider_time_label"], "%b %d, %Y").replace(tzinfo=ET)
    except (KeyError, TypeError, ValueError):
        return None


def valid_at(stamp, row, now):
    receipt = crypto_time(row.get("received_at"))
    return stamp if stamp and stamp <= min(receipt or now, now) else None


def protect_observations(candidate, prior, feed, now):
    for symbol, row in candidate["prices"].items():
        old = prior["prices"].get(symbol, {})
        same_instrument = feed == "nasdaq" or row.get("source") == old.get("source")
        previous = crypto_time(old.get("watermark_at")) or observation_floor(old)
        previous = valid_at(previous, old, now)
        observed = observation_floor(row)
        if observed and not valid_at(observed, row, now):
            row.update(price_usd=None, freshness="missing", provider_observed_at=None,
                       provider_time_label=None)
            row.pop("watermark_at", None)
            observed = None
        if previous and same_instrument and (observed is None or observed < previous):
            # Preserve the watermark even through a gap. Only reuse a prior price
            # if its original time and receipt are available in the prior object.
            if row["price_usd"] is not None:
                retained = dict(old)
                if retained["price_usd"] is not None:
                    ts = crypto_time(retained.get("provider_observed_at"))
                    retained["freshness"] = "fresh" if ts and 0 <= (now-ts).total_seconds() <= 60 else "stale"
                candidate["prices"][symbol] = row = retained
            row["watermark_at"] = previous.isoformat()
        elif observed:
            row["watermark_at"] = observed.isoformat()


def crypto_freshness(prices, assessed_at):
    providers = {}
    for symbol, row in prices.items():
        stats = providers.setdefault(row["source"], {
            "counts": Counter(), "oldest_symbol": None, "oldest_age_seconds": None,
        })
        observed = crypto_time(row.get("provider_observed_at"))
        if row["price_usd"] is None:
            bucket = row["freshness"] = "missing"
        elif observed is None:
            bucket = row["freshness"] = "unknown_time"
        else:
            age = (assessed_at - observed).total_seconds()
            row["freshness"] = "fresh" if age <= 60 else "stale"
            bucket = "fresh" if age <= 60 else "stale_to_5m" if age <= 300 else "stale_over_5m"
            if stats["oldest_age_seconds"] is None or age > stats["oldest_age_seconds"]:
                stats.update(oldest_symbol=symbol, oldest_age_seconds=age)
        stats["counts"][bucket] += 1
    return {"assessed_at": assessed_at.isoformat(), "providers": providers}


def publish(client, bucket, feed, candidate, now, *, crypto_receipts=None):
    if candidate.get("schema_version") != "panel_minute.v1" or candidate.get("feed") != feed:
        raise ValueError("Invalid capture identity")
    stamp = crypto_time(candidate.get("started_at"))
    completed = crypto_time(candidate.get("completed_at"))
    if not stamp or not completed or not stamp <= completed <= now or (now - stamp).total_seconds() > 55:
        raise ValueError("Expired or future minute capture")
    if not 0 < len(candidate["prices"]) <= (50 if feed == "crypto" else 150):
        raise ValueError("Bad coverage")
    protect_observations(candidate, {"prices": {}}, feed, now)
    if not any(v["price_usd"] is not None for v in candidate["prices"].values()):
        raise ValueError("No priced rows")
    key = feed + "/prices.json"
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        with response["Body"] as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Prior minute object exceeds limit")
        previous = (raw, response["ETag"])
    except Exception as error:
        if _error_code(error) not in ("NoSuchKey", "NotFound", "404"):
            raise
        previous = None
    condition = {"IfNoneMatch": "*"}
    if previous:
        raw, etag = previous
        if feed == "crypto" and crypto_receipts is not None:
            spot_receipts.reconcile(crypto_receipts, "minute", raw, utcnow())
        prior = json.loads(raw)
        if crypto_time(prior["started_at"]) >= stamp:
            return "superseded"
        if crypto_time(prior["baseline_at"]) > crypto_time(candidate["baseline_at"]):
            return "older_baseline"
        condition = {"IfMatch": etag}
        protect_observations(candidate, prior, feed, now)
    if feed == "crypto":
        candidate["freshness_summary"] = crypto_freshness(candidate["prices"], utcnow())
    candidate["coverage"] = dict(Counter(v["freshness"] for v in candidate["prices"].values()))
    body = encode(candidate)
    attempt = None
    if feed == "crypto" and crypto_receipts is not None:
        attempt = spot_receipts.prepare(crypto_receipts, "minute", body, utcnow())
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                      CacheControl="no-store", **condition)
    if attempt is not None:
        spot_receipts.confirm(crypto_receipts, attempt, utcnow())
    return "published"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "publish"])
    parser.add_argument("feed", choices=FEEDS)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    root, feed = args.root, args.feed
    if args.action == "capture":
        if not args.baseline:
            parser.error("capture requires --baseline")
        if os.name == "nt":
            # The SSH caller can time out while a Windows process still runs.
            import msvcrt
            with (root / (feed + ".lock")).open("a+b") as lock:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                print(encode(capture(feed, args.baseline, root / (feed + "-cooldown.json"))).decode())
        else:
            print(encode(capture(feed, args.baseline, root / (feed + "-cooldown.json"))).decode())
        return
    if feed == "nasdaq" and not regular_session(json.loads((root / "sessions.json").read_bytes()), utcnow()):
        return
    hourly = root.parent / "optionsview-panel-publishers"
    if feed == "crypto":
        remote = (root / "msi-command.txt").read_text().strip()
        result = subprocess.run([str(hourly / "ssh-msi"), remote], capture_output=True,
                                check=True, timeout=47)
        if len(result.stdout) > MAX_BYTES:
            raise ValueError("Remote capture exceeds limit")
        candidate = json.loads(result.stdout)
    else:
        candidate = capture(feed, hourly / "data/nasdaq/latest.json", root / "nasdaq-cooldown.json")
        if not regular_session(json.loads((root / "sessions.json").read_bytes()), utcnow()):
            return
    import boto3
    from botocore.config import Config
    client = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                          config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))
    outcome = publish(client, os.environ["NASDAQ_R2_BUCKET"], feed, candidate, utcnow(),
                      crypto_receipts=root / "private" / "crypto-spot.sqlite3")
    report = dict(feed=feed, outcome=outcome, completed_at=candidate["completed_at"],
                  coverage=candidate["coverage"], calls=candidate["calls"])
    if outcome == "published" and feed == "crypto":
        report["freshness_summary"] = candidate["freshness_summary"]
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
