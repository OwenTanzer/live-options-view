"""R2 publication for the NASDAQ-100 snapshot (opt-in only).

Writes ``nasdaq/latest.json`` and its ``snapshot_provenance.v1`` sidecar
``nasdaq/latest.provenance.json``. Configuration comes from the environment
and is **unconfirmed against live service config** — the bucket name and
endpoint below are the collector's expected names, not a verified live
deployment:

- ``R2_ENDPOINT_URL`` — R2 S3-compatible endpoint
- ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
- ``NASDAQ_R2_BUCKET`` — bucket holding the ``nasdaq/`` prefix

Nothing in this module runs unless the CLI is given ``--publish``.
"""

from __future__ import annotations

import json
import os

from .provenance import ARTIFACT_KEY, SIDECAR_KEY

ENV_VARS = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", "NASDAQ_R2_BUCKET")


def read_config(env=None):
    """Read R2 config from the environment; raise if any value is missing."""
    env = os.environ if env is None else env
    missing = [name for name in ENV_VARS if not env.get(name)]
    if missing:
        raise RuntimeError(
            "missing R2 configuration for NASDAQ publish: "
            + ", ".join(missing))
    return {name: env[name] for name in ENV_VARS}


def make_client(config):
    """Build a boto3 S3 client pointed at the R2 endpoint (lazy import)."""
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=config["R2_ENDPOINT_URL"],
        aws_access_key_id=config["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=config["R2_SECRET_ACCESS_KEY"],
    )


def publish_snapshot(snapshot, provenance, config=None, client=None):
    """Upload the snapshot and sidecar; return the written keys.

    ``config`` defaults to ``read_config()``; ``client`` may be injected
    (tests pass a fake). The snapshot is uploaded first, then the sidecar;
    a sidecar failure is raised, not silently swallowed, so a published
    snapshot is never left without its provenance claim.
    """
    config = config or read_config()
    client = client or make_client(config)
    bucket = config["NASDAQ_R2_BUCKET"]
    body = json.dumps(snapshot, separators=(",", ":")).encode()
    client.put_object(Bucket=bucket, Key=ARTIFACT_KEY, Body=body,
                      ContentType="application/json")
    sidecar_body = json.dumps(provenance, separators=(",", ":")).encode()
    client.put_object(Bucket=bucket, Key=SIDECAR_KEY, Body=sidecar_body,
                      ContentType="application/json")
    return {"bucket": bucket, "keys": [ARTIFACT_KEY, SIDECAR_KEY]}
