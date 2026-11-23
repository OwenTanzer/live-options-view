"""OA-203 options return scanner: run, universe, smoke, build, snapshot, inspect, readout.

``run`` is the deployed command (``market_data/options_scanner/__main__.py run``) and
the only one that touches the live provider and R2 unattended. ``universe`` and
``smoke`` select or sample without publishing; ``build``, ``snapshot``,
``snapshot-supervise``, ``inspect`` and ``readout`` work from a local day
directory.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import runtime
from .active import ACTIVE_BUILD_SECONDS, build_active_snapshot, write_active_attempt
from .archive import SUMMARY, DayArchive
from .outputs import build_contract_rows, write_outputs
from .readout import inspect_contract, readout
from .runtime import ET, Config, on_stop
from .session import build_universe_for, finalize, run_day
from .sweep import run_sweep, select_universe
from .tradier import Tradier, load_token
from .windows_combine import combine_coverage_from_dict, combine_emit
from .archive_cache import prune_archive_cache
from .windows_live import build_windows, prune_windows_emit, publish_windows
from .windows_publish import LocalFilesystemBackend, r2_backend


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="daily session (cron)")
    uni = sub.add_parser("universe", help="select today's universe and print it (no sampling)")
    uni.add_argument("--date", type=date.fromisoformat, default=None)
    uni.add_argument("--size", type=int, default=None)
    smoke = sub.add_parser("smoke", help="sample named underlyings now, locally, without R2")
    smoke.add_argument("--symbols", required=True)
    smoke.add_argument("--sweeps", type=int, default=2)
    smoke.add_argument("--dir", type=Path, required=True)
    smoke.add_argument("--no-backfill", action="store_true")
    build = sub.add_parser("build", help="rebuild leaderboard from a local day directory")
    build.add_argument("--dir", type=Path, required=True)
    for command in ("snapshot", "snapshot-supervise"):
        snap = sub.add_parser(command, help="build or supervise one cutoff snapshot")
        snap.add_argument("--dir", type=Path, required=True)
        snap.add_argument("--sweep", type=int, required=True)
        snap.add_argument("--open-ms", type=int, required=True)
        snap.add_argument("--close-ms", type=int, required=True)
    insp = sub.add_parser("inspect", help="show one contract's path and return calculation")
    insp.add_argument("--dir", type=Path, required=True)
    insp.add_argument("--symbol", required=True)
    rd = sub.add_parser("readout", help="concentration readout across day directories")
    rd.add_argument("--dir", type=Path, action="append", required=True)
    rd.add_argument("--top", type=int, default=100)
    rd.add_argument("--all", action="store_true", help="use all ranks, not only clean rows")
    wb = sub.add_parser("windows-build", help="build #130 windows from local finalized day archives (no provider, no R2)")
    wb.add_argument("--dir", type=Path, action="append", required=True, help="one finalized day directory per session")
    wb.add_argument("--underlyings", nargs="+", required=True, help="underlyings (at most 50; with --full-universe the cap is 500 per day and the union over the window may exceed 500)")
    wb.add_argument("--emit-root", type=Path, required=True)
    wb.add_argument("--top", type=int, default=15)
    wb.add_argument("--full-universe", action="store_true", help="load the complete selected universe (at most 500 underlyings per day; the union over the window may exceed 500 as the per-day set rotates); the set must cover every selected underlying of every day, or the build fails closed")
    wc = sub.add_parser("windows-combine", help="combine per-batch window emits into one full-universe emit (local only, no provider, no R2)")
    wc.add_argument("--emit-root", type=Path, action="append", required=True, help="one batch emit root per batch (distinct roots, same window set)")
    wc.add_argument("--out", type=Path, required=True, help="combined emit root")
    wc.add_argument("--coverage", type=Path, required=True, help="JSON coverage contract: expected_underlyings, expected_batches, expected_sources [[key, sha256], ...], scope full_universe|bounded_subset (required: no combine without an explicit contract)")
    wp = sub.add_parser("windows-publish", help="publish a local windows emit root to a local backend (no R2)")
    wp.add_argument("--emit-root", type=Path, required=True)
    wp.add_argument("--out", type=Path, required=True, help="local backend root directory")
    wpe = sub.add_parser("windows-prune-emit", help="prune old per-cycle window emit roots (derived output only; dry-run by default; local backend only)")
    wpe.add_argument("--emit-root", type=Path, required=True, help="emit root holding per-cycle <YYYY-MM-DD>/ dirs")
    wpe.add_argument("--backend-root", type=Path, required=True, help="local backend root directory (published pointers are verified here)")
    wpe.add_argument("--keep-cycles", type=int, default=0, help="newest cycles to keep as a recency preference even when over budget (default 0: the byte budget alone decides; a hard floor cannot fit the headroom)")
    wpe.add_argument("--max-emit-mb", type=int, default=192, help="emit budget in MiB (default 192: sized so pruning to it leaves room for the next 32-window build)")
    wpe.add_argument("--confirm", action="store_true", help="actually delete (without this, dry-run: list only)")
    acp = sub.add_parser("archive-cache-prune", help="prune older redundant local day archives after fresh exact remote digest verification (dry-run by default; local backend only)")
    acp.add_argument("--spool", type=Path, required=True, help="spool holding per-day <YYYY-MM-DD>/ archives")
    acp.add_argument("--backend-root", type=Path, required=False, help="local backend root directory (remote objects are verified here; required unless --r2 is given)")
    acp.add_argument("--keep-sessions", type=int, default=10, help="newest finalized sessions to retain (default 10, the builder lookback; below 10 is refused)")
    acp.add_argument("--max-days", type=int, default=2, help="max days pruned per run (default 2)")
    acp.add_argument("--max-bytes-mb", type=int, default=512, help="max bytes pruned per run in MiB (default 512)")
    acp.add_argument("--confirm", action="store_true", help="actually delete (without this, dry-run: list only; deletion also requires OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1)")
    acp.add_argument("--r2", action="store_true", help="verify against the real R2 backend (production); without this, --backend-root is verified")
    acp.add_argument("--max-remote-requests", type=int, default=1024, help="hard cap on remote requests for the run (default 1024)")
    acp.add_argument("--max-remote-bytes-mb", type=int, default=1024, help="hard cap on remote bytes read for the run in MiB (default 1024)")
    acp.add_argument("--max-verify-seconds", type=float, default=600.0, help="hard wall-clock budget for the run in seconds (default 600)")
    args = parser.parse_args(argv)
    cfg = Config.from_env()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    if args.command == "run":
        return run_day(cfg)
    if args.command == "universe":
        day = args.date or datetime.now(ET).date()
        client = Tradier(load_token(), cfg.max_rpm, cfg.reserve, timeout=(cfg.connect_timeout_s, cfg.read_timeout_s))
        with tempfile.TemporaryDirectory() as tmp:
            archive = DayArchive(Path(tmp), day, cfg.r2_prefix, upload=False)
            cfg.universe_size = args.size or cfg.universe_size
            universe = build_universe_for(day, client, cfg, archive)
        print(json.dumps({k: v for k, v in universe.items() if k != "selected"}, indent=2, default=str))
        for row in universe["selected"][:25]:
            print(row)
        return 0
    if args.command == "smoke":
        day = datetime.now(ET).date()
        cfg.upload = False
        client = Tradier(load_token(), cfg.max_rpm, cfg.reserve, timeout=(cfg.connect_timeout_s, cfg.read_timeout_s))
        archive = DayArchive(args.dir, day, cfg.r2_prefix, upload=False)
        ranked = [(s.strip().upper(), {"contracts": 0, "calls": 0, "puts": 0})
                  for s in args.symbols.split(",") if s.strip()]
        universe = select_universe(client, ranked, day, len(ranked), cfg.workers)
        universe["trade_date"] = day.isoformat()
        archive.write_json("universe.json", universe)
        for sweep in range(1, args.sweeps + 1):
            name = f"sweeps/sweep_{sweep:04d}.jsonl.gz"
            entry = run_sweep(client, universe["selected"], sweep, archive.path(name),
                              cfg.block_size, cfg.workers)
            archive.append_manifest(entry)
            runtime.log("oa203_sweep", sweep=sweep, rows=entry["rows"], status=entry["status_counts"])
        summary = finalize(archive, client, cfg, universe, None, None,
                           backfill_enabled=not args.no_backfill)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if args.command == "build":
        rows = build_contract_rows(args.dir, cfg.policy, *_bounds_from_summary(args.dir),
                                   qualified_policy=cfg.qualified_policy)
        print(json.dumps(write_outputs(args.dir, rows)))
        return 0
    if args.command in ("snapshot", "snapshot-supervise"):
        if args.command == "snapshot-supervise":
            archive = DayArchive(args.dir.parent, date.fromisoformat(args.dir.name), cfg.r2_prefix, cfg.upload)
            # Module entry works from the image repository root.
            command = [sys.executable, "-m", "market_data.options_scanner", "snapshot", "--dir", str(args.dir),
                       "--sweep", str(args.sweep), "--open-ms", str(args.open_ms),
                       "--close-ms", str(args.close_ms)]
            try:
                write_active_attempt(archive, args.sweep, "building")
                from market_data.options_scanner.active_budget import constrain_builder
                subprocess.run(command, timeout=ACTIVE_BUILD_SECONDS, check=True,
                               preexec_fn=constrain_builder if sys.platform == "linux" else None)
                write_active_attempt(archive, args.sweep, "complete")
            except Exception as exc:
                reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "build_failed"
                try:
                    write_active_attempt(archive, args.sweep, "failed", reason)
                except Exception as report_exc:
                    runtime.log("oa203_active_attempt_report_failed", error=str(report_exc)[:200])
                runtime.log("oa203_active_snapshot_skipped", sweep=args.sweep,
                    reason=reason)
                return 1
            return 0
        archive = DayArchive(args.dir.parent, date.fromisoformat(args.dir.name), cfg.r2_prefix, cfg.upload)
        pointer = build_active_snapshot(archive, cfg, args.sweep, args.open_ms, args.close_ms)
        runtime.log("oa203_active_snapshot_published", sweep=args.sweep,
            archive=pointer["archive"], cutoff_ms=pointer["cutoff_ms"])
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect_contract(args.dir, args.symbol, cfg.policy, cfg.qualified_policy),
                         indent=2, default=str))
        return 0
    if args.command == "readout":
        print(readout(args.dir, args.top, clean_only=not args.all))
        return 0
    if args.command == "windows-build":
        # Local build only. No provider, no R2, no schedule.
        report = build_windows(args.dir, {u.strip() for u in args.underlyings},
                               args.emit_root, top=args.top, full_universe=args.full_universe)
        print(json.dumps(report, indent=2, default=str))
        return 0
    if args.command == "windows-combine":
        # Local combine only. No provider, no R2, no schedule.
        coverage = combine_coverage_from_dict(
            json.loads(args.coverage.read_text(encoding="utf-8")))
        report = combine_emit(args.emit_root, args.out, coverage=coverage)
        print(json.dumps(report, indent=2, default=str))
        return 0
    if args.command == "windows-publish":
        # Local backend only. R2 publication is a release step (Chej) via
        # windows_publish.r2_backend(); this command never constructs it.
        results = publish_windows(args.emit_root, LocalFilesystemBackend(args.out))
        print(json.dumps(results, indent=2, default=str))
        return 0
    if args.command == "windows-prune-emit":
        # Local backend only, dry-run by default. Deletes ONLY verified
        # per-cycle emit dirs under --emit-root; never day archives,
        # never source history, never live/published data. For R2
        # verification Chej calls prune_windows_emit with r2_backend().
        report = prune_windows_emit(args.emit_root, LocalFilesystemBackend(args.backend_root),
                                    keep_cycles=args.keep_cycles,
                                    max_emit_bytes=args.max_emit_mb * 1024 * 1024,
                                    dry_run=not args.confirm)
        print(json.dumps(report, indent=2, default=str))
        return 0
    if args.command == "archive-cache-prune":
        # Dry-run by default. --r2 verifies against the real R2 backend
        # (production); otherwise the local backend root is verified.
        # Deletes ONLY older finalized day dirs under --spool whose every
        # artifact verifies against the remote backend by fresh GET +
        # SHA-256 at prune time; never R2 objects, never the newest 10
        # finalized sessions, never unfinalized/pending/recovery/linked
        # days. Deletion additionally requires
        # OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1. The same verification code
        # and hard remote budgets serve both backends.
        if args.r2:
            backend = r2_backend()
        else:
            if args.backend_root is None:
                parser.error("--backend-root is required unless --r2 is given")
            backend = LocalFilesystemBackend(args.backend_root)
        report = prune_archive_cache(args.spool, backend,
                                     keep_sessions=args.keep_sessions,
                                     max_days_per_run=args.max_days,
                                     max_bytes_per_run=args.max_bytes_mb * 1024 * 1024,
                                     max_remote_requests=args.max_remote_requests,
                                     max_remote_bytes=args.max_remote_bytes_mb * 1024 * 1024,
                                     max_verify_seconds=args.max_verify_seconds,
                                     dry_run=not args.confirm)
        print(json.dumps(report, indent=2, default=str))
        return 0
    return 2


def _bounds_from_summary(day_dir: Path) -> tuple[int | None, int | None]:
    path = day_dir / "summary.json"
    if not path.exists():
        return None, None
    summary = json.loads(path.read_text(encoding="utf-8"))
    return summary.get("session_open_ms"), summary.get("session_close_ms")
