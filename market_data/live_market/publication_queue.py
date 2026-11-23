"""Durable FIFO of exact chain publication inputs, one worker per instrument.

Pending keys live outside the public commit namespace. Only a successful
immutable commit, compatible latest update and provenance write retire an item.
"""
import hashlib
import json
import re
import time
import threading
from datetime import datetime, timezone

from .config import log
from .one_clock import publish_snapshot
from .publications import EVIDENCE_BYTES, _missing
from .state import UNDERLYING_SESSIONS


def encode(value):
    return json.dumps(value, default=str, separators=(",", ":")).encode()


def read(s3, bucket, key, limit):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        raw = body.read(limit + 1)
    finally:
        body.close()
    if len(raw) > limit:
        raise ValueError("publication input exceeds size bound")
    return raw, response


class PublicationQueue:
    def __init__(self, s3, bucket, symbol):
        self.s3, self.bucket = s3, bucket
        self.lane = UNDERLYING_SESSIONS[symbol]
        self.prefix = f"collector-pending/v1/{symbol}/"
        self.wake = threading.Event()
        self.session = None

    def start(self):
        threading.Thread(target=self.run, name=self.prefix, daemon=True).start()
        self.wake.set()  # discover any items left by a replaced process

    def enqueue(self, payload, provenance, csv_bytes):
        snapshot_key = payload["snapshot_key"]
        pending = {"payload": payload, "provenance": provenance,
                   "csv_sha256": hashlib.sha256(csv_bytes).hexdigest()}
        raw = encode(pending)
        if len(raw) > 2 * EVIDENCE_BYTES:
            raise ValueError("publication item exceeds size bound")
        key = self.prefix + snapshot_key + ".json"
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw,
                               ContentType="application/json", IfNoneMatch="*")
        except Exception:
            # Resolve a lost acknowledgement against the serialized item.
            existing, _ = read(self.s3, self.bucket, key, len(raw))
            if existing != raw:
                raise ValueError("pending snapshot collision")
        self.wake.set()

    def publish_oldest(self):
        page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
        if not page.get("Contents"):
            return False
        key = page["Contents"][0]["Key"]
        raw, _ = read(self.s3, self.bucket, key, 2 * EVIDENCE_BYTES)
        pending = json.loads(raw)
        payload, provenance = pending["payload"], pending["provenance"]
        csv_key = payload["snapshot_key"]
        expected_prefix = self.lane.day_prefix(payload["date"].replace("-", ""))
        if (key != self.prefix + csv_key + ".json" or payload["symbol"] != self.lane.symbol
                or provenance["snapshot_key"] != csv_key or provenance["symbol"] != self.lane.symbol
                or not csv_key.startswith(expected_prefix)
                or not re.fullmatch(r"snapshot_\d{12}\.csv", csv_key[len(expected_prefix):])):
            raise ValueError("pending item does not belong to this capture lane")
        csv, _ = read(self.s3, self.bucket, csv_key, EVIDENCE_BYTES)
        if hashlib.sha256(csv).hexdigest() != pending["csv_sha256"]:
            raise ValueError("archived CSV differs from queued snapshot")
        publication = publish_snapshot(self.s3, self.bucket, payload, csv)
        payload["one_clock_publication"] = provenance["one_clock_publication"] = publication
        published = self.advance_latest(self.lane.latest_key, payload)
        provenance["publication"] = {"status": "published" if published else "committed",
                                     "reason": None if published else "newer_latest_exists"}
        provenance["latest_published_at"] = published
        self.s3.put_object(Bucket=self.bucket, Key=csv_key[:-4] + ".provenance.json",
                           Body=encode(provenance), ContentType="application/json",
                           CacheControl="no-cache, max-age=0")
        self.s3.delete_object(Bucket=self.bucket, Key=key)
        session = self.session
        if published and session and payload["date"] == session[0]:
            session[1].inc_snapshot(payload["timestamp"])
        return True

    def advance_latest(self, key, payload):
        # Preserve a newer view even after an ambiguous latest acknowledgement,
        # replacement process, or replay of a pending item from a prior day.
        try:
            raw, response = read(self.s3, self.bucket, key, EVIDENCE_BYTES)
        except Exception as exc:
            if not _missing(exc):
                raise
            condition = {"IfNoneMatch": "*"}
        else:
            existing = json.loads(raw)
            if existing.get("snapshot_key") == payload["snapshot_key"]:
                if existing.get("one_clock_publication") != payload["one_clock_publication"]:
                    raise ValueError("latest publication identity mismatch")
                return response["LastModified"].isoformat()
            if datetime.fromisoformat(existing["timestamp"]) >= datetime.fromisoformat(payload["timestamp"]):
                return None
            condition = {"IfMatch": response["ETag"]}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=encode(payload),
                           ContentType="application/json", CacheControl="no-cache, max-age=0",
                           **condition)
        return datetime.now(timezone.utc).isoformat()

    def run(self):
        backoff = 2
        while True:
            self.wake.wait()
            self.wake.clear()
            try:
                while self.publish_oldest():
                    backoff = 2
            except Exception as exc:
                log.warning("Pending publication retained %s retry in %ss: %s",
                            self.prefix, backoff, exc)
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
                self.wake.set()
