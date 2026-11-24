"""R2 publication for single-stock historical candles (opt-in only).

Writes, per ticker, the eleven interval files
``nasdaq/candles/<TICKER>/<interval>.json`` and the ticker's
``snapshot_provenance.v1`` sidecar
``nasdaq/candles/<TICKER>/provenance.json``. Configuration comes from
the environment and is **unconfirmed against live service config** —
the bucket name and endpoint below are the collector's expected
names, not a verified live deployment:

- ``R2_ENDPOINT_URL`` — R2 S3-compatible endpoint
- ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
- ``TIINGO_R2_BUCKET`` — bucket holding the ``nasdaq/`` prefix
  (expected to be the same ``qqq-options-chain-data`` bucket that
  holds ``nasdaq/latest.json``; unconfirmed)

Nothing in this module runs unless the CLI is given ``--publish``.
The interval files are uploaded first, then the sidecar; a sidecar
failure is raised, not silently swallowed, so a published ticker is
never left without its provenance claim.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os

from .provenance import interval_key, sidecar_key

ENV_VARS = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", "TIINGO_R2_BUCKET")


def read_config(env=None):
    """Read R2 config from the environment; raise if any value is missing."""
    env = os.environ if env is None else env
    missing = [name for name in ENV_VARS if not env.get(name)]
    if missing:
        raise RuntimeError(
            "missing R2 configuration for single-stock candle publish: "
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


def publish_symbol(symbol, files, provenance, config=None, client=None):
    """Upload one ticker's interval files and sidecar; return written keys.

    ``files`` is ``{interval: content-dict}``. ``config`` defaults to
    ``read_config()``; ``client`` may be injected (tests pass a fake).
    The sidecar is uploaded last; a failure there raises, so a caller
    never reports a ticker as published without its provenance.
    """
    config = config or read_config()
    client = client or make_client(config)
    bucket = config["TIINGO_R2_BUCKET"]
    keys = []
    receipt = deepcopy(provenance)
    for interval in sorted(files):
        body = json.dumps(files[interval], indent=2).encode("utf-8")
        key = interval_key(symbol, interval)
        client.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType="application/json")
        keys.append(key)
        entry = receipt["files"][interval]
        entry["sha256"] = sha256(body).hexdigest()
        entry["available_at"] = datetime.now(timezone.utc).isoformat()
    receipt["publication"] = {"status": "published", "reason": None}
    receipt["latest_published_at"] = max(entry["available_at"] for entry in receipt["files"].values())
    sidecar_body = json.dumps(receipt, separators=(",", ":")).encode()
    sidecar = sidecar_key(symbol)
    client.put_object(Bucket=bucket, Key=sidecar, Body=sidecar_body,
                      ContentType="application/json")
    keys.append(sidecar)
    return {"bucket": bucket, "keys": keys, "provenance": receipt}
