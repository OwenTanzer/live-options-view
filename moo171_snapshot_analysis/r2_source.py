"""Cached, read-only access to intraday QQQ snapshot archives in R2.

Same bucket and prefix (`intraday/{YYYYMMDD}/snapshot_HHMMSSffffff.csv`) as
`synthetic_days/r2_sources.py`, but self-contained here so this analysis has
no import dependency on that sibling project. Never calls `put_object` --
raw archives are immutable per MOO-171's requirement.

Reads are cached to a local disk directory so repeated analysis runs don't
re-hit R2. Uses the same env vars as the rest of the repo's R2 access:
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

R2_BUCKET = os.environ.get("R2_BUCKET_NAME", "qqq-options-chain-data")
DEFAULT_CACHE_DIR = Path(os.environ.get("MOO171_CACHE_DIR", "./r2_cache"))


class ManifestMismatch(RuntimeError):
    """Input bytes/keys (or source/config identity) differ from a frozen
    source manifest. Raised before the offending bytes are parsed, so a
    reproduction run never silently analyzes modified input."""


def make_s3():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


class SnapshotSource:
    """Cached reader over `intraday/{YYYYMMDD}/*.csv`.

    `s3=None` is a valid mode: everything already in the local cache still
    works with no credentials and no network, so the analysis can be re-run
    repeatedly without re-authenticating every time.
    """

    def __init__(
        self, bucket: str = R2_BUCKET, cache_dir: Path | str = DEFAULT_CACHE_DIR, s3: Any = None,
        expected_sha256: dict[str, str] | None = None,
    ):
        self.bucket = bucket
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._s3 = s3
        self._s3_tried = s3 is not None
        # sha256 of the exact bytes returned by _get_bytes for each key
        # actually read THIS run -- not copied from R2/cache listing
        # metadata, so it verifies the bytes that were actually parsed
        # rather than trusting a reported ETag/size alone.
        self._read_sha256: dict[str, str] = {}
        # Frozen-manifest mode: when set, every snapshot read must be one of
        # these keys AND hash to exactly this value, or _get_bytes raises
        # ManifestMismatch before the bytes reach a parser. None = new-run
        # mode (hashes are only recorded, not checked).
        self.expected_sha256 = expected_sha256

    @property
    def s3(self):
        if not self._s3_tried:
            self._s3_tried = True
            try:
                self._s3 = make_s3()
            except KeyError:
                self._s3 = None  # no R2 creds in env -- cache-only mode
        return self._s3

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key

    def _record_and_check(self, key: str, body: bytes) -> bytes:
        digest = hashlib.sha256(body).hexdigest()
        if self.expected_sha256 is not None:
            expected = self.expected_sha256.get(key)
            if expected is None:
                raise ManifestMismatch(f"{key}: not in the frozen manifest")
            if digest != expected:
                raise ManifestMismatch(
                    f"{key}: sha256 {digest} does not match frozen manifest {expected}"
                )
        self._read_sha256[key] = digest
        return body

    def _get_bytes(self, key: str) -> bytes | None:
        cache_path = self._cache_path(key)
        if cache_path.exists():
            return self._record_and_check(key, cache_path.read_bytes())
        if self.s3 is None:
            if self.expected_sha256 is not None and key in self.expected_sha256:
                raise ManifestMismatch(f"{key}: in the frozen manifest but not readable (no cache, no R2 creds)")
            return None
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            if self.expected_sha256 is not None and key in self.expected_sha256:
                raise ManifestMismatch(f"{key}: in the frozen manifest but missing from R2")
            return None
        # Check before caching, so mismatched bytes never land in the cache.
        self._record_and_check(key, body)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)
        return body

    def list_all_objects(self, prefix: str) -> list[dict[str, Any]]:
        """Every object (key, size, etag) under prefix -- not just CSVs, so
        callers can see and explicitly exclude non-snapshot files (state
        files, alias copies) rather than silently filtering by extension
        alone before an audit has looked at what's actually there."""
        cache_marker = self._cache_path(prefix.rstrip("/") + "/_listing.json")
        if cache_marker.exists():
            return json.loads(cache_marker.read_text())
        if self.s3 is None:
            return []
        objects: list[dict[str, Any]] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "etag": obj.get("ETag", "").strip('"'),
                })
        cache_marker.parent.mkdir(parents=True, exist_ok=True)
        cache_marker.write_text(json.dumps(objects))
        return objects

    def snapshot_objects(self, yyyymmdd: str) -> list[dict[str, Any]]:
        """Objects under intraday/{yyyymmdd}/ whose name matches the
        snapshot_HHMMSSffffff.csv pattern -- excludes state files
        (momentum_log.jsonl, vwap_state.json, _listing.json) and any
        differently-named alias csv (e.g. a first.csv mirror)."""
        import re

        pattern = re.compile(r"snapshot_\d{12,}\.csv$")
        objects = self.list_all_objects(f"intraday/{yyyymmdd}/")
        return sorted(
            (o for o in objects if pattern.search(o["key"])),
            key=lambda o: o["key"],
        )

    def verified_manifest(self, yyyymmdd: str) -> list[dict[str, Any]]:
        """snapshot_objects() entries augmented with `sha256` computed from
        the actual bytes returned by `_get_bytes` for that key THIS run
        (via `snapshot_csv_rows`, which every audit call already makes).
        `sha256` is None for a key that was never actually read this run
        (e.g. a listing-only call) -- callers should treat that as
        unverified, not silently trust the listing's `etag`/`size` alone.
        """
        return [
            {**obj, "sha256": self._read_sha256.get(obj["key"])}
            for obj in self.snapshot_objects(yyyymmdd)
        ]

    def snapshot_csv_rows(self, key: str) -> list[dict[str, str]]:
        import csv
        import io

        raw = self._get_bytes(key)
        if not raw:
            return []
        reader = csv.DictReader(io.StringIO(raw.decode()))
        return list(reader)
