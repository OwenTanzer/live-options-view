# MediaFlow news pilot: publishing and operating plan (M5 + G3 generations + G6 settlement, prepared, inactive)

This describes the prepared pilot. It is not active. `deploy/news-pilot.json` has `active: false`.
While it is false, `pilot-cycle --live` refuses to start, and so does every `publish-*` command
given `--live`. Activation needs a reviewed change plus the storage decisions in "Before a live
start".

## What one cycle does

`python -m market_data.news pilot-cycle` runs once and exits; the scheduler runs it every 15 minutes.

1. If `news/v1/publish/stop.json` exists, it exits 0 with `status: stopped`. Nothing is collected or written.
2. It collects from the four enabled sources: OilPrice, EIA Today in Energy, TechCrunch and Ars
   Technica. The streams are `oil` and `ai_buildout`. It uses the accepted M2.1 archive, which
   provides the writer lease, spooling, the manifest commit, commit.json availability and
   per-entry failure isolation.
3. It builds sanitized views for the last 7 UTC dates with the accepted M4a builder. No price input
   is connected, so every price is `missing`, pages carry `price_series.input: "unavailable"`, and
   the data label says "prices unavailable".
   R2: the same build also carries one research page. It is the sanitized projection of the private
   research ledger (`news/v1/research/`), **fixed** to research mode at this build's cutoff, and is
   named by the index's `research` entry (state `published`, `none` or `omitted`). It is a page of
   the generation like any other. See RESEARCH.md, "Publication".
4. It publishes the views to the public store, in this order (see `publish.py`):
   - The index and every page it names are checked before any write: no state, snapshot, lease,
     receipt or public object is touched by a generation that fails.
     - The index must carry every field the panel and publisher read (`data_label`, `build_as_of`,
       `complete`, `overflow_reasons`, `views_total`, `views_omitted`, and per view `mode`, `stream`,
       `date`, `first_page`, `page_count`, `rows_total`, `complete`). Its counts must agree:
       `views_total` = listed + omitted, and `complete` only when nothing is omitted or overflowed.
     - Each page must pass the panel's `validatePage` checks (kept in step by a test) and also have
       every nested shape the unchanged renderer reads: rows (instruments, price, time quality,
       classification, later context), coverage segments and their sources, price intervals, tail
       and hindsight lists (`PAGE_SPEC` in `publish.py`). A test runs the real renderer over every
       such field set to each wrong shape and requires the publisher to refuse every case the
       renderer cannot render cleanly.
     - Its mode, stream, date, page number, page count and `build_as_of` must match its key and
       index entry. Its `prev_page`/`next_page` must form exactly the chain page-0001 to page-N.
       Orphan pages are refused.
   - The build is then qualified into an immutable **generation** (G3, see "Generations"): every
     page moves under `news/v1/views/g/<gen>/`, links stay in that generation, and the index names
     `gen` and `pages_sha256`. The qualified generation is validated again in that form.
   - The run reconciles the ledger with the live index, cleans up orphan and expired generations,
     checks admission (no unsettled index write, page cap), writes the generation's pages
     create-if-absent with `Cache-Control: public, max-age=31536000, immutable`, then `index.json`
     with `Cache-Control: no-store` (the commit point), then reconciles again. Nothing is deleted at
     the commit: the replaced generation stays public for the reader grace.

## Generations and settlement (G3 + G6: implemented locally, inactive, never run)

The accepted G2 v2 design (G3) with the accepted G5 settlement design and lifecycle addendum (G6).
**Nothing in this section has been executed**: no test, no fixture generation, no provider or bucket.
What the provider documents and what this code assumes are listed separately at the end.

### Contract

- **Identity.** `gen = <build_as_of as YYYYMMDDTHHMMSSZ>-<first 16 hex of SHA-256 over the unqualified
  build>`, fixed before any key is qualified. An **incarnation** `<gen>.<n>` (n = 1..9999, no leading
  zero) is one page namespace of that generation. Final integrity is separate: `pages_sha256` in the
  index, over the qualified pages.
