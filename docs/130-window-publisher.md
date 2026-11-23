# #130 window publisher (F2) — offline implementation

Status: implemented and tested offline (fake + local-filesystem backends
only). No push, no PR, no deployment, no live R2 publication, no activation.
Chej owns review/release.

## What was built

- `market_data/options_scanner/windows_publish.py`: validates one local
  `oa203/windows/` window against the consumer's exact contract
  (`market_mcp/consumer.js` `windowSummary` + rankings + paths-detail
  checks) and publishes it to a pluggable storage backend.
- `tests/test_oa203_windows_publish.py`: 25 tests over a fake in-memory
  backend and a local-filesystem backend. No network, no credentials, no
  live R2 in any test.
- `tests/test_oa203_windows_parity.py` + `market_mcp/parity.test.js`:
  producer-publisher-consumer parity for all 6 kinds x 3 bases x
  final/partial (see `docs/131-window-consumer.md`).
- `tests/test_oa203_windows_concurrency.py`: 7 tests proving the
  same-window and global-budget races below with deterministic
  one-shot interleaving hooks (no threads, no sleeps).
- The real R2 backend reuses the scanner's existing upload client
  (`market_data.tradier.probe` via `market_data.options_scanner._probe`:
  `r2_client()` + `put_bytes_verified()`); no new credentials, host, or
  secrets. It is constructed only by `r2_backend()` and is never used in
  tests.

## Exact R2 layout

```
oa203/windows/oa203-windows-v1/<kind>/<window_id>/
  summary.json            # flat consumer keys (paths, rankings, summary last)
  rankings.csv
  paths.csv.gz
  rev_<sha256(summary.json)>/summary.json
  rev_<sha256(summary.json)>/rankings.csv
  rev_<sha256(summary.json)>/paths.csv.gz
  latest.json             # pointer to the current revision (operators/audit)
  latest-attempt.json     # outcome of the last reserved attempt
  budget/<rev>-<attempt>.json   # conditional reservation tokens (1..3)
```

The consumer reads the three flat keys plus `latest.json`
(`market_mcp/source.js` allowlist); `rev_*/`, `latest-attempt.json`
and `budget/` are never queried by the consumer. The pointer is what
pins a consumer read to one revision: after its before/after digest
check the consumer verifies the flat summary/rankings digests against
the pointer's member digests and refuses a mismatch, and detail pins
the paths bytes to the pointer the same way. Revision members are never overwritten in place: a corrected
republication writes a new `rev_<sha>/` and moves the pointer, so the
old revision's bytes stay on disk and auditable. That is NOT a read
pin: the consumer never reads `rev_*/`, so the old revision staying
on disk does not by itself protect any read (see "Availability
limitation" below).
The flat keys are what the consumer reads, so they are overwritten on
republication. The revision itself is `rev_<sha256(summary.json)>`:
two emits with identical summaries but different rankings/paths
collide on revision, so the pointer's member digests are the real
distinguisher -- a retry with the same revision but different member
digests fails closed (tampered pointer), and the consumer refuses any
resulting mix.

Upload order: revision members → flat members (paths, rankings, summary
last, so an interrupted publish leaves the old summary -- whose counts
still describe the old rankings -- rather than a new summary over old
rankings) → `latest.json` pointer last → `latest-attempt.json` ledger.
Every upload is verified by a full readback (GET + SHA-256), not just
the uploader's HEAD length check. A readback mismatch fails closed with
no pointer written; already-uploaded revision members are immutable
and harmless. Note the flat order is defence in depth only: it cannot
by itself prevent a mixed revision (see Concurrency below); the
consumer's pointer check is the load-bearing guard.

## States (decision, recorded explicitly)

- `final` and `partial` windows are published with their true state. A
  partial window is never marked final.
- `in_progress` windows are **refused entirely**: no member is published,
  not even a summary-only one. A half-populated window directory is a
  footgun (the consumer serves in-progress as summary-only with no rows,
  and a later final publish must overwrite it); an in-progress window
  becomes publishable when its sessions finalize and it is re-emitted.
- A window is published only with all three members present and valid.
  A missing `paths.csv.gz` is a refusal, not a rankings-only partial
  publish: the consumer tolerates a missing paths file at read time, but
  the publisher never creates that state.

## Validation (mirrors the consumer, plus full-universe proofs)

The publisher holds the full universe in `paths.csv.gz`, so it proves
things the consumer cannot:

- Policy fixed to `oa203-windows-v1`; `close_tolerance_s == 900`,
  `open_tolerance_s == 900`, `qualified_endpoint_policy.max_quote_age_s
  == 1800`. Window-id shape per kind, and id/session agreement (weekly
  id names its ISO week; span ids name their first/last session days).
- Per-basis counts: `eligible + excluded == contracts_evaluated`,
  `shortlist == min(200, eligible)`, exclusion-reason sums match
  `excluded`. Hindsight label required for `weekly_cheapest` /
  `weekly_cheapest_best_exit`.
- Rankings: exact header, grouped by basis in producer order, per basis
  exactly ranks `1..shortlist`, pct-descending with symbol tiebreak,
  negative-count rule, OCC identity cross-check, pct arithmetic within
  `2e-6`.
- Paths: expanded size at most 48 MiB (the consumer ceiling; measured full-universe maximum 34,295,267 B), exact
  header, row count equals `contracts_evaluated`, symbols unique
  (conflicting attributions for one symbol fail closed with no canonical
  path asserted), per-basis eligible/excluded counts **and**
  exclusion-reason breakdowns recomputed from the full universe match
  the summary exactly, every ranked row matches its paths row on basis
  status / pct / entry / exit / sessions, endpoints inside their own
  sessions with exit after entry, quote stamps at or before observation
  time and not before 2000-01-01, quote ages non-negative, path
  arithmetic within `2e-6` (pct) / `1e-3` (per-contract change).

Any failure raises `ValueError` before any upload; a validation refusal
uploads nothing at all, including no ledger entry.

## Idempotency and attempts

- A retry of the same window (same summary SHA-256) returns
  `already_published` with the same revision and uploads nothing, once
  the remote pointer and revision members are verified to match. A
  pointer with the same revision but different member digests fails
  closed (tampered pointer).
- Each revision gets at most 3 attempts, reserved by conditional
  `budget/<rev>-<attempt>.json` tokens (`IfNoneMatch: *`), mirroring
  `active_budget.py`. A failed attempt after reservation is recorded in
  `latest-attempt.json` (best effort); a failure to write the ledger
  after a successful publication is reported in the result, not treated
  as a failed publication.

## Concurrency (proven offline, deterministic interleavings)

- **Same window, same revision: safe.** Two publishers overlap, reserve
  different attempt tokens, write identical bytes, and the pointer is
  identical. A retry of the same window is `already_published`.
- **Same window, different revisions: a mixed flat tree CAN be produced.**
  If publisher B completes fully between A's member writes and A's
  pointer write, the flat members end up B's revision while the pointer
  claims A's. The publisher cannot detect this; the consumer's pointer
  check refuses it (`source_changed`, never served). A crashed
  publisher's late flat-paths write can likewise leave a paths-only
  mix (summary/rankings match the pointer, paths do not); the detail
  paths-pin refuses that one.
- **Global budget race: fixed by reserve-then-verify.** The old
  check-then-reserve let two publishers racing a nearly-full budget
  both pass on a stale inventory read and over-commit. `clean_and_reserve`
  now reserves the attempt token first, then re-reads the inventory and
  checks the global byte cap (and the revision cap) with the reservation
  counted; on failure it deletes the token it just created and raises,
  so at most one reservation lands past the cap. This assumes the
  backend's listing reflects a just-created token (true for the local
  backend and for R2/S3 strong read-after-write consistency on list).
