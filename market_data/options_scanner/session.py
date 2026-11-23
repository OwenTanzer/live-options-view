"""One trading day: build the universe, sweep, finalize, recover prior days.

This is the session loop the Railway service runs: it decides when to sweep,
writes each sweep and its manifest line, publishes the active snapshot, and
finalizes the day's artifacts after the close.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import runtime
from .active import ACTIVE_BUILD_SECONDS, ACTIVE_MAX_SWEEP, _active_cutoff_ms, active_snapshot_due
from .archive import SUMMARY, UPLOAD_JOURNAL, DayArchive, _is_date, published
from .assessment import MEASUREMENT_NOTE, assess_session, expected_sweep_s
from .backfill import run_backfill
from .occ import fetch_occ_volume, parse_occ_volume, rank_underlyings
from .outputs import build_contract_rows, write_outputs
from .returns import QUALIFIED
from .runtime import ET, Config, nyse_session_bounds, previous_session
from .sweep import run_sweep, select_universe
from .tradier import Tradier, load_token


# --------------------------------------------------------------------------
# Daily run
# --------------------------------------------------------------------------

MEASUREMENT_NOTE = (
    "Sampled-return leaderboard: quote returns come from ~5-minute chain samples. "
    "The trade-bar backfill only enriches contracts that already rank in those "
    "samples; a brief spike in a contract that never ranks is not discovered."
)

WINDOWS_LIVE_ENV = "OA203_WINDOWS_LIVE_ENABLED"


def windows_live_enabled() -> bool:
    """Live window hook enablement: env flag only, default off."""
    return os.getenv(WINDOWS_LIVE_ENV) == "1"


def maybe_run_windows_after_finalize(archive: DayArchive, universe: dict[str, Any], cfg: Config,
                                     *, hook: Callable | None = None) -> dict[str, Any]:
    """Disabled-by-default production hook: build + publish #130 windows.

    Called at the end of ``finalize``. When ``OA203_WINDOWS_LIVE_ENABLED``
    is not ``"1"`` this is a no-op that touches nothing (no build, no
    publish, no extra collection, no R2 call). When enabled it runs
    ``windows_live.run_after_finalize`` (or the injected ``hook``, which
    takes ``(spool_dir, day, universe)``) over the finalized spool and
    records the outcome in a best-effort ``windows_live.json`` artifact.
    A hook failure is logged and never unfinalizes the day: finalize has
    already written the final summary by the time this runs. Enabling
    the flag is live activation (Chej alone, after review); the code
    only implements the hook.
    """
    if not windows_live_enabled():
        return {"enabled": False}
    try:
        from .windows_live import run_after_finalize
        run = hook or run_after_finalize
        result = run(archive.dir.parent, archive.day, universe)
    except Exception as exc:
        runtime.log("oa203_windows_after_finalize_failed", date=archive.day.isoformat(),
                    error=f"{type(exc).__name__}: {exc}"[:300])
        return {"enabled": True, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        archive.write_json("windows_live.json", result)
        archive.push("windows_live.json")
    except Exception as exc:  # the publication stands; the record is best-effort
        runtime.log("oa203_windows_after_finalize_record_failed", date=archive.day.isoformat(),
                    error=f"{type(exc).__name__}: {exc}"[:200])
    return result


def _sleep_until(when: datetime) -> None:
    while not runtime.STOP:
        remaining = (when - datetime.now(ET)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def build_universe_for(day: date, client: Tradier, cfg: Config, archive: DayArchive) -> dict[str, Any]:
    report_date = previous_session(day)
    body = fetch_occ_volume(report_date)
    with published(archive.path(f"occ_volume_{report_date.isoformat()}.csv.gz")) as tmp:
        tmp.write_bytes(gzip.compress(body))
    ranked = rank_underlyings(parse_occ_volume(body, report_date))
    universe = select_universe(client, ranked, day, cfg.universe_size, cfg.workers,
                               cfg.spy_qqq_second_expiry)
    universe.update({
        "trade_date": day.isoformat(),
        "occ_report_date": report_date.isoformat(),
        "occ_sha256": hashlib.sha256(body).hexdigest(),
        "occ_underlyings_ranked": len(ranked),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "config": cfg.as_dict(),
    })
    return universe


def finalize(archive: DayArchive, client: Tradier, cfg: Config, universe: dict[str, Any],
             open_ms: int | None, close_ms: int | None, backfill_enabled: bool = True,
             windows_hook: Callable | None = None) -> dict[str, Any]:
    """Build outputs, backfill, reconcile uploads and write the final summary.

    Safe to re-run: a crash or stop anywhere before ``summary.json`` is
    written leaves the day unfinalized, and the next ``run`` finalizes it.
    After the summary is written, the disabled-by-default windows hook
    runs (see ``maybe_run_windows_after_finalize``); it never unfinalizes
    the day. ``windows_hook`` is an injectable hook for tests.
    """
    damaged: list[dict[str, Any]] = []
    rows = build_contract_rows(archive.dir, cfg.policy, open_ms, close_ms, damaged,
                               cfg.qualified_policy)
    backfill = None
    if backfill_enabled and rows:
        # Rewrites backfill_timesales.jsonl.gz atomically, replacing any damaged copy.
        backfill = run_backfill(client, archive.day, archive.dir, rows, cfg.backfill_top, cfg.workers)
        damaged = []
        rows = build_contract_rows(archive.dir, cfg.policy, open_ms, close_ms, damaged,
                                   cfg.qualified_policy)
    outputs = write_outputs(archive.dir, rows)
    # Everything except the summary itself must be verified in R2 before the
    # summary can call the session complete.
    pending = [name for name in archive.reconcile() if name != SUMMARY]
    manifest = archive.manifest()
    bounds_open = open_ms or min((m["started_ms"] for m in manifest), default=0)
    bounds_close = close_ms or max((m["ended_ms"] for m in manifest), default=0)
    selected_count = len(universe.get("selected") or [])
    summary = {
        "final": True,
        "trade_date": archive.day.isoformat(),
        "session_open_ms": open_ms, "session_close_ms": close_ms,
        "assessment": assess_session(universe, manifest, bounds_open, bounds_close, pending, backfill,
                                     dict(client.stats), (expected_sweep_s(selected_count, cfg) +
                                         sum(bool(u.get("additional_expiration"))
                                             for u in universe.get("selected") or []) /
                                         max(1, cfg.max_rpm) * 60) * 1000,
                                     archive.interruptions(), damaged),
        "measurement": MEASUREMENT_NOTE,
        "universe": {k: universe.get(k) for k in ("target", "selected_count", "shortfall",
                                                  "skip_counts", "occ_report_date")},
        "additional_expiries": {u["underlying"]: u["additional_expiration"]
                                for u in universe.get("selected") or [] if u.get("additional_expiration")},
        "contracts": len(rows),
        "ranked_contracts": sum(1 for r in rows if r.get("rank")),
        "clean_ranked_contracts": sum(1 for r in rows if r.get("clean_rank")),
        "qualified_ranked_contracts": sum(1 for r in rows if r.get(f"{QUALIFIED}_rank")),
        "qualified_status_counts": dict(Counter(r.get(f"{QUALIFIED}_status") for r in rows)),
        "qualified_reason_counts": dict(Counter(reason for r in rows
                                                  for reason in r.get(f"{QUALIFIED}_reasons", []))),
        "backfill": backfill,
        "interruptions": archive.interruptions(),
        "damaged_artifacts": damaged,
        "unfinalized_earlier_days": unfinalized_days(archive.dir.parent, archive.day),
        "request_stats": dict(client.stats),
        "outputs": outputs,
        "return_policy": cfg.policy.as_dict(),
        "qualified_policy": cfg.qualified_policy.as_dict(),
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    }
    archive.write_json(SUMMARY, summary)
    archive.push(SUMMARY)  # if this fails, the next run's reconcile retries it
    if archive.upload and archive.path(UPLOAD_JOURNAL).exists():
        archive.push(UPLOAD_JOURNAL)  # includes summary.json; retry via normal recovery
    remaining = archive.pending_uploads()
    if remaining:
        runtime.log("oa203_uploads_still_pending", date=archive.day.isoformat(), pending=remaining)
    runtime.log("oa203_session_finalized", date=archive.day.isoformat(),
        **{**summary["assessment"], "pending_uploads": remaining},
        contracts=summary["contracts"])
    summary["windows_live"] = maybe_run_windows_after_finalize(archive, universe, cfg,
                                                                hook=windows_hook)
    return summary


def _session_ms(bounds: tuple[datetime, datetime] | None) -> tuple[int | None, int | None]:
    if bounds is None:
        return None, None
    return int(bounds[0].timestamp() * 1000), int(bounds[1].timestamp() * 1000)


def unfinalized_days(spool: Path, before: date) -> list[str]:
    """Earlier spooled sessions with a universe but no final summary."""
    if not spool.exists():
        return []
    out = []
    for day_dir in sorted(p for p in spool.iterdir() if p.is_dir() and _is_date(p.name)):
        if date.fromisoformat(day_dir.name) >= before or not (day_dir / "universe.json").exists():
            continue
        summary = day_dir / SUMMARY
        try:
            final = summary.exists() and json.loads(summary.read_text(encoding="utf-8")).get("final")
        except ValueError:
            final = False
        if not final:
            out.append(day_dir.name)
    return out


def recover_days(cfg: Config, client: Tradier, today: date, now: datetime,
                 bounds_for: Callable[[date], tuple[datetime, datetime] | None],
                 uploader: Callable[[Path, str, str], dict[str, Any]] | None = None) -> dict[str, list[str]]:
    """Finalize unfinished sessions whose close has passed; re-upload pending artifacts.

    Covers a stop or crash during collection or during end-of-day
    finalization: the next scheduled ``run`` (or a same-day restart after
    the close) picks the day up from the local spool. Each day is isolated:
    a failure is logged, appended to that day's ``recovery_errors.jsonl``,
    and leaves the day unfinalized (so the next run retries it and later
    summaries list it under ``unfinalized_earlier_days``) without stopping
    other days or the current session.
    """
    result: dict[str, list[str]] = {"recovered": [], "failed": []}
    if not cfg.spool_dir.exists():
        return result
    for day_dir in sorted(p for p in cfg.spool_dir.iterdir() if p.is_dir() and _is_date(p.name)):
        day = date.fromisoformat(day_dir.name)
        if day > today or not (day_dir / "universe.json").exists():
            continue
        archive = None
        try:
            bounds = bounds_for(day)
            if bounds is not None and now < bounds[1]:
                continue  # that session is still open; run_day collects it
            archive = DayArchive(cfg.spool_dir, day, cfg.r2_prefix, cfg.upload, uploader)
            if archive.finalized():
                remaining = archive.reconcile()
                if remaining:
                    runtime.log("oa203_uploads_still_pending", date=day.isoformat(), pending=remaining)
                continue
            runtime.log("oa203_recovering_session", date=day.isoformat())
            universe = json.loads((day_dir / "universe.json").read_text(encoding="utf-8"))
            finalize(archive, client, cfg, universe, *_session_ms(bounds))
            result["recovered"].append(day.isoformat())
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            runtime.log("oa203_recovery_failed", date=day.isoformat(), error=error)
            result["failed"].append(day.isoformat())
            try:
                (archive or DayArchive(cfg.spool_dir, day, cfg.r2_prefix, cfg.upload, uploader)
                 ).record_recovery_failure(error)
            except Exception as record_exc:  # never let bookkeeping block today
                runtime.log("oa203_recovery_failure_unrecorded", date=day.isoformat(), error=str(record_exc)[:300])
    return result


def run_day(cfg: Config, *, client: Tradier | None = None,
            now: Callable[[], datetime] = lambda: datetime.now(ET),
            bounds_for: Callable[[date], tuple[datetime, datetime] | None] = nyse_session_bounds,
            sleep_until: Callable[[datetime], None] = _sleep_until,
            sweep_fn: Callable[..., dict[str, Any]] = run_sweep,
            uploader: Callable[[Path, str, str], dict[str, Any]] | None = None) -> int:
    client = client or Tradier(load_token(), cfg.max_rpm, cfg.reserve, timeout=(cfg.connect_timeout_s, cfg.read_timeout_s))
    day = now().date()
    try:
        recovery = recover_days(cfg, client, day, now(), bounds_for, uploader)
        if recovery["failed"]:
            runtime.log("oa203_recovery_pending", failed=recovery["failed"])
    except Exception as exc:  # e.g. an unreadable spool directory
        runtime.log("oa203_recovery_skipped", error=f"{type(exc).__name__}: {exc}"[:300])
    bounds = bounds_for(day)
    if bounds is None:
        runtime.log("oa203_no_session", date=day.isoformat())
        return 0
    open_at, close_at = bounds
    if now() >= close_at:
        runtime.log("oa203_session_already_closed", date=day.isoformat())
        return 0
    archive = DayArchive(cfg.spool_dir, day, cfg.r2_prefix, cfg.upload, uploader)
    archive.reconcile()  # artifacts an earlier process today could not upload

    universe_path = archive.path("universe.json")
    if universe_path.exists():  # same-day restart keeps the persisted selection
        universe = json.loads(universe_path.read_text(encoding="utf-8"))
        runtime.log("oa203_universe_reloaded", selected=universe["selected_count"])
    else:
        sleep_until(open_at - timedelta(minutes=cfg.universe_lead_min))
        universe = build_universe_for(day, client, cfg, archive)
        archive.write_json("universe.json", universe)
        archive.push("universe.json")
        archive.push(f"occ_volume_{universe['occ_report_date']}.csv.gz")
        runtime.log("oa203_universe_selected", selected=universe["selected_count"],
            shortfall=universe["shortfall"], skips=universe["skip_counts"])

    sleep_until(open_at)
    sweep = max((m["sweep"] for m in archive.manifest()), default=0)
    previous_start = None
    active_proc: subprocess.Popen | None = None
    active_started = 0.0
    open_ms = _session_ms(bounds)[0]
    last_snapshot_ms = 0  # launched this process; the published pointer covers restarts
    while not runtime.STOP and now() < close_at:
        sweep += 1
        name = f"sweeps/sweep_{sweep:04d}.jsonl.gz"
        before_stats = dict(client.stats)
        if isinstance(client, Tradier):
            client.deadline = close_at.timestamp()
        try:
            entry = sweep_fn(client, universe["selected"], sweep, archive.path(name),
                             cfg.block_size, cfg.workers, deadline=close_at,
                             budget_s=cfg.sweep_budget_s or None)
        finally:
            if isinstance(client, Tradier):
                client.deadline = client.budget_deadline = None
        entry["start_interval_seconds"] = ((entry["started_ms"] - previous_start) / 1000
                                            if previous_start is not None else None)
        previous_start = entry["started_ms"]
        entry["request_stats_delta"] = {k: v - before_stats.get(k, 0) for k, v in client.stats.items()
                                         if k != "max_request_seconds"}
        upload_started = time.monotonic()
        archive.append_manifest(entry)
        archive.push(name)
        archive.push("sweeps/manifest.jsonl")
        runtime.log("oa203_sweep", sweep=sweep, rows=entry["rows"], status=entry["status_counts"],
            seconds=round((entry["ended_ms"] - entry["started_ms"]) / 1000, 1),
            truncated=entry["truncated"], budget_exhausted=entry.get("budget_exhausted", False),
            requests=client.stats["requests"],
            start_interval_seconds=entry["start_interval_seconds"],
            upload_seconds=round(time.monotonic() - upload_started, 3),
            request_stats_delta=entry["request_stats_delta"],
            max_request_seconds=client.stats.get("max_request_seconds", 0),
            effective_rpm=getattr(client, "effective_rpm", cfg.max_rpm))
        if active_proc is not None and active_proc.poll() is not None:
            runtime.log("oa203_active_snapshot_exit", code=active_proc.returncode)
            active_proc = None
        if active_proc is not None and time.monotonic() - active_started > ACTIVE_BUILD_SECONDS + 10:
            # The supervisor normally enforces 240 s itself. This is only a
            # last-resort guard if it stopped responding.
            active_proc.kill()
            runtime.log("oa203_active_snapshot_supervisor_killed", sweep=sweep)
            active_proc = None
        if (os.getenv("OA203_ACTIVE_SNAPSHOTS_ENABLED") == "1" and
                sweep <= ACTIVE_MAX_SWEEP and active_proc is None and
                active_snapshot_due(entry["ended_ms"], max(open_ms, last_snapshot_ms, _active_cutoff_ms(archive)),
                                    expected_sweep_s(len(universe["selected"]), cfg))):
            last_snapshot_ms = entry["ended_ms"]
            command = [sys.executable, "-m", "market_data.options_scanner", "snapshot-supervise",
                       "--dir", str(archive.dir), "--sweep", str(sweep),
                       "--open-ms", str(_session_ms(bounds)[0]),
                       "--close-ms", str(_session_ms(bounds)[1])]
            active_proc = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            active_started = time.monotonic()
            runtime.log("oa203_active_snapshot_started", sweep=sweep, pid=active_proc.pid)
    if runtime.STOP:
        # Bounded: local writes only. The next run reconciles uploads and,
        # once the close has passed, finalizes the day with this recorded.
        archive.record_interruption(reason="signal", last_sweep=sweep,
                                    before_close=now() < close_at)
        runtime.log("oa203_stopped", sweep=sweep, date=day.isoformat())
        return 0
    finalize(archive, client, cfg, universe, *_session_ms(bounds))
    return 0
