# Live window producer / publication wiring — #130 / F2 inventory

Status: INVENTORY + DORMANT WIRING (live-builder implemented offline, flag off; Chej's four release blockers on `6de0d7a4` repaired offline 2026-11-09, flag still off).
Nothing in this document activates, deploys, or publishes anything. The first bounded batch publication
remains an acceptance probe (docs/130-first-window-publication-plan.md);
this doc answers what has to exist for window products to be produced
and published continuously, not just once.

Live facts used (established 2026-11-09, not re-asserted from stale
deployment-plan files): Railway `market-mcp-read-only` healthz 200,
`/mcp` 401 `missing_token`; R2 active pointer sweep 72 published
20:41:38Z, cadence on_time; final daily summary 2026-11-09 complete
(76 sweeps); R2 `oa203/windows/` overnight Oct 5–6 summary 404 — no
published windows found.

## The live chain, piece by piece

| # | Piece | Location | Status |
|---|---|---|---|
| 1 | Session collection: one weekday scanner job | `market_data/options_scanner/session.py:run_day` (line ~214); `cli.py` `run`; Railway service `oa203-banana-scanner-AWsT`, `python -u -m market_data.options_scanner run`, cron `45 12 * * 1-5` UTC, restart `NEVER` (`market_data/options_scanner/README.md`) | PRESENT, live |
| 2 | Intraday active snapshots (~30-min cadence) | `session.py:299` flag `OA203_ACTIVE_SNAPSHOTS_ENABLED == "1"` → subprocess `snapshot-supervise` → `active.py:build_active_snapshot` | PRESENT, live (sweep 72, 2026-11-09) |
| 3 | Final daily summary + R2 upload | `session.py:finalize` (build_contract_rows → backfill → write_outputs → summary.json → `DayArchive.push`) | PRESENT, live |
| 4 | Window evaluation (all 6 kinds × 3 bases) | `market_data/options_scanner/windows.py:weekly_windows`, `overnight_windows`, `multi_session_windows`, `evaluate` (line 408) | PRESENT as code, offline only |
| 5 | Emit writer (summary/rankings/paths) | `market_data/options_scanner/windows_outputs.py:write_window` (line 114); state via `window_state` (line 62) | PRESENT as code, offline only |
| 6 | Local-day loader (finalized archives → observations) | `market_data/options_scanner/windows_live.py:load_local_session` (new in this commit) | IMPLEMENTED, offline |
| 7 | Build driver (all kinds → local emit root) | `windows_live.py:build_windows`; CLI `windows-build` (`cli.py`) | IMPLEMENTED, offline |
| 8 | Publisher (validate → budget → readback → pointer) | `market_data/options_scanner/windows_publish.py:publish_window` (line 912); `WindowBudget` (line 734: 90 days, 8 GiB, 20 MiB reserve, 3 attempts, 16 revisions); `R2Backend` + `r2_backend()` (line 714, reuses `market_data.tradier.probe` `r2_client`) | PRESENT as code, offline only |
| 9 | Publish-all driver (emit root → backend) | `windows_live.py:publish_windows`; CLI `windows-publish` (local backend only); with `window_ids` it publishes exactly one cycle's explicit list and refuses a missing listed window before any upload | IMPLEMENTED, offline |
| 10 | Consumer (window_rankings + pointer check) | `market_mcp/consumer.js:window()`, `server.js:window_rankings`; image pinned to exact `c8fcf529` in `.github/workflows/publish-market-mcp-renaissance.yml` | PRESENT as code; deployment is Chej's release step |
| 11 | Live trigger (build + publish after finalize) | `session.py:finalize` → `maybe_run_windows_after_finalize` → `windows_live.run_after_finalize`, gated by env `OA203_WINDOWS_LIVE_ENABLED == "1"` (default off; enabling is Chej's activation step); each cycle writes its own emit root `<spool>/windows_emit/<day>/` + `cycle.json` and publishes only that cycle's explicit window list | IMPLEMENTED, offline, disabled by default |
| 12 | Partial-day admission (final but assessment ≠ complete) | `windows_replay.validate_manifest` admits assessment `complete` or `partial`; `load_session` carries `assessment_reasons`; `windows_outputs.window_state` propagates `session_partial:<day>`; torn/invalid manifests still refused | IMPLEMENTED, offline |
| 13 | Full-universe combine-emit (14 batches → one emit) | `market_data/options_scanner/windows_combine.py` (`combine_rows` / `combine_csv_rows` / `combine_emit` / `rows_from_paths`); `combine_emit` requires an explicit `CombineCoverage` contract (expected underlyings, batches, source locks, scope); `build_windows(full_universe=True)` ranks the combined universe before the shortlist with the 500 cap applied per day, not to the union; CLI `windows-build --full-universe` + `windows-combine --coverage` | IMPLEMENTED, offline |
| 14 | Retention janitor (standalone expiry deletion) | `WindowBudget` retention is enforced only inside `clean_and_reserve` during a publish; no independent expiry runner exists — by decision, publish-time cleanup is sufficient (next publish is the next cleanup); no standalone janitor is added | DECIDED — publish-time cleanup only |
| 15 | Intraday `in_progress` windows | Publisher refuses `in_progress` outright; consumer serves summary-only | NOT SUPPORTED BY DESIGN (scope disposition, not a gap) |

## What the offline chain proves

`windows_live` closes pieces 6–9 with the same evidence standard as
the parity work: the real producer (`windows.evaluate` +
`windows_outputs.write_window`) over local finalized archives, the
real publisher over a local backend, all 6 kinds × 3 bases. State is
truthful about scope: a bounded-subset build is served `partial` with
`bounded_universe_subset` (`live_archive` scope, never full-universe
final); a full-universe build keeps a genuinely final window final
(replay emits stay forced `partial`/`bounded_universe_replay`). Tests:
`tests/test_oa203_windows_live.py` (subset build is partial;
full-universe build final; partial-day admitted with reasons; torn
manifest refused; shared-symbol conflict excluded; bounds;
publish-all + idempotent re-publish; CLI build + publish),
`tests/test_oa203_windows_combine.py` (combine conflict/duplicate/
rank-after-combine + the real 14-batch replay counts; plus the blocker
regressions: coverage contract, quote-only conflicts, per-basis conflict
report) and
`tests/test_oa203_windows_trigger.py` (flag off no-op; flag on
build + publish over a local backend). The chain is callable today, by
hand, offline:

```sh
python -m market_data.options_scanner windows-build \
  --dir <day-2026-10-05> --dir <day-2026-10-06> --dir <day-2026-10-07> \
  --dir <day-2026-10-08> --dir <day-2026-10-09> \
  --underlyings AAA BBB CCC --emit-root <emit-root>
python -m market_data.options_scanner windows-publish \
  --emit-root <emit-root> --out <local-backend-root>
```

## Blocker repairs (2026-11-09, on Chej's review of `6de0d7a4`)

All four are implementation fixes in local commits on `nova/f1-f2-renaissance`,
offline, flag still off. No push, no activation.

1. **Combine coverage contract (`windows_combine.combine_emit`).** A
   single offline-replay subset used to come out `final`/`full_universe`
   because the scope was inferred from whatever arrived. `combine_emit`
   now requires an explicit `CombineCoverage`: expected underlyings,
   batch names, `(key, sha256)` source locks, and scope
   (`full_universe` | `bounded_subset`). A missing batch, unexpected
   batch, aliased duplicate batch (identical window content under two
   names, or the same underlying in two batches), underlyings or source
   locks that do not match the contract exactly, or summaries that
   disagree all fail closed. Every summary is validated including the
   first (kind/window id, policy version, sessions, session status,
   sources present). A subset/partial input comes out
   `bounded_subset`/`partial` with the reason recorded (or is refused);
   only a `full_universe` contract matching the actual inputs exactly
   may produce `full_universe`/`final`, and the contract is recorded in
   `combine_report.json` and the summary scope. CLI `windows-combine`
   takes required `--coverage` (JSON).
2. **Full per-basis evidence (`windows_combine._row_evidence`).** Two
   rows differing only in `qualified.entry_ask_ms` used to dedupe with
   zero conflicts, because only status/reasons/pct/entry/exit/entry_ms/
   exit_ms were compared per basis. Evidence is now every field
   `windows_outputs.BASIS_COLUMNS` carries per basis — status, reasons,
   values, sessions, boundary offsets, and the full quote evidence
   (bid/ask, quote stamps, sizes, ages, spreads) — plus identity
   (type/strike/expiration/root/contract size), flags and exclusions.
   This mirrors the #130 checkpoint's `EVIDENCE_FIELDS`
   (`combine-full-universe.py`) and extends it to evaluated rows;
   nothing in that list is narrowed. `conflicts.csv` keeps its flat
   qualified-basis columns and adds the same evidence for every basis
   (prefixed `mid_`/`exec_`/`qualified_`); `combine_report.json`
   carries the full per-basis attributions, not just batch names.
3. **Per-day universe rotation (`windows_live.build_windows`).** The
   500-underlying cap is a DAILY cap, not a union cap: each loaded day
   selects at most 500 underlyings (a day selecting 501 still fails
   closed); the per-day set may rotate, so the combined window universe
   — the union of the per-day capped sets — may exceed 500 (bounded by
   10 sessions x 500). A 500-day + 1 replacement (501-union) builds and
   ranks correctly. The passed set must still cover every selected
   underlying of every loaded day.
4. **Per-cycle publication (`windows_live.run_after_finalize` /
   `publish_windows`).** The shared emit root accumulated old windows
   and every publish republished them all. Each cycle now writes to its
   own emit root (`<emit_root>/<day>`, default
   `<spool>/windows_emit/<day>/`) with a `cycle.json` manifest listing
   exactly which windows it built, and `publish_windows` publishes only
   that explicit list (a listed window missing from the emit root is
   refused before any upload). Each window's summary bytes are a function of that window's own inputs only (stable per-window generated_at from its exit-session close, window-scoped scope and rows), so re-running a cycle with unchanged inputs rebuilds byte-identically and the publisher returns `already_published` (no fresh revision, no re-upload); a changed window gets a new revision only for that window. A retry at a later wall-clock time, or a next-day cycle overlapping the same windows, republishes only genuinely changed/added window products, never unchanged historical ones. Manual publish-all without a window list is
   unchanged.

### Spool / history prerequisite for ongoing production

The hook reads ONLY the local spool on the scanner host; nothing is
fetched from R2 and no spool backup, replication, or R2 read-back of
day archives exists. What must persist between cycles on that host:

- The finalized day archives for the last 10 sessions:
  `<spool>/<YYYY-MM-DD>/{summary.json, universe.json,
  sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz}` — the exact files
  `finalize` wrote. `run_after_finalize` loads up to 10 sessions ending
  at the finalized day from these.
- The per-cycle emit roots `<spool>/windows_emit/<day>/{cycle.json,
  oa203/windows/...}` if a cycle is to be re-run idempotently from disk
  (the manifest is the record of what that cycle built).
- The backend's published state: per-window `latest.json` pointers,
  immutable `rev_<sha256(summary.json)>` members, flat consumer keys,
  and per-window `latest-attempt.json` ledgers under
  `oa203/windows/oa203-windows-v1/<kind>/<window_id>/`. The
  budget/reservation check reads that same inventory — there is no
  separate budget-ledger file — and `already_published` depends on the
  pointer and revision members still being there.

What breaks if it does not persist: a fresh machine with no spool
history cannot build multi-session windows at all —
`run_after_finalize` raises `no finalized day archives in spool ...`
on an empty spool, and with fewer than 10 days it builds windows only
over the days present (weekly windows need a complete calendar week, so
a partial week yields no weekly windows). If the spool is lost, the day
archives are gone except for what is already published to R2 as window
products (summaries/rankings/paths per window), which cannot rebuild
other windows. If the backend's pointers are lost, re-publication
re-uploads everything (no `already_published` shortcut) and retention
accounting restarts from the visible inventory. If a cycle emit root is lost, the cycle rebuilds from the spool instead -- same result while the spool matches (the cycle wall-clock timestamp no longer enters any window summary), but the on-disk `cycle.json` record is gone.

## Local-output retention / disk headroom

Evidence (Chej, 2026-11-09, not independently verified by Nova -- do
not start collection to inspect it; the Railway volume SFTP inventory
timed out): the scanner's persistent `/data` volume is 5000 MB with
4190 MB used, so about 810 MB (~773 MiB) is free. Default spool is
`/data/oa203`; the hook reads up to ten finalized sessions and writes
one per-cycle emit root per finalized day, and no bounded emit cleanup
was found. The scanner is a scheduled/build-only service.

### How fast the emit grows (measured + capped)

- Worst case per the publisher's own member caps: a 10-session cycle
  builds at most 32 windows (6 weekly + 9 overnight + 9 two-session +
  8 three-session), each at most 9 MiB of emit members (summary <=1 MiB
  + rankings <=4 MiB + paths <=4 MiB compressed) = 288 MiB per cycle.
  14 windows was the 5-session fixture, NOT the maximum. At 288 MiB
  the ~810 MB free holds about 2-3 cycles of accumulated emit -- which
  is why the hook removes a successfully published cycle's emit root
  (see the retention policy below) instead of accumulating it.
- Bounded/batch scale, measured: the preserved batch-01 emit (5946
  contracts, 5 windows) holds 5 windows totalling ~1.5 MB of members
  (summary ~38 KB, rankings ~63 KB, paths.gz ~230 KB per window), so
  a bounded cycle is roughly 20 MB per cycle, and the free headroom
  holds roughly 30-40 cycles (about 6-8 weeks of daily cycles).
- Full-universe scale, measured (real producer, 14-batch combine,
  71,710 contracts/window, 2026-11-09): summary at most 55,492 B,
  rankings at most 65,497 B (shortlist-capped at 600 rows), paths.gz
  at most 2,436,802 B, paths expanded at most 34,295,267 B. A
  full-universe 32-window cycle is therefore at most ~100 MB of emit
  (32 x ~3.1 MB), not ~230 MB. The old ~8 MB figures were old-format
  qualified-only ranked CSVs, not new-format members, and do NOT show
  rankings exceeding the 4 MiB cap: measured new-format rankings are
  ~65 KB. Only the expanded-paths ceiling was demonstrated
  insufficient (32.7 MiB measured vs the old 24 MiB bound) and is
  raised to 48 MiB (`docs/130-window-publisher.md`).
- Binding constraint, stated plainly: the scanner's own day-archive
  growth (~110 MB/day) is what fills the volume, not windows emit.
  At ~110 MB/day the ~773 MiB free lasts ~7 days total. The window
  builder no longer contributes: its emit moved to ephemeral scratch
  (see below), so the 576 MiB pre-check now guards the scratch
  filesystem, not the archive volume, and archive growth no longer
  makes the hook refuse builds. The remaining ~7-day limit is the
  scanner's own archive mirror, not a windows problem -- that is a
  Chej/Owen capacity decision (volume resize is a paid service change,
  out of scope; deleting source archives is forbidden). A bounded
  local cache lifecycle for the mirror is implemented below
  (archive_cache.py): older local copies are pruned only after fresh
  exact remote digest verification, newest 10 finalized sessions
  retained, OFF by default behind OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1
  (Chej).

### The retention policy (local output only)

- Automatic cleanup, no manual operator dependency: `run_after_finalize`
  removes a successfully published cycle's emit root (publish/verify
  then remove expendable derived output; the products live in the
  backend and the emit is rebuildable from the spool while the spool
  matches), records the cleanup in its return, and best-effort prunes
  older verified cycles (`keep_cycles=0`, `max_emit_bytes=0`). By
  default the emit root is ephemeral scratch (a fresh
  `oa203-windows-*` temporary directory under
  `OA203_WINDOWS_SCRATCH_DIR` or the system temp dir), NOT the spool:
  the hook writes nothing to the archive volume except the small
  `windows_live.json` record in the day archive, and the scratch dir
  is removed on success AND on failure (the backend attempt ledger
  plus the deterministic byte-identical rebuild are the recovery
  evidence). An explicit `emit_root` (tests / operator runs) keeps
  the old behaviour, including keeping the emit on publish failure,
  and the scratch root is refused when it resolves inside the spool.
  The hook binds ONE effective backend (`effective_backend`: the
  passed backend, else `r2_backend()`) and uses it for BOTH the
  publish and the older prune, so the production default path prunes
  too. Steady state emit is ~0 (only unverified cycles remain).
- There is NO hard cycle-count floor: `DEFAULT_KEEP_CYCLES = 0` and
  the manual prune budget is `DEFAULT_MAX_EMIT_BYTES = 192 MiB`.
  The old sizing note (192 + 576 = 768 MiB <= ~773 MiB free on /data)
  applied when emit lived on the archive volume; since emit moved to
  scratch, the 576 MiB pre-check applies to the scratch filesystem
  and the manual prune now covers legacy spool emit leftovers
  (`windows-prune-emit`) only. A floor of 10 cycle dirs cannot fit
  the headroom at full-universe scale; `keep_cycles` is an explicit
  operator recency preference only, and the byte budget alone decides.
- A per-cycle emit root may be pruned ONLY when its manifest is
  present, well-formed and complete (cycle == day == dir name,
  non-empty ISO days, non-empty windows of [kind, window_id] pairs for
  known kinds/ids, timezone-aware `generated_at`) AND lists exactly
  the windows present in the local emit tree AND every window proves
  durable acceptance: PUBLISHED (pointer revision == local summary
  hash, every member digest matches the local bytes, `published_at`
  not earlier than the cycle minus 5 min) or SUPERSEDED (newer pointer
  with strictly later `published_at` AND that revision's members
  verified in the backend by re-reading them). A missing/empty/
  malformed manifest qualifies NOTHING; a newer pointer whose revision
  members do not verify does NOT qualify; a member digest mismatch
  does NOT qualify. Anything unverified is skipped with a reason and
  left in place.
- Escape rejections: the prune never follows a symlink/junction out of
  the emit root (resolved paths must stay inside it), never operates
  on a cycle holding a link or a `sweeps/` day-archive marker, and
  raises outright when pointed at a spool (direct children holding day
  archives).
- Prune deletes ONLY derived emit dirs (`<emit_root>/<YYYY-MM-DD>/`
  holding `cycle.json`). It NEVER deletes the finalized day archives,
  source history, or live/published data; the archives live in the
  spool, not under the emit root, and the prune never looks there.
- The prune is dry-run by default (`prune_windows_emit(..., dry_run=True)`
  / `windows-prune-emit` without `--confirm`): it lists what would be
  deleted, verifies published state first, deletes only the verified
  dirs, and re-lists to confirm each one is gone. Tests in
  `tests/test_oa203_windows_retention.py` prove the dry-run, the
  delete, the recency preference (not a floor), the unpublished-cycle
  skip, the exact manifest/supersede/member evidence rules, the
  symlink/junction/spool rejections, the 32-window 10-session build,
  the auto-cleanup (remove after publish, keep on failure, prune
  older, low-headroom boundary), and the scratch default (scratch,
  not spool; removed on success and on failure; inside-spool and
  missing-root refusals; scratch headroom).

### Headroom pre-check (fail closed before a build)

- `build_windows` (and therefore the `run_after_finalize` hook and the
  `windows-build` CLI) requires at least `MIN_EMIT_FREE_BYTES`
  (576 MiB = 2x the corrected worst-case per-cycle 288 MiB: 1x the
  build peak plus 1x for a local-backend copy) free before building,
  and raises `ValueError` naming free/required bytes otherwise. The
  check probes the filesystem the emit will actually land on: the
  scratch filesystem for the hook's default path
  (`scratch_emit=True`, refusal names the scratch filesystem), the
  explicit emit volume when an explicit `emit_root` is given. The
  build never starts on a filesystem too full to finish it. The
  refusal message names the actual situation: emit pruning only helps
  if emit is what fills the volume (see the binding constraint above).
- Spool-missing behaviour is already fail-closed and is now tested:
  an empty spool raises `ValueError` (`no finalized day archives`), a
  missing spool directory raises `FileNotFoundError`; a torn or
  non-final day is still refused by the loader. A pruned day is
  restored from R2 by `restore_day` (archive_cache.py, below),
  digest-verified against the remote journal; there is no spool
  backup or replication beyond the R2 mirror.

### Bounded local archive-cache lifecycle (archive_cache.py)

- The local mirror is bounded without deleting any remote evidence:
  `prune_archive_cache(spool, backend)` retains the newest 10 finalized
  sessions (the builder's lookback; `KEEP_FINALIZED_SESSIONS = 10`,
  pinned equal to `windows_live.MAX_SESSIONS` by test) plus every
  unfinalized, current or recovery-marked day, and prunes only older
  redundant local copies.
- Fresh exact verification at prune time, per candidate day
  (`verify_day_remote`): every local artifact's SHA-256 must match both
  the local journal and a fresh GET of the remote object; the remote
  journal is fetched and checked for contradictions with the local
  digests (a missing entry is noted, not blocking -- the remote journal
  is legitimately stale for `windows_live.json`, pushed after the
  journal). In-progress snapshots verify the same way (GET + SHA-256),
  except the local-only upload-attempts ledger, corroborated by exact
  set match against the remote budget tokens, and transient build debris
  (`.build.lock`, `*.building`), which protects the day outright. The
  journal's own upload check is length-only (PUT + HEAD ContentLength),
  so the journal alone never proves a remote copy -- the fresh GET +
  SHA-256 here is the first digest-level proof.
- Hard remote budgets (`RemoteBudget`): every run is bounded by a
  maximum number of remote requests, a maximum number of remote bytes
  read, and a maximum wall-clock time (run defaults 1024 requests /
  1 GiB / 600 s; per-day defaults 512 / 512 MiB / 300 s). The bounds
  are enforced BEFORE / WHILE consuming, not after: the request count
  and deadline are preflighted before any GET is issued (an exhausted
  budget refuses without a remote call), and each object is read
  through `_bounded_get`, which checks ContentLength against the
  REMAINING byte budget before reading any body byte, streams in chunks
  capped so the in-memory overshoot is at most one byte when the server
  lies about the length, and always closes the body. `R2Backend` gained
  an additive `get_stream` for this (its `get()` is unchanged; the
  publisher's reads stay as they are); `list_keys` keeps its 1000-key
  cap. Backends without a streaming read (local filesystem, test stubs)
  read the whole object and charge afterward -- that is accounting-only,
  not a hard resource bound, and the verification evidence says so
  (`bounded_read: streaming` vs `accounting-only`). Any budget breach
  fails the run closed: nothing deleted, failure recorded.
- The current day is never prunable, even if its summary claims
  final=true and even if it is otherwise malformed -- the date alone
  protects it (checked before any other classification in the prune
  and at the top of verification).
- Before any whole-day delete, the ACTUAL local directory is enumerated
  (rglob) and compared against the verified evidence set; any local
  file the evidence does not explicitly account for aborts the day's
  deletion, fail closed. The `DayArchive.artifacts()` listing is never
  treated as the universe of what exists: it skips `*.tmp`, so a
  `operator-recovery.tmp` left in a day dir used to be pruned silently
  and now refuses the day (Chej's `review_unknown_tmp.py` repro
  confirms: preserved, decision refused). The same check covers
  root-level recovery/operator files and stray files at any level.
- Pre-delete recheck: after verification and immediately before each
  delete, the day is re-listed and re-hashed (`_recheck_day_unchanged`);
  any added, removed, or modified file since verification stops the
  run with nothing further deleted. No deletion ever happens on stale
  evidence.
- Fail closed: any verification failure means NOTHING is deleted in
  that run and the failure is recorded. Protected, never candidates:
  the current day, non-finalized days, days within the keep set, days
  with pending uploads, days with recovery markers
  (`interruptions.jsonl` / `recovery_errors.jsonl`), any day holding a
  symlink/junction, and non-ISO-date entries (ignored). Deletion is
  `shutil.rmtree` + re-list to confirm; `backend.delete` is never
  called -- no R2 object is ever deleted or overwritten by this module.
- Bounded operations: at most `max_days_per_run` (default 2) days and
  `max_bytes_per_run` (default 512 MiB) per run, oldest first; verify
  ALL selected days first, delete only if all verify. Dry-run evidence
  first (the report lists exactly what would be deleted and the
  verification evidence for each day). Deletion requires the explicit
  switch `OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1` (Chej) AND
  `dry_run=False` (CLI `--confirm`); `keep_sessions` below 10 is
  refused.
- Production path: the same verification code and hard budgets serve
  both backends. CLI `--r2` verifies against the real R2 backend
  (`r2_backend()`); without it, `--backend-root` is verified.
  `archive-cache-prune --spool /data/oa203 --r2 [--keep-sessions 10]
  [--max-days 2] [--max-bytes-mb 512] [--max-remote-requests 1024]
  [--max-remote-bytes-mb 1024] [--max-verify-seconds 600] [--confirm]`
  (dry-run by default; deletion also requires
  `OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1`).
- Normal finalize hook: `run_after_finalize` runs the lifecycle audit
  after successful window work when `OA203_ARCHIVE_CACHE_AUDIT_ENABLED=1`
  (Chej, OFF by default). The audit always produces a dry-run report;
  actual pruning only when `OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1` is
  also set. Every run persists its report to
  `<spool>/archive-cache-reports/<day>.json` (path in the return as
  `lifecycle.report_path`). A lifecycle failure is isolated: recorded
  in the return and the report, never unfinalizes the day, unpublishes
  windows, or fails the finalize -- publication stands.
- Operator dry run (production, nothing deleted): set
  `OA203_ARCHIVE_CACHE_AUDIT_ENABLED=1` and run the normal finalize
  flow, or run `python -m market_data.options_scanner
  archive-cache-prune --spool /data/oa203 --r2`. The standalone CLI
  prints its JSON report to stdout; save that output as the audit receipt.
  The normal finalize hook persists its report at
  `/data/oa203/archive-cache-reports/<day>.json`. To move from dry-run
  to destructive enablement Chej additionally sets
  `OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1` after independent
  review/dry-run.
- Restoration recipe: `restore_day(spool, day, backend)` downloads
  exactly the artifacts listed in the remote journal, verifying every
  downloaded byte against the journal's SHA-256 and size before writing
  it atomically (`archive.published()`); writes the local journal from
  the remote journal bytes verbatim plus a fresh receipt, so
  `pending_uploads()` is empty for the restored day; asserts the
  restored summary is final; reports remote keys NOT in the journal
  (notably `windows_live.json` and `inprogress/` members) as
  `not_restored`. A restore with a non-empty `not_restored` is PARTIAL
  (`complete: false`): the journaled artifacts are byte-identical, but
  it is NOT a full restoration of the original local day from the
  journal alone -- `windows_live.json` and `inprogress/` are not in
  any journal and are not restored (they remain preserved on R2 under
  their known keys). Any failure removes the partial restore and
  raises. Restored days lack local `inprogress/` snapshots (the builder
  reads only summary/universe/sweeps; active snapshots live on R2
  under 30-day retention).
- Tests in `tests/test_oa203_archive_cache.py` (53): the dry-run, the
  confirm+env delete (newest 10 survive; remote untouched; `delete`
  never called), remote digest mismatch / missing member / journal
  contradiction refusals, pending / unfinalized / recovery / symlink
  protection, per-run bounds, the env gate, the ledger corroboration,
  build-debris protection, the restore round-trip (byte-identical for
  journaled artifacts; `complete: false` with the standard fixture's
  journal omissions, `complete: true` when the journal lists every
  remote object) plus restore failures (missing member, digest
  mismatch) removing the partial dir; hard budgets (request / byte /
  time exceeded, each failing closed with nothing deleted, budget state
  in the dry-run report) and the bounded-read gap (oversized body
  refused before any body byte is read, lying ContentLength stopped at
  the bound with the body closed, request/time preflight refusing
  before any GET is issued, no deletion in any refusal); current-day
  protection (final=true and a malformed-final variant); unrecognized
  `.tmp` files at root, nested, and deep levels (Chej's repro) plus the
  already-fixed unjournaled plain-file pending path; pre-delete recheck
  (modified / added / removed, plus a mutation injected between
  verification and deletion); the R2 path (stub backend through the
  same verification code, CLI `--r2` wiring); and the finalize hook
  (off by default, audit dry-run with persisted report, destructive
  only with both flags, failure isolated from publication).

## Historical implementation checklist

The checklist below records the original implementation handoff. Window
publication and connected-client readback subsequently shipped in PR211;
PR212 isolated scratch and enabled the approved bounded second expiry.
Current release receipts and remaining qualifying live observations are in
the Feature Grab Bag quest Log. The archive-cache lifecycle above is the
separate follow-up; older approval language below is not a new approval gate.

1. **Live trigger — IMPLEMENTED offline (live-builder); owner Chej
   (release), gate: Owen/Chej activation approval.** `finalize` now
   calls `maybe_run_windows_after_finalize` (flag
   `OA203_WINDOWS_LIVE_ENABLED`, default off); when enabled it runs
   `windows_live.run_after_finalize` (full-universe build + publish
   with `r2_backend()`). Enabling the flag is live activation and is
   NOT done here — Chej alone enables it after review. Until then,
   windows are produced only by the manual commands above or by the
   one-off batch in the first-window plan.
2. **Partial-day admission — IMPLEMENTED offline (live-builder).**
   A day whose summary is `final` but whose assessment is `partial`
   (e.g. real Oct 8, 2026) is admitted with explicit
   `session_partial:<day>` reasons propagated into the window summary
   (partial, never final); torn/invalid manifests are still refused
   fail-closed. The parity work already proves the consumer serves
   such partial windows correctly.
3. **Full-universe combine-emit — IMPLEMENTED offline (live-builder +
   blocker repairs).** `market_data/options_scanner/windows_combine.py`
   merges batch emits with the same conflict semantics as
   `combine-full-universe.py` (conflicting attributions excluded
   fail-closed with a conflict report; pure duplicates deduped);
   `build_windows(full_universe=True)` combines and ranks the full
   universe before the shortlist is cut. Since the blocker repairs,
   `combine_emit` requires an explicit coverage contract (see above),
   evidence compares all per-basis quote/path fields, and the 500 cap
   applies per day with the union allowed to rotate past it.
4. **Retention janitor — DECIDED, no janitor added.** Publish-time
   cleanup inside `clean_and_reserve` is sufficient within the 8 GiB
   cap: the next publish is the next cleanup (see
   `docs/130-window-publisher.md`). No standalone expiry run.
5. **Live R2 publication + attached-client verification — owner Chej.**
   Covered by the first-window plan's pre-checks (bounded builder,
   conditional reservation/readback/delete of a trial window, verified
   deployed revision, saved activation flag) and by the pinned MCP
   image workflow. No step here approves or performs them.