- **Revision cap counts pending tokens.** Distinct revisions are rev
  dirs plus revisions with a budget token, so a racing publisher cannot
  slip a 17th distinct revision past the 16-revision cap. Retries of
  the same revision share one entry; a failed attempt's revision does
  occupy a slot until retention cleanup (fail-closed).
- **Retention deletion race: benign.** Two publishers deleting the same
  expired objects is idempotent, and deletion is re-verified by
  re-listing.

## Availability limitation — the pointer is a refusal, not an immutability guarantee

The pointer check refuses a mixed revision; it does not make flat
reads immutable, and it does not protect any read that does not use it:

- Pointer present and digests match: the read is pinned to that one
  revision; the consumer serves it.
- Pointer present and flat summary/rankings (or detail paths) digests
  mismatch the pointer's member digests: the consumer refuses with
  `source_changed` and serves nothing. This is an AVAILABILITY loss
  (a correct window becomes unreadable until the flat tree is
  repaired or republished), not a silent wrong answer — and not a
  guarantee that the data is intact.
- No pointer (`latest.json` absent): the consumer serves the flat
  keys with a `not revision-pinned` warning. A mixed flat revision
  (new summary, old rankings, or vice versa) can then be served
  without any refusal. The publisher always writes a pointer, so a
  missing one means a legacy or hand-placed window.
- Any reader that reads the flat keys without the pointer — any
  client, script, or future consumer version that skips the pointer
  check — gets NO protection at all: the immutable `rev_*/` members
  are not what it reads, and the pin does NOT survive a flat-only
  read. Do not claim otherwise anywhere: the `rev_<sha>/` copy is an
  audit artifact, not a read path.

In short: immutability lives in `rev_*/` (never overwritten), the pin
lives in `latest.json` + the consumer's digest check, and the two meet
only when the consumer actually reads the pointer. A mixed flat tree
is refused, never repaired, by the consumer.

## Retention / budget (measured, implemented)

