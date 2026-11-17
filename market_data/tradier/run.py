"""``main``: one weekday's collection, from settings to exit code.

Read top to bottom as the run's phases: read settings, wait for the pre-open
setup, claim the lease, prepare the primary and optional lanes, capture the
session, finalize each lane's evidence, then translate coverage into an exit
code. This module is deliberately the only place that composes the others.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ._probe import DEFAULT_UNDERLYING, Tradier, r2_client, utc_now
from .artifacts import json_artifact, write_health
from .archive import lease_key
from . import config
from .recent import RecentPrints
from .capture import CaptureResult, capture_session
from .config import (
    ET,
    HEALTH_PUBLISH_INTERVAL_SECONDS,
    PREOPEN_SETUP_SECONDS,
    SPOOL_VOLUME_MAX_FRACTION,
    nyse_session_bounds,
    parse_underlyings,
)
from .lanes import Lane, StreamRouter, admit_optional_lanes, prepare_optional_lane
from .lease import acquire_lease, run_lease_deadline_watchdog, run_lease_heartbeat
from .recovery import reconcile_existing_segments, recover_stale_sessions
from .spool import SegmentSpool, Uploader, count_ndjson_gz_records
from .universe import load_or_select_universe

@dataclass(frozen=True)
class RunSettings:
    """Everything the environment can change about one run, validated once."""

    token: str
    strike_count: int
    checkpoint_seconds: int
    max_reconnects: int
    lease_ttl_seconds: int
    max_spool_bytes: int
    optional_spool_bytes: int | None
    lane_specs: list[tuple[str, str]]
    spool_base: Path


def load_settings(env: dict[str, str] | None = None) -> RunSettings:
    """Read and validate ``MOO144_*``/``TRADIER_TOKEN``.

    Raises ``RuntimeError`` with the operator-facing message for a value that
    cannot be used, before any network or storage call is made.
    """
    env = os.environ if env is None else env
    token = env.get("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is required")
    strike_count = int(env.get("MOO144_STRIKE_COUNT", "8"))
    checkpoint_seconds = int(env.get("MOO144_CHECKPOINT_SECONDS", "180"))
    max_reconnects = int(env.get("MOO144_MAX_CONSECUTIVE_RECONNECTS", "5"))
    lease_ttl_seconds = int(env.get("MOO144_LEASE_TTL_SECONDS", "300"))
    # The primary lane keeps the full, existing cap; optional lanes need their
    # own explicit allocation rather than silently taking part of it.
    max_spool_bytes = int(env.get("MOO144_MAX_SPOOL_BYTES", str(512 * 1024 * 1024)))
    optional_spool_raw = env.get("MOO144_OPTIONAL_SPOOL_BYTES", "").strip()
    optional_spool_bytes = int(optional_spool_raw) if optional_spool_raw else None
    lane_specs = parse_underlyings(env.get("MOO144_UNDERLYINGS", DEFAULT_UNDERLYING))
    spool_base = Path(env.get("MOO144_SPOOL_DIR", tempfile.gettempdir()))
    if not 2 <= strike_count <= 20:
        raise RuntimeError("MOO144_STRIKE_COUNT must be between 2 and 20")
    if not 30 <= checkpoint_seconds <= 300:
        raise RuntimeError("MOO144_CHECKPOINT_SECONDS must be between 30 and 300")
    if not 1 <= max_reconnects <= 10:
        raise RuntimeError("MOO144_MAX_CONSECUTIVE_RECONNECTS must be between 1 and 10")
    return RunSettings(
        token=token,
        strike_count=strike_count,
        checkpoint_seconds=checkpoint_seconds,
        max_reconnects=max_reconnects,
        lease_ttl_seconds=lease_ttl_seconds,
        max_spool_bytes=max_spool_bytes,
        optional_spool_bytes=optional_spool_bytes,
        lane_specs=lane_specs,
        spool_base=spool_base,
    )


def main(
    *,
    clock_et: Callable[[], datetime] = lambda: datetime.now(ET),
    sleeper: Callable[[float], None] = time.sleep,
    session_bounds: Callable[[date], tuple[datetime, datetime] | None] = nyse_session_bounds,
    uploader_sleeper: Callable[[float], None] = time.sleep,
    drain_timeout_seconds: float = 60.0,
) -> int:
    settings = load_settings()

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
            if config.STOP:
                return 0
            sleeper(min(1.0, (setup_at - now_et).total_seconds()))
            now_et = clock_et()
    if config.STOP:
        return 0
    late_start_seconds = max(0.0, (clock_et() - session_open).total_seconds())
    if clock_et() >= session_close:
        print(json.dumps({"event": "session_already_closed", "date": run_date}), flush=True)
        return 0

    owner_id = uuid.uuid4().hex[:12]
    run_id = f"{run_date}-{owner_id}"
    started_at = utc_now()
    print(json.dumps({"event": "collector_start", "run_id": run_id,
                      "underlyings": [symbol for symbol, _ in settings.lane_specs]}), flush=True)

    client = Tradier(settings.token)
    r2, bucket = r2_client()
    # One lease for the one provider stream, taken before any stream exists.
    lease = acquire_lease(r2, bucket, run_date, owner_id, settings.lease_ttl_seconds)
    lanes = [Lane(symbol, policy, run_date, settings.spool_base) for symbol, policy in settings.lane_specs]
    primary, optional = lanes[0], lanes[1:]
    primary.spool_cap_bytes = settings.max_spool_bytes
    allocation_problem = None
    if optional:
        if settings.optional_spool_bytes is None or settings.optional_spool_bytes <= 0:
            allocation_problem = "optional_spool_allocation_unset"
        else:
            settings.spool_base.mkdir(parents=True, exist_ok=True)
            allocated = settings.max_spool_bytes + settings.optional_spool_bytes * len(optional)
            if allocated > shutil.disk_usage(settings.spool_base).total * SPOOL_VOLUME_MAX_FRACTION:
                allocation_problem = "spool_allocation_exceeds_volume"
    lease_expires_at = datetime.fromisoformat(lease["expires_at"])
    configured_stream = {
        "shared_stream": True,
        "underlyings": [lane.underlying for lane in lanes],
        "lease_key": lease_key(run_date),
    }

    def optional_preflight(lane: Lane, universe: dict[str, Any], reconciliation: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "issue": "MOO-169",
            "run_id": run_id,
            "started_at": started_at,
            "session_open": session_open.isoformat(),
            "session_close": session_close.isoformat(),
            "late_start_seconds": late_start_seconds,
            "underlying": lane.underlying,
            "expiration_policy": lane.expiration_policy,
            "universe": universe,
            # Written before the cutoff decides the active set; the summary
            # records the final stream composition.
            "stream": configured_stream,
            "admission": "pending_cutoff",
            "spool_cap_bytes": lane.spool_cap_bytes,
            "lease": lease,
            "reconciled_segments": len(reconciliation["artifacts"]),
            "resumed_segments": len(reconciliation["resume"]),
            "needs_review": reconciliation["needs_review"],
            "stale_sessions_recovered": "deferred_until_after_close",
        }

    pending_optional: list[Lane] = []
    for lane in optional:
        if allocation_problem is not None:
            lane.unavailable_reason, lane.frozen = allocation_problem, True
            print(json.dumps({"event": "optional_lane_excluded", "underlying": lane.underlying,
                              "reason": allocation_problem}), flush=True)
            continue
        lane.spool_cap_bytes = settings.optional_spool_bytes
        pending_optional.append(lane)
        threading.Thread(
            target=prepare_optional_lane,
            args=(lane, lambda: Tradier(settings.token), r2, bucket, settings.strike_count, run_date, clock_et, session_open,
                  optional_preflight, lambda: datetime.now(timezone.utc) < lease_expires_at,
                  f"{lane.prefix}/run-started-{owner_id}.json"),
            name=f"prepare-{lane.underlying}", daemon=True,
        ).start()

    # The primary follows the single-underlying startup path unchanged,
    # concurrently with any optional lane preparation.
    primary.symbols, primary.universe = load_or_select_universe(
        client, r2, bucket, primary.prefix, settings.strike_count, run_date, clock_et(), session_open=session_open,
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
    # Optional lanes wrote their own preflight during admission.
    for lane in [primary]:
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
            "stale_sessions_recovered": lane.stale_recovery,
        }
        lane.preflight = json_artifact(r2, bucket, f"{lane.prefix}/run-started-{owner_id}.json", start_payload)
        print(json.dumps({"event": "r2_preflight_pass", "underlying": lane.underlying,
                          "key": lane.preflight["key"]}), flush=True)

    for lane in active:
        lane.uploader = Uploader(r2, bucket, lane.prefix, lane.spool_cap_bytes, sleeper=uploader_sleeper)
        for path in lane.reconciliation["resume"]:
            lane.uploader.enqueue(path)
        lane.uploader.start()
        lane.spool = SegmentSpool(lane.spool_dir, owner_id, lane.uploader, settings.checkpoint_seconds)
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
        args=(r2, bucket, run_date, owner_id, settings.lease_ttl_seconds, lease_confirmed_until_ref, lease_stop, lease_lost_event),
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

    recent = RecentPrints()
    # Restore this session's order-flow accumulation from the archive before new
    # prints arrive; overlaps deduplicate by identity and gaps stay visible.
    recent.rebuild(r2, bucket, primary.prefix, local_segments=primary.reconciliation.get("resume", ()))
    threading.Thread(target=recent.publish, args=(r2, bucket, lease_stop, lease_lost_event),
                     daemon=True).start()
    capture_started_at = clock_et()
    try:
        if datetime.now(timezone.utc) >= lease_confirmed_until_ref["value"]:
            lease_loss_reason.setdefault("reason", "ownership_uncertain")
            lease_lost_event.set()
        capture_result = capture_session(
            client, router.symbols, stream_spool, stream_stats, session_close, settings.max_reconnects,
            lease_lost=lease_lost_event, sleeper=sleeper, now_et=clock_et,
            result=capture_result, recent=recent,
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
    return finalize_run(
        run_date=run_date,
        run_id=run_id,
        owner_id=owner_id,
        started_at=started_at,
        finished_at=finished_at,
        session_open=session_open,
        session_close=session_close,
        late_start_seconds=late_start_seconds,
        effective_late_start_seconds=effective_late_start_seconds,
        capture_result=capture_result,
        stream_info=stream_info,
        lease=lease,
        lanes=lanes,
        primary=primary,
        active=active,
        optional=optional,
        r2=r2,
        bucket=bucket,
        clock_et=clock_et,
        fatal_error=fatal_error,
        attempt_total_events=attempt_total_events,
        lease_loss_kind=lease_loss_kind,
        uploader_sleeper=uploader_sleeper,
        drain_timeout_seconds=drain_timeout_seconds,
    )



def finalize_run(
    *,
    run_date: str,
    run_id: str,
    owner_id: str,
    started_at: str,
    finished_at: str,
    session_open: datetime,
    session_close: datetime,
    late_start_seconds: float,
    effective_late_start_seconds: float,
    capture_result: CaptureResult,
    stream_info: dict[str, Any],
    lease: dict[str, Any],
    lanes: list[Lane],
    primary: Lane,
    active: list[Lane],
    optional: list[Lane],
    r2: Any,
    bucket: str,
    clock_et: Callable[[], datetime],
    fatal_error: str | None,
    attempt_total_events: int,
    lease_loss_kind: str | None,
    uploader_sleeper: Callable[[float], None],
    drain_timeout_seconds: float,
) -> int:
    """Turn the captured session into per-lane evidence and one exit code.

    This is everything after the stream stops: recover an optional lane's
    prior dates, derive the stream-level coverage reasons every lane shares,
    write each lane's summary and manifest, and decide whether the deployment
    should restart this process.
    """
    opening_ready = capture_result.opening_stream_ready_at
    def stale_recovery_failures(lane: Lane) -> dict[str, str]:
        report = lane.stale_recovery
        if isinstance(report.get("recovery_failed"), str):
            return {"*": report["recovery_failed"]}
        return {day: entry["recovery_failed"] for day, entry in report.items()
                if isinstance(entry, dict) and entry.get("recovery_failed")}

    # Optional lanes' prior-date recovery, deferred off the pre-open path;
    # only while this process still owns the stream lease. A failure is
    # recorded for that lane and never prevents the primary's finalization.
    for lane in optional:
        if capture_result.stop_reason == "lease_lost" or config.STOP:
            lane.stale_recovery = {"deferred": "stopped_by_signal" if config.STOP else "ownership_not_held"}
            continue
        try:
            lane.stale_recovery = recover_stale_sessions(
                lane.base_spool_dir, run_date, r2, bucket,
                uploader_sleeper=uploader_sleeper, drain_timeout_seconds=drain_timeout_seconds,
                root=lane.root, isolate_failures=True,
            )
        except Exception as exc:  # e.g. the spool directory itself unreadable
            lane.stale_recovery = {"recovery_failed": f"{type(exc).__name__}: {exc}"}
        failed = stale_recovery_failures(lane)
        if failed:
            print(json.dumps({"event": "stale_recovery_failed", "underlying": lane.underlying,
                              "failures": failed}), flush=True)

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
    if config.STOP:
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

    def finalize_lane(lane: Lane) -> None:
        if lane not in active:
            partial_reasons = [f"lane_unavailable: {lane.unavailable_reason}"]
            if stale_needs_review(lane):
                partial_reasons.append("stale_session_needs_review")
            partial_reasons += [f"stale_recovery_failed: {day}: {why}"
                                for day, why in stale_recovery_failures(lane).items()]
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
            return
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
        partial_reasons += [f"stale_recovery_failed: {day}: {why}"
                            for day, why in stale_recovery_failures(lane).items()]
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
            "stopped_by_signal": config.STOP,
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
            "peak_spool_bytes": uploader.peak_spool_bytes,
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

    # The primary is finalized first and its errors propagate as before; an
    # optional lane's finalization error is reported without costing the
    # others their evidence.
    for lane in lanes:
        if lane is primary:
            finalize_lane(lane)
            continue
        try:
            finalize_lane(lane)
        except Exception as exc:
            lane_results[lane.underlying] = {
                "status": "partial",
                "partial_reasons": [f"lane_finalization_failed: {type(exc).__name__}: {exc}"],
                "manifest": None,
                "event_counts": dict(lane.stats.counts),
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
        or (attempt_total_events == 0 and capture_result.stop_reason != "lease_lost" and not config.STOP)
    )
    if recoverable:
        raise RuntimeError(
            f"MOO-169 collection for {run_date} ended in a recoverable state requiring restart: "
            + "; ".join(f"{name}: {', '.join(r['partial_reasons']) or 'incomplete coverage'}"
                        for name, r in lane_results.items())
        )
    return 0
