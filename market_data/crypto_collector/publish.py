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
import os
from pathlib import Path

from .collector import ARTIFACT_KEY, iso, snapshot_to_json, utcnow
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


def _publish_snapshot(snapshot, client, bucket):
    provenance = build_provenance(snapshot, publication_status="published")
    # A reader receives prices and their provenance in one atomic object read.
    # Publication acknowledgement time becomes knowable only after the PUT.
    payload = {**snapshot, "provenance": provenance}
    body = snapshot_to_json(payload).encode("utf-8")
    client.put_object(Bucket=bucket, Key=LATEST_KEY, Body=body,
                      ContentType="application/json")
    receipt = {**provenance, "latest_published_at": iso(utcnow()),
               "snapshot_sha256": hashlib.sha256(body).hexdigest()}
    client.put_object(Bucket=bucket, Key=SIDECAR_KEY,
                      Body=snapshot_to_json(receipt).encode("utf-8"),
                      ContentType="application/json")
    return receipt
