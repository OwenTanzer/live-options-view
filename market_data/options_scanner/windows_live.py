"""Build and publish #130 windows from local finalized day archives.

This is the producer-to-publisher link for live window products:

* It reads ONLY local day directories (the scanner host's spool, the same
  files ``finalize`` wrote: summary.json, universe.json,
  sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz). It never touches a
  provider and never reads R2.
* ``build_windows`` writes a local emit root (``oa203/windows/`` tree).
  ``publish_windows`` publishes an emit root — or, with ``window_ids``,
  exactly the windows one cycle built — to an injected backend.
  The CLI's ``windows-publish`` only offers the local-filesystem backend;
  R2 publication is a release step (Chej) via ``windows_publish.r2_backend``.
* The production hook is ``run_after_finalize`` (called from
  ``session.finalize``): it runs only when the env flag
  ``OA203_WINDOWS_LIVE_ENABLED == "1"`` (disabled by default). Enabling
  the flag is live activation (Chej alone, after review); the code only
  implements the hook.
* ``build_windows`` defaults to a bounded 50-underlying subset
  (``universe_scope: bounded_subset`` — always served partial, never
  final). ``full_universe=True`` loads the complete selected universe
  (at most 500 underlyings PER DAY — the cap is daily, not on the
  union: the per-day selected set may change day to day, so the union
  over the window may exceed 500; the union is bounded by 10 sessions
  x 500) and combines it before ranking (see ``windows_combine``); the
  passed underlyings must cover every selected underlying of every
  loaded day, or the build fails closed.

The loader reuses ``windows_replay.load_session`` exactly (same manifest,
calendar, row and contract-identity validation) by pointing its ``fetch``
at the local day directory for one call. That is the point: one
implementation of the validation, no fork. A final day assessed
``partial`` is admitted with explicit ``session_partial:<day>`` reasons
(see ``windows_replay.validate_manifest``); a torn or invalid manifest
is still refused fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import market_data.options_scanner.windows as w
import market_data.options_scanner.windows_combine as combine
import market_data.options_scanner.windows_outputs as outputs
import market_data.options_scanner.windows_publish as pub
import market_data.options_scanner.windows_replay as replay
from market_data.options_scanner.archive_cache import (
    archive_cache_audit_enabled,
    archive_cache_destructive_enabled,
    run_archive_cache_audit,
)
from market_data.options_scanner.returns import ContractInfo, Observation
from market_data.options_scanner.windows_spool import BuildLimits, WindowSpool

MAX_UNDERLYINGS = 50
MAX_UNDERLYINGS_FULL = 500  # live universe size (Config.universe_size default)
MAX_SESSIONS = 10
WINDOWS_LIVE_ENV = "OA203_WINDOWS_LIVE_ENABLED"
WINDOWS_SCRATCH_ENV = "OA203_WINDOWS_SCRATCH_DIR"

# Local-output retention / disk headroom (see docs/130-live-window-wiring.md).
# Corrected peak accounting: a 10-session cycle builds at most 32 windows
# (6 weekly + 9 overnight + 9 two-session + 8 three-session), each at most
# 9 MiB of emit members (summary <=1 MiB + rankings <=4 MiB + paths <=4 MiB
# compressed). Peak per cycle is therefore 32 x 9 MiB = 288 MiB. The hook
# requires twice that free before starting a build (1x the build peak plus
# 1x for a local-backend copy of the same cycle) and fails closed below it.
# The manual prune budget (192 MiB) is sized so pruning to it always leaves
# room for the next build. Since the cycle emit moved to ephemeral scratch
# (see run_after_finalize), the 576 MiB pre-check now applies to the
# scratch filesystem, not the archive volume: the hook writes nothing to
# /data except the small windows_live.json record in the day archive.
# The archive volume's own headroom is a separate owner concern -- the
# scanner's day archives grow ~110 MB/day on /data (Chej's figures, 5000 MB
# total / 4190 MB used, not independently verified here) and no code here
# deletes them; see archive_cache.py for the bounded local cache lifecycle.
# There is NO hard cycle-count floor: a floor
# of 10 cycle dirs cannot fit the headroom at full-universe scale, so the
# byte budget alone decides (keep_cycles=0 by default; an operator may ask
# for a recency preference explicitly, and the report then shows the
# over-budget state instead of silently exceeding it).
MAX_WINDOW_EMIT_BYTES = (1 + 4 + 4) * 1024 * 1024
MAX_WINDOWS_PER_CYCLE = 32
MAX_CYCLE_EMIT_BYTES = MAX_WINDOWS_PER_CYCLE * MAX_WINDOW_EMIT_BYTES
MIN_EMIT_FREE_BYTES = 2 * MAX_CYCLE_EMIT_BYTES
DEFAULT_KEEP_CYCLES = 0
DEFAULT_MAX_EMIT_BYTES = 192 * 1024 * 1024


def load_local_session(day_dir: Path, underlyings: set[str],
                       rows_by_symbol: dict, sampled: dict, *,
                       max_source_bytes: int | None = None,
                       max_row_bytes: int | None = None) -> dict:
    """Load one finalized local day archive through the replay loader.

    ``day_dir`` must be named for its ISO date (e.g. ``2026-10-05``) and
    hold summary.json, universe.json and sweeps/ exactly as ``finalize``
    wrote them. Returns the replay session metadata plus ``sources`` (the
    local files read, each with its SHA-256). Raises on any missing,
    torn, non-final or invalid day; a final day assessed partial is
    admitted (see ``windows_replay.validate_manifest``): nothing is
    invented.
    """
    day = day_dir.name
    date.fromisoformat(day)  # ValueError on a non-date directory name
    prefix = f"oa203/scanner/{day}/"
    sources: list[dict] = []

    def fetch(key: str) -> bytes:
        if not key.startswith(prefix):
            raise RuntimeError(f"{day}: unexpected archive key: {key}")
        path = day_dir / key[len(prefix):]
        if not path.is_file():
            raise RuntimeError(f"{day}: missing local archive file: {key[len(prefix):]}")
        if max_source_bytes is None:
            body = path.read_bytes()
        else:
            with path.open("rb") as source:
                body = source.read(max_source_bytes + 1)
            if len(body) > max_source_bytes:
                raise ValueError(f"window source byte budget exceeded: {key}")
        sources.append({"key": key, "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body), "local": True})
        return body

    original_fetch = replay.fetch
    replay.fetch = fetch  # type: ignore[method-assign]
    try:
        meta = replay.load_session(day, underlyings, rows_by_symbol, sampled,
                                   max_row_bytes=max_row_bytes)
    finally:
        replay.fetch = original_fetch
    meta["sources"] = sources
    return meta


def _bounded_local_read(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as source:
        body = source.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"window source byte budget exceeded: {path.name}")
    return body


def selected_underlyings(day_dirs: Sequence[Path], *,
                         max_source_bytes: int = BuildLimits.max_source_bytes) -> set[str]:
    """Union of selected underlyings across local day archives."""
    out: set[str] = set()
    for d in day_dirs:
        universe = json.loads(_bounded_local_read(Path(d) / "universe.json", max_source_bytes).decode("utf-8"))
        out.update(u["underlying"] for u in universe.get("selected", []))
    return out


def finalized_day_dirs(spool: Path, *, through_day: date | None = None,
                       limit: int = MAX_SESSIONS) -> list[Path]:
    """Finalized local day directories in the spool, oldest first.

    A directory counts as finalized when it holds a summary.json with
    ``final: true``; a final-but-partial day counts (its partiality is
    carried as session_partial reasons by the loader). Torn or non-final
    days are skipped here and would still fail closed if passed to
    build_windows directly.
    """
    spool = Path(spool)
    dirs = []
    for day_dir in sorted(p for p in spool.iterdir() if p.is_dir()):
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if through_day is not None and day > through_day:
            continue
        summary_path = day_dir / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            if not json.loads(_bounded_local_read(summary_path, BuildLimits.max_source_bytes).decode("utf-8")).get("final"):
                continue
        except ValueError:
            continue
        dirs.append(day_dir)
    return dirs[-limit:] if limit else dirs


def windows_live_enabled() -> bool:
    """Live window hook enablement: env flag only, default off."""
    return os.getenv(WINDOWS_LIVE_ENV) == "1"


def _resolve_cycle_emit(spool: Path, cycle: str,
                        emit_root: Path | str | None,
                        scratch_dir: Path | str | None
                        ) -> tuple[Path, str, Path | None, tempfile.TemporaryDirectory | None]:
    """Resolve one cycle's emit dir: explicit root, or ephemeral scratch.

    Returns ``(emit, location, scratch_root, scratch_tmp)``. With an
    explicit ``emit_root`` the location is ``"explicit"`` and there is no
    scratch dir to clean up. Otherwise the emit root is a fresh
    ``oa203-windows-*`` temporary directory under ``scratch_dir``,
    ``$OA203_WINDOWS_SCRATCH_DIR``, or the system temp dir; the caller
    must clean it up (``scratch_tmp.cleanup()``) on success AND on
    failure. Fails closed when the scratch root is missing, is not a
    directory, or resolves inside the spool: derived output must never
    live on the archive volume.
    """
    if emit_root is not None:
        return Path(emit_root) / cycle, "explicit", None, None
    configured = scratch_dir if scratch_dir is not None else os.getenv(WINDOWS_SCRATCH_ENV)
    root = Path(configured) if configured else Path(tempfile.gettempdir())
    resolved = root.resolve()
    spool_resolved = Path(spool).resolve()
    if resolved == spool_resolved or spool_resolved in resolved.parents:
        raise ValueError(
            f"windows scratch root {resolved} is inside the spool {spool_resolved}; "
            "derived output must live on ephemeral scratch, not the archive volume")
    if not resolved.is_dir():
        raise ValueError(f"windows scratch root is not an existing directory: {root}")
    tmp = tempfile.TemporaryDirectory(prefix="oa203-windows-", dir=str(resolved))
    return Path(tmp.name) / cycle, "scratch", resolved, tmp


def run_after_finalize(spool_dir: Path | str, day, universe: Mapping[str, Any], *,
                       backend: pub.StorageBackend | None = None,
                       emit_root: Path | str | None = None,
                       scratch_dir: Path | str | None = None,
                       now: datetime | None = None,
                       min_free_bytes: int = MIN_EMIT_FREE_BYTES) -> dict[str, Any]:
    """Build + publish windows for the finalized day (hook implementation).

    Builds from the finalized day archives in the spool (up to 10
    sessions ending at ``day``) over the complete selected universe,
    then publishes to ``backend`` (default: the R2 backend — constructed
    only here, i.e. only when the hook actually runs). Callers must gate
    this on ``windows_live_enabled()``; this function itself performs
    the build + publish and must never be called from an unflagged path.
    Returns the build report plus per-window publish results.

    Per-cycle output: one cycle (one finalized day) writes to its own
    emit root and records exactly which windows it built in ``cycle.json``
    there. By default the emit root is ephemeral scratch (a
    ``oa203-windows-*`` temporary directory under ``OA203_WINDOWS_SCRATCH_DIR``
    or the system temp dir), NOT the spool: the hook writes nothing to the
    archive volume except the small ``windows_live.json`` record in the day
    archive, and the scratch dir is removed on success AND on failure (the
    publisher's attempt ledger in the backend plus the deterministic
    byte-identical rebuild are the recovery evidence). An explicit
    ``emit_root`` (tests / operator runs) keeps the old behaviour: the
    cycle dir is ``<emit_root>/<day>``. ``publish_windows`` is then given that
    explicit list -- never a directory scan of an accumulated emit root --
    so stale windows from earlier cycles are never republished and an
    unchanged re-run of the same cycle is idempotent: each window's
    summary bytes are a function of that window's own inputs only
    (stable per-window generated_at from its exit-session close,
    window-scoped scope and rows), so the rebuild is byte-identical
    for unchanged inputs and the publisher returns
    ``already_published`` with no new revision and no re-upload. A
    retry at a later wall-clock time, or a next-day cycle overlapping
    the same windows, republishes only genuinely changed/added window
    products; unchanged historical windows stay already_published.
    """
    spool = Path(spool_dir)
    day = date.fromisoformat(day) if isinstance(day, str) else day
    dirs = finalized_day_dirs(spool, through_day=day)
    if not dirs:
        raise ValueError(f"no finalized day archives in spool {spool}")
    today_selected = {u["underlying"] for u in universe.get("selected", [])}
    unders = selected_underlyings(dirs)
    if today_selected and not today_selected <= unders:
        raise ValueError(f"finalized day {day.isoformat()}: universe disagrees with its own archive")
    cycle = day.isoformat()
    emit, emit_location, scratch_root, scratch_tmp = _resolve_cycle_emit(
        spool, cycle, emit_root, scratch_dir)
    try:
        report = build_windows(dirs, unders, emit, full_universe=True, generated_at=now,
                               min_free_bytes=min_free_bytes,
                               scratch_emit=(emit_location == "scratch"))
        window_ids = sorted({(w["kind"], w["window_id"]) for w in report["windows"]})
        manifest = {"cycle": cycle, "day": day.isoformat(), "days": [d.name for d in dirs],
                    "underlyings": sorted(unders), "universe_scope": report["universe_scope"],
                    "generated_at": report["generated_at"],
                    "windows": [[kind, window_id] for kind, window_id in window_ids]}
        (emit / "cycle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        effective_backend = backend if backend is not None else pub.r2_backend()
        results = publish_windows(emit, effective_backend,
                                  window_ids={(kind, window_id) for kind, window_id in window_ids},
                                  now=now)
        # Publish/verify then remove expendable derived output: this cycle's
        # emit root is rebuildable from the spool while the spool matches, and
        # its products now live in the backend. Removing it here is what keeps
        # ongoing publication bounded: a successful cycle leaves no accumulated
        # emit behind. A failed publish raises above; with an explicit emit
        # root the emit is kept as recovery evidence, while a scratch emit is
        # still removed by the finally (the backend attempt ledger plus the
        # deterministic byte-identical rebuild are the recovery evidence).
        # Older cycles converge the same way: every
        # verified older cycle is pruned (keep_cycles=0, max_emit_bytes=0);
        # unverified cycles are kept as evidence. Both steps are best-effort
        # and recorded, never raised after a successful publish.
        cleanup = _remove_cycle_emit(emit)
        try:
            older = prune_windows_emit(emit.parent, effective_backend, keep_cycles=0,
                                       max_emit_bytes=0, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - recorded, never masks a publish
            older = {"error": f"{type(exc).__name__}: {exc}"}
        cleanup["older_prune"] = older
        # Bounded archive-cache audit, after successful window work.
        # Runs only when OA203_ARCHIVE_CACHE_AUDIT_ENABLED=1 (Chej, OFF
        # by default); dry-run report always, actual pruning only when
        # OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1 is also set. A lifecycle
        # failure is isolated: it is recorded in the return and the
        # persisted report, and never unfinalizes the day, unpublishes
        # windows, or fails the finalize -- publication stands.
        lifecycle: dict[str, Any] | None = None
        if archive_cache_audit_enabled():
            try:
                lifecycle = run_archive_cache_audit(
                    spool, effective_backend, day,
                    dry_run=not archive_cache_destructive_enabled())
            except Exception as exc:  # noqa: BLE001 - isolated, never fails finalize
                lifecycle = {"enabled": True, "day": day.isoformat(), "dry_run": True,
                             "decision": "error: audit failed",
                             "errors": [{"day": day.isoformat(),
                                         "errors": [f"{type(exc).__name__}: {exc}"]}],
                             "report_path": None}
        return {"enabled": True, "cycle": cycle, "day": day.isoformat(), "days": [d.name for d in dirs],
                "underlyings": sorted(unders), "emit_root": str(emit),
                "emit_location": emit_location,
                "scratch_root": (str(scratch_root) if scratch_root is not None else None),
                "manifest": manifest, "build": report, "publish": results,
                "cleanup": cleanup, "lifecycle": lifecycle}
    finally:
        # Scratch is removed on success AND on failure; a cleanup
        # failure must never mask the publish/build outcome.
        if scratch_tmp is not None:
            try:
                scratch_tmp.cleanup()
            except Exception:
                pass


def build_windows(day_dirs: list[Path] | tuple[Path, ...], underlyings: set[str],
                  emit_root: Path, *, top: int = 15, full_universe: bool = False,
                  generated_at: datetime | str | None = None,
                  min_free_bytes: int = MIN_EMIT_FREE_BYTES,
                  scratch_emit: bool = False,
                  limits: BuildLimits | None = None) -> dict[str, Any]:
    """Build unchanged full-universe products with bounded working sets.

    Raw observations and evaluated results live in a disposable SQLite spool
    below emit_root. One complete symbol (all attributions) is evaluated at a
    time; then one complete window is combined and globally ranked at a time.
    Resource limits refuse the whole build, never omit contracts. See
    _build_windows for archive validation and stable product identity.
    """
    root = Path(emit_root)
    _check_emit_headroom(root, min_free_bytes=min_free_bytes, scratch=scratch_emit)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".windows-spool-", dir=root) as tmp:
        spool = WindowSpool(Path(tmp) / "rows.sqlite", limits or BuildLimits())
        try:
            return _build_windows(day_dirs, underlyings, root, top=top,
                                  full_universe=full_universe, generated_at=generated_at,
                                  min_free_bytes=min_free_bytes, scratch_emit=scratch_emit,
                                  spool=spool)
        finally:
            spool.close()


def _build_windows(day_dirs: list[Path] | tuple[Path, ...], underlyings: set[str],
                  emit_root: Path, *, top: int = 15, full_universe: bool = False,
                  generated_at: datetime | str | None = None,
                  min_free_bytes: int = MIN_EMIT_FREE_BYTES,
                  scratch_emit: bool = False,
                  spool: WindowSpool) -> dict[str, Any]:
    """Evaluate every supported window kind over local finalized days.

    ``day_dirs`` are finalized day-archive directories (sorted by date,
    at most 10 sessions); ``underlyings`` is an explicit set of
    underlyings. By default at most 50 (a bounded subset, served
    partial as ``bounded_universe_subset`` — it never claims
    full-universe completion). With ``full_universe=True`` the cap is
    per day, not on the union: each loaded day selects at most 500
    underlyings (the live universe size) and the passed set must cover
    every selected underlying of every loaded day; the per-day set may
    rotate, so the union over the window may exceed 500 (bounded by 10
    sessions x 500). The combined full universe is ranked before the
    shortlist is cut (see ``windows_combine``).
    Writes the ``oa203/windows/`` tree under ``emit_root`` with scope
    ``live_archive``. A full-universe build keeps a genuinely final
    window final; a bounded-subset build is served partial as
    ``bounded_universe_subset`` (replay emits stay forced partial as
    ``bounded_universe_replay``). Returns the build report. Writes
    nothing outside ``emit_root`` and touches no provider or R2.

    Stable product identity: each window's summary bytes are a function
    of that window's own inputs only (its sessions, source locks,
    per-basis rows/counts/evidence, state and reasons). generated_at is
    the window's exit-session close, scope lists only the window's own
    sessions' selected underlyings, and rows cover only contracts with
    observations in the window's sessions. The cycle wall clock
    (``generated_at`` parameter / report ``generated_at``) is cycle
    metadata and never enters a window summary, so a retry at a later
    wall-clock time rebuilds byte-identical summaries for unchanged
    windows and the publisher returns already_published with no fresh
    revision. A genuinely changed window still gets a new revision.
    """
    _check_emit_headroom(Path(emit_root), min_free_bytes=min_free_bytes,
                           scratch=scratch_emit)
    dirs = [Path(d) for d in day_dirs]
    if not 1 <= len(dirs) <= MAX_SESSIONS:
        raise ValueError(f"bounded build: 1..{MAX_SESSIONS} sessions, got {len(dirs)}")
    unders = set(underlyings)
    if full_universe:
        # Per-day cap, not a union cap: MAX_UNDERLYINGS_FULL is the live
        # universe size for ONE day. The per-day selected set may rotate
        # (a replacement underlying enters on a later day), so the union
        # over the window may exceed 500 — 500 + one replacement is 501
        # and must NOT fail. The union is bounded by 10 sessions x 500.
        if not unders:
            raise ValueError("bounded build: at least 1 underlying")
        if len(unders) > MAX_SESSIONS * MAX_UNDERLYINGS_FULL:
            raise ValueError(f"bounded build: union over at most {MAX_SESSIONS} sessions x "
                             f"{MAX_UNDERLYINGS_FULL} underlyings, got {len(unders)}")
        for d in dirs:
            selected = len(selected_underlyings([d], max_source_bytes=spool.limits.max_source_bytes))
            if selected > MAX_UNDERLYINGS_FULL:
                raise ValueError(f"full_universe: {d.name} selects {selected} underlyings, "
                                 f"over the per-day cap {MAX_UNDERLYINGS_FULL}")
    elif not 1 <= len(underlyings) <= MAX_UNDERLYINGS:
        raise ValueError(f"bounded build: 1..{MAX_UNDERLYINGS} underlyings, got {len(underlyings)}")
    if [d.name for d in dirs] != sorted(d.name for d in dirs) or len({d.name for d in dirs}) != len(dirs):
        raise ValueError("day directories must be unique and sorted by date")
    rows_by_symbol = spool
    sampled: dict[str, set] = defaultdict(set)
    sessions_meta = [load_local_session(d, unders, rows_by_symbol, sampled,
                                        max_source_bytes=spool.limits.max_source_bytes,
                                        max_row_bytes=spool.limits.max_row_bytes) for d in dirs]
    sessions = [m["session"] for m in sessions_meta]
    if full_universe:
        for d in dirs:
            missing = selected_underlyings([d], max_source_bytes=spool.limits.max_source_bytes) - unders
            if missing:
                raise ValueError(f"full_universe: {d.name} selected underlyings not covered: {sorted(missing)[:5]}")
    complete_weeks = replay.validate_calendar(sessions)
    import pandas_market_calendars as mcal
    calendar_version = mcal.__version__
    policy = w.WindowPolicy()
    weekly, skipped = w.weekly_windows(sessions, policy)
    weekly = [win for win in weekly
              if date.fromisoformat(win.sessions[0].day).isocalendar()[:2] in complete_weeks]
    for yw in sorted({date.fromisoformat(s.day).isocalendar()[:2] for s in sessions} - complete_weeks):
        skipped.append({"window_id": f"weekly-{yw[0]}-W{yw[1]:02d}", "reason": "incomplete_calendar_week"})
    windows = weekly + w.overnight_windows(sessions, policy) + w.multi_session_windows(sessions, policy)

    # One candidate path per (symbol, underlying) attribution. A symbol
    # seen under more than one underlying is NOT stitched into a single
    # path: each attribution is evaluated separately and resolved by
    # windows_combine (pure duplicate -> one row; differing evidence ->
    # all attributions excluded, conflict recorded).
    candidate_count = 0
    for symbol, rs in rows_by_symbol.items():
        by_under: dict[str, list] = defaultdict(list)
        for r in rs:
            by_under[r["underlying"]].append(r)
        for under, urs in by_under.items():
            urs.sort(key=lambda r: (r["t"], r["sweep"]))
            last = urs[-1]
            replay.validate_contract_identity(symbol, last)
            identity = ("underlying", "type", "strike", "expiration", "contract_size")
            if any(any(r.get(k) != last.get(k) for k in identity) or
                   (r.get("root") and last.get("root") and r["root"] != last["root"]) for r in urs):
                raise RuntimeError(f"contract identity changed within path: {symbol} ({under})")
            info = ContractInfo(symbol=symbol, underlying=last["underlying"], option_type=last["type"],
                                strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
                                contract_size=int(last.get("contract_size") or 100))
            path = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"),
                                ask_ms=r.get("ask_ms"), underlying_price=r.get("spot"),
                                bid_size=r.get("bid_size"), ask_size=r.get("ask_size"),
                                volume=r.get("volume"), open_interest=r.get("oi")) for r in urs]
            candidate_count += 1
            seen = {s.day for s in sessions for o in path if s.open_ms <= o.t < s.close_ms}
            cd = seen & sampled.get((info.underlying, info.expiration), set())
            for win in windows:
                if cd & {s.day for s in win.sessions}:
                    spool.add_result(win.window_id, under, w.evaluate(win, info, path, cd, policy))
            del path
        # Do not retain the previous symbol while materializing the next one.
        del rs, by_under, urs

    all_sources = [s for m in sessions_meta for s in m["sources"]]
    status = {m["day"]: {"final": m["final"], "assessment": m["assessment"],
                         "assessment_reasons": m.get("assessment_reasons", []),
                         "sweeps": m["sweeps"]} for m in sessions_meta}
    if generated_at is None:
        generated = datetime.now(timezone.utc).isoformat()
    elif isinstance(generated_at, datetime):
        generated = generated_at.isoformat()
    else:
        generated = str(generated_at)
    universe_scope = "full_universe" if full_universe else "bounded_subset"
    all_conflicts: list[dict] = []
    conflict_bytes = 0
    kept_total = 0
    report: dict[str, Any] = {"builder": "windows_live", "mode": "live_archive",
                              "generated_at": generated,
                              "universe_scope": universe_scope,
                              "calendar": {"name": "NYSE", "package": "pandas_market_calendars",
                                           "version": calendar_version},
                              "policy": policy.as_dict(),
                              "sessions": [{k: v for k, v in m.items() if k not in ("session", "sources")}
                                           for m in sessions_meta],
                              "underlyings": sorted(unders), "contracts": candidate_count,
                              "contracts_kept": 0, "conflicts": all_conflicts,
                              "conflicting_symbols": 0,
                              "skipped_weeks": skipped, "windows": []}
    dir_by_day = {d.name: d for d in dirs}
    for win in windows:
        days = {s.day for s in win.sessions}
        # Window-scoped rows: only contracts with in-session observations
        # in THIS window's own sessions are evaluated for it. A contract
        # first sampled on an unrelated later day (or rotated out of the
        # window's days) contributes no row here, so unrelated universe
        # rotation cannot leak rows into a historical window.
        evaluated = spool.results(win.window_id)
        rows, conflicts, _stats = combine.combine_rows(
            win.window_id, [(under, [row]) for under, row in evaluated])
        for c in conflicts:
            conflict = {"window_id": win.window_id, "symbol": c["symbol"],
                        "attributions": c["attributions"]}
            conflict_bytes += len(json.dumps(conflict, separators=(",", ":")).encode("utf-8"))
            if conflict_bytes > spool.limits.max_conflict_bytes:
                raise ValueError("window conflict report budget exceeded")
            all_conflicts.append(conflict)
        kept_total += len(rows)
        srcs = [x for x in all_sources if x["key"].split("/")[2] in days]
        # Stable canonical summary bytes (retry fix): the summary is a
        # function of THIS window's own inputs only, so an unchanged
        # window rebuilds byte-identically and the released
        # rev_<sha256(summary.json)> genuinely denotes the same content
        # (no protocol change: the revision is still the SHA-256 of the
        # summary bytes). generated_at is the window's exit-session close
        # -- its data-as-of time -- NOT the cycle wall clock; scope lists
        # only underlyings selected on the window's own sessions and in
        # this build's universe; the calendar package version is build
        # metadata in the report, not a window input.
        win_generated = datetime.fromtimestamp(
            win.sessions[-1].close_ms / 1000, tz=timezone.utc).isoformat()
        win_unders = sorted(selected_underlyings([dir_by_day[d] for d in days],
                                                  max_source_bytes=spool.limits.max_source_bytes) & unders)
        outputs.write_window(Path(emit_root), win, rows, policy,
                             {d: status[d] for d in days}, srcs, win_generated,
                             {"mode": "live_archive", "universe_scope": universe_scope,
                              "underlyings": win_unders,
                              "note": ("built from the complete selected universe on the scanner host; "
                                       "no provider, no R2 read") if full_universe else
                                      ("built from a bounded subset of the selected universe on the scanner host; "
                                       "not the full universe; no provider, no R2 read")})
        summary: dict[str, Any] = {**win.as_dict(), "evaluated": len(rows)}
        for basis in w.BASES:
            ranked = w.rank_window(rows, basis)
            elig = [r for r in ranked if r[f"{basis}_rank"]]
            reasons: dict[str, int] = defaultdict(int)
            for r in ranked:
                if not r[f"{basis}_rank"]:
                    for why in r[basis]["reasons"][:1]:
                        reasons[why.split(":")[0]] += 1
            summary[basis] = {"eligible": len(elig),
                              "exclusion_reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
                              "top": [{"rank": r[f"{basis}_rank"], "symbol": r["symbol"],
                                       "pct": r[basis]["pct"]} for r in elig[:top]]}
        report["windows"].append(summary)
        del evaluated, rows, ranked, elig
    report["contracts_kept"] = kept_total
    report["conflicting_symbols"] = len({c["symbol"] for c in all_conflicts})
    return report


def _check_emit_headroom(emit: Path, *, min_free_bytes: int = MIN_EMIT_FREE_BYTES,
                          scratch: bool = False) -> None:
    """Fail closed before a build when the emit volume is too full.

    Resolves the nearest existing ancestor of ``emit`` (the emit root
    may not exist yet) and requires at least ``min_free_bytes`` free.
    Raises ValueError naming free/required bytes. With ``scratch`` the
    probe is the ephemeral scratch filesystem, not the archive volume;
    otherwise the operator prunes older per-cycle emit roots
    (``prune_windows_emit`` / ``windows-prune-emit``), never the
    finalized day archives.
    """
    probe = Path(emit)
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < min_free_bytes:
        if scratch:
            advice = ("free space on the scratch filesystem "
                      "(OA203_WINDOWS_SCRATCH_DIR or the system temp dir); "
                      "the hook writes nothing to the archive volume")
        else:
            advice = ("prune verified per-cycle emit roots "
                      "(windows-prune-emit), never the finalized day archives. "
                      "Note: emit pruning only helps if emit is what fills the "
                      "volume; the scanner's own day archives also grow on "
                      "this volume.")
        raise ValueError(
            f"emit headroom: {free} bytes free on {probe}, need at least "
            f"{min_free_bytes} bytes before building (32-window cycle peak "
            f"{MAX_CYCLE_EMIT_BYTES} bytes x2); {advice}")


def _is_link(path: Path) -> bool:
    """True for symlinks and (on Windows) junctions; False on any stat failure."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return False


