"""Fail-safe publisher of the sanitized OI skew meter projection (H1 delivery).

After each OI Skew decision is durably written to the private ledger, the
runner hands the record to this publisher. It builds the sanitized
`oi_skew_meter.v1` document (crassus.oi_skew_projection), checks it again
with `assert_sanitized`, and uploads exactly one object:

    crassus/oi-skew/v1/latest.json

Guarantees (each covered by scripts/verify_oi_skew_publisher.py):

* **Trading never depends on it.** `offer()` only replaces a one-slot
  "latest wins" mailbox and returns immediately; a daemon thread does the
  upload. Any exception -- build, sanitize, network, auth -- is caught,
  counted and logged without the record contents or credentials.
* **Missing configuration disables publication safely.** It uses its own
  configuration, CRASSUS_OI_SKEW_PUBLIC_{ACCOUNT_ID, BUCKET, ACCESS_KEY_ID,
  SECRET_ACCESS_KEY}, never the private archive's credentials. If none is
  set, publication is off. If the set is partial or invalid, or the bucket
  equals the private archive bucket, publication is off with a logged
  reason. The runner never fails to start because of this publisher.
* **One key only.** The destination key is fixed. Only OI Skew records
  carrying strategy metadata are published, and a document that fails
  sanitization is dropped, never uploaded.
* **Truthful staleness.** Nothing is re-published on a timer; the object's
  own observed_snapshot_timestamp and the browser's freshness rules show it
  as stale or closed when the runner stops deciding.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Callable

from .oi_skew_projection import STRATEGY_ID, assert_sanitized, build_projection

log = logging.getLogger("crassus.oi_skew_publisher")

PUBLIC_KEY = "crassus/oi-skew/v1/latest.json"
ENV_PREFIX = "CRASSUS_OI_SKEW_PUBLIC_"
_SETTINGS = ("ACCOUNT_ID", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
_ACCOUNT_RE = re.compile(r"[0-9a-f]{32}")


class OiSkewPublisher:
    def __init__(self, client: Any, bucket: str, *, build: Callable = build_projection):
        self.client = client
        self.bucket = bucket
        self._build = build
        self._slot: dict | None = None
        self._cond = threading.Condition()
        self._stop = False
        self.published = 0
        self.failures = 0
        self.dropped_unsanitized = 0
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._loop, name="oi-skew-publisher", daemon=True)

    # -- runner side ---------------------------------------------------------
    def start(self) -> "OiSkewPublisher":
        self._thread.start()
        return self

    def offer(self, record: Any) -> None:
        """Ledger observer: never raises, never blocks on the network."""
        try:
            if not isinstance(record, dict) or record.get("strategy_id") != STRATEGY_ID:
                return
            decision = record.get("decision")
            if not isinstance(decision, dict) or not isinstance(decision.get("metadata"), dict):
                return  # e.g. a reconcile failure: keep the last object; it ages to stale
            with self._cond:
                self._slot = record          # latest wins
                self._cond.notify()
        except Exception:  # pragma: no cover - defensive
            self.failures += 1

    def close(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout)

    # -- worker side -----------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._slot is None and not self._stop:
                    self._cond.wait()
                if self._slot is None and self._stop:
                    return
                record, self._slot = self._slot, None
            self.publish_now(record)

    def publish_now(self, record: dict) -> bool:
        """Build, re-check and upload one record. Returns True on upload."""
        try:
            doc = self._build(record)
        except Exception as exc:
            self.failures += 1
            self.last_error = f"build:{type(exc).__name__}"
            log.warning("oi skew projection build failed (%s); not published", type(exc).__name__)
            return False
        try:
            assert_sanitized(doc)
        except ValueError:
            self.dropped_unsanitized += 1
            self.last_error = "sanitize"
            log.error("oi skew projection failed sanitization; not published")
            return False
        body = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.client.put_object(Bucket=self.bucket, Key=PUBLIC_KEY, Body=body,
                                   ContentType="application/json",
                                   CacheControl="no-store, max-age=0")
        except Exception as exc:
            self.failures += 1
            self.last_error = f"put:{type(exc).__name__}"
            log.warning("oi skew projection upload failed (%s); trading unaffected", type(exc).__name__)
            return False
        self.published += 1
        self.last_error = None
        return True


def config_from_environment(environ: dict[str, str] | None = None) -> tuple[dict[str, str] | None, str]:
    """(settings, reason). settings is None when publication must stay off."""
    env = os.environ if environ is None else environ
    values = {name: (env.get(ENV_PREFIX + name) or "").strip() for name in _SETTINGS}
    if not any(values.values()):
        return None, "not_configured"
    missing = [ENV_PREFIX + n for n, v in values.items() if not v]
    if missing:
        return None, "incomplete_configuration:" + ",".join(missing)
    if not _ACCOUNT_RE.fullmatch(values["ACCOUNT_ID"]):
        return None, "invalid_account_id"
    if values["BUCKET"] == (env.get("CRASSUS_ARCHIVE_BUCKET") or "").strip():
        return None, "bucket_is_the_private_archive_bucket"
    return values, "configured"


def from_environment(environ: dict[str, str] | None = None,
                     client_factory: Callable[[dict[str, str]], Any] | None = None) -> OiSkewPublisher | None:
    """A started publisher, or None (publication off). Never raises."""
    try:
        settings, reason = config_from_environment(environ)
        if settings is None:
            log.info("oi skew public projection disabled: %s", reason)
            return None
        client = (client_factory or _r2_client)(settings)
        return OiSkewPublisher(client, settings["BUCKET"]).start()
    except Exception as exc:
        log.error("oi skew public projection disabled: setup failed (%s)", type(exc).__name__)
        return None


def _r2_client(settings: dict[str, str]):
    for name in ("boto3", "botocore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=f"https://{settings['ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=settings["ACCESS_KEY_ID"],
                        aws_secret_access_key=settings["SECRET_ACCESS_KEY"], region_name="auto",
                        config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 2},
                                      request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required"))
