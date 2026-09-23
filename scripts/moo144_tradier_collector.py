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
  ingestion) rather than clobbering another owner's lease. A renewal
  failure that isn't a confirmed loss (a transient network/storage error)
  is retried, not swallowed -- ownership is only relinquished once the
  last *confirmed* deadline actually elapses. A crash/restart reconciles
  against R2 by content hash before resuming, and re-enqueues any
  locally-spooled segment R2 doesn't already have -- content identity is
  always verified (downloading and hashing when the listing ETag alone
  can't establish it), never assumed, before deleting local evidence.
- spools each session under its own per-date subdirectory, so a crash on
  one date can never be resumed under a different date's archive prefix;
  any other date's leftover spool is recovered under its own prefix
  before today's session starts.
- persists the day's selected contract universe once and reloads it on any
  same-day restart, instead of re-selecting against the current spot price.
- owns the account's single Tradier market-data stream. Tradier permits one
  simultaneous market-data stream, so several underlyings (``MOO144_UNDERLYINGS``,
  e.g. ``QQQ,IBIT:nearest``) share one subscription and one stream lease; a
  ``StreamRouter`` fans events out to per-underlying archive lanes, each with
  its own universe, spool, uploader, reconciliation, stats and summary. The
  first-listed underlying is the primary and keeps the single-underlying
  startup path; the others are optional, prepared concurrently and read-only,
  and admitted only if ready by a pre-open cutoff, so they can never delay the
  primary's connection.
- derives "complete" vs "partial" from actual coverage -- on-time setup
  (not just an on-time open-wait), at least one captured event, no
  reconnect gaps, spool fully drained, reconciliation clean -- rather than
  merely "reached wall-clock close without an exception". A gap stays open
  (measuring the full outage) until data actually resumes, not merely
  after a backoff sleep.
- exits non-zero on a recoverable operational failure (spool exhaustion,
  an undrained upload backlog, zero captured events) so the deployment's
  restart policy actually fires, while deliberately exiting 0 on lease
  loss -- another owner is legitimately active for that run_date, and
  restarting would only start a competing-restart loop against them.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
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
    DEFAULT_UNDERLYING,
    EXPIRATION_POLICIES,
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
PREOPEN_SETUP_SECONDS = 60
HEALTH_PUBLISH_INTERVAL_SECONDS = 60
MAX_UNDERLYINGS = 4
# Optional lanes must be prepared this long before the open, leaving the main
# thread time to persist/preflight them, create the stream session, connect
# and prove readiness before the opening boundary.
OPTIONAL_LANE_RESERVE_SECONDS = 20
# When the primary is only ready after that cutoff (late start or restart),
# the session is already late; optional lanes get this much more, bounded, so a
# restart does not drop them for the rest of the day over a scheduling race.
LATE_START_OPTIONAL_BUDGET_SECONDS = 5
OPTIONAL_LANE_POLL_SECONDS = 0.25
# Safety net only: configured spool caps may use at most this share of the
# spool volume, leaving room for recovery files and filesystem overhead.
SPOOL_VOLUME_MAX_FRACTION = 0.8
# QQQ keeps the original layout so existing archives, leases and the deployed
# service are unaffected; other underlyings get a sibling namespace.
QQQ_ARCHIVE_ROOT = "moo144/tradier"


def archive_root(underlying: str) -> str:
    if underlying == DEFAULT_UNDERLYING:
        return QQQ_ARCHIVE_ROOT
    return f"{QQQ_ARCHIVE_ROOT}-{underlying.lower()}"


def spool_dir_name(underlying: str) -> str:
    if underlying == DEFAULT_UNDERLYING:
        return "moo144-collector-spool"
    return f"moo144-collector-spool-{underlying.lower()}"


def parse_underlyings(raw: str) -> list[tuple[str, str]]:
    """Parse ``MOO144_UNDERLYINGS``: comma-separated ``SYMBOL[:policy]``.

    All listed underlyings share this process's one Tradier market-data
    stream, so they are configured together rather than as separate services.
    The policy defaults to ``same_day`` (strict 0DTE).
    """
    entries: list[tuple[str, str]] = []
    for item in raw.split(","):
        if not item.strip():
            continue
        symbol, _, policy = item.partition(":")
        symbol = symbol.strip().upper()
        policy = policy.strip().lower() or "same_day"
        if not (symbol.isascii() and symbol.isalpha() and 1 <= len(symbol) <= 6):
            raise RuntimeError(f"MOO144_UNDERLYINGS: invalid symbol {symbol!r}")
        if policy not in EXPIRATION_POLICIES:
            raise RuntimeError(
                f"MOO144_UNDERLYINGS: {symbol} policy must be one of {', '.join(EXPIRATION_POLICIES)}"
            )
        if any(symbol == existing for existing, _ in entries):
            raise RuntimeError(f"MOO144_UNDERLYINGS: duplicate symbol {symbol}")
        entries.append((symbol, policy))
    if not 1 <= len(entries) <= MAX_UNDERLYINGS:
        raise RuntimeError(f"MOO144_UNDERLYINGS must list 1-{MAX_UNDERLYINGS} underlyings")
    return entries


class LeaseLost(RuntimeError):
    """Base class: this process can no longer prove it holds the lease.

    Never raised directly by renew_lease -- always one of the two specific
    subclasses below, since "confirmed takeover" and "confirmed absence"
    require different recovery behavior (see run_lease_heartbeat). Kept as
    a common base so any code that only needs "should ingestion stop" can
    still catch it broadly.
    """


class LeaseTakenByAnotherOwner(LeaseLost):
    """A verified, live competing owner now holds the lease. This process
    is definitively no longer the owner -- restarting would only compete
    with the real new owner, so this must never trigger a restart."""


class LeaseMissing(LeaseLost):
    """The lease object is confirmed absent (deleted, or expired and never
    recreated) -- NOT evidence that another owner exists. Unlike a
    confirmed takeover, restarting here is the correct recovery: nobody
    is known to be collecting, so a fresh process should reacquire."""


class LeaseOwnershipUncertain(LeaseLost):
    """Stop intake and recover; no live competing owner has been verified."""


class LeaseUnavailable(RuntimeError):
    """Raised when the lease object could not be read due to a transient
    storage/service failure -- distinct from a *confirmed* absence
    (never created) or a confirmed takeover (another owner_id present).
    Ownership is neither proven lost nor proven held in this case."""


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
        self.preopen_quote_context: dict[str, dict[str, Any]] = {}
        self.quote_age_reservoir: deque[int] = deque(maxlen=reservoir_size)
        self.quote_age_count = 0
        self.quote_age_sum = 0
        self.quote_age_min: int | None = None
        self.quote_age_max: int | None = None
        self.last_receipt_ts: str | None = None
        self.first_receipt_ts: str | None = None
        self.excluded_timesales: Counter[str] = Counter()

    def observe_malformed(self) -> None:
        self.malformed += 1

    def observe_excluded(self, _event: dict[str, Any], reason: str) -> None:
        self.counts["excluded_timesale"] += 1
        self.excluded_timesales[reason] += 1

    def retain_quote(self, event: dict[str, Any], *, preopen: bool = False) -> None:
        """Retain quote context without adding a regular-session observation."""
        symbol = str(event.get("symbol", "unknown"))
        candidates = [value for value in (
            parse_epoch_ms(event.get("biddate")), parse_epoch_ms(event.get("askdate"))
        ) if value is not None]
        if not candidates:
            return
        timestamp = max(candidates)
        if timestamp < self.quote_timestamps.get(symbol, 0):
            return
        self.quote_timestamps[symbol] = timestamp
        if preopen:
            self.preopen_quote_context[symbol] = dict(event)
        else:
            self.preopen_quote_context.pop(symbol, None)

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type", "unknown"))
        self.counts[event_type] += 1
        receipt = event.get("collector_receipt_timestamp")
        if receipt:
            self.first_receipt_ts = self.first_receipt_ts or receipt
            self.last_receipt_ts = receipt
        symbol = str(event.get("symbol", "unknown"))
        if event_type == "quote":
            self.retain_quote(event)
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
            if symbol in self.preopen_quote_context:
                event["preceding_quote_source"] = "preopen"
                event["preceding_quote_context"] = self.preopen_quote_context[symbol]
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
    """The day's single stream lease. It guards the one Tradier market-data
    stream, not an archive, so it stays at this fixed key whichever
    underlyings a collector is configured for: any two collectors sharing
    the token contend here and at most one can open a stream."""
    return f"{QQQ_ARCHIVE_ROOT}/{run_date}/lease.json"


def _is_confirmed_absent(exc: Exception) -> bool:
    """True only for a response that positively confirms the object doesn't
    exist (a 404/NoSuchKey) -- never for a 5xx or other service/transport
    failure, which proves nothing about whether the lease exists."""
    response = getattr(exc, "response", {}) or {}
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    code = (response.get("Error") or {}).get("Code")
    return status == 404 or code in {"NoSuchKey", "404", "NotFound"}


def _read_lease(client: Any, bucket: str, run_date: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read the lease object, or (None, None) for a *confirmed* absence.

    A transient storage/service failure (a 5xx, timeout, or any ClientError
    that isn't a positive 404) raises ``LeaseUnavailable`` instead of being
    silently treated as "no lease" -- that conflation is what let a single
    InternalError look identical to a genuinely deleted/never-created lease
    to every caller.
    """
    key = lease_key(run_date)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body), head.get("ETag")
    except client.exceptions.NoSuchKey:
        return None, None
    except client.exceptions.ClientError as exc:
        if _is_confirmed_absent(exc):
            return None, None
        raise LeaseUnavailable(f"transient error reading lease for {run_date}") from exc


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


def _require_live_lease_owner(
    current: dict[str, Any] | None, owner_id: str, run_date: str,
    now: Callable[[], datetime],
) -> None:
    if current is None:
        raise LeaseMissing(f"MOO-144 lease for {run_date} is confirmed absent")
    try:
        current_owner = current["owner_id"]
        expires_at = datetime.fromisoformat(current["expires_at"])
        if not isinstance(current_owner, str) or not current_owner or expires_at.tzinfo is None:
            raise ValueError("invalid lease identity or expiry")
        live = expires_at > now()
    except (KeyError, TypeError, ValueError) as exc:
        raise LeaseOwnershipUncertain(f"MOO-144 lease for {run_date} is invalid") from exc
    if not live:
        raise LeaseOwnershipUncertain(f"MOO-144 lease for {run_date} has expired")
    if current_owner != owner_id:
        raise LeaseTakenByAnotherOwner(
            f"MOO-144 lease for {run_date} is now owned by {current_owner!r}"
        )


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

    Raises ``LeaseMissing`` if the lease object is confirmed absent (never
    proof of a competing owner), or ``LeaseTakenByAnotherOwner`` if a live
    competing owner is actually present -- these require different
    recovery behavior and must never be conflated (see run_lease_heartbeat).
    A transient read failure propagates as ``LeaseUnavailable`` from
    ``_read_lease`` unchanged.
    """
    current, etag = _read_lease(client, bucket, run_date)
    _require_live_lease_owner(current, owner_id, run_date, now)
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
            # A failed conditional write proves only that renewal failed.
            # Deletion also causes this response; verify ownership afresh.
            try:
                current, _ = _read_lease(client, bucket, run_date)
            except Exception as read_exc:
                raise LeaseOwnershipUncertain(
                    f"MOO-144 lease for {run_date} could not be verified after renewal conflict"
                ) from read_exc
            _require_live_lease_owner(current, owner_id, run_date, now)
            raise LeaseOwnershipUncertain(
                f"MOO-144 lease for {run_date} renewal conflicted without a verified takeover"
            ) from exc
        raise
    return lease


def run_lease_heartbeat(
    r2: Any,
    bucket: str,
    run_date: str,
    owner_id: str,
    ttl_seconds: int,
    confirmed_until_ref: dict[str, datetime],
    stop: threading.Event,
    lease_lost: threading.Event,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    wait: Callable[[float], bool] | None = None,
    loss_reason: dict[str, str] | None = None,
) -> None:
    """Renew the collection lease every ``ttl_seconds/3`` until told to stop.

    This function does NOT enforce the confirmed-ownership deadline itself
    -- that used to happen here (checked only after a renewal request
    returned), which meant a slow/blocked request could carry the process
    past its deadline before the check ever ran. Deadline enforcement is
    now ``run_lease_deadline_watchdog``'s job, on its own tight polling
    cadence that is never blocked by a network call. This function's only
    responsibilities are: attempt renewals, keep ``confirmed_until_ref``
    current on success (read by the watchdog), and record *why* ownership
    was confirmed lost when that happens -- three genuinely different
    outcomes, via ``loss_reason``:

    - ``LeaseTakenByAnotherOwner`` (a verified, live competing owner) means
      this process is definitively no longer the owner. Restarting would
      only compete with the real new owner.
      ``loss_reason["reason"] = "confirmed_takeover"``.
    - ``LeaseMissing`` (the lease object is confirmed absent -- deleted or
      never recreated) is NOT evidence of a competing owner. Restarting is
      the correct recovery here: nobody is known to be collecting.
      ``loss_reason["reason"] = "confirmed_absence"``.
    - Any other failure (``LeaseUnavailable``, or an unexpected exception)
      proves nothing about ownership either way and is simply retried;
      the watchdog is what eventually stops intake if this keeps failing
      long enough to cross the confirmed deadline.
    """
    wait = wait if wait is not None else stop.wait
    while not wait(ttl_seconds / 3):
        try:
            lease = renew_lease(r2, bucket, run_date, owner_id, ttl_seconds, now=now)
            confirmed_until_ref["value"] = datetime.fromisoformat(lease["expires_at"])
        except LeaseTakenByAnotherOwner:
            if loss_reason is not None:
                loss_reason["reason"] = "confirmed_takeover"
            lease_lost.set()
            return
        except LeaseMissing:
            if loss_reason is not None:
                loss_reason["reason"] = "confirmed_absence"
            lease_lost.set()
            return
        except LeaseLost:
            # Unclassified loss is not proof of a live competing owner.
            # Reacquisition remains conditional, so recovery cannot steal
            # a lease from a verified live owner.
            if loss_reason is not None:
                loss_reason.setdefault("reason", "ownership_uncertain")
            lease_lost.set()
            return
        except Exception:
            # Transient failure -- keep retrying. The confirmed deadline is
            # enforced independently by run_lease_deadline_watchdog.
            pass


def run_lease_deadline_watchdog(
    confirmed_until_ref: dict[str, datetime],
    stop: threading.Event,
    lease_lost: threading.Event,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    wait: Callable[[float], bool] | None = None,
    poll_seconds: float = 1.0,
    loss_reason: dict[str, str] | None = None,
) -> None:
    """Independently enforce the confirmed-ownership deadline.

    Runs on its own short, fixed polling cadence -- never blocked by a
    network call -- so a renewal request that hangs or takes far longer
    than expected can never postpone stopping intake past the deadline
    this process last actually proved it held. ``confirmed_until_ref`` is
    the same shared reference ``run_lease_heartbeat`` updates on every
    successful renewal, so an extended deadline is picked up on this
    thread's very next poll.

    Uses ``loss_reason.setdefault`` rather than overwriting: if the
    heartbeat has already recorded a more specific reason (a confirmed
    takeover or confirmed absence) for the same ``lease_lost`` signal,
    that stays authoritative over this fail-safe's generic
    ``"ownership_uncertain"``.
    """
    wait = wait if wait is not None else stop.wait
    while not wait(poll_seconds):
        if now() >= confirmed_until_ref["value"]:
            if loss_reason is not None:
                loss_reason.setdefault("reason", "ownership_uncertain")
            lease_lost.set()
            return


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
    session_open: datetime | None = None,
    *,
    underlying: str = DEFAULT_UNDERLYING,
    expiration_policy: str = "same_day",
) -> tuple[list[str], dict[str, Any]]:
    """Load the day's already-selected contract universe, or select and
    persist it once. A same-day restart must never re-select against a
    (possibly moved) current spot price -- MOO-169 requires the universe
    stay fixed for the whole session.
    """
    loaded = load_persisted_universe(r2, bucket, prefix)
    if loaded is not None:
        return loaded
    symbols, universe = select_symbols(
        client, strike_count, 0, run_date, now_et, session_open=session_open,
        underlying=underlying, expiration_policy=expiration_policy,
    )
    return persist_universe(r2, bucket, prefix, symbols, universe)


def load_persisted_universe(r2: Any, bucket: str, prefix: str) -> tuple[list[str], dict[str, Any]] | None:
    try:
        body = r2.get_object(Bucket=bucket, Key=universe_key(prefix))["Body"].read()
        payload = json.loads(body)
        return payload["symbols"], payload["universe"]
    except r2.exceptions.NoSuchKey:
        return None
    except r2.exceptions.ClientError:
        return None


def persist_universe(
    r2: Any, bucket: str, prefix: str, symbols: list[str], universe: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Persist a freshly selected universe once; an existing one wins."""
    key = universe_key(prefix)
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
        total = 0
        with self.lock:
            for path in self.queue:
                # The upload thread unlinks a finished segment before taking the
                # lock to dequeue it; a vanished file is uploaded, not an error.
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
        return total

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
                    # Only read the file for a record count once the upload
                    # has actually succeeded -- not on every failed retry.
                    artifact["records"] = count_ndjson_gz_records_local(path)
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


def _remote_content_matches(
    r2: Any, bucket: str, key: str, local_path: Path, remote_item: dict[str, Any],
) -> bool:
    """Verify local file content against the durable remote object.

    Always returns a definitive True/False -- unknown identity must never
    authorize deletion. When the listing's ETag is a plain MD5 (the normal
    case for our single-part uploads) it's compared directly. When it
    isn't (e.g. a multipart upload, or missing), the listing alone can't
    establish identity, so the actual remote bytes are fetched and hashed
    instead of trusting the upload's own historical verification.
    """
    import hashlib
    local_digest = hashlib.md5()
    with local_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            local_digest.update(chunk)
    local_hex = local_digest.hexdigest()

    etag = str(remote_item.get("ETag") or "").strip('"')
    if etag and "-" not in etag:
        return local_hex == etag

    try:
        remote_bytes = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return False  # can't verify -- never delete on an unverifiable identity
    return local_hex == hashlib.md5(remote_bytes).hexdigest()


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
                key = f"{prefix}/{path.name}"
                if _remote_content_matches(r2, bucket, key, path, remote):
                    path.unlink(missing_ok=True)
                else:
                    needs_review.append(f"{path.name}: local content differs from archived object of the same name")
                continue
            if _is_readable_gzip_ndjson(path):
                resume.append(path)
            else:
                needs_review.append(f"{path.name}: truncated/corrupt, not resumable")

    return {"artifacts": artifacts, "resume": resume, "needs_review": needs_review}


def spool_dir_for(base_spool_dir: Path, run_date: str) -> Path:
    """Per-date spool subdirectory, so segments from different sessions never
    share a directory or filename-collide, and a stale date's leftovers are
    trivially distinguishable from today's."""
    return base_spool_dir / run_date


def recover_stale_sessions(
    base_spool_dir: Path,
    current_run_date: str,
    r2: Any,
    bucket: str,
    uploader_sleeper: Callable[[float], None] = time.sleep,
    drain_timeout_seconds: float = 30.0,
    *,
    root: str = QQQ_ARCHIVE_ROOT,
) -> dict[str, Any]:
    """Recover any prior day's spool left behind by a crash, before today's
    session starts.

    Each stale date subdirectory is reconciled and resumed against its OWN
    archive prefix (``<root>/<that date>``) -- never today's -- so a
    leftover segment from a previous session is never misfiled under the
    current one. Best-effort and time-bounded: a date that doesn't finish
    draining within ``drain_timeout_seconds`` is left on disk for the next
    run rather than blocking today's collection indefinitely.
    """
    report: dict[str, Any] = {}
    if not base_spool_dir.exists():
        return report
    for entry in sorted(base_spool_dir.iterdir()):
        if not entry.is_dir() or entry.name == current_run_date:
            continue
        try:
            date.fromisoformat(entry.name)
        except ValueError:
            continue  # not a session date directory -- leave it alone
        stale_prefix = f"{root}/{entry.name}"
        reconciliation = reconcile_existing_segments(r2, bucket, stale_prefix, entry)
        resumed_artifacts: list[dict[str, Any]] = []
        if reconciliation["resume"]:
            stale_uploader = Uploader(
                r2, bucket, stale_prefix, max_spool_bytes=1 << 62, sleeper=uploader_sleeper,
            )
            for path in reconciliation["resume"]:
                stale_uploader.enqueue(path)
            stale_uploader.start()
            stale_uploader.drain_and_stop(timeout=drain_timeout_seconds)
            resumed_artifacts = list(stale_uploader.artifacts)
        report[entry.name] = {
            "prefix": stale_prefix,
            "reconciled_artifacts": len(reconciliation["artifacts"]),
            "resumed_artifacts": resumed_artifacts,
            "resume_incomplete": len(resumed_artifacts) < len(reconciliation["resume"]),
            "needs_review": reconciliation["needs_review"],
        }
    return report


def count_ndjson_gz_records(r2: Any, bucket: str, key: str) -> int | None:
    """Best-effort record count for a reconciled segment this process didn't
    write itself, so manifest totals can be audited against event_parts."""
    try:
        body = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
        return sum(1 for line in gzip.decompress(body).splitlines() if line.strip())
    except Exception:
        return None


def count_ndjson_gz_records_local(path: Path) -> int | None:
    """Best-effort record count for a local segment before/at upload time --
    covers both freshly-rotated and resumed-orphan segments uniformly, so
    every uploaded artifact carries a durable count, not only reconciled
    remote ones."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
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


class Lane:
    """One underlying's archive state, fed by the shared stream."""

    def __init__(self, underlying: str, expiration_policy: str, run_date: str, spool_base: Path) -> None:
        self.underlying = underlying
        self.expiration_policy = expiration_policy
        self.root = archive_root(underlying)
        self.prefix = f"{self.root}/{run_date}"
        self.base_spool_dir = spool_base / spool_dir_name(underlying)
        self.spool_dir = spool_dir_for(self.base_spool_dir, run_date)
        self.symbols: list[str] = []
        self.universe: dict[str, Any] | None = None
        self.unavailable_reason: str | None = None
        self.stats = BoundedStats()
        self.spool_cap_bytes = 0
        # Optional-lane handover: preparation publishes into this lane only
        # under ``lock`` and only while not ``frozen``.
        self.lock = threading.Lock()
        self.frozen = False
        self.prepared = threading.Event()
        self.needs_persist = False
        self.stale_recovery: dict[str, Any] = {}
        self.reconciliation: dict[str, Any] = {"artifacts": [], "resume": [], "needs_review": []}
        self.resumed_names: set[str] = set()
        self.preflight: dict[str, Any] | None = None
        self.uploader: Uploader | None = None
        self.spool: SegmentSpool | None = None


def prepare_optional_lane(
    lane: Lane,
    client_factory: Callable[[], Tradier],
    r2: Any,
    bucket: str,
    strike_count: int,
    run_date: str,
    clock_et: Callable[[], datetime],
    session_open: datetime,
) -> None:
    """Prepare an optional lane concurrently with the primary.

    Read-only with respect to the archive: it loads or selects the universe
    and reconciles the local spool (which only deletes local copies verified
    to be archived already), but never writes remote state. Persisting a newly
    selected universe and the preflight happen on the main thread, and only
    for a lane admitted before the cutoff. Results are handed over under the
    lane lock; once the lane is frozen they are discarded, so late work can
    neither change the subscription nor publish anything.
    """
    needs_persist = False
    symbols: list[str] = []
    universe: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None
    error: str | None = None
    try:
        loaded = load_persisted_universe(r2, bucket, lane.prefix)
        if loaded is None:
            symbols, universe = select_symbols(
                client_factory(), strike_count, 0, run_date, clock_et(), session_open=session_open,
                underlying=lane.underlying, expiration_policy=lane.expiration_policy,
            )
            needs_persist = True
        else:
            symbols, universe = loaded
        reconciliation = reconcile_existing_segments(r2, bucket, lane.prefix, lane.spool_dir)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    with lane.lock:
        if lane.frozen:
            print(json.dumps({"event": "optional_lane_result_discarded", "underlying": lane.underlying,
                              "error": error}), flush=True)
            return
        if error is not None:
            lane.unavailable_reason = f"preparation_failed: {error}"
        else:
            lane.symbols, lane.universe = symbols, universe
            lane.reconciliation = reconciliation
            lane.needs_persist = needs_persist
        lane.prepared.set()


def admit_optional_lanes(
    lanes: list[Lane],
    session_open: datetime,
    clock_et: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> None:
    """Wait, bounded, for optional lanes; then freeze every one of them.

    The deadline is the pre-open cutoff, or a short fixed budget when the
    primary was only ready after it. Lanes not prepared by then are excluded
    for this session. Nothing waits on their threads afterwards.
    """
    cutoff = session_open - timedelta(seconds=OPTIONAL_LANE_RESERVE_SECONDS)
    now = clock_et()
    deadline = cutoff if now < cutoff else now + timedelta(seconds=LATE_START_OPTIONAL_BUDGET_SECONDS)
    while not STOP and any(not lane.prepared.is_set() for lane in lanes):
        now = clock_et()
        if now >= deadline:
            break
        sleeper(min(OPTIONAL_LANE_POLL_SECONDS, (deadline - now).total_seconds()))
    for lane in lanes:
        with lane.lock:
            lane.frozen = True
            if not lane.prepared.is_set() and lane.unavailable_reason is None:
                lane.unavailable_reason = "missed_preparation_cutoff"
        if lane.unavailable_reason is not None:
            print(json.dumps({"event": "optional_lane_excluded", "underlying": lane.underlying,
                              "reason": lane.unavailable_reason}), flush=True)


class StreamRouter:
    """Fans the single provider stream out to per-underlying lanes.

    Provides the spool/stats calls ``capture_session`` makes. An event for a
    subscribed symbol goes only to that symbol's lane. Records without a
    routable symbol (gaps, heartbeats, malformed payloads) describe the
    shared connection, so they go to every lane: each archive stays
    self-describing about its coverage and nothing is dropped.
    """

    def __init__(self, lanes: list[Lane]) -> None:
        self.lanes = lanes
        self.by_symbol: dict[str, Lane] = {}
        for lane in lanes:
            for symbol in lane.symbols:
                if symbol in self.by_symbol:
                    raise RuntimeError(
                        f"{symbol} selected for both {self.by_symbol[symbol].underlying} and {lane.underlying}"
                    )
                self.by_symbol[symbol] = lane
        self.symbols = list(self.by_symbol)

    def lanes_for(self, event: dict[str, Any]) -> list[Lane]:
        payload = event.get("provider_payload") if event.get("type") == "excluded_timesale" else event
        symbol = payload.get("symbol") if isinstance(payload, dict) else None
        lane = self.by_symbol.get(symbol) if isinstance(symbol, str) else None
        return [lane] if lane is not None else self.lanes

    def write(self, event: dict[str, Any]) -> None:
        for lane in self.lanes_for(event):
            lane.spool.write(event)

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        for lane in self.lanes_for(event):
            lane.stats.observe(event)
        return event

    def retain_quote(self, event: dict[str, Any], *, preopen: bool = False) -> None:
        for lane in self.lanes_for(event):
            lane.stats.retain_quote(event, preopen=preopen)

    def observe_malformed(self) -> None:
        for lane in self.lanes:
            lane.stats.observe_malformed()

    def observe_excluded(self, event: dict[str, Any], reason: str) -> None:
        for lane in self.lanes_for(event):
            lane.stats.observe_excluded(event, reason)


class CaptureResult:
    def __init__(self) -> None:
        self.reconnects = 0
        self.gap_seconds = 0.0
        self.stop_reason: str | None = None  # None, "lease_lost", or "spool_exhausted"
        self.stream_connected_at: datetime | None = None
        self.opening_stream_ready_at: datetime | None = None
        self.preopen_events_discarded = 0
        self.excluded_timesales: Counter[str] = Counter()


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
    result: CaptureResult | None = None,
    session_open: datetime | None = None,
) -> CaptureResult:
    """Run the stream-read loop until session close, a lost lease, or an
    exhausted spool.

    ``result`` may be supplied by the caller so it can be shared with a
    concurrently-running health publisher: this function mutates it in
    place (rather than only returning a fresh one at the end) so
    ``reconnects``/``gap_seconds`` are visible to another thread *during*
    a long-running capture, not just after it returns.

    A gap stays open (no ``stream_reconnect_resumed`` is written, and
    ``consecutive_failures`` keeps climbing) across every failed reconnect
    attempt following one ``stream_disconnect`` -- it only closes once an
    actual event is received again, and ``gap_seconds`` accumulates the
    full outage, not just backoff sleep time.
    """
    result = result if result is not None else CaptureResult()
    lease_lost = lease_lost or threading.Event()
    consecutive_failures = 0
    gap_open = False
    gap_started_at: float | None = None
    # The boundary a gap starts from is the last moment the stream was known
    # to be alive -- not when the current connection attempt began. A long
    # healthy connection that later drops must not have its entire lifetime
    # counted as outage.
    last_good_at = monotonic()

    def stop_requested() -> bool:
        return STOP or lease_lost.is_set()

    while not stop_requested() and now_et() < session_close:
        try:
            session_id = client.create_market_session()
            payload = stream_payload(symbols, session_id)
            with client.session.get(
                STREAM, params=payload, stream=True, timeout=(15, 10)
            ) as response:
                response.raise_for_status()
                connected_at = now_et() if session_open is not None else None
                if result.stream_connected_at is None:
                    result.stream_connected_at = connected_at
                # Readiness belongs to this connection, never an old one that
                # disconnected before the open. A 200 response alone is insufficient.
                connection_ready_at = None
                # Do not wait for requests' default 512-byte buffer to fill at
                # the boundary; process each complete provider line immediately.
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    observed_at = now_et()
                    if stop_requested() or observed_at >= session_close:
                        break
                    if not line:
                        continue
                    last_good_at = monotonic()
                    if gap_open:
                        outage_seconds = monotonic() - gap_started_at
                        result.gap_seconds += outage_seconds
                        spool.write({
                            "type": "gap",
                            "reason": "stream_reconnect_resumed",
                            "receipt_timestamp": utc_now(),
                            "reconnect": result.reconnects,
                            "outage_seconds": round(outage_seconds, 3),
                        })
                        gap_open = False
                        gap_started_at = None
                        consecutive_failures = 0
                    receipt = utc_now()
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("non-object event")
                    except (json.JSONDecodeError, ValueError, TypeError):
                        if session_open is not None and observed_at < session_open:
                            continue
                        stats.observe_malformed()
                        raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                        spool.write({
                            "type": "malformed",
                            "collector_receipt_timestamp": receipt,
                            "provider_payload": raw,
                        })
                        continue
                    event["collector_receipt_timestamp"] = receipt
                    event["provider"] = "tradier"
                    if session_open is not None:
                        valid = (event.get("type") == "heartbeat" or (
                            event.get("type") in {"quote", "timesale"}
                            and event.get("symbol") in symbols))
                        if valid and connection_ready_at is None:
                            connection_ready_at = observed_at
                            print(json.dumps({"event": "stream_ready", "at": observed_at.isoformat(),
                                              "before_open": observed_at <= session_open}), flush=True)
                        if observed_at < session_open:
                            if valid and event.get("type") == "quote":
                                stats.retain_quote(event, preopen=True)
                            result.preopen_events_discarded += 1
                            continue
                        if result.opening_stream_ready_at is None and valid:
                            result.opening_stream_ready_at = connection_ready_at
                        if event.get("type") == "timesale":
                            trade_ms = parse_epoch_ms(event.get("date"))
                            session = event.get("session")
                            reason = None
                            if trade_ms is None:
                                reason = "missing_or_invalid_provider_time"
                            elif not session_open.timestamp() * 1000 <= trade_ms < session_close.timestamp() * 1000:
                                reason = "outside_regular_session"
                            elif session not in (None, "", "normal"):
                                reason = "non_regular_session_label"
                            if reason:
                                result.excluded_timesales[reason] += 1
                                # Keep the original evidence, but never expose it
                                # as a regular timesale or feed it into trade stats.
                                spool.write({"type": "excluded_timesale", "reason": reason,
                                             "collector_receipt_timestamp": receipt,
                                             "provider_payload": event})
                                stats.observe_excluded(event, reason)
                                continue
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
            if not gap_open:
                gap_open = True
                gap_started_at = last_good_at
                consecutive_failures = 1
                spool.write({
                    "type": "gap",
                    "reason": "stream_disconnect",
                    "receipt_timestamp": utc_now(),
                    "error_type": type(exc).__name__,
                })
            else:
                consecutive_failures += 1
            result.reconnects += 1
            if consecutive_failures > max_consecutive_reconnects:
                budget_exceeded = RuntimeError(
                    f"Tradier stream exceeded {max_consecutive_reconnects} consecutive reconnects"
                )
                budget_exceeded.reconnects = result.reconnects  # type: ignore[attr-defined]
                budget_exceeded.gap_seconds = (  # type: ignore[attr-defined]
                    result.gap_seconds + (monotonic() - gap_started_at)
                )
                raise budget_exceeded from exc
            delay = min(2 ** (consecutive_failures - 1), 15)
            remaining = delay
            while remaining > 0 and not stop_requested():
                interval = min(0.5, remaining)
                sleeper(interval)
                remaining -= interval
    if lease_lost.is_set():
        result.stop_reason = "lease_lost"
    if gap_open and gap_started_at is not None:
        # Terminal stop/close while still disconnected -- preserve the
        # unresolved outage instead of silently discarding its duration.
        result.gap_seconds += monotonic() - gap_started_at
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
    # The primary lane keeps the full, existing cap; optional lanes need their
    # own explicit allocation rather than silently taking part of it.
    max_spool_bytes = int(os.getenv("MOO144_MAX_SPOOL_BYTES", str(512 * 1024 * 1024)))
    optional_spool_raw = os.getenv("MOO144_OPTIONAL_SPOOL_BYTES", "").strip()
    optional_spool_bytes = int(optional_spool_raw) if optional_spool_raw else None
    lane_specs = parse_underlyings(os.getenv("MOO144_UNDERLYINGS", DEFAULT_UNDERLYING))
    spool_base = Path(os.getenv("MOO144_SPOOL_DIR", tempfile.gettempdir()))
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
    setup_at = session_open - timedelta(seconds=PREOPEN_SETUP_SECONDS)
    if now_et < setup_at:
        print(json.dumps({"event": "waiting_for_preopen_setup", "at": setup_at.isoformat()}), flush=True)
        while now_et < setup_at:
            if STOP:
                return 0
            sleeper(min(1.0, (setup_at - now_et).total_seconds()))
            now_et = clock_et()
    if STOP:
        return 0
    late_start_seconds = max(0.0, (clock_et() - session_open).total_seconds())
    if clock_et() >= session_close:
        print(json.dumps({"event": "session_already_closed", "date": run_date}), flush=True)
        return 0

    owner_id = uuid.uuid4().hex[:12]
    run_id = f"{run_date}-{owner_id}"
    started_at = utc_now()
    print(json.dumps({"event": "collector_start", "run_id": run_id,
                      "underlyings": [symbol for symbol, _ in lane_specs]}), flush=True)

    client = Tradier(token)
    r2, bucket = r2_client()
    # One lease for the one provider stream, taken before any stream exists.
    lease = acquire_lease(r2, bucket, run_date, owner_id, lease_ttl_seconds)
    lanes = [Lane(symbol, policy, run_date, spool_base) for symbol, policy in lane_specs]
    primary, optional = lanes[0], lanes[1:]
    primary.spool_cap_bytes = max_spool_bytes
    allocation_problem = None
    if optional:
        if optional_spool_bytes is None or optional_spool_bytes <= 0:
            allocation_problem = "optional_spool_allocation_unset"
        else:
            spool_base.mkdir(parents=True, exist_ok=True)
            allocated = max_spool_bytes + optional_spool_bytes * len(optional)
            if allocated > shutil.disk_usage(spool_base).total * SPOOL_VOLUME_MAX_FRACTION:
                allocation_problem = "spool_allocation_exceeds_volume"
    pending_optional: list[Lane] = []
    for lane in optional:
        if allocation_problem is not None:
            lane.unavailable_reason, lane.frozen = allocation_problem, True
            print(json.dumps({"event": "optional_lane_excluded", "underlying": lane.underlying,
                              "reason": allocation_problem}), flush=True)
            continue
        lane.spool_cap_bytes = optional_spool_bytes
        pending_optional.append(lane)
        threading.Thread(
            target=prepare_optional_lane,
            args=(lane, lambda: Tradier(token), r2, bucket, strike_count, run_date, clock_et, session_open),
            name=f"prepare-{lane.underlying}", daemon=True,
        ).start()

    # The primary follows the single-underlying startup path unchanged,
    # concurrently with any optional lane preparation.
    primary.symbols, primary.universe = load_or_select_universe(
        client, r2, bucket, primary.prefix, strike_count, run_date, clock_et(), session_open=session_open,
        underlying=primary.underlying, expiration_policy=primary.expiration_policy,
    )
    primary.stale_recovery = recover_stale_sessions(
        primary.base_spool_dir, run_date, r2, bucket,
        uploader_sleeper=uploader_sleeper, drain_timeout_seconds=drain_timeout_seconds,
        root=primary.root,
    )
    primary.reconciliation = reconcile_existing_segments(r2, bucket, primary.prefix, primary.spool_dir)
    if pending_optional:
        admit_optional_lanes(pending_optional, session_open, clock_et, sleeper)
    for lane in optional:
        if lane.unavailable_reason is None and lane.needs_persist:
            try:
                lane.symbols, lane.universe = persist_universe(
                    r2, bucket, lane.prefix, lane.symbols, lane.universe)
            except Exception as exc:
                lane.unavailable_reason = f"universe_persist_failed: {type(exc).__name__}: {exc}"
    active = [primary, *(lane for lane in optional if lane.unavailable_reason is None)]
    router = StreamRouter(active)
    stream_info = {
        "shared_stream": True,
        "underlyings": [lane.underlying for lane in lanes],
        "active_underlyings": [lane.underlying for lane in active],
        "unavailable_underlyings": {lane.underlying: lane.unavailable_reason
                                    for lane in lanes if lane.unavailable_reason is not None},
        "subscribed_symbols": len(router.symbols),
        "lease_key": lease_key(run_date),
    }

    for lane in active:
        lane.resumed_names = {path.name for path in lane.reconciliation["resume"]}
        start_payload = {
            "schema_version": 2,
            "issue": "MOO-169",
            "run_id": run_id,
            "started_at": started_at,
            "session_open": session_open.isoformat(),
            "session_close": session_close.isoformat(),
            "late_start_seconds": late_start_seconds,
            "underlying": lane.underlying,
            "expiration_policy": lane.expiration_policy,
            "universe": lane.universe,
            "stream": stream_info,
            "spool_cap_bytes": lane.spool_cap_bytes,
            "lease": lease,
            "reconciled_segments": len(lane.reconciliation["artifacts"]),
            "resumed_segments": len(lane.reconciliation["resume"]),
            "needs_review": lane.reconciliation["needs_review"],
            # Optional lanes recover stale dates after the close, off the
            # pre-open critical path.
            "stale_sessions_recovered": (lane.stale_recovery if lane is primary
                                         else "deferred_until_after_close"),
        }
        lane.preflight = json_artifact(r2, bucket, f"{lane.prefix}/run-started-{owner_id}.json", start_payload)
        print(json.dumps({"event": "r2_preflight_pass", "underlying": lane.underlying,
                          "key": lane.preflight["key"]}), flush=True)

    for lane in active:
        lane.uploader = Uploader(r2, bucket, lane.prefix, lane.spool_cap_bytes, sleeper=uploader_sleeper)
        for path in lane.reconciliation["resume"]:
            lane.uploader.enqueue(path)
        lane.uploader.start()
        lane.spool = SegmentSpool(lane.spool_dir, owner_id, lane.uploader, checkpoint_seconds)
    # A single underlying writes straight to its lane, exactly as before.
    if len(active) == 1:
        stream_spool: Any = active[0].spool
        stream_stats: Any = active[0].stats
    else:
        stream_spool = stream_stats = router
    fatal_error: str | None = None
    capture_result = CaptureResult()

    lease_stop = threading.Event()
    lease_lost_event = threading.Event()
    lease_confirmed_until_ref: dict[str, datetime] = {"value": datetime.fromisoformat(lease["expires_at"])}
    lease_loss_reason: dict[str, str] = {}

    def publish_health() -> None:
        for lane in active:
            write_health(r2, bucket, lane.prefix, lane.stats, lane.uploader,
                         capture_result.reconnects, capture_result.gap_seconds)

    def health_publisher() -> None:
        while not lease_stop.wait(HEALTH_PUBLISH_INTERVAL_SECONDS):
            try:
                publish_health()
            except Exception:
                pass

    heartbeat_thread = threading.Thread(
        target=run_lease_heartbeat,
        args=(r2, bucket, run_date, owner_id, lease_ttl_seconds, lease_confirmed_until_ref, lease_stop, lease_lost_event),
        kwargs={"now": lambda: datetime.now(timezone.utc), "loss_reason": lease_loss_reason},
        daemon=True,
    )
    heartbeat_thread.start()
    # Independent of the heartbeat: enforces the confirmed deadline on its
    # own tight poll so a slow/hung renewal request can never postpone
    # stopping intake past the moment ownership can no longer be proven.
    watchdog_thread = threading.Thread(
        target=run_lease_deadline_watchdog,
        args=(lease_confirmed_until_ref, lease_stop, lease_lost_event),
        kwargs={"now": lambda: datetime.now(timezone.utc), "loss_reason": lease_loss_reason},
        daemon=True,
    )
    watchdog_thread.start()
    health_thread = threading.Thread(target=health_publisher, daemon=True)
    health_thread.start()

    capture_started_at = clock_et()
    try:
        if datetime.now(timezone.utc) >= lease_confirmed_until_ref["value"]:
            lease_loss_reason.setdefault("reason", "ownership_uncertain")
            lease_lost_event.set()
        capture_result = capture_session(
            client, router.symbols, stream_spool, stream_stats, session_close, max_reconnects,
            lease_lost=lease_lost_event, sleeper=sleeper, now_et=clock_et,
            result=capture_result,
            session_open=session_open,
        )
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        capture_result.reconnects = getattr(exc, "reconnects", capture_result.reconnects)
        capture_result.gap_seconds = getattr(exc, "gap_seconds", capture_result.gap_seconds)
    finally:
        lease_stop.set()
        for lane in active:
            lane.spool.close()
        for lane in active:
            lane.uploader.drain_and_stop(timeout=drain_timeout_seconds)

    try:
        publish_health()
    except Exception:
        pass

    finished_at = utc_now()
    opening_ready = capture_result.opening_stream_ready_at
    effective_late_start_seconds = max(
        0.0, ((opening_ready or capture_started_at) - session_open).total_seconds()
    )
    attempt_total_events = sum(sum(lane.stats.counts.values()) for lane in active)

    lease_loss_kind = lease_loss_reason.get("reason") if capture_result.stop_reason == "lease_lost" else None

    # Optional lanes' prior-date recovery, deferred off the pre-open path;
    # only while this process still owns the stream lease.
    for lane in optional:
        if capture_result.stop_reason == "lease_lost" or STOP:
            lane.stale_recovery = {"deferred": "stopped_by_signal" if STOP else "ownership_not_held"}
            continue
        lane.stale_recovery = recover_stale_sessions(
            lane.base_spool_dir, run_date, r2, bucket,
            uploader_sleeper=uploader_sleeper, drain_timeout_seconds=drain_timeout_seconds,
            root=lane.root,
        )

    # Stream-level coverage applies to every lane sharing the connection.
    stream_reasons: list[str] = []
    if fatal_error is not None:
        stream_reasons.append(f"fatal_error: {fatal_error}")
    if capture_result.stop_reason:
        reason_label = capture_result.stop_reason
        if lease_loss_kind:
            # Distinguish a confirmed takeover (another owner is now live --
            # restarting would only compete with them) from an uncertain
            # loss after repeated transient renewal failures (no competing
            # owner is known to exist -- restarting is the right recovery).
            reason_label = f"{reason_label}:{lease_loss_kind}"
        stream_reasons.append(reason_label)
    if STOP:
        stream_reasons.append("stopped_by_signal")
    if clock_et() < session_close:
        stream_reasons.append("did_not_reach_session_close")
    if opening_ready is None:
        stream_reasons.append("opening_stream_readiness_unproven")
    if effective_late_start_seconds > 0:
        stream_reasons.append(f"late_start_seconds={effective_late_start_seconds:.1f}")
    if capture_result.reconnects > 0:
        stream_reasons.append(f"reconnects={capture_result.reconnects}")

    def stale_needs_review(lane: Lane) -> bool:
        return any(isinstance(session, dict) and session.get("needs_review")
                   for session in lane.stale_recovery.values())

    lane_results: dict[str, dict[str, Any]] = {}
    for lane in lanes:
        if lane not in active:
            partial_reasons = [f"lane_unavailable: {lane.unavailable_reason}"]
            if stale_needs_review(lane):
                partial_reasons.append("stale_session_needs_review")
            summary = {
                "schema_version": 2,
                "issue": "MOO-169",
                "run_id": run_id,
                "provider": "tradier",
                "started_at": started_at,
                "finished_at": finished_at,
                "session_open": session_open.isoformat(),
                "session_close": session_close.isoformat(),
                "status": "partial",
                "partial_reasons": partial_reasons,
                "underlying": lane.underlying,
                "expiration_policy": lane.expiration_policy,
                "universe": None,
                "lane_unavailable_reason": lane.unavailable_reason,
                "stream": stream_info,
                "attempt_event_counts": {},
                "stale_sessions_recovered": lane.stale_recovery,
                "event_parts": [],
                "limitations": ["This underlying was not subscribed this session; no events were captured."],
            }
            summary_meta = json_artifact(r2, bucket, f"{lane.prefix}/summary-{owner_id}.json", summary)
            manifest_meta = json_artifact(r2, bucket, f"{lane.prefix}/manifest-{owner_id}.json", {
                "schema_version": 2, "issue": "MOO-169", "run_id": run_id, "prefix": lane.prefix,
                "underlying": lane.underlying, "status": "partial", "partial_reasons": partial_reasons,
                "artifacts": [summary_meta], "lease": lease,
            })
            lane_results[lane.underlying] = {"status": "partial", "partial_reasons": partial_reasons,
                                             "manifest": manifest_meta, "event_counts": {}}
            continue
        stats, uploader, reconciliation = lane.stats, lane.uploader, lane.reconciliation
        reconciled_records_total = 0
        for artifact in reconciliation["artifacts"]:
            records = count_ndjson_gz_records(r2, bucket, artifact["key"])
            artifact["records"] = records
            if records is not None:
                reconciled_records_total += records

        resumed_records_total = 0
        for artifact in uploader.artifacts:
            if Path(artifact["key"]).name in lane.resumed_names and artifact.get("records") is not None:
                resumed_records_total += artifact["records"]

        attempt_event_counts = dict(stats.counts)
        partial_reasons = list(stream_reasons)
        if stats.excluded_timesales.get("missing_or_invalid_provider_time"):
            partial_reasons.append("unclassifiable_timesale_provider_time")
        if not uploader.fully_drained():
            partial_reasons.append("upload_spool_not_fully_drained")
        if reconciliation["needs_review"]:
            partial_reasons.append("reconciliation_needs_review")
        if stale_needs_review(lane):
            partial_reasons.append("stale_session_needs_review")
        if stats.counts.get("quote", 0) + stats.counts.get("timesale", 0) == 0:
            partial_reasons.append("no_events_captured")
        status = "partial" if partial_reasons else "complete"

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
            "effective_late_start_seconds": effective_late_start_seconds,
            "stream_connected_at": (capture_result.stream_connected_at.isoformat()
                                    if capture_result.stream_connected_at else None),
            "opening_stream_ready_at": opening_ready.isoformat() if opening_ready else None,
            "preopen_events_discarded": capture_result.preopen_events_discarded,
            "excluded_timesales": dict(stats.excluded_timesales),
            "stopped_by_signal": STOP,
            "fatal_error": fatal_error,
            "status": status,
            "partial_reasons": partial_reasons,
            "underlying": lane.underlying,
            "expiration_policy": lane.expiration_policy,
            "universe": lane.universe,
            "lane_unavailable_reason": None,
            "stream": stream_info,
            "reconnects": capture_result.reconnects,
            "gap_seconds": round(capture_result.gap_seconds, 3),
            "spool_cap_bytes": lane.spool_cap_bytes,
            "spool_overloaded": uploader.overloaded,
            "upload_failures": uploader.failures,
            "attempt_event_counts": attempt_event_counts,
            "reconciled_prior_records": reconciled_records_total,
            "resumed_local_records": resumed_records_total,
            "total_records_this_run_plus_reconciled": (
                sum(attempt_event_counts.values()) + reconciled_records_total + resumed_records_total
            ),
            "needs_review": reconciliation["needs_review"],
            "stale_sessions_recovered": lane.stale_recovery,
            **stats.summary(),
            "limitations": [
                "The preserved payload is normalized/enriched JSON, not byte-exact wire data.",
                "Dedup horizon is per-symbol last-seen sequence only; it resets on restart.",
                "Reconnecting does not backfill missed transactions; gap_seconds is the measured outage total.",
                "Customer identity, opening/closing status, and multi-leg grouping are not inferred.",
                "Stream-level records (gaps, heartbeats, malformed payloads) are copied to every "
                "underlying sharing the stream; preopen_events_discarded counts the whole stream.",
            ],
            "event_parts": [*reconciliation["artifacts"], *uploader.artifacts],
        }
        summary_meta = json_artifact(r2, bucket, f"{lane.prefix}/summary-{owner_id}.json", summary)
        manifest = {
            "schema_version": 2,
            "issue": "MOO-169",
            "run_id": run_id,
            "prefix": lane.prefix,
            "underlying": lane.underlying,
            "status": status,
            "partial_reasons": partial_reasons,
            "artifacts": [lane.preflight, *reconciliation["artifacts"], *uploader.artifacts, summary_meta],
            "lease": lease,
        }
        manifest_meta = json_artifact(r2, bucket, f"{lane.prefix}/manifest-{owner_id}.json", manifest)
        lane_results[lane.underlying] = {
            "status": status,
            "partial_reasons": partial_reasons,
            "manifest": manifest_meta,
            "event_counts": attempt_event_counts,
        }
    print(json.dumps({
        "event": "collector_complete",
        "run_id": run_id,
        "status": "complete" if all(r["status"] == "complete" for r in lane_results.values()) else "partial",
        "lanes": lane_results,
    }), flush=True)

    if fatal_error is not None:
        raise RuntimeError(fatal_error)

    # Recoverable operational failures must exit non-zero so the deployment's
    # on_failure restart policy actually fires -- a partial *summary* is not
    # enough on its own, since nothing reads it synchronously. Exactly one
    # lease-loss kind is excluded from recovery: a *confirmed* takeover,
    # where another owner is verified live for this run_date and restarting
    # this process would only fight them in a competing-restart loop. The
    # other two kinds ARE recoverable, since restarting can only help and
    # there's no real owner to compete with: a *confirmed absence* (the
    # lease object is simply gone -- nobody is known to be collecting) and
    # an *uncertain* loss (repeated transient renewal failures past the
    # confirmed deadline, with no observed competing owner either).
    # An optional lane being unavailable is deliberately not recoverable:
    # restarting would interrupt the shared stream for the primary.
    recoverable = (
        capture_result.stop_reason == "spool_exhausted"
        or any(not lane.uploader.fully_drained() for lane in active)
        or lease_loss_kind in ("ownership_uncertain", "confirmed_absence")
        or (attempt_total_events == 0 and capture_result.stop_reason != "lease_lost" and not STOP)
    )
    if recoverable:
        raise RuntimeError(
            f"MOO-169 collection for {run_date} ended in a recoverable state requiring restart: "
            + "; ".join(f"{name}: {', '.join(r['partial_reasons']) or 'incomplete coverage'}"
                        for name, r in lane_results.items())
        )
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