- **Keys (fresh namespaces).** `news/v2/views/index.json` (fixed, the only commit point) and
  `news/v2/views/p/<gen>.<n>/<mode>/<stream>/<YYYY-MM-DD>/page-NNNN.json` (immutable, written once).
  The same exact rule is enforced by `publish.py`, `site/js/news-panel.js`,
  `backend/browser-data.js` and `frontend/preview-policy.mjs`. Legacy keys
  (`news/v1/views/index.json`, `news/v1/views/g/...`) are never read, written or deleted, and are no
  longer served. Unqualified page keys are builder output only.
- **Index body.** Schema `news-view-index/v2`, `state: "published"`, `gen` (the incarnation id, which
  pages also carry), `logical_gen`, `pages_sha256`, and `commit.nonce`: a fresh 128-bit nonce for
  every index write. No two index bodies are ever byte-identical, so an ETag never repeats.
- **Tombstone.** The same key and schema with `state: "withdrawn"` (withdrawal) or `"unpublished"`
  (before the first publication), a nonce, and no generation or views. The index key is created once
  and **never deleted**.
- **Readers.** The panel follows only links of the incarnation its index names. Every request has a
  20 s deadline (late bodies discarded) and asks for no cached copy. The index lease is 30 min from
  the index request's start, checked on a monotonic and a wall clock (and again on visibilitychange,
  pageshow and online), so sleep and clock changes end it. A page 404, a request past its deadline, an
  incarnation mismatch or an ended lease shows **refresh required** (reload the fixed index), never a
  page of another incarnation. A tombstone shows "News views are not currently published"
  (withdrawn) or "No news views published yet" (unpublished, the same as a 404) and requests no page.
  The Worker gives News keys a 15 s upstream deadline (504) and serves the index with
  `Cache-Control: no-store` whatever upstream says.

### Records

- **Head** (`news/v1/publish/head.json`, private). The one shared record, changed only by
  compare-and-swap with a fresh nonce in its body: the live commit, every incarnation (`pinned` or
  `retiring`, its reserved page count, its lifecycle events, any unanswered page-upload attempts),
  prepared commits, per-generation
  high-water marks, and `withdrawing`. Every operation changes the head **before** it touches the
  public store.
- **Commit record** (`news/v1/publish/commits/<nonce>.json`, private, create-if-absent). One per
  index write: kind (`genesis`, `publish`, `restore`, `reseal`, `tombstone`), incarnation, the
  expected ETag and nonce of the index it replaces, the body hash and the page set. Written, then
  registered in the head, **before** the index write is sent.
- `state.json` is informational (clock high-water marks, last failure, blocked/paused notes, a
  summary of what is live). No decision reads it.

### Index writes

- **Compare-and-swap.** Every index write is one request with `If-Match` on the ETag read from the
  live index (the very first is `If-None-Match: *` and writes an "unpublished" tombstone). The store
  wrapper has no unconditional index put and no index delete. No hidden retries: the bucket client
  used for these calls sends one request per call.
- **Explicit outcomes.** *Applied*: the commit must then be observed live. *Rejected* (412 only):
  known not applied and unable to apply later; the commit is closed at once and the run reports a
  conflict. *Ambiguous* (anything else, or no answer): **the commit stays open in the head.**
- **An open commit** can land only while the index it was conditioned on is still live. Any later
  landed index write ends that for good. Nothing else closes it: no timer, no retry, no operator
  note. It does not pause publication: whichever open commit lands, the live index is a recorded
  commit whose pages are present, because an incarnation named by an open commit is never retired.
- **Observation first.** Every run reads the live index in full (bytes against the prepared body, the
  commit nonce, every page hash) and records what that proves. An index with an unknown nonce, other
  bytes, or a missing page **blocks** the run: nothing written or deleted.
- **Landed is not ceased.** A commit has landed if it is live, or if a later commit was conditioned
  on it. A generation **ceases** only when a later full observation finds a different committed
  incarnation, or a tombstone, live. That event is stamped from a paired clock reading taken
  **after** the observation, never from a commit's preparation time and never from run start.
