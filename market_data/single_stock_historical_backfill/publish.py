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
from pathlib import Path
from functools import lru_cache
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


def _code(error):
    return str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))


def _read(client, bucket, key):
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if _code(error) in {"NoSuchKey", "404", "NotFound"}:
            return None, None
        raise
    with obj["Body"] as stream:
        return stream.read(), obj["ETag"]


def _encode(value):
    return json.dumps(value, indent=2, allow_nan=False).encode()


def _put(client, bucket, key, data, etag=None):
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/json",
                      CacheControl="no-store", **condition)


def _retain(client, bucket, key, data):
    try:
        _put(client, bucket, key, data)
    except Exception as error:
        if _code(error) not in {"PreconditionFailed", "412"}:
            raise
        existing, _ = _read(client, bucket, key)
        if existing != data:
            raise ValueError("Immutable candle object differs from its digest")


def _revision_key(symbol, digest):
    return f"nasdaq/candles/{symbol}/versions/{digest}.json"


@lru_cache(maxsize=1)
def _session_calendar():
    path = Path(__file__).resolve().parents[2] / "backend/one-clock-nyse-calendar.json"
    if not path.exists():
        return {}
    return json.loads(path.read_bytes())["sessions"]


def publish_symbol(symbol, files, provenance, config=None, client=None):
    """Commit immutable interval bytes through a conditional per-symbol manifest.

    The flat keys remain compatibility copies. Readers qualify a revision by
    digest and use the manifest's ordered history when rewinding.
    """
    config = config or read_config()
    client = client or make_client(config)
    bucket = config["TIINGO_R2_BUCKET"]
    manifest = sidecar_key(symbol)
    previous, etag = _read(client, bucket, manifest)
    prior = json.loads(previous) if previous is not None else {}
    if prior and (prior.get("symbol") != symbol or prior.get("dataset") != provenance["dataset"]):
        raise ValueError("Unexpected candle manifest identity")
    receipt = deepcopy(provenance)
    history = deepcopy(prior.get("history", {}))
    changed = False
    written = []
    local_files = {}
    for interval, file in sorted(files.items()):
        key = interval_key(symbol, interval)
        old = prior.get("files", {}).get(interval, {})
        versions = history.setdefault(interval, [])
        # Migrate a previously published v1 flat file before replacing it.
        if not versions and old.get("sha256") and old.get("available_at"):
            original, _ = _read(client, bucket, key)
            if original is None or sha256(original).hexdigest() != old["sha256"]:
                raise ValueError("Cannot qualify earlier flat candle file")
            version_key = _revision_key(symbol, old["sha256"])
            _retain(client, bucket, version_key, original)
            old = dict(old, version_key=version_key, generated_at=json.loads(original).get("generated_at"))
            versions.append(old)
            changed = True
        normalized = dict(file)
        if versions:
            normalized["generated_at"] = versions[-1].get("generated_at")
        if versions and sha256(_encode(normalized)).hexdigest() == versions[-1]["sha256"]:
            entry = versions[-1]
            data = _encode(normalized)
        else:
            if prior.get("constructed_at") and provenance["constructed_at"] < prior["constructed_at"]:
                raise ValueError("Older backfill cannot replace a newer publication")
            data = _encode(file)
            digest = sha256(data).hexdigest()
            version_key = _revision_key(symbol, digest)
            _retain(client, bucket, version_key, data)
            written.append(version_key)
            entry = dict(receipt["files"][interval], sha256=digest, version_key=version_key,
                         generated_at=file.get("generated_at"),
                         available_at=datetime.now(timezone.utc).isoformat())
            versions.append(entry)
            changed = True
        receipt["files"][interval] = entry
        local_files[interval] = data
    if changed:
        calendar = _session_calendar()
        receipt["sessions"] = {day: calendar[day] for day in
                               {bar["t"][:10] for obj in files.values() for bar in obj["bars"]}
                               if day in calendar}
        receipt.update(history=history, publication={"status": "published", "reason": None},
                       latest_published_at=max(entry["available_at"] for entry in receipt["files"].values()))
        _put(client, bucket, manifest, _encode(receipt), etag)
        written.append(manifest)
    else:
        receipt = prior
    # Repair flat copies even after an interruption following manifest commit.
    # Read the alias before the manifest so a concurrent change fails its CAS.
    for interval, data in local_files.items():
        key = interval_key(symbol, interval)
        stored, current_etag = _read(client, bucket, key)
        if stored == data:
            continue
        current, _ = _read(client, bucket, manifest)
        if json.loads(current)["files"][interval]["sha256"] != sha256(data).hexdigest():
            raise RuntimeError("Publication superseded; retry from the current manifest")
        _put(client, bucket, key, data, current_etag)
        written.append(key)
    return {"bucket": bucket, "keys": written, "provenance": receipt, "files": local_files}
