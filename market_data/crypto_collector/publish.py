"""Publish a crypto snapshot: local files, optionally R2.

Local write is the default and the only thing tests and smoke runs
do: ``<out>/crypto/latest.json`` plus the provenance sidecar
``<out>/crypto/latest.provenance.json``.

R2 publication (``--publish``) uses boto3's S3-compatible client
against the same bucket the other producers use
(``qqq-options-chain-data``). Credentials and endpoint come from the
environment -- the same R2 service configuration the live collector
runs under -- and are never read from the repository:

- ``R2_ENDPOINT_URL`` (e.g. ``https://<account>.r2.cloudflarestorage.com``)
- ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
  (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` also accepted)
- ``CRYPTO_R2_BUCKET`` (default ``qqq-options-chain-data``)

The latest snapshot carries the producer provenance in the same object.
The separate receipt records acknowledgement time and the digest of the
exact object acknowledged. A failed receipt write leaves the new snapshot
self-contained. Only a matching receipt can establish its publication time.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
import os
from pathlib import Path

from .collector import ARTIFACT_KEY, iso, parse_time, snapshot_to_json, utcnow
from .provenance import build_provenance

LATEST_KEY = ARTIFACT_KEY  # crypto/latest.json
SIDECAR_KEY = "crypto/latest.provenance.json"


def write_local(out_dir, snapshot, *, publication_status="published"):
    """Write latest.json + sidecar under ``out_dir``; return paths."""
    out = Path(out_dir)
    crypto_dir = out / "crypto"
    crypto_dir.mkdir(parents=True, exist_ok=True)
    latest_path = crypto_dir / "latest.json"
    sidecar_path = crypto_dir / "latest.provenance.json"
    latest_path.write_text(snapshot_to_json(snapshot), encoding="utf-8")
    provenance = build_provenance(
        snapshot, publication_status=publication_status,
        latest_published_at=iso(utcnow()))
    sidecar_path.write_text(
        snapshot_to_json(provenance), encoding="utf-8")
    return {"latest": latest_path, "sidecar": sidecar_path,
            "provenance": provenance}


def publish_r2(snapshot, *, bucket=None, endpoint_url=None):
    """Upload latest.json + sidecar to R2. Returns the sidecar dict.

    boto3 is imported lazily so offline/test runs never need it.
    Raises on any upload failure.
    """
    import boto3  # noqa: PLC0415 -- optional runtime dependency

    bucket = bucket or os.environ.get("CRYPTO_R2_BUCKET", "qqq-options-chain-data")
    endpoint_url = endpoint_url or os.environ.get("R2_ENDPOINT_URL")
    if not endpoint_url:
        raise RuntimeError("R2_ENDPOINT_URL is not set; refusing to publish")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID")
        or os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY")
        or os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    return _publish_snapshot(snapshot, client, bucket)


def _error_code(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _read(client, bucket, key):
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _error_code(exc) in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    with closing(response["Body"]) as stream:
        body = stream.read()
    return body, response["ETag"], response["LastModified"]


def _timestamp(record):
    timestamp = parse_time(record.get("constructed_at"))
    if timestamp is None:
        raise ValueError("Missing or invalid snapshot construction time")
    return timestamp


def _advance(client, bucket, key, record):
    """Compare the exact version read; retry only a concurrent-write conflict."""
    stamp = _timestamp(record)
    body = snapshot_to_json(record).encode("utf-8")
    for _ in range(8):
        current = _read(client, bucket, key)
        condition = {"IfNoneMatch": "*"}
        if current is not None:
            existing, etag, modified = current
            prior = json.loads(existing)
            previous_stamp = _timestamp(prior)
            if previous_stamp > stamp:
                return None, None
            if previous_stamp == stamp:
                if existing != body and not (
                    key == SIDECAR_KEY and prior.get("snapshot_sha256") == record["snapshot_sha256"]
                ):
                    raise ValueError(f"Conflicting {key} contents for the same construction time")
                return prior, iso(modified)
            condition = {"IfMatch": etag}
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body,
                              ContentType="application/json", **condition)
        except Exception as exc:
            if _error_code(exc) in ("PreconditionFailed", "ConditionalRequestConflict", "409", "412"):
                continue
            raise
        return record, iso(utcnow())
    raise RuntimeError(f"Concurrent writers prevented publication of {key}")


def _publish_snapshot(snapshot, client, bucket):
    provenance = build_provenance(snapshot, publication_status="published")
    payload = {**snapshot, "provenance": provenance}
    accepted, acknowledged = _advance(client, bucket, LATEST_KEY, payload)
    if accepted is None:
        return None
    digest = hashlib.sha256(snapshot_to_json(payload).encode("utf-8")).hexdigest()
    receipt = {**provenance, "latest_published_at": acknowledged, "snapshot_sha256": digest}
    stored, _ = _advance(client, bucket, SIDECAR_KEY, receipt)
    return stored