- **Reseal.** `publish-reseal` rewrites the live generation's index with a fresh nonce. Readers see no
  change. Once it lands, every open commit is known dead: it is how an operator cancels an unwanted
  open restore.

### Pages and incarnations

- **Never reuse a namespace after a delete starts.** Before the first page delete in an incarnation,
  the head marks it `retiring` (permanent). Page deletes are unconditional and may land late; a
  retiring or retired incarnation is never written again, so a late delete can only hit pages that
  were to be deleted anyway.
- **Restore and republication** reuse an incarnation of that generation that is still `pinned` (no
  delete ever started in it; only a new index is written). Otherwise the build is re-qualified from
  its snapshot under `<gen>.<n+1>`. Without a usable snapshot the restore is refused and the pause
  stays. Snapshots hold the unqualified build for this reason.
- **A mismatching page** of a never-offered incarnation is not deleted and rewritten: the
  incarnation is abandoned (retiring) and the next run uses a fresh one.
- **Page uploads are recorded before they are sent.** Before its first page PUT a run records an
  upload attempt in the incarnation's head entry (one compare-and-swap, refused unless the
  incarnation is still pinned). Only after every PUT of the attempt returned and its page read back
  as written is the attempt recorded answered. An attempt left recorded (a PUT timed out, the
  process exited, the lease lapsed, the answered write was ambiguous) means a PUT may still land at
  any time. That incarnation is **quarantined**: it keeps its head entry and its full page
  reservation and is never retired or released. An empty listing, elapsed time, a later identical
  upload, a local process exit or a tombstone is **not** settlement. If the incarnation is retiring
  (abandoned, withdrawn) its pages are deleted every run as they land, and the entry stays.
  **There is no in-code release**: quarantined reservations accumulate until the owner provides a
  real settlement mechanism or a fence, and enough of them pause publication at the cap.
- **Cleanup before admission.** Orphan (pinned, never offered, no open commit, no unanswered upload)
  and expired incarnations are marked retiring by one head compare-and-swap, which revalidates on
  the document it replaces that no commit names them and no upload into them is unanswered. Only
  then are pages deleted; namespaces a fresh listing shows empty are recorded retired, except those
  with an unanswered upload, which stay in the head with their reservation. Current, protected,
  open, quarantined and unknown pages are never deleted.
- **Capacity.** `page_cap` (6,300) is counted as pages **reserved in the head** (every pinned or
  retiring incarnation, written or not, with or without an open commit or an unanswered upload)
  plus public pages with no head record. The reservation is taken in the same compare-and-swap that pins the incarnation, so
  two admissions cannot both fit into the last free room. At the cap publication pauses (visible);
  nothing protected is evicted; cleanup keeps running.
  R1: the research page counts as one page of its incarnation. The pilot maximum is 1,261 pages per
  generation, so five generations at that maximum (6,305) exceed the unchanged cap, and the fifth
  pauses instead of evicting. This is an availability limit; the cap stays 6,300.
- **Retirement.** A replaced generation stays public for at least `reader_grace_s` (45 min). Its pages
  are deleted only when the producer clock and the store clock (the private heartbeat's
  LastModified) both show 45 min + `clock_epsilon_s` + `clock_skew_s` = 49 min since it ceased, they
  agree within the skew, neither went backwards, and the deployment has verified the store-clock
  bound. **`clock_bound_verified` is false in the inactive config: the 2-minute store-clock bound is
  an assumption that has not been verified for any provider, so expiry deletion is refused and
  `publish-status` reports `expiry_blocked_clock_bound_unverified`.**

### Withdrawal

Explicit withdrawal is immediate. `publish-withdraw`:

1. records `withdrawing` in the head and marks every incarnation retiring. From then nothing is
   admitted and no commit naming a generation is prepared;
2. writes a tombstone over the index by compare-and-swap (a 412 means a prepared commit landed
   first: the index is re-read and the next tombstone is conditioned on it; at most 3 attempts per
   call, each with a recorded result; an ambiguous attempt is not resent blindly);
