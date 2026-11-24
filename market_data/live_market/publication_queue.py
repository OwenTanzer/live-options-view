"""Durable FIFO of exact chain publication inputs, one worker per instrument.

Pending keys live outside the public commit namespace. Only a successful
immutable commit, compatible latest update and provenance write retire an item.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
import re
import time
import threading
from datetime import datetime, timedelta, timezone

from ..overview.readings import finish_publication, readings_digest
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
    def __init__(self, s3, bucket, symbol, *, spool_dir=None):
        self.s3, self.bucket = s3, bucket
        self.lane = UNDERLYING_SESSIONS[symbol]
        self.prefix = f"collector-pending/v1/{symbol}/"
        self.wake = threading.Event()
        self.session = None
        self.spool = None
        if spool_dir is not None:
            directory = Path(spool_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.spool = directory / f"{symbol}.sqlite3"
            with self._spool_connection() as db:
                db.execute("CREATE TABLE IF NOT EXISTS pending (key TEXT PRIMARY KEY, body BLOB NOT NULL)")

    def _spool_connection(self):
        return sqlite3.connect(self.spool)

    def start(self):
        threading.Thread(target=self.run, name=self.prefix, daemon=True).start()
        self.wake.set()  # discover any items left by a replaced process

    def enqueue(self, payload, provenance, csv_bytes):
        snapshot_key = payload["snapshot_key"]
        pending = {"first_attempt_at": None, "payload": payload, "provenance": provenance,
                   "csv_sha256": hashlib.sha256(csv_bytes).hexdigest()}
        raw = encode(pending)
        if len(raw) > 2 * EVIDENCE_BYTES:
            raise ValueError("publication item exceeds size bound")
        key = self.prefix + snapshot_key + ".json"
        try:
            self._admit(self.s3, key, raw, snapshot_key)
        except ValueError:
            raise  # immutable identity collision, never enqueue different bytes
        except Exception as error:
            if self.spool is None:
                raise
            # Both the create and its reconciliation were inconclusive. Keep
            # exact inputs across process restart while R2 is unavailable.
            with self._spool_connection() as db:
                added = db.execute("INSERT OR IGNORE INTO pending VALUES (?, ?)", (key, raw))
                if not added.rowcount and db.execute(
                    "SELECT body FROM pending WHERE key = ?", (key,)
                ).fetchone()[0] != raw:
                    raise ValueError("local pending snapshot collision")
            log.warning("Queue admission parked for retry %s: %s", snapshot_key, error)
            self.wake.set()
            return False
        self.wake.set()
        return True

    def _admit(self, store, key, raw, snapshot_key):
        try:
            store.put_object(Bucket=self.bucket, Key=key, Body=raw,
                               ContentType="application/json", IfNoneMatch="*")
        except Exception:
            # Resolve a lost acknowledgement against the serialized item.
            try:
                existing, _ = read(store, self.bucket, key, 2 * EVIDENCE_BYTES)
            except Exception as read_error:
                if not _missing(read_error):
                    raise
                # Another in-flight wake may already have finished this job.
                meta_key = snapshot_key[:-4] + ".provenance.json"
                final, _ = read(store, self.bucket, meta_key, EVIDENCE_BYTES)
                receipt = json.loads(final)
                if receipt.get("snapshot_key") == snapshot_key and receipt.get("one_clock_publication"):
                    return
                raise
            restored = json.loads(existing)
            if "first_attempt_at" in restored:
                restored["first_attempt_at"] = None
            if encode(restored) != raw:
                raise ValueError("pending snapshot collision")

    def publish_oldest(self):
        page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
        remote = next(iter(page.get("Contents", [])), {}).get("Key")
        if self.spool is not None:
            with self._spool_connection() as db:
                item = db.execute("SELECT key, body FROM pending ORDER BY key LIMIT 1").fetchone()
            if item and (remote is None or item[0] <= remote):
                key, raw = item
                # Restart after a remote worker already completed this item.
                csv_key = json.loads(raw)["payload"]["snapshot_key"]
                try:
                    meta, _ = read(self.s3, self.bucket, csv_key[:-4] + ".provenance.json", EVIDENCE_BYTES)
                except Exception as exc:
                    if not _missing(exc):
                        raise
                    completed = False
                else:
                    record = json.loads(meta)
                    completed = record.get("snapshot_key") == csv_key and record.get("one_clock_publication")
                if not completed:
                    self._admit(self.s3, key, raw, csv_key)
                with self._spool_connection() as db:
                    db.execute("DELETE FROM pending WHERE key = ? AND body = ?", (key, raw))
                return True
        if not page.get("Contents"):
            return False
        key = page["Contents"][0]["Key"]
        raw, metadata = read(self.s3, self.bucket, key, 2 * EVIDENCE_BYTES)
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
        # Older queued jobs already carry exact prepared envelopes. Changing
        # their bytes could conflict with a previously saved immutable intent.
        if "first_attempt_at" in pending:
            if pending["first_attempt_at"] is None:
                pending["first_attempt_at"] = datetime.now(timezone.utc).isoformat()
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=encode(pending),
                                   ContentType="application/json", IfMatch=metadata["ETag"])
            readings = payload["market_readings"]
            finish_publication(readings, published_at=datetime.fromisoformat(pending["first_attempt_at"]),
                               status="published")
            provenance["market_readings"] = readings
            payload["market_readings_digest"] = provenance["market_readings_digest"] = readings_digest(readings)
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
                # S3 Last-Modified can be rounded down to a whole second.
                # Use a confirmed upper bound when recovering a lost PUT ack.
                modified = response["LastModified"]
                if not modified.microsecond:
                    modified = min(datetime.now(timezone.utc), modified + timedelta(seconds=1))
                return modified.isoformat()
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
