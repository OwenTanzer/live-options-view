"""Cache stores: a local directory store and the R2 store interface.

The layout is the PR #393 layout: ``nasdaq/candles/<TICKER>/<interval>.json``
plus ``nasdaq/candles/<TICKER>/provenance.json``. A store only moves
bytes under that layout; it never fetches market data, never invents
a bar, and never decides what is fresh — freshness is the selector's
job (``selector.py``).

R2 publication is opt-in only and configured from the environment,
with the same variable names as the PR #393 publisher (unconfirmed
against live service config — these are the publisher's expected
names, not a verified live deployment):

- ``R2_ENDPOINT_URL`` — R2 S3-compatible endpoint
- ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
- ``TIINGO_R2_BUCKET`` — bucket holding the ``nasdaq/`` prefix
  (expected to be the same ``qqq-options-chain-data`` bucket that
  holds ``nasdaq/latest.json``; unconfirmed)

No credential is ever hardcoded, logged or stored in a file. The
concrete R2 transport is ``boto3`` (lazy import, as in PR #393);
without it — or without the environment — ``publish_symbol`` fails
closed with a plain error instead of publishing partially.
"""

from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from .provenance import interval_key, sidecar_key

ENV_VARS = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", "TIINGO_R2_BUCKET")


class CacheStore(ABC):
    """Byte store for one cache tree, keyed by R2-style keys."""

    @abstractmethod
    def read_bytes(self, key):
        """Return the bytes at ``key``; raise ``KeyError`` if absent."""

    @abstractmethod
    def write_bytes(self, key, data):
        """Write ``data`` (bytes) at ``key`` atomically where possible."""

    @abstractmethod
    def exists(self, key):
        """True when ``key`` is present."""

    @abstractmethod
    def list_keys(self, prefix):
        """Sorted keys under ``prefix``."""

    # -- convenience wrappers over the byte interface ------------------

    def read_json(self, key):
        return json.loads(self.read_bytes(key).decode("utf-8"))

    def write_json(self, key, payload):
        self.write_bytes(key, json.dumps(payload, indent=2).encode("utf-8"))


class LocalDirectoryStore(CacheStore):
    """A cache tree rooted at a local directory.

    ``root`` holds ``nasdaq/candles/<TICKER>/...`` exactly as R2
    would, so a local tree can be diffed, inspected and published
    without a network round trip. Writes are atomic (temp file in the
    destination directory + rename).
    """

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key):
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"key escapes store root: {key!r}")
        return path

    def read_bytes(self, key):
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def write_bytes(self, key, data):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def exists(self, key):
        return self._path(key).is_file()

    def list_keys(self, prefix):
        base = self._path(prefix)
        if not base.exists():
            return []
        keys = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.startswith(".tmp-"):
                keys.append(str(path.relative_to(self.root)).replace(os.sep, "/"))
        return keys


class R2Store(CacheStore):
    """The R2 bucket behind the ``nasdaq/`` prefix (opt-in only).

    Constructed from the environment (``read_r2_config``) or from an
    explicit config dict; the transport is a ``boto3`` S3 client
    pointed at the R2 endpoint (lazy import). A pre-built client may
    be injected — tests pass a fake; production passes none and the
    client is built on first use. No read or write happens at
    construction time.
    """

    def __init__(self, config=None, client=None):
        self.config = dict(config) if config is not None else read_r2_config()
        self.bucket = self.config["TIINGO_R2_BUCKET"]
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = make_client(self.config)
        return self._client

    def read_bytes(self, key):
        from botocore.exceptions import ClientError
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            return body.read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyError(key) from None
            raise

    def write_bytes(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType="application/json")

    def exists(self, key):
        try:
            self.read_bytes(key)
            return True
        except KeyError:
            return False

    def list_keys(self, prefix):
        keys = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in page.get("Contents", ()))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return sorted(keys)


def read_r2_config(env=None):
    """Read R2 config from the environment; raise if any value is missing."""
    env = os.environ if env is None else env
    missing = [name for name in ENV_VARS if not env.get(name)]
    if missing:
        raise RuntimeError(
            "missing R2 configuration for candle-cache publish: "
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


def publish_symbol(symbol, files, provenance, config=None, client=None,
                   store=None):
    """Upload one ticker's interval files and sidecar; return written keys.

    ``files`` is ``{interval: content-dict}``. ``config`` defaults to
    ``read_r2_config()``; ``client`` may be injected (tests pass a
    fake) or a ready ``R2Store`` may be passed as ``store``. The
    interval files are uploaded first, then the sidecar; a sidecar
    failure raises, so a caller never reports a ticker as published
    without its provenance. Nothing here runs unless the caller
    opts in (the CLI's ``--publish``).
    """
    if store is None:
        store = R2Store(config=config, client=client)
    keys = []
    for interval in sorted(files):
        body = json.dumps(files[interval], separators=(",", ":")).encode()
        key = interval_key(symbol, interval)
        store.write_bytes(key, body)
        keys.append(key)
    sidecar_body = json.dumps(provenance, separators=(",", ":")).encode()
    sidecar = sidecar_key(symbol)
    store.write_bytes(sidecar, sidecar_body)
    keys.append(sidecar)
    return {"bucket": store.bucket, "keys": keys}