3. deletes every public page at once, whatever the tombstone's outcome;
4. is **confirmed** only when a tombstone is read back live.

Until then the command exits 1 and `publish-status` reports **`withdrawal_unconfirmed`**. In that
interval the public index may still be the old index, or an index from a commit prepared before the
withdrawal that lands late. Either names pages that no longer exist: a reader loading it sees
"refresh required" on every page. **This dangling-reference interval has no time bound.** It ends
when a tombstone is confirmed; run `publish-withdraw` again to send another. Cached copies and
downloads cannot be recalled in any case.

The tombstone confirms the index only. A page PUT sent before the withdrawal whose upload attempt
has no recorded answer may land after the pages were deleted, and the page reappears (named by no
index). The result lists those incarnations in `uploads_unsettled`; they keep their head entry and
full reservation, and every later `publish-withdraw` or cleanup run deletes what lands.
**This page-reappearance interval also has no time bound**, and nothing in code ends it.

### Migration and recovery

- **Fresh namespaces.** The new code writes only `news/v2/views/index.json` and
  `news/v2/views/p/...`. The claim that makes this a boundary is an **assumption to be established
  by the owner, not verified here**: no unconditional request (index put, index delete, page delete)
  has ever targeted those keys. Conditions on future requests do not fence requests already sent to
  the legacy keys; those can still land there, where nothing reads them.
- **Legacy state is not migrated.** A G3 `state.json` (schema v2), ledger files or snapshots (schema
  v1) are refused, not converted. No G3 state was ever deployed.
- **Blocked run.** `publish-status` names the reason. A foreign index (unknown nonce) is not repaired
  by the publisher: the owner decides. An unreadable head blocks everything.
- **Quarantined incarnations** (`page_uploads_unsettled`) have no recovery command. Settlement needs
  evidence the publisher does not have (that the outstanding PUT can no longer land, or a fence that
  makes it harmless); providing it is an owner decision outside this patch. Removing the `uploads`
  record by hand without that evidence reopens the late-landing case.
- **Conflict (`PublishConflict`).** A head or index compare-and-swap lost, or a head write could not
  be verified as the committed head. Nothing further was done for that intent. Run again.
- **Open commits.** Listed in `publish-status` (`open_commits`). They close when the next index
  write lands (the next cycle, or `publish-reseal`).
- **After an interrupted cleanup.** The incarnation stays `retiring`; the next run deletes what is
  left and records it retired.

### Provider and SDK: documented, and assumed

Documented by the provider (R2 conditional-operations and consistency pages, read 2026-11-12):
`PutObject` accepts `If-Match` / `If-None-Match`; a failed condition returns 412; the condition is
evaluated when the write commits and competing conditional writes are serialized; `If-Match` fails
on an absent key; a timed-out write may still commit and a 404 read does not prove it failed;
identical content gives an identical ETag; reads are strongly consistent after a write; a listing
reflects all objects at that point in time.

**Assumed, not verified by any run of this code:**

1. two different bodies never share an ETag;
2. `put_object` accepts `IfMatch` / `IfNoneMatch`, returns the new ETag, and surfaces a 412 as
   `PreconditionFailed` or `412`;
3. the no-retry client (`total_max_attempts = 1`) sends exactly one request per call;
4. a `GetObject` response's body and ETag describe the same object version;
5. the private store gives the same conditional-write behavior;
6. an unanswered page delete may land later with no bound (the safe reading);
7. only this publisher writes the v2 keys.

The tests use scripted local stores. They model this contract; they are not evidence about it.

