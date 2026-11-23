"""Immutable public commit log using the existing collector's S3 client.

No feed, thread, timer or local credential loading. Prepared intents bind a
publication UUID to exact commit bytes before PUT, so an ambiguous acknowledgement
can be resolved after a process restart without creating a second identity.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


ENTRY_BYTES = 16384
EVIDENCE_BYTES = 4 * 1024 * 1024
RECOVERY_STEPS = 8


class PublicationError(RuntimeError):
    pass


class PublicationBudget(PublicationError):
    pass


class BudgetedS3:
    """Cooperative wall/request budget; owning client supplies socket timeouts."""
    def __init__(self, client, *, seconds=5, requests=40, clock=time.monotonic):
        self.client, self.clock, self.deadline, self.limit = client, clock, clock() + seconds, requests
        self.requests = 0

    def check(self):
        if self.clock() >= self.deadline:
            raise PublicationBudget("publication time budget exhausted")

    def _charge(self):
        self.check()
        if self.requests >= self.limit:
            raise PublicationBudget("publication request budget exhausted")
        self.requests += 1

    def get_object(self, **kwargs):
        self._charge()
        result = self.client.get_object(**kwargs)
        body, budget = result["Body"], self

        class BoundedBody:
            def read(self, size):
                remaining, pieces = size, []
                while remaining:
                    budget.check()
                    piece = body.read(min(16384, remaining))
                    if not piece:
                        break
                    pieces.append(piece); remaining -= len(piece)
                return b"".join(pieces)

            def close(self):
                body.close()

        return {**result, "Body": BoundedBody()}

    def put_object(self, **kwargs):
        self._charge()
        return self.client.put_object(**kwargs)


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text_units(value):
    """Wire limits are JavaScript UTF-16 code units, not Python code points."""
    return len(value.encode("utf-16-le")) // 2


def _missing(error):
    return str(getattr(error, "response", {}).get("Error", {}).get("Code")) in {
        "404", "NoSuchKey", "NotFound"}


def _read(s3, bucket, key, bound):
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _missing(exc):
            return None
        raise PublicationError("publication read unavailable") from exc
    body = obj["Body"]
    try:
        raw = body.read(bound + 1)
    finally:
        body.close()
    if len(raw) > bound:
        raise PublicationError("publication object exceeds bound")
    return raw, obj.get("ETag")


def _create_verified(s3, bucket, key, raw, content_type, bound):
    """Even ambiguous PUT and precondition failure resolve against exact bytes."""
    if not raw or len(raw) > bound:
        raise PublicationError("publication object exceeds bound")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=raw, ContentType=content_type,
                      IfNoneMatch="*")
    except Exception:
        # Do not infer either failure or success from a lost acknowledgement.
        pass
    actual = _read(s3, bucket, key, bound)
    if actual is None or actual[0] != raw:
        raise PublicationError("immutable publication collision or unresolved write")
    return actual


class PublicationLog:
    def __init__(self, s3, bucket, *, source, instrument, owner, outputs, session_basis="new-york"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", instrument):
            raise PublicationError("invalid publication stream")
        if not isinstance(owner, str) or not 1 <= text_units(owner) <= 100:
            raise PublicationError("invalid publication owner")
        self.s3, self.bucket, self.owner = s3, bucket, owner
        if session_basis not in {"new-york", "utc"} or not isinstance(outputs, dict) or not 1 <= len(outputs) <= 10:
            raise PublicationError("invalid public descriptor")
        for name, fields in outputs.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) or not isinstance(fields, dict) or not 1 <= len(fields) <= 32:
                raise PublicationError("invalid public output")
            for field, kind in fields.items():
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", field) or kind not in {"number", "string", "boolean"}:
                    raise PublicationError("invalid public field")
        self.outputs, self.session_basis = outputs, session_basis
        self.stream = source + "/" + instrument
        self.prefix = "publications/v1/" + self.stream + "/"
        self.head_key = self.prefix + "head.json"

    def key(self, sequence):
        if type(sequence) is not int or not 1 <= sequence < 10**12:
            raise PublicationError("publication sequence bound")
        return self.prefix + f"entries/{sequence:012d}.json"

    def _validate_entry(self, raw, sequence=None):
        try:
            value = json.loads(raw)
            if value["schema"] != "one-clock.publication.v1" or value["stream"] != self.stream or value["owner"] != self.owner:
                raise ValueError()
            self.key(value["sequence"])
            if sequence is not None and value["sequence"] != sequence:
                raise ValueError()
            if str(uuid.UUID(value["publicationId"])) != value["publicationId"]:
                raise ValueError()
            if set(value) != {"schema", "stream", "sequence", "publicationId", "owner", "previousDigest", "createdAt", "records", "evidence"}:
                raise ValueError()
            if value["sequence"] == 1:
                if value["previousDigest"] is not None:
                    raise ValueError()
            elif not re.fullmatch(r"[a-f0-9]{64}", value["previousDigest"] or ""):
                raise ValueError()
            records, created = self._records(value["records"], value["createdAt"])
            if records != value["records"] or created != value["createdAt"] or not 1 <= len(value["evidence"]) <= 4:
                raise ValueError()
            for ref in value["evidence"]:
                if (set(ref) != {"key", "sha256", "bytes", "contentType", "schema"} or
                        not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"]) or ref["key"] != self.prefix + "blobs/" + ref["sha256"] or
                        type(ref["bytes"]) is not int or not 1 <= ref["bytes"] <= EVIDENCE_BYTES or
                        ref["contentType"] not in {"application/json", "text/csv"} or not isinstance(ref["schema"], str) or not 1 <= text_units(ref["schema"]) <= 100):
                    raise ValueError()
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise PublicationError("invalid publication entry") from exc

    def _head(self):
        result = _read(self.s3, self.bucket, self.head_key, 1024)
        if result is None:
            return 0, None, None
        try:
            head = json.loads(result[0])
            if head["stream"] != self.stream or head["schema"] != "one-clock.head.v1":
                raise ValueError()
            sequence = head["sequence"]
            entry = _read(self.s3, self.bucket, self.key(sequence), ENTRY_BYTES)
            if entry is None or digest(entry[0]) != head["digest"]:
                raise ValueError()
            self._validate_entry(entry[0], sequence)
            return sequence, head["digest"], result[1]
        except (ValueError, KeyError, TypeError) as exc:
            raise PublicationError("invalid publication head") from exc

    def _advance_head(self, sequence, sha, old_etag):
        raw = wire({"schema": "one-clock.head.v1", "stream": self.stream,
                    "sequence": sequence, "digest": sha})
        try:
            response = self.s3.put_object(Bucket=self.bucket, Key=self.head_key, Body=raw,
                              ContentType="application/json",
                              **({"IfMatch": old_etag} if old_etag else {"IfNoneMatch": "*"}))
        except Exception:
            # Commit remains authoritative and discoverable by direct next GET.
            return False
        return (response or {}).get("ETag") or _read(self.s3, self.bucket, self.head_key, 1024)[1]

    def _identity_key(self, record):
        return self.prefix + "records/" + digest(wire([record["source"], record["id"]])) + ".json"

    def _identity(self, record):
        found = _read(self.s3, self.bucket, self._identity_key(record), 4096)
        if not found:
            return None, None
        try:
            value = json.loads(found[0])
            if (value["source"] != record["source"] or value["id"] != record["id"] or
                    type(value["revision"]) is not int or value["revision"] < 1 or
                    type(value["sequence"]) is not int or value["sequence"] < 1):
                raise ValueError()
            return value, found[1]
        except (ValueError, KeyError, TypeError) as exc:
            raise PublicationError("invalid publication identity head") from exc

    def _check_revisions(self, entry):
        for record in entry["records"]:
            prior, _ = self._identity(record)
            if prior and (prior["session"] != record["session"] or prior["revision"] >= record["revision"]):
                raise PublicationError("publication identity or revision conflict")

    def _repair_identities(self, entry):
        for record in entry["records"]:
            prior, etag = self._identity(record)
            target = {k: record[k] for k in ("source", "id", "session", "revision")}
            target.update(sequence=entry["sequence"], publicationId=entry["publicationId"])
            if prior:
                if prior["session"] != target["session"]:
                    raise PublicationError("publication identity scope conflict")
                if prior["sequence"] > target["sequence"] and prior["revision"] > target["revision"]:
                    continue
                if prior == target:
                    continue
                if prior["sequence"] >= target["sequence"] or prior["revision"] >= target["revision"]:
                    raise PublicationError("publication identity repair conflict")
            raw = wire(target)
            try:
                self.s3.put_object(Bucket=self.bucket, Key=self._identity_key(record), Body=raw,
                                   ContentType="application/json", **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}))
            except Exception:
                actual = _read(self.s3, self.bucket, self._identity_key(record), 4096)
                if actual is None or actual[0] != raw:
                    raise PublicationError("publication identity repair unavailable")

    def recover(self):
        sequence, sha, etag = self._head()
        for _ in range(RECOVERY_STEPS):
            entry = _read(self.s3, self.bucket, self.key(sequence + 1), ENTRY_BYTES)
            if entry is None:
                return sequence, sha
            value = self._validate_entry(entry[0], sequence + 1)
            if value["previousDigest"] != sha:
                raise PublicationError("publication chain mismatch")
            self._repair_identities(value)
            sequence, sha = sequence + 1, digest(entry[0])
            etag = self._advance_head(sequence, sha, etag)
            if not etag:
                raise PublicationError("publication head repair unavailable")
        # Repair the hint in bounded increments. Never infer the unseen tail.
        raise PublicationError("publication recovery continuation required")

    def _records(self, records, created_at):
        if not isinstance(records, list) or not 1 <= len(records) <= 10:
            raise PublicationError("publication record bound")
        created = self._time(created_at)
        created_at = self._iso(created)
        records = [{**record, "eventAt": self._iso(self._time(record.get("eventAt")))} for record in records]
        identities = set()
        for record in records:
            if set(record) != {"source", "id", "revision", "session", "eventAt", "value"} or record["source"] not in self.outputs:
                raise PublicationError("unregistered publication record")
            if not isinstance(record["id"], str) or not 1 <= text_units(record["id"]) <= 500 or type(record["revision"]) is not int or not 1 <= record["revision"] <= 2**53-1:
                raise PublicationError("invalid publication record identity")
            identity = record["source"], record["id"]
            if identity in identities:
                raise PublicationError("duplicate publication record")
            identities.add(identity)
            event = self._time(record["eventAt"])
            zone = ZoneInfo("America/New_York") if self.session_basis == "new-york" else timezone.utc
            if event > created or event.astimezone(zone).date().isoformat() != record["session"]:
                raise PublicationError("publication record scope")
            value, fields = record["value"], self.outputs[record["source"]]
            if value is not None:
                if not isinstance(value, dict) or set(value) != set(fields):
                    raise PublicationError("publication field allowlist")
                for key, kind in fields.items():
                    v = value[key]
                    if v is None:
                        continue
                    valid = (kind == "number" and type(v) in {int, float} and math.isfinite(v) or
                             kind == "boolean" and type(v) is bool or
                             kind == "string" and isinstance(v, str) and text_units(v) <= 2000)
                    if not valid:
                        raise PublicationError("publication field type")
        return records, created_at

    def publish(self, *, publication_id, records, evidence, created_at):
        if str(uuid.UUID(publication_id)) != publication_id:
            raise PublicationError("invalid publication UUID")
        if not 1 <= len(evidence) <= 4:
            raise PublicationError("publication evidence bound")
        records, created_at = self._records(records, created_at)
        refs = []
        for raw, content_type, schema in evidence:
            if not isinstance(raw, bytes) or content_type not in {"application/json", "text/csv"} or not isinstance(schema, str) or not 1 <= text_units(schema) <= 100:
                raise PublicationError("invalid evidence")
            key = self.prefix + "blobs/" + digest(raw)
            _create_verified(self.s3, self.bucket, key, raw, content_type, EVIDENCE_BYTES)
            refs.append({"key": key, "sha256": digest(raw), "bytes": len(raw),
                         "contentType": content_type, "schema": schema})
        intent_key = self.prefix + "intents/" + publication_id + ".json"
        saved = _read(self.s3, self.bucket, intent_key, ENTRY_BYTES)
        if saved:
            raw = saved[0]
            entry = self._validate_entry(raw)
            if entry["publicationId"] != publication_id or entry["records"] != records or entry["evidence"] != refs or entry["createdAt"] != created_at:
                raise PublicationError("immutable publication intent mismatch")
        else:
            sequence, previous = self.recover()
            entry = {"schema": "one-clock.publication.v1", "stream": self.stream,
                     "sequence": sequence + 1, "publicationId": publication_id,
                     "owner": self.owner, "previousDigest": previous,
                     "createdAt": created_at, "records": records, "evidence": refs}
            raw = wire(entry)
            _create_verified(self.s3, self.bucket, intent_key, raw, "application/json", ENTRY_BYTES)
        sequence = entry["sequence"]
        committed = _read(self.s3, self.bucket, self.key(sequence), ENTRY_BYTES)
        if committed is not None and committed[0] != raw:
            raise PublicationError("immutable publication collision")
        if committed is None:
            # Recover outstanding identity repairs before preflight. The stream
            # head advances only after every committed record head is repaired.
            last, previous = self.recover()
            if last != sequence - 1 or previous != entry["previousDigest"]:
                raise PublicationError("publication intent predecessor changed")
            self._check_revisions(entry)
        _create_verified(self.s3, self.bucket, self.key(sequence), raw, "application/json", ENTRY_BYTES)
        # A failed hint update never turns a durable commit into an unpublished
        # one. A newer head is left alone on an idempotent old-intent retry.
        try:
            self._repair_identities(entry)
            current, _, etag = self._head()
            if current < sequence:
                self._advance_head(sequence, digest(raw), etag)
        except PublicationError:
            pass
        return {"stream": self.stream, "sequence": sequence, "publicationId": publication_id,
                "digest": digest(raw), "key": self.key(sequence)}

    @staticmethod
    def _iso(value):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _time(value):
        try:
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
            elif type(value) is int and 10**12 <= value <= 2**53-1:
                parsed = datetime.fromtimestamp(value / 1000, timezone.utc)
            else:
                raise ValueError()
            return parsed
        except (ValueError, OverflowError, OSError) as exc:
            raise PublicationError("invalid publication time") from exc


def snapshot_publication_id(snapshot_key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "one-clock:collector:" + snapshot_key))
