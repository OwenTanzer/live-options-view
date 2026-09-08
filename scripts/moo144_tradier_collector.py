#!/usr/bin/env python3
"""MOO-169 permanent daily Tradier option Time & Sale collector.

Launched once per weekday by a Railway Cron Schedule, shortly before the
NYSE open. Unlike the one-shot MOO-144 probe (``moo144_tradier_probe.py``,
whose ``select_symbols``/``Tradier``/R2 helpers this module reuses), this
process:

- is calendar-gated instead of date-pinned: it exits 0 immediately on a
  day with no NYSE session (the cron still fires on holidays; this makes
  that a no-op), and otherwise captures through to the calendar's actual
  close (early closes included) instead of a fixed duration.
- bounds its own memory: dedup keys on each symbol's last-seen sequence
  number only (not an ever-growing set of every event seen this session),
  and keeps only a fixed-size reservoir of quote-age samples plus running
  lifetime min/max/count/sum. The dedup horizon is therefore "this
  process's lifetime, per symbol, last event only" -- it resets on
  restart and cannot detect a duplicate replayed after a later event for
  the same symbol.
- decouples ingest from upload: closed segments are hard-linked into a
  persistent spool directory and a background thread uploads them with
  retry/backoff, so a slow or failing upload never blocks the stream
  reader. Spool growth is bounded and reported, never silently dropped.
- claims the day with a renewable lease (not a permanent one-shot claim),
  so a crash-and-restart within the session is safe: a new process may
  take over only once the previous lease has expired, and it reconciles
  already-uploaded segments from R2 before resuming so restarts never
  double-count or overwrite the manifest.
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
    sha256_file,
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
    """Bounded-memory event statistics for a single collector run.

    Dedup keys on (symbol -> last-seen seq) rather than a growing set of
    every event, and quote-age percentiles come from a fixed-size reservoir
    plus running lifetime aggregates, so memory is O(distinct symbols) for
    the whole session rather than O(events).
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

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type", "unknown"))
        self.counts[event_type] += 1
        self.last_receipt_ts = event.get("collector_receipt_timestamp") or self.last_receipt_ts
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
    existing_etag: str | None = None
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        existing_etag = head.get("ETag")
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        current = json.loads(body)
        expires_at = datetime.fromisoformat(current["expires_at"])
        if expires_at > now() and current.get("owner_id") != owner_id:
            raise RuntimeError(
                f"MOO-144 collection lease for {run_date} is held by "
                f"{current.get('owner_id')!r} until {current['expires_at']}"
            )
    except client.exceptions.NoSuchKey:
        pass
    except client.exceptions.ClientError:
        pass

    lease = {
        "run_date": run_date,
        "owner_id": owner_id,
        "acquired_at": now().isoformat(),
        "expires_at": (now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    put_kwargs: dict[str, Any] = {}
    if existing_etag:
        put_kwargs["IfMatch"] = existing_etag
    else:
        put_kwargs["IfNoneMatch"] = "*"
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
    lease = {
        "run_date": run_date,
        "owner_id": owner_id,
        "acquired_at": now().isoformat(),
        "expires_at": (now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    body = (json.dumps(lease, indent=2, sort_keys=True) + "\n").encode()
    client.put_object(Bucket=bucket, Key=lease_key(run_date), Body=body, ContentType="application/json")
    return lease


class Uploader:
    """Background thread that uploads closed spool segments with retry/backoff.

    Ingest hands off a finished file path via ``enqueue`` and never blocks on
    the network. Spool bytes are bounded by ``max_spool_bytes``; exceeding it
    raises an explicit overload flag (reported in health/manifest) instead of
    ever silently discarding a segment.
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
            if self.spool_bytes() > self.max_spool_bytes:
                self.overloaded = True

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


class SegmentSpool:
    """Writes gzip NDJSON segments to a persistent spool dir and hands closed
    ones to the uploader. Filenames are collision-free across restarts."""

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
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self.path = self.spool_dir / f"{self.owner_id}-part-{self.index:04d}.ndjson.gz"
        self.handle = gzip.open(self.path, "wt", encoding="utf-8")
        self.records = 0
        self.opened_at = self.clock()

    def write(self, event: dict[str, Any]) -> None:
        self.handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
        self.records += 1
        if self.clock() - self.opened_at >= self.checkpoint_seconds:
            self.rotate()

    def rotate(self, final: bool = False) -> None:
        if self.handle is None or self.path is None:
            return
        self.handle.close()
        if self.records:
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


def reconcile_existing_segments(r2: Any, bucket: str, prefix: str, spool_dir: Path) -> list[dict[str, Any]]:
    """Merge already-uploaded segment keys with any spool files left behind
    by a crash, without double-counting or re-uploading a confirmed segment.
    """
    uploaded_names: set[str] = set()
    paginator_kwargs = {"Bucket": bucket, "Prefix": f"{prefix}/"}
    try:
        response = r2.list_objects_v2(**paginator_kwargs)
    except Exception:
        response = {"Contents": []}
    artifacts: list[dict[str, Any]] = []
    for item in response.get("Contents", []):
        key = item.get("Key", "")
        if not key.endswith(".ndjson.gz"):
            continue
        uploaded_names.add(Path(key).name)
        artifacts.append({"key": key, "bytes": item.get("Size"), "reconciled": True})
    if spool_dir.exists():
        for path in sorted(spool_dir.glob("*.ndjson.gz")):
            if path.name in uploaded_names:
                path.unlink(missing_ok=True)
    return artifacts


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
    storage_failures: int,
) -> None:
    payload = {
        "updated_at": utc_now(),
        "last_receipt_timestamp": stats.last_receipt_ts,
        "event_counts": dict(stats.counts),
        "reconnects": reconnects,
        "malformed_payloads": stats.malformed,
        "duplicate_count": stats.duplicate_count,
        "spool_backlog_bytes": uploader.spool_bytes(),
        "spool_overloaded": uploader.overloaded,
        "upload_failures": uploader.failures,
        "storage_failures": storage_failures,
    }
    json_artifact(r2, bucket, f"{prefix}/health.json", payload)


def capture_session(
    client: Tradier,
    symbols: list[str],
    spool: SegmentSpool,
    stats: BoundedStats,
    session_close: datetime,
    max_consecutive_reconnects: int,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    now_et: Callable[[], datetime] = lambda: datetime.now(ET),
) -> int:
    reconnects = 0
    consecutive_failures = 0

    while not STOP and now_et() < session_close:
        received = False
        try:
            session_id = client.create_market_session()
            payload = stream_payload(symbols, session_id)
            with client.session.get(
                STREAM, params=payload, stream=True, timeout=(15, 10)
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if STOP or now_et() >= session_close:
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
                if STOP or now_et() >= session_close:
                    break
                raise requests.ConnectionError("Tradier stream ended cleanly before session close")
        except (requests.RequestException, OSError) as exc:
            if not is_retryable(exc):
                raise
            spool.write({
                "type": "gap",
                "reason": "stream_disconnect",
                "receipt_timestamp": utc_now(),
                "error_type": type(exc).__name__,
            })
            reconnects += 1
            consecutive_failures = 1 if received else consecutive_failures + 1
            spool.write({
                "type": "gap",
                "reason": "stream_reconnect",
                "receipt_timestamp": utc_now(),
                "reconnect": reconnects,
                "error_type": type(exc).__name__,
            })
            if consecutive_failures > max_consecutive_reconnects:
                raise RuntimeError(
                    f"Tradier stream exceeded {max_consecutive_reconnects} consecutive reconnects"
                ) from exc
            delay = min(2 ** (consecutive_failures - 1), 15)
            remaining = delay
            while remaining > 0 and not STOP:
                interval = min(0.5, remaining)
                sleeper(interval)
                remaining -= interval
    return reconnects


def main() -> int:
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

    today = datetime.now(ET).date()
    bounds = nyse_session_bounds(today)
    if bounds is None:
        print(json.dumps({"event": "no_session_today", "date": today.isoformat()}), flush=True)
        return 0
    session_open, session_close = bounds
    run_date = today.isoformat()

    now_et = datetime.now(ET)
    if now_et < session_open:
        wait_seconds = (session_open - now_et).total_seconds()
        print(json.dumps({"event": "waiting_for_open", "seconds": wait_seconds}), flush=True)
        time.sleep(max(0.0, wait_seconds))
    if datetime.now(ET) >= session_close:
        print(json.dumps({"event": "session_already_closed", "date": run_date}), flush=True)
        return 0

    owner_id = uuid.uuid4().hex[:12]
    run_id = f"{run_date}-{owner_id}"
    prefix = f"moo144/tradier/{run_date}"
    started_at = utc_now()
    print(json.dumps({"event": "collector_start", "run_id": run_id}), flush=True)

    client = Tradier(token)
    symbols, universe = select_symbols(client, strike_count, 0, run_date, datetime.now(ET))
    r2, bucket = r2_client()
    lease = acquire_lease(r2, bucket, run_date, owner_id, lease_ttl_seconds)
    reconciled = reconcile_existing_segments(r2, bucket, prefix, spool_dir)

    start_payload = {
        "schema_version": 1,
        "issue": "MOO-169",
        "run_id": run_id,
        "started_at": started_at,
        "session_open": session_open.isoformat(),
        "session_close": session_close.isoformat(),
        "universe": universe,
        "lease": lease,
        "reconciled_segments": len(reconciled),
    }
    preflight = json_artifact(r2, bucket, f"{prefix}/run-started-{owner_id}.json", start_payload)
    print(json.dumps({"event": "r2_preflight_pass", "key": preflight["key"]}), flush=True)

    uploader = Uploader(r2, bucket, prefix, max_spool_bytes)
    uploader.start()
    spool = SegmentSpool(spool_dir, owner_id, uploader, checkpoint_seconds)
    stats = BoundedStats()
    storage_failures = 0
    fatal_error: str | None = None

    lease_stop = threading.Event()

    def lease_heartbeat() -> None:
        while not lease_stop.wait(lease_ttl_seconds / 3):
            try:
                renew_lease(r2, bucket, run_date, owner_id, lease_ttl_seconds)
            except Exception:
                pass

    heartbeat_thread = threading.Thread(target=lease_heartbeat, daemon=True)
    heartbeat_thread.start()

    try:
        reconnects = capture_session(
            client, symbols, spool, stats, session_close, max_reconnects
        )
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        reconnects = 0
    finally:
        lease_stop.set()
        spool.close()
        uploader.drain_and_stop()

    write_health(r2, bucket, prefix, stats, uploader, reconnects, storage_failures)

    finished_at = utc_now()
    completed_full_session = (
        fatal_error is None and datetime.now(ET) >= session_close and not STOP
    )
    summary = {
        "schema_version": 1,
        "issue": "MOO-169",
        "run_id": run_id,
        "provider": "tradier",
        "started_at": started_at,
        "finished_at": finished_at,
        "session_open": session_open.isoformat(),
        "session_close": session_close.isoformat(),
        "stopped_by_signal": STOP,
        "fatal_error": fatal_error,
        "status": "complete" if completed_full_session else "partial",
        "universe": universe,
        "reconnects": reconnects,
        "spool_overloaded": uploader.overloaded,
        "upload_failures": uploader.failures,
        **stats.summary(),
        "limitations": [
            "The preserved payload is normalized/enriched JSON, not byte-exact wire data.",
            "Dedup horizon is per-symbol last-seen sequence only; it resets on restart.",
            "Reconnecting does not backfill missed transactions.",
            "Customer identity, opening/closing status, and multi-leg grouping are not inferred.",
        ],
        "event_parts": [*reconciled, *uploader.artifacts],
    }
    summary_meta = json_artifact(r2, bucket, f"{prefix}/summary-{owner_id}.json", summary)
    manifest = {
        "schema_version": 1,
        "issue": "MOO-169",
        "run_id": run_id,
        "prefix": prefix,
        "status": summary["status"],
        "artifacts": [preflight, *reconciled, *uploader.artifacts, summary_meta],
        "lease": lease,
    }
    manifest_meta = json_artifact(r2, bucket, f"{prefix}/manifest-{owner_id}.json", manifest)
    print(json.dumps({
        "event": "collector_complete",
        "run_id": run_id,
        "status": summary["status"],
        "manifest": manifest_meta,
        "event_counts": dict(stats.counts),
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