**Truncated index.** If the builder cuts the index (over `index_views`, 500, or the 256 KB
`index_bytes` cap), it retains whole views newest date first. Within a date it keeps the build order
(mode, then stream), so the first views dropped are the oldest date's last ones. The retained views
are still listed in build order, and the cut is deterministic. It emits pages only for the views it
still lists, so the generation has no orphan pages and publishes. The index says `complete: false`,
`views_total` counts every view built and `views_omitted` the rest, and `overflow_reasons` names the
cap. The panel shows "INCOMPLETE index: N of M views omitted". The publisher stays strict: an index
whose reasons are not the index-level ones, or that omits views without a reason (or gives a reason
with nothing omitted), is refused like any other invalid generation, and the last good publication
stays live. The byte cap is checked with the counters at their widest, so an index needs about
50 bytes of headroom under the cap to stay complete. A cut so small that no view fits (views built,
none listed) is refused before any private or public write, so the last good publication stays live
and the cycle fails visibly. This is separate from empty sources: an empty archive still builds
complete views with no rows, which publish, and an index with no views built (`views_total` 0)
remains valid. The pilot's 7 days × 9 views = 63 views is far below either cap.

## Storage

| Store | Holds | Readers |
|---|---|---|
| private | `news/v1/runs/**` (immutable raw history), `news/v1/index/**`, `news/v1/latest.json`, `news/v1/_writer/lease.json`, `news/v1/publish/**` (head, commit records, state, stop marker, publish lease, snapshots, receipts) | the collector/publisher only |
| public | `news/v2/views/index.json`, `news/v2/views/p/<gen>.<n>/<mode>/<stream>/<date>/page-NNNN.json` (legacy `news/v1/views/**` objects, if any exist, are left untouched and unread) | the site: same-origin `/browser-data/` proxy and direct public-R2 path |

The public store wrapper refuses every key except the exact view-key rule the panel and proxy use.
A test pins that the Python rule matches the panel's `isNewsViewKey`. The two stores must be
distinct buckets (or distinct non-nested directories locally), configured through separate
`NEWS_PRIVATE_R2_*` and `NEWS_PUBLIC_R2_*` variables.

## Cadence and load (estimates)

- **Schedule:** `*/15 * * * *` UTC, one-shot, restart policy NEVER. A cycle that finds a lease held
  exits non-zero without writing, and the next tick retries.
- **Fetch:** 4 requests per cycle, with the accepted 180 s budget and the 4-worker bound.
- **Public writes:** each cycle writes a new generation (its pages under new keys, because
  `build_as_of` changes) and the index: about 64 PUTs and a read-back GET each, about 6.1k PUTs a
  day, plus deletions of expired generations (once the clock bound is verified). Re-publishing the
  current build writes nothing. The pilot build reserves headroom so qualified pages and the index
  stay within the accepted 256 KB caps.
- **Private reads (history checkpoint).** This reduces object GETs; it does not make a cycle's work
  constant. Without the checkpoint, every cycle verified all committed
  runs three times (collect, the index rebuild and loading for views). That is 4 objects per run, so
  about 1.2k GETs per cycle at 96 runs, rising to about 8k by day 7. With the checkpoint (see
  `README.md`, "History checkpoint"), a cycle makes a constant 26 private GETs in the fixture
  measurement, plus 4 per run it has not seen yet. Each history read is one listing plus the head
  and body. Once a day the writer replays everything (`FULL_VERIFY_S`), which costs one full-replay
  cycle. The body's bytes still grow with history, because it holds every record. It is about the
  size of the compressed archive (the raw-archive estimate below is about 1 MB a day, so about 7 MB
  at day 7; unmeasured at live item counts), and it is read about 3 times and written once per
  cycle, so bytes transferred and the memory to hold it grow with history. The run listing also
  grows by 4 keys a run, and R2 returns at most 1,000 keys per list page, so list requests grow too
  (about one more per 250 runs, roughly every 2.6 days). Measured counts are in
  `tests/test_news_checkpoint.py` (`ReadCounts`); they are local fixture counts, not a validated
  capacity limit.

## Retention

- **Raw archive:** kept, immutable. It is the research record. Size is roughly 1 MB a day at this
  source count (estimate). Review at the end of the pilot.
- **Public views:** the live generation (the last 7 UTC dates) plus reader-protected generations for
  at least 45 min after they are replaced (see "Generations"); bounded by the 6,300-page cap.
