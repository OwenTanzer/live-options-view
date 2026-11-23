# Local windows builder memory repair

Prepared offline on 2026-11-10, based on master `17b6c26`. The affected live
builder at that base is unchanged from deployed `a60f5141`. This patch is not a
release, deployment, restart, or recovery execution.

## Change and preserved contract

The original builder holds every raw observation across ten sessions, then
creates a second universe-sized set of Observation paths. This repair sends the
existing replay loader's validated rows into a disposable SQLite database below
the emit root. A compact projection retains exactly the fields consumed by the
builder, including the original missing-required-field rejection behavior.
Archives are read once, not once per underlying or partition.

One complete symbol is loaded at a time, including every underlying attribution.
The original per-attribution identity checks, chronological `(t, sweep)` order,
contract/session sampling intersection, evaluator and conflict combiner remain
in use. Every attribution's complete path is evaluated against every relevant
window. Evaluated rows are spooled, then each complete window is combined and
ranked globally before any shortlist. No underlying, negative return, or excluded
contract is discarded to fit memory. First-encounter symbol/attribution order is
preserved, including pure-duplicate selection. Source locks and order, scoped
window universe, and canonical window timestamps are unchanged. Resource controls
do not enter canonical summary bytes.

SQLite uses an 8 MiB page cache, no WAL or journal (the whole database is
rebuildable), and primary-key traversal rather than an in-memory global sort.
The database is closed and its temporary directory removed on success or failure.
The existing publish hook cannot proceed if the build raises. An error during
later output writing may leave local, unpublished emit members, as before; do not
manually publish a failed build's emit directory.

## Resource envelope, not a constant-RSS promise

Defaults are explicit fail-closed admission limits:

- 16 MiB per source file, including the preflight universe/summary reads.
- 64 KiB per decoded JSONL row (checked before JSON parsing).
- 500,000 distinct symbols across all selected sessions, with 16 MiB aggregate
  UTF-8 symbol keys. The ten-day universe may rotate; this is not a daily cap.
- 20,000 observations and 8 MiB compact serialized evidence per complete symbol,
  across all attributions. A skewed symbol is refused, never split.
- 512 MiB serialized evaluated rows per window, and 32 MiB aggregate conflict
  report evidence across all windows.
- 32 GiB maximum SQLite database, enforced by SQLite page count. Scratch free
  space is checked at startup and during ingestion/evaluation, retaining a
  576 MiB threshold. Concurrent disk use can still cause an I/O error, which
  aborts the build. No automatic source-archive pruning is introduced.

These are byte/count bounds, not a 512 MiB resident-memory cap. One window's
parsed dictionaries, conflict grouping, ranking copies and CSV/gzip buffers can
coexist. Report metadata and symbol bookkeeping also consume memory. Python
allocator behavior and retained collector objects affect total process RSS.
The verified scanner allocation reported during review is 8 GB; this patch does
not change it. Actual ephemeral disk capacity is **not verified**. A 32 GiB
maximum is a refusal ceiling, not a claim that Railway provides that capacity.

`build_windows(..., limits=BuildLimits(...))` supports explicit reviewed limits
for offline callers. The scheduled hook uses defaults. Raising them is not a
substitute for measuring its available memory and scratch disk. The archive
volume is not an acceptable spool destination: the normal live hook already
chooses ephemeral scratch outside it; retain that configuration. Explicit manual
emit roots must also be placed on adequately sized scratch, not source storage.

## Offline checks

Use a dedicated virtual environment with `deployment/python/requirements.txt`.
From repository root:

```
python -m unittest discover -s tests -p 'test_oa203_windows*.py' -v
PYTHONPATH=. python scripts/benchmarks/windows_spool_scale.py 100000
PYTHONPATH=. python scripts/benchmarks/windows_spool_scale.py 1000000 128
PYTHONPATH=. python scripts/benchmarks/windows_build_scale.py 71710
```

Benchmarks generate only temporary synthetic data, use no provider/network/R2,
and report maximum process RSS on Linux. The optional spool argument is a Linux
address-space limit in MiB. These are opt-in scale checks, not part of CI discovery.

Measured during preparation:

- Observation ingestion/traversal: 100,000 rows / 10,000 symbols, 43.7 MiB peak
  RSS, 12.6 MiB database, 1.23 seconds.
- 1,000,000 rows / 10,000 symbols, 43.4 MiB peak RSS, 129.7 MiB database,
  13.88 seconds. The same million-row case also completed under a 128 MiB
  address-space limit at 43.5 MiB RSS in 13.13 seconds.
- Real full builder: 71,710 contracts, two complete synthetic sessions,
  286,840 archive observations. Both overnight and sessions2 products retained
  all 71,710 contracts. Peak RSS 1,139.7 MiB, total fixture+build 55.59 seconds.

Final focused suite: 170 tests run, 164 passed and 6 skipped. Skips: two
preserved replay-checkpoint tests, three opt-in real full-universe tests, and
one Windows-only junction test on this Linux host. `git diff --check` passed.
Independent review also compared 42 generated small-fixture files with the
original builder and found byte-identical output.

The small-fixture suite covers existing duplicate/conflict behavior, rotating
universes, scoped source identities, all window kinds, negative/excluded rows,
global rankings, and deterministic retries. New tests cover all principal bounds,
cleanup, missing-versus-null quotes, SQLite page exhaustion, conflict-report
budget, archive preservation and failure before publication.

These tests do **not** establish runtime, disk footprint, or peak memory for
45 million production rows across ten days, nor collector finalization's retained
objects. No live archive fetch, live recovery, Docker packaging or remote CI was
run as part of this isolated preparation. The full repository collector/web suites
are separate release checks.

## Safe application and missed-window recovery (not executed)

1. Review the local patch and differential fixture evidence. Re-run required
   collector checks on the exact candidate commit before any approved release.
2. Before activation, verify actual ephemeral free disk, source sizes/row counts,
   and baseline RSS after daily finalization. Run a representative ten-session
   offline build on preserved local copies with an agreed envelope. Reject, rather
   than raise limits blindly, if a resource bound is exceeded.
3. Release and restart only after explicit authorization. Do not delete or rewrite
   finalized day archives, and do not assume restart rebuilds missing products:
   `recover_days` skips the windows hook for already-finalized archives.
4. For the reported Nov10 gap, separately verify the complete Nov6/9/10 local
   source archives and immutable input hashes. Build from those three sessions
   into a fresh ephemeral emit directory with this reviewed code. Review all
   products and current pointers; the proposed missing-target list is overnight
   Nov9–10, sessions2 Nov9–10, and sessions3 Nov6–10. Revalidate that list first.
5. After separate publication approval, use the existing publisher with exactly
   the verified missing `(kind, window_id)` set. Verify summary/member hashes,
   final/partial state and pointer readback. Do not publish unrelated rebuilt
   windows. No backfill or scheduler changes are included in this patch.
