# Active Big Banana lookup (#131): design and open decisions

Issue: [#131](https://github.com/OwenTanzer/live-options-view/issues/131).
Companion: #130 (multi-session windows, draft PR #134), whose window contract
this lookup also serves.
Status: **implementation candidate for independent review**. The code is
disabled for production collection by default. No collection setting or
deployment has been changed.

## Candidate implementation and activation gates

The scanner now has a cutoff-aware `build_contract_rows(..., cutoff_sweep=N)`
path. It stops reading the manifest at N and opens only those N sweep files.
Later quotes, later manifest coverage and after-close backfill are excluded.
Every six completed sweeps, an opt-in supervisor can launch one child build
with a 240-second wall-clock timeout. It never waits in the sampling loop.
Only one local builder may hold the build lock. A timed-out build is killed,
reported as a failed attempt and skipped; a later six-sweep trigger can try
again. The scanner's existing five-minute collection target is unchanged.

The child publishes `summary.json`, `leaderboard.csv`,
`qualified_ask_bid_v1.csv` and a cutoff `manifest.jsonl` under the immutable
`inprogress/sweep_NNNN/rev_<summary_sha256>/` path. The manifest records
SHA-256 for each original sweep. The upload checks the scanner's verified
sweep-upload journal first,
uploads and verifies every immutable member, then uploads a per-sweep pointer
and `latest.json` last. The summary omits wall-clock build time, so rebuilding
unchanged source and policy after local loss produces the same revision bytes
and key. The mutable pointer records the actual build attempt time. It also
carries member digests, cutoff, publication time and observed
cadence. A retry may republish the same generation but never move the pointer
backwards. Local snapshots are not part of the scanner's mutable daily upload
journal. `latest-attempt.json` records building, complete or failed attempts.
The post-close daily output remains the only final ranking.

The MCP appends `active_rankings`. It resolves `session: "current"` through
the existing bounded XNYS calendar, or accepts one explicit date. `sweep`
pins a retained generation; omitting it follows `latest.json`. It offers
`all`, `clean` and `qualified_ask_bid_v1` using producer ranks and rows.
`result_detail` verifies the pinned summary, board, manifest and at most two
source sweeps per page by digest. Two measured-size expanded sweeps leave
headroom under the MCP's 64 MiB per-query budget; the publisher rejects a
member over 4 MiB or a manifest line over 256 KiB before moving the pointer.
The hard byte/request/time limits still apply. Pointer advancement does not change an existing
reference. It reports the effective session, cutoff, state, freshness,
coverage, cadence and newer failed attempt. Closed sessions with a final
summary route to the existing final-return path; active snapshots never
masquerade as final.

**Activation remains gated.** `OA203_ACTIVE_SNAPSHOTS_ENABLED=1` is required
to trigger builds, and is unset by default. The automatic cadence has at most
16 six-sweep triggers per session. Each immutable revision has four members,
each capped at 4 MiB: at most 16 MiB of member bytes per build. A local
`inprogress/upload-attempts.json` ledger reserves an attempt before any member
upload call, permits at most 16 distinct cutoff sweeps and three upload
attempts per sweep, and fails closed if corrupt. Each attempt makes at most
four member and two pointer uploader calls. With the same surviving spool,
that is at most 48 attempts and 288 application-level uploader calls for
these six objects; an underlying storage client may retry a network PUT.
Automatic supervision can also emit two attempt-status uploads per trigger. These are
technical limits for a surviving local spool, **not remote storage, retention
or spending guarantees**. Loss of the entire spool also loses the ledger;
source amendments could then create new digest revisions. Failed attempts
can leave orphaned immutable members. The approved intent to retain snapshots
still needs a specific remote retention duration, lifecycle rule, durable
cost limit and estimate. Nothing here purges remote snapshots. Do not enable
publication until that bounded policy, scanner memory/CPU sizing for a second
process, and the 240-second budget have passed engineering review. The
current 27–75-second / 430–585-MB desktop measurements do not establish
Railway behavior. The separate #134 window candidate is not yet published;
integration remains unverified. This candidate exposes only the existing
daily session product and does not claim weekly or multi-session support.
No live Chej/client check has occurred.

The active response reports cutoff age separately from ranked quote age. The
600-second quote-age threshold is a **proposed display policy awaiting Owen's
decision**; it does not expand quote eligibility. Published 2026-10-09 sweep 1
has 52,832 rows, including 8,938 `trade_ms: 0` rows and eight rows with both
`bid_ms` and `ask_ms` zero beside missing prices. The active parser and detail
reader normalize those exact missing-value sentinels to `null`, while rejecting
zero timestamps beside priced quotes and future positive stamps.

Offline acceptance: run `python -m unittest discover -s tests -p
test_oa203_active.py`, the same command with `test_oa203_return_scanner.py`,
and `node --test market_mcp/*.test.js`.
The cutoff fixture rebuilds the same early ranking after six later sweeps
and a partial later manifest write, then confirms the uncut daily result has
different low-coverage/clean classification. MCP fixtures cover pointer
advancement, digest mismatch, bounded detail and missing states.

Goal: Chej asks during the trading day "what leads Big Banana so far today?"
and gets an honestly labeled, as-of leaderboard built only from evidence
collected up to that time. It can then follow one result to its exact
evidence.

## Verified baseline

This comes from source on `master` and the public 2026-10-09 artifacts.

- **Evidence is already public during the session.** After every sweep,
  `run_day` pushes `sweeps/sweep_NNNN.jsonl.gz` and the appended
  `sweeps/manifest.jsonl` to R2. `universe.json` is pushed before the
  open. Rankings (`summary.json`, `leaderboard.csv`,
  `qualified_ask_bid_v1.csv`) are built only at finalization after the
  close.
- **Cadence and size.** On 2026-10-09 there were 77 sweeps with a median
  duration of 306 s. One sweep is about 1.5 MB gzip and about **20 MB
  expanded**.
- **The MCP's existing bounds** (`market_mcp/README.md`) are 12 source
  requests, 64 MiB of total query bytes, 24 MiB per expanded artifact, a
  15 s deadline and 256 KiB of output.
- **#131 forbids duplicating producer calculations in the consumer.** The
  ranking must come from `oa203_returns.py`'s own build.

## Measured build cost (2026-10-09, 52,832 contracts, 77 sweeps)

`build_contract_rows` was run on the full downloaded day on a 32-thread
desktop, untraced. Peak memory is from a separate traced run. Railway is
likely slower.

| Cutoff | Time | Peak memory |
| --- | --- | --- |
| After sweep 12 (~1 h in) | 27 s | ~430 MB |
| After sweep 40 (midday) | 48 s | ~500 MB |
| Full day | 75 s | ~585 MB |

Every cutoff build currently reads **all** sweep files and then discards
rows after the cutoff. Sweep files are one per sweep, so a cutoff build can
read only the sweeps ≤ N listed in the truncated manifest. That makes early
snapshots proportionally cheaper and removes the future rows by construction.

## Options

| | Option | Feasibility | Verdict |
| --- | --- | --- | --- |
| A | MCP consumer rebuilds rankings on demand from raw sweeps | A mid-session build needs every sweep so far (≈40 × 20 MB), against a 64 MiB / 12-request / 15 s budget. It would also re-implement the producer calculation in JS. | **Not viable** |
| B | **Producer publishes immutable in-progress snapshots** using the same build code, and the MCP reads a pointer plus one snapshot | About 3–4 small requests per query and no consumer recalculation. The cost moves to the scanner (below). | **Recommended** |
| C | A separate on-demand builder service the MCP calls | New hosting, credentials and grants | Out of scope without separate approval |

## Proposed design (option B)

### Producer: in-progress snapshots

Every K completed sweeps (decision A1), the scanner builds the existing
outputs over observations **up to a cutoff**:

```
oa203/scanner/<date>/inprogress/sweep_<NNNN>/summary.json
oa203/scanner/<date>/inprogress/sweep_<NNNN>/leaderboard.csv
oa203/scanner/<date>/inprogress/sweep_<NNNN>/qualified_ask_bid_v1.csv
oa203/scanner/<date>/inprogress/latest.json      # pointer: sweep, cutoff_ms, digests, state
```

- **Same code, cut off at the boundary.** The snapshot calls
  `build_contract_rows(day_dir, policy, start_ms=open, end_ms=cutoff_ms)`,
  where `cutoff_ms` is the end time of sweep NNNN. **The manifest must be
  truncated to sweeps ≤ NNNN as well.** Today `chain_ok_sweeps` counts
  successes over the whole day's manifest. That is fine after the close, but
  it would leak later coverage into the `low_coverage` flag and the clean
  view of an earlier snapshot. This one-line truncation is a required change
  and gets a no-look-ahead test.
- **Immutable snapshots, moving pointer.** A snapshot directory is never
  rewritten. `latest.json` names the newest one and carries the SHA-256 of
  each member, the same pattern as the squeeze scanner's
  `latest.json` → `runs/<id>`. A reference returned to Chej is pinned to a
  snapshot, so later sweeps or finalization can't change its explanation.
- **State.** Each snapshot is `in_progress`, with the as-of cutoff and the
  sweeps included. The post-close `summary.json` stays the only `final`
  product. Snapshots never overwrite daily outputs.
- **It must never slow collection.** A late-day build (~75 s locally, likely
  more on Railway) is too long to run inline between ~5-minute sweeps. The
  build runs in a **separate process** under a hard wall-clock budget
  (decision A2), at most one at a time. A build still running when the next
  snapshot is due is not doubled up. A build that exceeds the budget is killed,
  logged and skipped. If the build is slow, the snapshot cadence drops; the
  sweep rate doesn't.
- **Read only what the cutoff needs.** The build reads sweep files ≤ N from
  the truncated manifest instead of filtering all 77 files after reading them.

### Consumer: MCP tool

There are two ways to expose it (decision A3):

1. **Extend `return_rankings`** with `session: "current"` (resolved
   through the XNYS calendar, returning the actual session) and
   `snapshot: "final" | "latest" | <sweep>`. Existing calls stay the same
   because the default is `final`.
2. **A new `active_rankings` tool** with the same views and filters, so
   `return_rankings` stays strictly the finalized-session contract.

Either way, every response:

- uses only the pinned snapshot, with no rebuild or re-scoring
- reports the effective cutoff, the sweeps included, the snapshot age, the
  expected cadence, and any newer failed snapshot attempt
- reports `before_open`, `in_progress`, `closed_pending_final`, `final`,
  `holiday`, `missing` or `failed` explicitly. It **never falls back to
  yesterday** when today's snapshot is absent.
- states that the sweeps are not simultaneous, that results are sampled and
  that values are quote comparisons, not fills
- keeps the existing all/clean/qualified views, producer ranks and reasons,
  and bounds

Detail follows the existing `result_detail` path, reading only that
snapshot's sweeps up to its cutoff and the selected contract. It pages at
most 6 sweeps per request, as today.

**#130 windows:** once #130's producer publishes `oa203/windows/…`, a weekly
or multi-session window with a later exit session than today is returned as
`in_progress` (entry chosen, no final exit). The tool never invents a final
endpoint.

## Decisions for the owner

| ID | Question | Recommended default | Alternatives |
| --- | --- | --- | --- |
| A1 | Snapshot cadence | Every **6 sweeps** (about 30 min), plus one at the close before finalization | Every 3 sweeps (about 15 min); hourly |
| A2 | Build budget | Separate process, one at a time, **240 s** wall-clock cap (under one sweep interval), kill and skip on overrun. Measured 27–75 s locally (above). | Inline between sweeps with a 60 s cap, which would skip most afternoon snapshots |
| A3 | MCP surface | **New `active_rankings` tool** (keeps `return_rankings` final-only) | Extend `return_rankings` with `session: "current"` and `snapshot` |
| A4 | Which views get snapshots | all, clean and qualified (the same three as final) | Qualified only, to cut build cost |
| A5 | Snapshot retention | Keep the day's snapshots. Optionally prune them after the session is final. | Keep only the latest |

**A1 amendment (2026-10-15, proposed).** "Every 6 sweeps" assumed ~5-minute
sweeps. On 2026-10-13, provider 504s stretched sweeps to 486–1,001 s and
sweeps 6→12 took 57.8 minutes. The trigger is now elapsed time: a snapshot is
due once 30 minutes have passed since the previous cutoff, to the nearest
sweep end (half a nominal sweep of tolerance keeps the healthy cadence at six
sweeps). Each pointer carries a `cadence` report (status `first`/`on_time`/`late`,
sweeps covered, longest sweep, budget-cut sweeps, server and transport errors), and
`active_rankings` reports `late` or `overdue` from elapsed time; for a pinned
sweep, `overdue` is judged from the current latest pointer and `newer_snapshot`
says whether a newer sweep exists. The sweep budget is enforced at request
dispatch: no first attempt or retry starts after it, and rate-limit and backoff
waits are capped at it. Activation
remains gated by `OA203_ACTIVE_SNAPSHOTS_ENABLED`.

## Required tests (beyond the issue's checklist)

- A snapshot at sweep N is identical whether it is built at N or rebuilt
  later from the archive with the cutoff at N (no future rows, no future
  manifest entries).
- The `low_coverage` and clean views at cutoff N don't change when sweeps
  after N are appended.
- Pointer-advance races: a reference pinned to snapshot N survives
  `latest.json` moving to N+6, and a mismatched digest returns
  `source_changed`.
- A slow build is skipped without delaying the next sweep's start.

## Not in scope

Continuous estimates, streaming, alerts, a higher sampling rate, automatic
trading, or any deployment by virtue of this document.