- **Snapshots:** the last 3 published generations, in the private store. The live generation and its
  predecessor are never pruned.
- **Receipts:** one per publish, restore and withdrawal, kept for the pilot.

## Ownership

| Role | Who | What |
|---|---|---|
| Activation, buckets, credentials, scheduler service | Owen | provider changes; approves the config flip |
| Code, operating checks, incident first look | Jayden | reads `publish-status`, runs stop/restore/withdraw when asked |
| Review | Chej | review before activation and after the first live day |

## Failure visibility

`python -m market_data.news publish-status` returns JSON. It exits 1 when any of these problems is
present:

- `last_publication_failed`: the stage (pages, index, cleanup, state) and the error are in `last_failure`.
- `publication_interrupted`: a generation was started and not committed.
- `published_views_stale`: the last publication is older than 2 × 15 min + 180 s and the pilot is not stopped.
- `public_index_differs_from_state`: the live index is not the one the publisher committed.
- `never_published`: no generation has ever been committed, and the pilot is neither stopped nor withdrawn.
- `publication_blocked`: the live index could not be matched to a prepared commit, or the head is
  unreadable; nothing was written or deleted. An operator settles it.
- `publication_paused`: the page cap, or a withdrawal in progress, stopped admission (cleanup still runs).
- `open_index_commits`: one or more prepared index writes have an unknown outcome and may still land
  (listed in `open_commits`). Not a pause. They close when the next index write lands.
- `withdrawal_unconfirmed`: pages are deleted but no tombstone is confirmed live (see "Withdrawal").
- `head_unreadable`: `head.json` is not a readable head record.
- `expiry_blocked_clock_bound_unverified`: a replaced generation is past its grace on the producer
  clock, but expiry deletion is refused until the store-clock bound is verified.
- `unknown_public_generations`: public pages with no head record (retained, never deleted, counted against the cap).
- `page_uploads_unsettled`: an incarnation has a page-upload attempt with no recorded answer (its
  `uploads_unsettled` in `generations`). A PUT may still land; the incarnation keeps its full
  reservation and is never released. Not a pause by itself. Nothing in code clears it.
- `live_index_unmatched`: the live index matches no prepared commit in full (nonce, bytes, every page).
- `stale_writer_lease` / `stale_publish_lease`: an expired lease left by a process that did not exit
  cleanly. The `leases` field shows the holder and expiry. An unexpired lease is listed but is not a
  problem by itself, because a cycle may be running.

It also reports leftover non-view keys under the public prefix, the stop marker and retained snapshots.

A generation refused by validation writes nothing, not even `last_failure`, so the old publication
and the publisher state stay byte-identical. The cycle exits non-zero with the reason (for example
`page.rows[0].instruments is not an object`), and `publish-status` reports `published_views_stale`
once the last publication is older than the threshold above.

Readers can see failures too. Every page shows `build_as_of`, and collection failures are part of
the published coverage: failed sources, missed intervals and partial runs (accepted M3/M4a semantics).

A failed publication never leaves the live index pointing at a missing page, and never mixes builds:
pages of the new incarnation are unreachable until its index is written, and pages are never
rewritten in place. Leftover pages of a never-offered incarnation are deleted by the next run. The one
exception is stated under "Withdrawal": an unconfirmed withdrawal can leave an index naming deleted pages.

## Stop, restore, roll back

