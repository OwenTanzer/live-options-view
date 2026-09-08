#!/usr/bin/env python3
"""MOO-169 permanent daily Tradier option Time & Sale collector.

Launched once per weekday by a Railway Cron Schedule, shortly before the
NYSE open. Unlike the one-shot MOO-144 probe (``moo144_tradier_probe.py``,
whose ``select_symbols``/``Tradier``/R2 helpers this module reuses), this
process:

- is calendar-gated instead of date-pinned: it exits 0 immediately on a
  day with no NYSE session, and otherwise captures through to the
  calendar's actual close (early closes included) instead of a fixed
  duration.
- bounds its own memory: dedup keys on each symbol's last-seen sequence
  number only, and keeps only a fixed-size reservoir of quote-age samples
  plus running lifetime min/max/count/sum. The dedup horizon is "this
  process's lifetime, per symbol, last event only" -- it resets on restart.
- decouples ingest from upload: closed segments go to a persistent spool
  directory and a background thread uploads them with retry/backoff, so a
  slow or failing upload never blocks the stream reader. Spool growth is
  bounded: exceeding the cap stops ingestion (SpoolExhausted) rather than
  ever silently dropping an event.
- claims the day with a renewable, fenced lease: a heartbeat renews it
  conditionally against the owner's own copy, and loses ownership (stopping
  ingestion) rather than clobbering another owner's lease. A crash/restart
  reconciles against R2 by content hash before resuming, and re-enqueues
  any locally-spooled segment R2 doesn't already have.
- persists the day's selected contract universe once and reloads it on any
  same-day restart, instead of re-selecting against the current spot price.
- derives "complete" vs "partial" from actual coverage (no late start, no
  reconnect gaps, spool fully drained, reconciliation clean) rather than
  merely "no exception was raised".
"""

from __future__ import annotations

import gzip
import json
import os
import signal
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from moo144_tradier_probe import (
    STREAM,
    Tradier,
    is_retryable,
    normalize,  # noqa: F401  (re-exported for tests / callers)
    parse_epoch_ms,
    put_bytes_verified,
    r2_client,
    select_symbols,
    sha256_file,  # noqa: F401  (re-exported for tests / callers)
    stream_payload,
    upload_file_verified,
    utc_now,
)

ET = ZoneInfo("America/New_York")
STOP = False
EXPECTED_TIMESALE_FIELDS = (
    "symbol", "exch", "bid", "ask", "last", "size", "date", "seq",
    "flag", "cancel", "correction", "session",
)
QUOTE_AGE_RESERVOIR_SIZE = 5000
LATE_START_TOLERANCE_SECONDS = 5
HEALTH_PUBLISH_INTERVAL_SECONDS = 60


class LeaseLost(RuntimeError):
    """Raised when this process's collection lease was taken by another owner."""


class SpoolExhausted(RuntimeError):
    """Raised when the upload spool has grown past its configured cap."""