def _resolve_inside(path: Path, root: Path) -> Path | None:
    """``path`` resolved, when it stays inside resolved ``root``; else None."""
    try:
        resolved = path.resolve()
        rroot = root.resolve()
    except OSError:
        return None
    if resolved == rroot or rroot in resolved.parents:
        return resolved
    return None


def _cycle_dir_safe(cycle_dir: Path, emit_root: Path) -> tuple[bool, str]:
    """Containment + link + day-archive-marker checks for one cycle dir.

    The emit root itself must not be a link; the cycle dir must resolve
    inside it, must not itself be a link, must hold no symlink/junction
    anywhere inside it, and must hold no day-archive marker (``sweeps/``).
    Anything else is refused: the prune never follows a link out of the
    emit root and never operates on source archives.
    """
    if _is_link(emit_root):
        return False, "emit root is a symlink/junction"
    resolved = _resolve_inside(cycle_dir, emit_root)
    if resolved is None:
        return False, "cycle dir escapes the emit root"
    if _is_link(cycle_dir):
        return False, "cycle dir is a symlink/junction"
    for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames) + filenames:
            if _is_link(here / name):
                return False, f"symlink/junction inside cycle: {name}"
        if (here / "sweeps").is_dir():
            return False, "cycle dir holds a day-archive marker (sweeps/)"
    return True, "safe"


