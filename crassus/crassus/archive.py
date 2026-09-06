"""Private, incremental R2 ledger backup; local files remain authoritative.

One writer per data root. A durable pending range makes ambiguous uploads
retry the identical object even while the ledger grows. Only acknowledged,
verified objects advance offsets. No data is pruned or automatically restored.
"""
from __future__ import annotations

import hashlib
import datetime
import re
import json
import logging
import os
import threading
import uuid
from pathlib import Path

from . import clock
from .durability import atomic_json
from .observability import event

log = logging.getLogger("crassus.archive")
BATCH_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024


class LedgerArchive:
    def __init__(self, ledger_dir: Path, checkpoint: Path, client, bucket: str):
        self.ledger_dir = ledger_dir
        self.checkpoint = checkpoint
        self.client = client
        self.bucket = bucket
        if checkpoint.exists():
            self.state = json.loads(checkpoint.read_text())
            self._validate_state(bucket)
        else:
            self.state = {"version": 1, "bucket": bucket, "namespace": uuid.uuid4().hex,
                          "files": {}, "last_success_at": None}
            self._save()
        self.stop = threading.Event()
        self.thread = None

    def _validate_state(self, bucket):
        state = self.state
        def invalid():
            raise ValueError("Invalid archive checkpoint or changed bucket")
        if (not isinstance(state, dict) or type(state.get("version")) is not int
                or state["version"] != 1 or state.get("bucket") != bucket
                or not isinstance(state.get("files"), dict)
                or not isinstance(state.get("namespace"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", state["namespace"])
                or "last_success_at" not in state):
            invalid()
        timestamp = state["last_success_at"]
        if timestamp is not None:
            if not isinstance(timestamp, str):
                invalid()
            try:
                parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    invalid()
            except ValueError:
                invalid()
        for name, entry in state["files"].items():
            if (not isinstance(name, str)
                    or not re.fullmatch(r"decisions-[A-Za-z0-9_-]+\.jsonl", name)
                    or not isinstance(entry, dict)
                    or type(entry.get("offset")) is not int or entry["offset"] < 0):
                invalid()
            if "pending" in entry:
                pending = entry["pending"]
                if (not isinstance(pending, dict) or type(pending.get("end")) is not int
                        or not 0 < pending["end"] - entry["offset"] <= BATCH_BYTES + MAX_RECORD_BYTES
                        or not isinstance(pending.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", pending["sha256"])):
                    invalid()

    def _save(self):
        atomic_json(self.checkpoint, self.state)

    def _batch(self, path, offset):
        chunks = []
        size = 0
        with path.open("rb") as stream:
            stream.seek(offset)
            while size < BATCH_BYTES:
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if len(line) > MAX_RECORD_BYTES:
                    raise ValueError("Ledger record exceeds archive limit")
                if not line or not line.endswith(b"\n"):
                    break  # concurrent append or damaged trailing record: retain locally
                json.loads(line)  # never checkpoint past corrupt data
                chunks.append(line)
                size += len(line)
        return b"".join(chunks)

    def _upload_one(self, path):
        entry = self.state["files"].setdefault(path.name, {"offset": 0})
        offset = entry["offset"]
        if not isinstance(offset, int) or offset < 0 or path.stat().st_size < offset:
            raise ValueError("Ledger truncated or invalid archive offset")
        pending = entry.get("pending")
        if pending:
            with path.open("rb") as stream:
                stream.seek(offset)
                body = stream.read(pending["end"] - offset)
            if hashlib.sha256(body).hexdigest() != pending["sha256"]:
                raise ValueError("Pending archive bytes changed")
        else:
            body = self._batch(path, offset)
            if not body:
                return False
            pending = {"end": offset + len(body), "sha256": hashlib.sha256(body).hexdigest()}
            entry["pending"] = pending
        self._save()  # freeze the range BEFORE every attempt, including failed local saves
        digest = pending["sha256"]
        key = (f"crassus-ledger/v1/{self.state['namespace']}/{path.name}/"
               f"{offset:020d}-{pending['end']:020d}-{digest}.jsonl")
        metadata = {"sha256": digest, "start-byte": str(offset), "end-byte": str(pending["end"])}
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                   ContentType="application/x-ndjson", Metadata=metadata,
                                   IfNoneMatch="*")
        except Exception as exc:
            # A previous attempt may have succeeded before its response was lost.
            # Only a conditional-exists response is eligible for verification.
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise
        remote = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = remote["Body"]
        try:
            downloaded = stream.read(len(body) + 1)
        finally:
            stream.close()
        if (remote.get("ContentLength") != len(body) or remote.get("Metadata") != metadata
                or hashlib.sha256(downloaded).hexdigest() != digest):
            raise ValueError("Archive object verification failed")
        entry["offset"] = pending["end"]
        entry.pop("pending", None)
        previous_success = self.state["last_success_at"]
        self.state["last_success_at"] = clock.iso_utc()
        # If save fails, revert in-memory progress too; disk pending retries safely.
        try:
            self._save()
        except Exception:
            entry["offset"] = offset
            entry["pending"] = pending
            self.state["last_success_at"] = previous_success
            raise
        event(log, "archive_uploaded", bytes_uploaded=len(body), sha256=digest,
              last_success_at=self.state["last_success_at"])
        return True

    def tick(self):
        files = sorted(self.ledger_dir.glob("decisions-*.jsonl"))
        missing = set(self.state["files"]) - {p.name for p in files}
        if missing:
            raise ValueError("Previously tracked ledger file missing")
        remaining = 4  # bound work per poll, and allow shutdown between requests
        for path in files:
            while remaining and not self.stop.is_set() and self._upload_one(path):
                remaining -= 1
            if not remaining or self.stop.is_set():
                break
        backlog = sum(max(0, p.stat().st_size - self.state["files"].get(p.name, {}).get("offset", 0))
                      for p in files)
        event(log, "archive_status", backlog_bytes=backlog,
              last_success_at=self.state["last_success_at"])
        return backlog

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                # SDK exception strings may contain request details. Never echo them.
                event(log, "archive_failed", level=logging.ERROR, error_type=type(exc).__name__,
                      last_success_at=self.state["last_success_at"])
            self.stop.wait(30)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="ledger-archive", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


def from_environment(ledger_dir: Path, state_dir: Path):
    names = ["BUCKET", "ACCOUNT_ID", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "PRIVATE"]
    values = {name: os.environ.get("CRASSUS_ARCHIVE_" + name, "") for name in names}
    if not any(values.values()):
        event(log, "archive_disabled")
        return None
    if not all(values.values()) or values["PRIVATE"] != "true":
        raise ValueError("Set all CRASSUS_ARCHIVE settings and PRIVATE=true for a private bucket")
    if values["BUCKET"] == os.environ.get("R2_BUCKET_NAME"):
        raise ValueError("Archive bucket must differ from collector bucket")
    account = values["ACCOUNT_ID"]
    if len(account) != 32 or any(c not in "0123456789abcdef" for c in account):
        raise ValueError("Invalid archive R2 account ID")
    # --verbose must not enable SDK request/signature/response-body dumps.
    for name in ("boto3", "botocore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    import boto3
    from botocore.config import Config
    client = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                          aws_access_key_id=values["ACCESS_KEY_ID"],
                          aws_secret_access_key=values["SECRET_ACCESS_KEY"], region_name="auto",
                          config=Config(connect_timeout=3, read_timeout=10,
                                        retries={"total_max_attempts": 1},
                                        request_checksum_calculation="when_required",
                                        response_checksum_validation="when_required"))
    return LedgerArchive(ledger_dir, state_dir / "archive" / "checkpoint.json", client, values["BUCKET"])