def on_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def nyse_session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Exchange hours for ``day``, including holidays/early closes; None if closed.

    Same pattern as ``collector.py``'s ``_weekly_session_bounds`` -- kept as
    its own copy here since ``collector.py`` isn't a shared library module.
    """
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (
        row["market_open"].to_pydatetime().astimezone(ET),
        row["market_close"].to_pydatetime().astimezone(ET),
    )


class BoundedStats:
    """Bounded-memory event statistics for a single collector process.

    Dedup keys on (symbol -> last-seen seq) rather than a growing set of
    every event, and quote-age percentiles come from a fixed-size reservoir
    plus running lifetime aggregates, so memory is O(distinct symbols) for
    the whole session rather than O(events). These are *this process's*
    counts only -- ``reconcile_prior_records`` in the manifest covers
    events captured by an earlier owner before a restart.
    """

    def __init__(self, reservoir_size: int = QUOTE_AGE_RESERVOIR_SIZE) -> None:
        self.counts: Counter[str] = Counter()
        self.timesale_by_symbol: Counter[str] = Counter()
        self.field_key_present: Counter[str] = Counter()
        self.field_non_null: Counter[str] = Counter()
        self.field_non_empty: Counter[str] = Counter()
        self.field_total: Counter[str] = Counter()
        self.flags: Counter[str] = Counter()
        self.sessions: Counter[str] = Counter()
        self.cancel_count = 0
        self.correction_count = 0
        self.duplicate_count = 0
        self.malformed = 0
        self.last_sequence: dict[str, int] = {}
        self.sequence_discontinuities: Counter[str] = Counter()
        self.sequence_out_of_order: Counter[str] = Counter()
        self.quote_timestamps: dict[str, int] = {}
        self.quote_age_reservoir: deque[int] = deque(maxlen=reservoir_size)
        self.quote_age_count = 0
        self.quote_age_sum = 0
        self.quote_age_min: int | None = None
        self.quote_age_max: int | None = None
        self.last_receipt_ts: str | None = None
        self.first_receipt_ts: str | None = None

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type", "unknown"))
        self.counts[event_type] += 1
        receipt = event.get("collector_receipt_timestamp")
        if receipt:
            self.first_receipt_ts = self.first_receipt_ts or receipt
            self.last_receipt_ts = receipt
        symbol = str(event.get("symbol", "unknown"))
        if event_type == "quote":
            candidates = [
                value for value in (
                    parse_epoch_ms(event.get("biddate")),
                    parse_epoch_ms(event.get("askdate")),
                )
                if value is not None
            ]
            if candidates:
                self.quote_timestamps[symbol] = max(candidates)
            return event
        if event_type != "timesale":
            return event

        self.timesale_by_symbol[symbol] += 1
        for field in EXPECTED_TIMESALE_FIELDS:
            self.field_total[field] += 1
            if field in event:
                self.field_key_present[field] += 1
            if event.get(field) is not None:
                self.field_non_null[field] += 1
            if event.get(field) not in (None, ""):
                self.field_non_empty[field] += 1
        self.flags[str(event.get("flag") or "<empty>")] += 1
        self.sessions[str(event.get("session") or "<missing>")] += 1
        self.cancel_count += int(bool(event.get("cancel")))
        self.correction_count += int(bool(event.get("correction")))

        try:
            sequence = int(event["seq"])
        except (KeyError, TypeError, ValueError):
            sequence = None

        if sequence is not None:
            previous = self.last_sequence.get(symbol)
            duplicate = previous is not None and sequence == previous
            event["duplicate_in_run"] = duplicate
            self.duplicate_count += int(duplicate)
            if previous is not None:
                if sequence < previous:
                    self.sequence_out_of_order[symbol] += 1
                elif sequence > previous + 1:
                    self.sequence_discontinuities[symbol] += 1
            self.last_sequence[symbol] = max(sequence, previous if previous is not None else sequence)
        else:
            event["duplicate_in_run"] = False

        trade_ms = parse_epoch_ms(event.get("date"))
        quote_ms = self.quote_timestamps.get(symbol)
        if trade_ms is not None and quote_ms is not None and quote_ms <= trade_ms:
            age = trade_ms - quote_ms
            self.quote_age_reservoir.append(age)
            self.quote_age_count += 1
            self.quote_age_sum += age
            self.quote_age_min = age if self.quote_age_min is None else min(self.quote_age_min, age)
            self.quote_age_max = age if self.quote_age_max is None else max(self.quote_age_max, age)
            event["preceding_quote_age_ms"] = age
        return event

    def age_summary(self) -> dict[str, Any]:
        sampled = sorted(self.quote_age_reservoir)
        summary: dict[str, Any] = {
            "count": self.quote_age_count,
            "reservoir_size": len(sampled),
        }
        if sampled:
            summary.update({
                "min": self.quote_age_min,
                "max": self.quote_age_max,
                "mean": round(self.quote_age_sum / self.quote_age_count, 3),
                "sampled_median": sampled[len(sampled) // 2],
                "sampled_p95": sampled[min(len(sampled) - 1, int(0.95 * (len(sampled) - 1)))],
            })
        return summary

    def summary(self) -> dict[str, Any]:
        return {
            "event_counts": dict(self.counts),
            "timesale_counts_by_symbol": dict(self.timesale_by_symbol),
            "timesale_field_population": {
                field: {
                    "total": self.field_total[field],
                    "key_present": self.field_key_present[field],
                    "non_null": self.field_non_null[field],
                    "non_empty": self.field_non_empty[field],
                }
                for field in EXPECTED_TIMESALE_FIELDS
            },
            "flag_frequencies": dict(self.flags),
            "session_frequencies": dict(self.sessions),
            "cancel_count": self.cancel_count,
            "correction_count": self.correction_count,
            "duplicate_count": self.duplicate_count,
            "dedup_horizon": "per-symbol last-seen sequence only; resets on restart",
            "sequence_discontinuities_by_symbol": dict(self.sequence_discontinuities),
            "sequence_out_of_order_by_symbol": dict(self.sequence_out_of_order),
            "preceding_quote_age_ms": self.age_summary(),
            "malformed_payloads": self.malformed,
        }


def lease_key(run_date: str) -> str:
    return f"moo144/tradier/{run_date}/lease.json"


def _read_lease(client: Any, bucket: str, run_date: str) -> tuple[dict[str, Any] | None, str | None]:
    key = lease_key(run_date)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body), head.get("ETag")
    except client.exceptions.NoSuchKey:
        return None, None
    except client.exceptions.ClientError:
        return None, None


def acquire_lease(
    client: Any,
    bucket: str,
    run_date: str,
    owner_id: str,
    ttl_seconds: int,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Acquire or take over the day's collection lease.

    Raises RuntimeError if another owner's lease is still live. A lease
    that exists but has expired may be taken over by a new owner.
    """
    key = lease_key(run_date)
    current, existing_etag = _read_lease(client, bucket, run_date)
    if current is not None:
        expires_at = datetime.fromisoformat(current["expires_at"])
        if expires_at > now() and current.get("owner_id") != owner_id:
            raise RuntimeError(
                f"MOO-144 collection lease for {run_date} is held by "
                f"{current.get('owner_id')!r} until {current['expires_at']}"
            )

    lease = {
        "run_date": run_date,
        "owner_id": owner_id,
        "acquired_at": now().isoformat(),
        "expires_at": (now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    put_kwargs: dict[str, Any] = {"IfMatch": existing_etag} if existing_etag else {"IfNoneMatch": "*"}
    body = (json.dumps(lease, indent=2, sort_keys=True) + "\n").encode()
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", **put_kwargs)
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status in (412, 409) or code in {"PreconditionFailed", "412"}:
            raise RuntimeError(
                f"MOO-144 collection lease for {run_date} was taken concurrently"
            ) from exc
        raise
    return lease


def renew_lease(
    client: Any,
    bucket: str,
    run_date: str,
    owner_id: str,
    ttl_seconds: int,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Renew this owner's lease, fenced against having lost ownership.

    Unlike acquisition, a renewal must never be able to clobber a newer
    owner's lease: it reads the current object first and requires both a
    matching owner_id and a conditional ``IfMatch`` on that exact version.
    Raises ``LeaseLost`` if the lease disappeared or now belongs to someone
    else -- the caller must stop ingesting in that case, not swallow it.
    """
    current, etag = _read_lease(client, bucket, run_date)
    if current is None:
        raise LeaseLost(f"MOO-144 lease for {run_date} disappeared")
    if current.get("owner_id") != owner_id:
        raise LeaseLost(
            f"MOO-144 lease for {run_date} is now owned by {current.get('owner_id')!r}"
        )
    lease = {
        "run_date": run_date,
        "owner_id": owner_id,
        "acquired_at": now().isoformat(),
        "expires_at": (now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    body = (json.dumps(lease, indent=2, sort_keys=True) + "\n").encode()
    try:
        client.put_object(
            Bucket=bucket, Key=lease_key(run_date), Body=body,
            ContentType="application/json", IfMatch=etag,
        )
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status in (412, 409) or code in {"PreconditionFailed", "412"}:
            raise LeaseLost(f"MOO-144 lease for {run_date} was renewed concurrently") from exc
        raise
    return lease


def universe_key(prefix: str) -> str:
    return f"{prefix}/universe.json"


def load_or_select_universe(
    client: Tradier,
    r2: Any,
    bucket: str,
    prefix: str,
    strike_count: int,
    run_date: str,
    now_et: datetime,
) -> tuple[list[str], dict[str, Any]]:
    """Load the day's already-selected contract universe, or select and
    persist it once. A same-day restart must never re-select against a
    (possibly moved) current spot price -- MOO-169 requires the universe
    stay fixed for the whole session.
    """
    key = universe_key(prefix)
    try:
        body = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
        payload = json.loads(body)
        return payload["symbols"], payload["universe"]
    except r2.exceptions.NoSuchKey:
        pass
    except r2.exceptions.ClientError:
        pass

    symbols, universe = select_symbols(client, strike_count, 0, run_date, now_et)
    payload = {"symbols": symbols, "universe": universe}
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    try:
        r2.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", IfNoneMatch="*")
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status in (412, 409) or code in {"PreconditionFailed", "412"}:
            # Another concurrent starter won the race -- use their selection.
            existing = json.loads(r2.get_object(Bucket=bucket, Key=key)["Body"].read())
            return existing["symbols"], existing["universe"]
        raise
    return symbols, universe


class Uploader:
    """Background thread that uploads closed spool segments with retry/backoff.

    Ingest hands off a finished file path via ``enqueue`` and never blocks on
    the network. ``spool_bytes()`` reflects every byte not yet durably
    uploaded (queued *and* in-flight); enforcement of the cap happens in
    ``SegmentSpool.write`` so a full spool stops ingestion with an explicit,
    reported ``SpoolExhausted`` rather than growing without bound.
    """

    def __init__(
        self,
        r2: Any,
        bucket: str,
        prefix: str,
        max_spool_bytes: int,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.r2 = r2
        self.bucket = bucket
        self.prefix = prefix
        self.max_spool_bytes = max_spool_bytes
        self.sleeper = sleeper
        self.queue: "deque[Path]" = deque()
        self.lock = threading.RLock()
        self.artifacts: list[dict[str, Any]] = []
        self.failures = 0
        self.overloaded = False
        self._stop = False
        self._thread: threading.Thread | None = None

    def spool_bytes(self) -> int:
        with self.lock:
            return sum(path.stat().st_size for path in self.queue if path.exists())

    def enqueue(self, path: Path) -> None:
        with self.lock:
            self.queue.append(path)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop or self.queue:
            with self.lock:
                path = self.queue[0] if self.queue else None
            if path is None:
                self.sleeper(0.05)
                continue
            attempt = 0
            while True:
                try:
                    key = f"{self.prefix}/{path.name}"
                    artifact = upload_file_verified(
                        self.r2, self.bucket, path, key, "application/x-ndjson", "gzip"
                    )
                    self.artifacts.append(artifact)
                    path.unlink(missing_ok=True)
                    with self.lock:
                        self.queue.popleft()
                        if self.spool_bytes() <= self.max_spool_bytes:
                            self.overloaded = False
                    break
                except Exception:
                    self.failures += 1
                    attempt += 1
                    delay = min(2 ** attempt, 30)
                    self.sleeper(delay)

    def drain_and_stop(self, timeout: float = 60.0) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def fully_drained(self) -> bool:
        return not self.overloaded and self.spool_bytes() == 0 and len(self.queue) == 0


class SegmentSpool:
    """Writes gzip NDJSON segments to a persistent spool dir and hands closed
    ones to the uploader. Filenames are collision-free across restarts.

    Every write checks total outstanding spool bytes (queued + the segment
    currently being written) against the uploader's cap *before* writing the
    event; once the cap would be exceeded, capture must stop rather than
    keep accepting events the disk can't durably hold.
    """

    def __init__(
        self,
        spool_dir: Path,
        owner_id: str,
        uploader: Uploader,
        checkpoint_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool_dir = spool_dir
        self.owner_id = owner_id
        self.uploader = uploader
        self.checkpoint_seconds = checkpoint_seconds
        self.clock = clock
        self.index = 0
        self.records = 0
        self.opened_at = clock()
        self.path: Path | None = None
        self.handle: Any = None
        self.segment_records: dict[str, int] = {}
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self.path = self.spool_dir / f"{self.owner_id}-part-{self.index:04d}.ndjson.gz"
        self.handle = gzip.open(self.path, "wt", encoding="utf-8")
        self.records = 0
        self.opened_at = self.clock()

    def _current_size(self) -> int:
        return self.path.stat().st_size if self.path and self.path.exists() else 0

    def write(self, event: dict[str, Any]) -> None:
        total_outstanding = self.uploader.spool_bytes() + self._current_size()
        if total_outstanding > self.uploader.max_spool_bytes:
            self.uploader.overloaded = True
            raise SpoolExhausted(
                f"spool backlog {total_outstanding} bytes exceeds cap "
                f"{self.uploader.max_spool_bytes} bytes; stopping ingestion"
            )
        self.handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
        self.records += 1
        self.handle.flush()  # keep on-disk size current for the spool-cap check above
        if self.clock() - self.opened_at >= self.checkpoint_seconds:
            self.rotate()

    def rotate(self, final: bool = False) -> None:
        if self.handle is None or self.path is None:
            return
        self.handle.close()
        if self.records:
            self.segment_records[self.path.name] = self.records
            self.uploader.enqueue(self.path)
        elif self.path.exists():
            self.path.unlink()
        self.handle = None
        self.path = None
        if not final:
            self.index += 1
            self._open()

    def close(self) -> None:
        self.rotate(final=True)


def _is_readable_gzip_ndjson(path: Path) -> bool:
    """True if ``path`` is a complete, uncorrupted gzip NDJSON segment.

    A segment truncated mid-write by a crash raises ``EOFError``/``OSError``
    when fully decompressed; that distinguishes a resumable finished segment
    from one that must be flagged for manual review instead of re-uploaded.
    """
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def _remote_content_matches(local_path: Path, remote_item: dict[str, Any]) -> bool | None:
    """Compare local file content against a remote listing entry.

    Returns True/False when a definitive comparison was possible, or None
    when the remote ETag isn't a plain MD5 (e.g. a multipart upload) and
    content identity can't be established from the listing alone.
    """
    etag = str(remote_item.get("ETag") or "").strip('"')
    if not etag or "-" in etag:
        return None
    import hashlib
    digest = hashlib.md5()
    with local_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == etag


def reconcile_existing_segments(
    r2: Any, bucket: str, prefix: str, spool_dir: Path,
) -> dict[str, Any]:
    """Reconcile local spool state against the durable archive before resuming.

    - Segments already durably uploaded (verified by content, not just name)
      are reported as artifacts and their local copy is removed.
    - Segments that exist locally but are NOT yet on R2, and are readable,
      are queued for the uploader to resume -- a crash must not strand data.
    - Segments that are neither confirmed-uploaded-with-matching-content nor
      cleanly readable are reported separately for manual review; they are
      never silently deleted or silently re-uploaded under an assumed identity.

    Listing failures propagate (they are not treated as "the archive is
    empty") so a caller never certifies completeness against state it
    could not actually verify.
    """
    uploaded: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{prefix}/"}
        if token:
            kwargs["ContinuationToken"] = token
        response = r2.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            key = item.get("Key", "")
            if key.endswith(".ndjson.gz"):
                uploaded[Path(key).name] = item
        if response.get("IsTruncated") and response.get("NextContinuationToken"):
            token = response["NextContinuationToken"]
            continue
        break

    artifacts = [
        {"key": f"{prefix}/{name}", "bytes": item.get("Size"), "reconciled": True}
        for name, item in uploaded.items()
    ]

    resume: list[Path] = []
    needs_review: list[str] = []
    if spool_dir.exists():
        for path in sorted(spool_dir.glob("*.ndjson.gz")):
            remote = uploaded.get(path.name)
            if remote is not None:
                matches = _remote_content_matches(path, remote)
                if matches is False:
                    needs_review.append(f"{path.name}: local content differs from archived object of the same name")
                    continue
                # matches is True, or None (can't verify -- trust the durable
                # upload's own head-verification at write time) either way
                # the archive already has this segment; don't re-upload it.
                path.unlink(missing_ok=True)
                continue
            if _is_readable_gzip_ndjson(path):
                resume.append(path)
            else:
                needs_review.append(f"{path.name}: truncated/corrupt, not resumable")

    return {"artifacts": artifacts, "resume": resume, "needs_review": needs_review}


def count_ndjson_gz_records(r2: Any, bucket: str, key: str) -> int | None:
    """Best-effort record count for a reconciled segment this process didn't
    write itself, so manifest totals can be audited against event_parts."""
    try:
        body = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
        return sum(1 for line in gzip.decompress(body).splitlines() if line.strip())
    except Exception:
        return None


def json_artifact(r2: Any, bucket: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    return put_bytes_verified(r2, bucket, key, body, "application/json")


def write_health(
    r2: Any,
    bucket: str,
    prefix: str,
    stats: BoundedStats,
    uploader: Uploader,
    reconnects: int,
    gap_seconds: float,
) -> None:
    payload = {
        "updated_at": utc_now(),
        "first_receipt_timestamp": stats.first_receipt_ts,
        "last_receipt_timestamp": stats.last_receipt_ts,
        "event_counts": dict(stats.counts),
        "reconnects": reconnects,
        "gap_seconds": round(gap_seconds, 3),
        "malformed_payloads": stats.malformed,
        "duplicate_count": stats.duplicate_count,
        "spool_backlog_bytes": uploader.spool_bytes(),
        "spool_overloaded": uploader.overloaded,
        "upload_failures": uploader.failures,
    }
    json_artifact(r2, bucket, f"{prefix}/health.json", payload)


class CaptureResult:
    def __init__(self) -> None:
        self.reconnects = 0
        self.gap_seconds = 0.0
        self.stop_reason: str | None = None  # None, "lease_lost", or "spool_exhausted"


def capture_session(
    client: Tradier,
    symbols: list[str],
    spool: SegmentSpool,
    stats: BoundedStats,
    session_close: datetime,
    max_consecutive_reconnects: int,
    lease_lost: threading.Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    now_et: Callable[[], datetime] = lambda: datetime.now(ET),
) -> CaptureResult:
    result = CaptureResult()
    lease_lost = lease_lost or threading.Event()
    consecutive_failures = 0

    def stop_requested() -> bool:
        return STOP or lease_lost.is_set()

    while not stop_requested() and now_et() < session_close:
        received = False
        try:
            session_id = client.create_market_session()
            payload = stream_payload(symbols, session_id)
            with client.session.get(
                STREAM, params=payload, stream=True, timeout=(15, 10)
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if stop_requested() or now_et() >= session_close:
                        break
                    if not line:
                        continue
                    received = True
                    receipt = utc_now()
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("non-object event")
                    except (json.JSONDecodeError, ValueError, TypeError):
                        stats.malformed += 1
                        raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                        spool.write({
                            "type": "malformed",
                            "collector_receipt_timestamp": receipt,
                            "provider_payload": raw,
                        })
                        continue
                    event["collector_receipt_timestamp"] = receipt
                    event["provider"] = "tradier"
                    spool.write(stats.observe(event))
                if stop_requested() or now_et() >= session_close:
                    break
                raise requests.ConnectionError("Tradier stream ended cleanly before session close")
        except SpoolExhausted:
            result.stop_reason = "spool_exhausted"
            return result
        except (requests.RequestException, OSError) as exc:
            if not is_retryable(exc):
                raise
            disconnect_at = monotonic()
            spool.write({
                "type": "gap",
                "reason": "stream_disconnect",
                "receipt_timestamp": utc_now(),
                "error_type": type(exc).__name__,
            })
            result.reconnects += 1
            consecutive_failures = 1 if received else consecutive_failures + 1
            if consecutive_failures > max_consecutive_reconnects:
                budget_exceeded = RuntimeError(
                    f"Tradier stream exceeded {max_consecutive_reconnects} consecutive reconnects"
                )
                budget_exceeded.reconnects = result.reconnects  # type: ignore[attr-defined]
                budget_exceeded.gap_seconds = result.gap_seconds  # type: ignore[attr-defined]
                raise budget_exceeded from exc
            delay = min(2 ** (consecutive_failures - 1), 15)
            remaining = delay
            while remaining > 0 and not stop_requested():
                interval = min(0.5, remaining)
                sleeper(interval)
                remaining -= interval
            result.gap_seconds += monotonic() - disconnect_at
            spool.write({
                "type": "gap",
                "reason": "stream_reconnect_resumed",
                "receipt_timestamp": utc_now(),
                "reconnect": result.reconnects,
                "outage_seconds": round(monotonic() - disconnect_at, 3),
                "error_type": type(exc).__name__,
            })
    if lease_lost.is_set():
        result.stop_reason = "lease_lost"
    return result


def main(
    *,
    clock_et: Callable[[], datetime] = lambda: datetime.now(ET),
    sleeper: Callable[[float], None] = time.sleep,
    session_bounds: Callable[[date], tuple[datetime, datetime] | None] = nyse_session_bounds,
    uploader_sleeper: Callable[[float], None] = time.sleep,
    drain_timeout_seconds: float = 60.0,
) -> int:
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is required")
    strike_count = int(os.getenv("MOO144_STRIKE_COUNT", "8"))
    checkpoint_seconds = int(os.getenv("MOO144_CHECKPOINT_SECONDS", "180"))
    max_reconnects = int(os.getenv("MOO144_MAX_CONSECUTIVE_RECONNECTS", "5"))
    lease_ttl_seconds = int(os.getenv("MOO144_LEASE_TTL_SECONDS", "300"))
    max_spool_bytes = int(os.getenv("MOO144_MAX_SPOOL_BYTES", str(512 * 1024 * 1024)))
    spool_dir = Path(os.getenv("MOO144_SPOOL_DIR", tempfile.gettempdir())) / "moo144-collector-spool"
    if not 2 <= strike_count <= 20:
        raise RuntimeError("MOO144_STRIKE_COUNT must be between 2 and 20")
    if not 30 <= checkpoint_seconds <= 300:
        raise RuntimeError("MOO144_CHECKPOINT_SECONDS must be between 30 and 300")
    if not 1 <= max_reconnects <= 10:
        raise RuntimeError("MOO144_MAX_CONSECUTIVE_RECONNECTS must be between 1 and 10")

    today = clock_et().date()
    bounds = session_bounds(today)
    if bounds is None:
        print(json.dumps({"event": "no_session_today", "date": today.isoformat()}), flush=True)
        return 0
    session_open, session_close = bounds
    run_date = today.isoformat()

    now_et = clock_et()
    if now_et < session_open:
        wait_seconds = (session_open - now_et).total_seconds()
        print(json.dumps({"event": "waiting_for_open", "seconds": wait_seconds}), flush=True)
        sleeper(max(0.0, wait_seconds))
    late_start_seconds = max(0.0, (clock_et() - session_open).total_seconds())
    if clock_et() >= session_close:
        print(json.dumps({"event": "session_already_closed", "date": run_date}), flush=True)
        return 0

    owner_id = uuid.uuid4().hex[:12]
    run_id = f"{run_date}-{owner_id}"
    prefix = f"moo144/tradier/{run_date}"
    started_at = utc_now()
    print(json.dumps({"event": "collector_start", "run_id": run_id}), flush=True)

    client = Tradier(token)
    r2, bucket = r2_client()
    lease = acquire_lease(r2, bucket, run_date, owner_id, lease_ttl_seconds)
    symbols, universe = load_or_select_universe(
        client, r2, bucket, prefix, strike_count, run_date, clock_et()
    )
    reconciliation = reconcile_existing_segments(r2, bucket, prefix, spool_dir)

    start_payload = {
        "schema_version": 2,
        "issue": "MOO-169",
        "run_id": run_id,
        "started_at": started_at,
        "session_open": session_open.isoformat(),
        "session_close": session_close.isoformat(),
        "late_start_seconds": late_start_seconds,
        "universe": universe,
        "lease": lease,
        "reconciled_segments": len(reconciliation["artifacts"]),
        "resumed_segments": len(reconciliation["resume"]),
        "needs_review": reconciliation["needs_review"],
    }
    preflight = json_artifact(r2, bucket, f"{prefix}/run-started-{owner_id}.json", start_payload)
    print(json.dumps({"event": "r2_preflight_pass", "key": preflight["key"]}), flush=True)

    uploader = Uploader(r2, bucket, prefix, max_spool_bytes, sleeper=uploader_sleeper)
    for path in reconciliation["resume"]:
        uploader.enqueue(path)
    uploader.start()
    spool = SegmentSpool(spool_dir, owner_id, uploader, checkpoint_seconds)
    stats = BoundedStats()
    fatal_error: str | None = None
    capture_result = CaptureResult()

    lease_stop = threading.Event()
    lease_lost_event = threading.Event()

    def lease_heartbeat() -> None:
        while not lease_stop.wait(lease_ttl_seconds / 3):
            try:
                renew_lease(r2, bucket, run_date, owner_id, lease_ttl_seconds)
            except LeaseLost:
                lease_lost_event.set()
                return

    def health_publisher() -> None:
        while not lease_stop.wait(HEALTH_PUBLISH_INTERVAL_SECONDS):
            try:
                write_health(r2, bucket, prefix, stats, uploader, capture_result.reconnects, capture_result.gap_seconds)
            except Exception:
                pass

    heartbeat_thread = threading.Thread(target=lease_heartbeat, daemon=True)
    heartbeat_thread.start()
    health_thread = threading.Thread(target=health_publisher, daemon=True)
    health_thread.start()

    try:
        capture_result = capture_session(
            client, symbols, spool, stats, session_close, max_reconnects,
            lease_lost=lease_lost_event, sleeper=sleeper, now_et=clock_et,
        )
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        capture_result.reconnects = getattr(exc, "reconnects", 0)
        capture_result.gap_seconds = getattr(exc, "gap_seconds", 0.0)
    finally:
        lease_stop.set()
        spool.close()
        uploader.drain_and_stop(timeout=drain_timeout_seconds)

    try:
        write_health(r2, bucket, prefix, stats, uploader, capture_result.reconnects, capture_result.gap_seconds)
    except Exception:
        pass

    reconciled_records_total = 0
    for artifact in reconciliation["artifacts"]:
        records = count_ndjson_gz_records(r2, bucket, artifact["key"])
        artifact["records"] = records
        if records is not None:
            reconciled_records_total += records

    finished_at = utc_now()
    partial_reasons: list[str] = []
    if fatal_error is not None:
        partial_reasons.append(f"fatal_error: {fatal_error}")
    if capture_result.stop_reason:
        partial_reasons.append(capture_result.stop_reason)
    if STOP:
        partial_reasons.append("stopped_by_signal")
    if clock_et() < session_close:
        partial_reasons.append("did_not_reach_session_close")
    if late_start_seconds > LATE_START_TOLERANCE_SECONDS:
        partial_reasons.append(f"late_start_seconds={late_start_seconds:.1f}")
    if capture_result.reconnects > 0:
        partial_reasons.append(f"reconnects={capture_result.reconnects}")
    if not uploader.fully_drained():
        partial_reasons.append("upload_spool_not_fully_drained")
    if reconciliation["needs_review"]:
        partial_reasons.append("reconciliation_needs_review")
    status = "partial" if partial_reasons else "complete"

    attempt_event_counts = dict(stats.counts)
    summary = {
        "schema_version": 2,
        "issue": "MOO-169",
        "run_id": run_id,
        "provider": "tradier",
        "started_at": started_at,
        "finished_at": finished_at,
        "session_open": session_open.isoformat(),
        "session_close": session_close.isoformat(),
        "late_start_seconds": late_start_seconds,
        "stopped_by_signal": STOP,
        "fatal_error": fatal_error,
        "status": status,
        "partial_reasons": partial_reasons,
        "universe": universe,
        "reconnects": capture_result.reconnects,
        "gap_seconds": round(capture_result.gap_seconds, 3),
        "spool_overloaded": uploader.overloaded,
        "upload_failures": uploader.failures,
        "attempt_event_counts": attempt_event_counts,
        "reconciled_prior_records": reconciled_records_total,
        "total_records_this_run_plus_reconciled": sum(attempt_event_counts.values()) + reconciled_records_total,
        "needs_review": reconciliation["needs_review"],
        **stats.summary(),
        "limitations": [
            "The preserved payload is normalized/enriched JSON, not byte-exact wire data.",
            "Dedup horizon is per-symbol last-seen sequence only; it resets on restart.",
            "Reconnecting does not backfill missed transactions; gap_seconds is the measured outage total.",
            "Customer identity, opening/closing status, and multi-leg grouping are not inferred.",
        ],
        "event_parts": [*reconciliation["artifacts"], *uploader.artifacts],
    }
    summary_meta = json_artifact(r2, bucket, f"{prefix}/summary-{owner_id}.json", summary)
    manifest = {
        "schema_version": 2,
        "issue": "MOO-169",
        "run_id": run_id,
        "prefix": prefix,
        "status": status,
        "partial_reasons": partial_reasons,
        "artifacts": [preflight, *reconciliation["artifacts"], *uploader.artifacts, summary_meta],
        "lease": lease,
    }
    manifest_meta = json_artifact(r2, bucket, f"{prefix}/manifest-{owner_id}.json", manifest)
    print(json.dumps({
        "event": "collector_complete",
        "run_id": run_id,
        "status": status,
        "partial_reasons": partial_reasons,
        "manifest": manifest_meta,
        "event_counts": attempt_event_counts,
    }), flush=True)
    if fatal_error is not None:
        raise RuntimeError(fatal_error)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({
            "event": "collector_failed",
            "error": type(exc).__name__,
            "message": str(exc),
        }), flush=True)
        raise