def _cycle_dirs(emit_root: Path) -> list[Path]:
    """Per-cycle emit roots: real direct children named YYYY-MM-DD holding cycle.json.

    Symlinked/junction children are never followed and never listed.
    """
    out = []
    for child in sorted(Path(emit_root).iterdir()):
        if _is_link(child) or not child.is_dir():
            continue
        try:
            date.fromisoformat(child.name)
        except ValueError:
            continue
        manifest = child / "cycle.json"
        if manifest.is_file() and not _is_link(manifest):
            out.append(child)
    return out


def _link_children(emit_root: Path) -> list[str]:
    """Names of symlinked/junction direct children (reported, never followed)."""
    return sorted(c.name for c in Path(emit_root).iterdir() if _is_link(c))


def _cycle_size(cycle_dir: Path) -> int:
    """Total bytes of real files under a cycle dir (links never followed)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(cycle_dir, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_link(here / d)]
        for name in filenames:
            p = here / name
            if _is_link(p):
                continue
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def _pointer_revision_verified(backend: pub.StorageBackend, prefix: str,
                               pointer: dict) -> bool:
    """True when the pointer's revision members exist in the backend with matching digests.

    A newer pointer alone proves nothing about durability: the revision it
    names must be readable from the backend with every member digest
    matching the pointer. Any read failure or digest mismatch is False.
    """
    revision = pointer.get("revision")
    members = pointer.get("members") or {}
    if not isinstance(revision, str) or not isinstance(members, dict):
        return False
    for name in pub.WINDOW_MEMBERS:
        digest = members.get(name)
        if not isinstance(digest, str):
            return False
        try:
            data = backend.get(f"{prefix}/{revision}/{name}")
        except Exception:
            return False
        if hashlib.sha256(data).hexdigest() != digest:
            return False
    return True


def _cycle_prunable(cycle_dir: Path, backend: pub.StorageBackend) -> tuple[bool, str]:
    """True only on exact, verified evidence that every window in this cycle is durably accepted.

    The manifest must be present, well-formed, and list every window the
    cycle built: cycle/day/days/generated_at all present and consistent
    (cycle == day == the directory name, days non-empty ISO dates,
    generated_at a timezone-aware timestamp), windows a non-empty list of
    [kind, window_id] pairs for known kinds/ids. A missing, empty or
    malformed manifest qualifies NOTHING -- there is no vacuous pass.

    For each window the local members must exist and match the manifest
    identity, and the backend pointer must prove durable acceptance:
    PUBLISHED -- pointer revision equals the local summary hash AND every
    pointer member digest equals the digest of the corresponding local
    member bytes AND published_at does not predate the cycle (5-minute
    clock tolerance); SUPERSEDED -- pointer names a different revision
    with published_at strictly later than the cycle AND that revision's
    members verify in the backend (see _pointer_revision_verified). A
    newer pointer whose revision members cannot be verified, a member
    digest mismatch, or a malformed timestamp is a refusal, never a pass.
    """
    ok, reason = _cycle_dir_safe(cycle_dir, cycle_dir.parent)
    if not ok:
        return False, reason
    try:
        manifest = json.loads((Path(cycle_dir) / "cycle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"cycle.json unreadable ({exc})"
    if not isinstance(manifest, dict):
        return False, "cycle.json is not a JSON object"
    cycle = manifest.get("cycle")
    day = manifest.get("day")
    if not isinstance(cycle, str) or not isinstance(day, str) or cycle != day:
        return False, "cycle.json cycle/day missing or mismatched"
    try:
        date.fromisoformat(day)
    except ValueError:
        return False, "cycle.json day is not an ISO date"
    if cycle_dir.name != day:
        return False, "cycle dir name does not match cycle.json day"
    days = manifest.get("days")
    if not isinstance(days, list) or not days or not all(isinstance(d, str) for d in days):
        return False, "cycle.json days missing or empty"
    try:
        for d in days:
            date.fromisoformat(d)
    except ValueError:
        return False, "cycle.json days holds a non-ISO date"
    windows = manifest.get("windows")
    if not isinstance(windows, list) or not windows:
        return False, "cycle.json windows missing or empty (no evidence)"
    for entry in windows:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or not isinstance(entry[0], str) or not isinstance(entry[1], str)
                or entry[0] not in w.KINDS
                or not pub.WINDOW_ID[entry[0]].match(entry[1])):
            return False, f"cycle.json windows holds a malformed entry: {entry!r}"
    # The manifest must list exactly the windows present in the cycle's
    # local emit tree: a manifest that omits a window is not evidence
    # about that window, and an extra directory the manifest does not
    # account for is not prunable on this evidence either.
    tree = Path(cycle_dir) / "oa203" / "windows" / pub.POLICY
    present: set[tuple[str, str]] = set()
    if tree.is_dir():
        for kind_dir in sorted(x for x in tree.iterdir() if x.is_dir()):
            for win_dir in sorted(x for x in kind_dir.iterdir() if x.is_dir()):
                present.add((kind_dir.name, win_dir.name))
    if present != {(k, w_id) for k, w_id in windows}:
        return False, "cycle.json windows do not match the windows in the local emit tree"
    try:
        cycle_time = datetime.fromisoformat(
            str(manifest.get("generated_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False, "cycle.json generated_at missing/unparseable"
    if cycle_time.tzinfo is None:
        return False, "cycle.json generated_at has no timezone"
    for kind, window_id in windows:
        member_dir = Path(cycle_dir) / "oa203" / "windows" / pub.POLICY / kind / window_id
        try:
            summary_bytes = (member_dir / "summary.json").read_bytes()
            rankings_bytes = (member_dir / "rankings.csv").read_bytes()
            paths_bytes = (member_dir / "paths.csv.gz").read_bytes()
        except OSError as exc:
            return False, f"{window_id}: local member missing ({exc})"
        try:
            summary = json.loads(summary_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False, f"{window_id}: local summary.json malformed"
        if summary.get("kind") != kind or summary.get("window_id") != window_id:
            return False, f"{window_id}: local summary identity mismatch"
        local_rev = "rev_" + hashlib.sha256(summary_bytes).hexdigest()
        local_digests = {"summary.json": hashlib.sha256(summary_bytes).hexdigest(),
                         "rankings.csv": hashlib.sha256(rankings_bytes).hexdigest(),
                         "paths.csv.gz": hashlib.sha256(paths_bytes).hexdigest()}
        try:
            pointer = pub._read_pointer(backend, pub.window_prefix(kind, window_id))
        except ValueError as exc:
            return False, f"{window_id}: pointer damaged ({exc})"
        if pointer is None:
            return False, f"{window_id}: not published (no pointer)"
        members = pointer.get("members")
        if (not isinstance(members, dict)
                or any(not isinstance(members.get(m), str)
                       or not re.fullmatch(r"[0-9a-f]{64}", members.get(m) or "")
                       for m in pub.WINDOW_MEMBERS)):
            return False, f"{window_id}: pointer members malformed"
        if not isinstance(pointer.get("revision"), str) or not pointer["revision"].startswith("rev_"):
            return False, f"{window_id}: pointer revision malformed"
        try:
            pub_time = datetime.fromisoformat(
                str(pointer.get("published_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False, f"{window_id}: pointer published_at missing/unparseable"
        if pub_time.tzinfo is None:
            return False, f"{window_id}: pointer published_at has no timezone"
        prefix = pub.window_prefix(kind, window_id)
        if pointer.get("revision") == local_rev:
            if members != local_digests:
                return False, f"{window_id}: pointer member digests differ from local members"
            if pub_time < cycle_time - timedelta(minutes=5):
                return False, f"{window_id}: pointer published_at predates the cycle"
            continue
        if pub_time <= cycle_time:
            return False, f"{window_id}: pointer is an older revision (cycle revision unpublished)"
        if not _pointer_revision_verified(backend, prefix, pointer):
            return False, f"{window_id}: newer pointer revision members not verified in backend"
    return True, "all windows published or superseded (member digests verified)"


def _delete_cycle_dir(cycle_dir: Path) -> None:
    """Delete one cycle dir after its safety re-check; re-list to confirm."""
    ok, reason = _cycle_dir_safe(cycle_dir, cycle_dir.parent)
    if not ok:
        raise RuntimeError(f"prune: refusing unsafe cycle dir {cycle_dir}: {reason}")
    shutil.rmtree(cycle_dir)
    if cycle_dir.exists():
        raise RuntimeError(f"prune: {cycle_dir} still exists after delete")


def _remove_cycle_emit(cycle_dir: Path) -> dict[str, Any]:
    """Remove one just-published cycle emit root; never raises.

    The cycle's products live in the backend and the emit is rebuildable
    from the spool while the spool matches, so a successfully published
    cycle's emit is expendable. A failure to remove is reported, not
    raised: the publish already succeeded, and the next cycle's headroom
    pre-check will refuse before the disk can fill.
    """
    try:
        ok, reason = _cycle_dir_safe(cycle_dir, cycle_dir.parent)
        if not ok:
            return {"removed": False, "bytes_removed": 0,
                    "error": f"unsafe cycle dir, kept: {reason}"}
        size = _cycle_size(cycle_dir)
        _delete_cycle_dir(cycle_dir)
        return {"removed": True, "bytes_removed": size, "error": None}
    except Exception as exc:
        return {"removed": False, "bytes_removed": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def prune_windows_emit(emit_root: Path, backend: pub.StorageBackend, *,
                       keep_cycles: int = DEFAULT_KEEP_CYCLES,
                       max_emit_bytes: int = DEFAULT_MAX_EMIT_BYTES,
                       dry_run: bool = True) -> dict[str, Any]:
    """Prune old per-cycle emit roots (derived output ONLY).

    Prunes verified cycles, oldest first, until the emit total is at or
    under ``max_emit_bytes``. A cycle is pruned only when every window in
    it is published or superseded with member digests verified against
    the backend (see ``_cycle_prunable``); anything unverified is skipped
    with a reason and left in place. ``keep_cycles`` is a recency
    preference only: the newest N cycles are never pruned even when over
    budget, and the report then shows the over-budget state. It is NOT a
    hard floor -- the default is 0, so the byte budget alone decides; a
    hard floor of 10 cycle dirs cannot fit the headroom at full-universe
    scale. Dry-run (default) lists what would be deleted and deletes
    nothing; with ``dry_run=False`` each deletion is re-listed to confirm
    the directory is gone. Only real direct children of ``emit_root``
    named YYYY-MM-DD and holding cycle.json are ever considered -- never
    the finalized day archives (they live in the spool, not under the emit
    root), never source history, never live/published data. Pointing the
    prune at a spool (direct children holding day archives) raises
    outright. Symlinked/junction children are reported as skipped and
    never followed. In dry-run mode ``pruned`` and ``bytes_after`` are
    projected, not actual.
    """
    root = Path(emit_root)
    for child in root.iterdir():
        if child.is_dir() and (child / "sweeps" / "manifest.jsonl").is_file():
            raise ValueError(f"refusing to prune {root}: it holds finalized day archives (spool)")
    cycles = _cycle_dirs(root)
    sizes = {c: _cycle_size(c) for c in cycles}
    total = sum(sizes.values())
    kept_floor = cycles[-keep_cycles:] if keep_cycles > 0 else []
    candidates = [c for c in cycles if c not in kept_floor]
    report: dict[str, Any] = {"emit_root": str(root), "dry_run": dry_run,
                              "keep_cycles": keep_cycles,
                              "max_emit_bytes": max_emit_bytes,
                              "bytes_before": total, "cycles_before": len(cycles),
                              "pruned": [], "skipped": [], "kept": [],
                              "bytes_after": total, "cycles_after": len(cycles)}
    for name in _link_children(root):
        report["skipped"].append({"cycle": name,
                                   "reason": "symlink/junction at emit root: never followed"})
    for cycle in candidates:
        if total <= max_emit_bytes:
            break
        ok, reason = _cycle_prunable(cycle, backend)
        if not ok:
            report["skipped"].append({"cycle": cycle.name, "reason": reason})
            continue
        if dry_run:
            report["pruned"].append(cycle.name)
            total -= sizes[cycle]
            continue
        _delete_cycle_dir(cycle)
        report["pruned"].append(cycle.name)
        total -= sizes[cycle]
    pruned_names = set(report["pruned"])
    report["kept"] = [c.name for c in cycles if c.name not in pruned_names]
    report["bytes_after"] = total
    report["cycles_after"] = len(cycles) - (0 if dry_run else len(report["pruned"]))
    return report


def publish_windows(emit_root: Path, backend: pub.StorageBackend, *,
                    budget: pub.WindowBudget | None = None,
                    window_ids: set[tuple[str, str]] | None = None,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Publish windows in a local emit root to ``backend``.

    With ``window_ids`` given, publishes ONLY those ``(kind, window_id)``
    pairs — the explicit list of windows one cycle built — and refuses
    if a listed window is missing from the emit root (checked before
    anything is published). Without it, every window in the emit root
    is published in sorted order (the manual CLI behaviour). Either way
    each window goes through ``windows_publish.publish_window``
    (validation, budget reservation, readback, pointer last). Returns
    the per-window pointer payloads. The caller chooses the backend:
    tests and the CLI use ``LocalFilesystemBackend``; R2 is a release
    step (Chej) via ``windows_publish.r2_backend``. Raises (and
    publishes nothing further) on the first invalid, over-budget or
    failed window.
    """
    root = Path(emit_root) / "oa203" / "windows" / w.WINDOW_POLICY_VERSION
    if not root.is_dir():
        raise ValueError(f"no windows emit root at {root}")
    planned: list[tuple[str, str]] = []
    for kind_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for win_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            if window_ids is not None and (kind_dir.name, win_dir.name) not in window_ids:
                continue
            planned.append((kind_dir.name, win_dir.name))
    if window_ids is not None:
        missing = sorted(window_ids - set(planned))
        if missing:
            raise ValueError(f"emit root is missing cycle windows: {missing}")
    if not planned:
        raise ValueError(f"no windows found under {root}")
    results: list[dict[str, Any]] = []
    for kind, window_id in planned:
        results.append(pub.publish_window(Path(emit_root), kind, window_id,
                                          backend, budget=budget, now=now))
    return results


def _report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