| Action | Command | Effect |
|---|---|---|
| Stop | `publish-stop --reason "..."` | Cycles no-op (exit 0). Views stay as last published and age visibly. |
| Resume | `publish-resume` | The next tick collects and publishes normally. |
| Roll back views | `publish-stop`, then `publish-restore --gen <gen_id>` | `<gen_id>` is the logical generation. Reuses its still-pinned incarnation (only a new index, fresh nonce) or writes a fresh incarnation from the snapshot, counted against the cap. The displaced generation's retirement starts when the restore is observed live. It stays in place until resume. |
| Open index commit | Usually none: the next cycle's index write closes it. To close it on demand: `publish-reseal` | An index write with an unknown outcome stays open and may land while the index it was conditioned on is live. There is no timer and no override. `publish-reseal` rewrites the live index with a fresh nonce; when that lands, every open commit is dead. |
| Take views down | `publish-stop`, then `publish-withdraw` (repeat while it reports `withdrawal_unconfirmed`) | Writes a tombstone over `index.json` (the key is never deleted), so the panel shows "News views are not currently published", and deletes every page at once (no grace). Unconfirmed until a tombstone is read back live; until then an index naming deleted pages may be served, with no time bound. Copies already cached by browsers or intermediaries, and downloads, cannot be recalled. Snapshots and the raw archive are kept. |
| Code rollback | redeploy the previous revision | The raw archive is append-only and versioned. Views are rebuilt on the next cycle. |

`gen_id` values are listed in `publish-status` (`snapshots`) and in receipts.

### Recovering from an abandoned lease

A cycle or publisher that is killed (OOM, redeploy, host restart) leaves its lease behind. Every
later cycle then exits non-zero with "writer lease held by …", and `publish-status` reports
`publication_interrupted` or `never_published`. After the lease expires (15 minutes), it also
reports `stale_writer_lease` or `stale_publish_lease`. Leases are never taken over automatically.

1. Run `publish-status` and read `leases`: note the holder (host:pid) and `expires_at`.
2. Confirm that holder is not running: no scheduler execution in progress, and no local process.
3. After `expires_at`, run one cycle with `pilot-cycle --break-stale-lease`. The same flag exists
   on `publish-restore` and `publish-withdraw`. An unexpired lease is still refused.
4. Run `publish-status` again. It should be healthy, with `recovered_from` in the cycle output and
   receipt. The writer side also recovers any spooled run, reusing its run id.

## Before a live start (decisions needed)

1. **Private bucket.** The raw archive must not live in a publicly readable bucket. The public R2
   origin the site reads (`pub-…r2.dev`) appears to expose the whole bucket it fronts. That is an
   assumption to verify, but if it holds, `news/v1/runs/**` must go elsewhere. This needs a new
   private bucket plus credentials.
2. **Public bucket and credentials.** Use the bucket behind `/browser-data/` and the public origin.
   The credentials should be scoped to `news/v2/views/` if the provider supports prefix-scoped
   tokens. Otherwise they are bucket-wide, and the code-level key guard is the only fence.
3. **Scheduler service.** A new Railway cron service, or other runner, with the cron above. Its
   watch paths must not overlap the collectors' (`market_data/news/**` is not watched by them today;
   see #225). Its variables are `NEWS_ALLOW_BUCKET_WRITES=1` plus the two prefixed R2 sets.
   `NEWS_GIT_REVISION` is optional.
4. **Pilot length.** Up to 7 days, as before. The history checkpoint cuts the per-cycle object GETs
   (see "Cadence and load"), but checkpoint bytes and memory, the run listing and R2 list pagination
   still grow with history, and none of this is measured at live volumes. Any longer pilot is a
   separate decision.
5. **Release of the site side.** Done 2026-11-14 at `55db036`: API Worker through `deploy.yml`, then
   assets through `deploy-staged.yml` (see `RELEASE_PLAN.md` section 5).

## Generation-specific page keys

Implemented locally in G3 and re-keyed in G6 (see "Generations and settlement"); inactive like the rest
of the pilot and never run. The fixed-key mixed-build limit no longer applies: a reader following one
index sees one build.

## Release and rollback plan

The G6-era list that stood here is superseded:
- G7 (`b02d664`) moved `deploy/news-pilot.json` to `storage.public.prefix` `news/v2/views/`. It validates and stays inactive.
- G7 bumped the `site/index.html` stamps, and R1/R2 and the corrections bumped them again.

The candidate, the remaining landing changes, the settled policy, the provider prerequisites, the
exact activation config, the activation order and the stop/withdraw/restore/rollback procedures are
in `RELEASE_PLAN.md` (R4 draft, preparation only).