Mirrors `active_budget.py`'s shape with numbers sized for windows
instead of intraday snapshots. The per-member caps are implemented
from measured full-universe evidence (real producer, 14-batch combine,
71,710 contracts/window, 2026-11-09): summary at most 55,492 B,
rankings at most 65,497 B (shortlist-capped at 600 rows), paths.gz at
most 2,436,802 B -- all inside their caps, which are NOT raised. Only
the expanded-paths ceiling was demonstrated insufficient (measured
32,806,912-34,295,267 B expanded, over the old 24 MiB bound) and is
raised to 48 MiB: 1.46x the measured maximum, still inside the 64 MiB
per-query budget with the other members. The old ~8 MB ranked-CSV
figures do NOT justify a rankings raise: those were old-format
qualified-only CSVs, not new-format rankings members.

| Bound | Value | Reasoning |
| --- | --- | --- |
| Per-member bytes | summary ≤ 1 MiB, rankings ≤ 4 MiB, paths ≤ 4 MiB compressed / 48 MiB expanded | Consumer artifact (4 MiB) and expanded (48 MiB) ceilings; only the expanded bound changed, from measurement |
| Reservation per revision | 20 MiB | Flat members (≤ 6.1 MiB measured at full-universe scale: summary ~0.06 + rankings ~0.07 + paths.gz ~2.4 MiB, plus pointer/ledger) + immutable revision copy (same again); 20 MiB still covers a revision with ~3x headroom |
| Global cap | 8 GiB | Matches the scanner's reviewed bound |
| Retention | 90 days | Expired revisions, flat members, pointers and ledgers deleted together; deletion re-verified by re-listing |
| Attempts | 3 per revision | Same as active snapshots |
| Revisions per window | 16 | Corrections stay bounded; beyond that, manual reconciliation |
| Inventory | 1,000 keys per kind prefix, 32,000 total | Same as active; truncated listings and unknown keys fail closed |

The byte cap binds before retention for full-universe windows near the
per-member ceilings (~400 revisions at the cap), so exhaustion refuses
new publications until cleanup or an approved raise — it never silently
deletes live data. There is no legacy data under `oa203/windows/`, so no
policy-start floor is needed (unlike the scanner prefix).

Retention timing (decision): cleanup runs inside `clean_and_reserve`
during a publish — expired objects are deleted and the deletion
re-verified before the new revision is charged — so the NEXT PUBLISH is
the next cleanup. There is no standalone janitor, no timer, and no
background expiry run. If publications stop, expired objects simply
stay until the next publish; the cap and the per-revision reservation
still bound every publish.

## What remains for live publication (release gate)

1. **Chej's release decision** for live publication and activation.
   The per-member caps and budget numbers above are implemented from
   measured evidence (not an approval wait); what remains is Chej's
   release step -- publication to R2 and enabling
   `OA203_WINDOWS_LIVE_ENABLED` -- after review.
2. **OA203-style pre-checks** before any live R2 write: regression tests
   green (`tests/test_oa203_windows_publish.py`,
   `tests/test_oa203_windows_parity.py`,
   `tests/test_oa203_windows_concurrency.py`,
   `tests/test_oa203_windows.py`, `tests/test_oa203_windows_replay.py`,
   `node --test market_mcp/*.test.js`); a bounded builder for the
   publish run; an R2 conditional reservation, readback verification,
   and delete of a trial window (including a two-publisher race against
   the trial budget, to verify the listing reflects a just-created
   token); a verified deployed revision; and a saved activation flag
   naming the first window to publish.
3. **Live R2 verification**: published keys readable at the public
   origin; `window_rankings` for a real window (all three bases) plus
   pinned `result_detail` from an attached client once market_mcp is
   deployed (Chej-owned release).
4. **Connected-client check**: same as the consumer's remaining item 3
   in `docs/131-window-consumer.md`.

Until the gate passes, `windows_publish.py` is offline-only: use the fake
or local-filesystem backend, never `r2_backend()`.

## Stable product identity (retry fix)

The revision is still `rev_<sha256(summary.json)>` and the member
digests are still the SHA-256 of the published member bytes -- no
protocol change. What changed is the PRODUCER: `build_windows` (and
`combine_emit`) now write stable canonical per-window summary bytes,
so an unchanged window rebuilds byte-identically and the same revision
genuinely denotes the same content:

- `generated_at` in a window summary is the window's exit-session
  close (its data-as-of time), NOT the build/cycle wall clock. The
  wall clock stays in the build report and `cycle.json` (cycle
  metadata), never in a window summary.
- `scope.underlyings` lists only underlyings selected on the window's
  own sessions and in the build's universe (the live builder
  previously listed the union across all loaded days).
- Rows cover only contracts with in-session observations in the
  window's own sessions, so an unrelated day's universe rotation
  cannot leak rows or scope into a historical window.

A retry at a later wall-clock time, or a next-day cycle overlapping
the same windows, therefore returns `already_published` for every
unchanged window (no fresh revision, no re-upload, no attempt token
consumed) and a new revision only for genuinely changed/added window
products. See `tests/test_oa203_windows_retry.py`.
