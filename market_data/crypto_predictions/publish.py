"""R2 publication for crypto predictions (opt-in only).

Writes, per coin, the prediction file
``crypto/predictions/<COIN>.json`` and the coin's
``snapshot_provenance.v1`` sidecar
``crypto/predictions/<COIN>/provenance.json``. Configuration comes
from the environment and is **unconfirmed against live service
config** — the bucket name and endpoint below are the collector's
expected names, not a verified live deployment:

- ``R2_ENDPOINT_URL`` — R2 S3-compatible endpoint
- ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
- ``CRYPTO_R2_BUCKET`` — bucket holding the ``crypto/`` prefix
  (expected to be the same ``qqq-options-chain-data`` bucket that
  holds ``crypto/latest.json`` and ``crypto/candles/``; unconfirmed)

Nothing in this module runs unless the CLI is given ``--publish``.
The prediction file is uploaded first, then the sidecar; a sidecar
failure is raised, not silently swallowed, so a published coin is
never left without its provenance claim.
"""

from __future__ import annotations

import json
import os

from .predictions import prediction_key
from .provenance import sidecar_key

ENV_VARS = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", "CRYPTO_R2_BUCKET")


def read_config(env=None):
    """Read R2 config from the environment; raise if any value is missing."""
    env = os.environ if env is None else env
    missing = [name for name in ENV_VARS if not env.get(name)]
    if missing:
        raise RuntimeError(
            "missing R2 configuration for crypto predictions publish: "
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


def publish_symbol(symbol, content, provenance, config=None, client=None):
    """Upload one coin's prediction file and sidecar; return written keys.

    ``content`` is None for an absent coin (only the sidecar is
    published, so the absence is on the record). ``config``
    defaults to ``read_config()``; ``client`` may be injected
    (tests pass a fake). The sidecar is uploaded last; a failure
    there raises, so a caller never reports a coin as published
    without its provenance.
    """
    config = config or read_config()
    client = client or make_client(config)
    bucket = config["CRYPTO_R2_BUCKET"]
    keys = []
    if content is not None:
        body = json.dumps(content, separators=(",", ":")).encode()
        key = prediction_key(symbol)
        client.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType="application/json")
        keys.append(key)
    sidecar_body = json.dumps(provenance, separators=(",", ":")).encode()
    sidecar = sidecar_key(symbol)
    client.put_object(Bucket=bucket, Key=sidecar, Body=sidecar_body,
                      ContentType="application/json")
    keys.append(sidecar)
    return {"bucket": bucket, "keys": keys}
