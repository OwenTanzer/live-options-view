"""Read access to the real historical data this repo already collects in R2.

Three sources, all in the same `qqq-options-chain-data` bucket that
`collector.py` / `backfill_history.py` / `backfill_options_history.py` write:

  history/{SYMBOL}/5min/{YYYY}.json          -- multi-year 5-min OHLCV bars
                                                 (backfill_history.py)
  history/{SYMBOL}/options_0dte/{DATE}.json  -- one EOD 0DTE chain snapshot
                                                 per trading day, many days of
                                                 history (backfill_options_history.py)
  intraday/{YYYYMMDD}/snapshot_*.csv         -- ~60s-cadence intraday chain
                                                 snapshots, but only for days
                                                 the live collector has
                                                 actually been running
                                                 (collector.py)

Everything here is read-only against that bucket -- this module never calls
`put_object`. Reads are cached to a local disk directory (default
`./r2_cache/`) so repeated simulation runs, and offline development, don't
re-hit R2/pay egress every time.

Uses the exact same env vars as the existing backfill scripts:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME
"""

from __future__ import annotations

import io
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import boto3

R2_BUCKET = os.environ.get("R2_BUCKET_NAME", "qqq-options-chain-data")
DEFAULT_CACHE_DIR = Path(os.environ.get("SYNTHETIC_DAYS_CACHE", "./r2_cache"))


def make_s3():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


class R2Source:
    """Cached reader over the three prefixes above.

    `s3=None` is a valid, deliberately supported mode: everything already in
    the local cache still works with no credentials and no network, which is
    what lets `validate_stats.py` and day-generation be re-run repeatedly
    during development without re-authenticating to R2 each time.
    """

    def __init__(self, bucket: str = R2_BUCKET, cache_dir: Path | str = DEFAULT_CACHE_DIR, s3: Any = None):
        self.bucket = bucket
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._s3 = s3
        self._s3_tried = s3 is not None

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

    def _get_bytes(self, key: str) -> bytes | None:
        cache_path = self._cache_path(key)
        if cache_path.exists():
            return cache_path.read_bytes()
        if self.s3 is None:
            return None
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None
        except Exception:
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)
        return body

    def _list(self, prefix: str) -> list[str]:
        cache_marker = self._cache_path(prefix.rstrip("/") + "/_listing.json")
        if cache_marker.exists():
            return json.loads(cache_marker.read_text())
        if self.s3 is None:
            return []
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        cache_marker.parent.mkdir(parents=True, exist_ok=True)
        cache_marker.write_text(json.dumps(keys))
        return keys

    # -- history/{SYMBOL}/5/{YYYY}.json ------------------------------------
    # "5" is MarketData.app's raw resolution string (backfill_history.py's
    # --resolution), not a human label -- confirmed against the live bucket.

    def five_min_bars(self, symbol: str, years: list[int]) -> dict[int, dict[str, list]]:
        """{year: {"t": [...], "o": [...], "h": [...], "l": [...], "c": [...], "v": [...]}}"""
        out = {}
        for year in years:
            key = f"history/{symbol}/5/{year}.json"
            raw = self._get_bytes(key)
            if raw:
                out[year] = json.loads(raw)
        return out

    # -- history/{SYMBOL}/options_0dte/{DATE}.json -------------------------

    def options_0dte_manifest(self, symbol: str) -> dict:
        raw = self._get_bytes(f"history/{symbol}/options_0dte/manifest.json")
        return json.loads(raw) if raw else {"days": {}}

    def options_0dte_day(self, symbol: str, day: date) -> dict | None:
        """Column-oriented EOD chain: {"strike": [...], "side": [...], "bid": [...], ...}"""
        raw = self._get_bytes(f"history/{symbol}/options_0dte/{day.isoformat()}.json")
        return json.loads(raw) if raw else None

    def all_options_0dte_days(self, symbol: str) -> list[date]:
        manifest = self.options_0dte_manifest(symbol)
        return sorted(date.fromisoformat(d) for d in manifest.get("days", {}))

    # -- intraday/{YYYYMMDD}/snapshot_*.csv --------------------------------

    def intraday_days_available(self) -> list[str]:
        """YYYYMMDD strings for every day the live collector wrote at least one snapshot."""
        keys = self._list("intraday/")
        days = sorted({k.split("/")[1] for k in keys if k.count("/") >= 2 and k.split("/")[1].isdigit()})
        return days

    def intraday_snapshots(self, yyyymmdd: str) -> list[str]:
        """CSV object keys for one day, in chronological order (filenames encode HHMMSSffffff)."""
        keys = self._list(f"intraday/{yyyymmdd}/")
        csvs = sorted(k for k in keys if k.endswith(".csv") and "snapshot_" in k)
        return csvs

    def intraday_snapshot_csv(self, key: str) -> list[dict[str, str]]:
        import csv

        raw = self._get_bytes(key)
        if not raw:
            return []
        reader = csv.DictReader(io.StringIO(raw.decode()))
        return list(reader)
