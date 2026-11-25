# One Clock implementation and extension contract

Status: shared clock, offline authoring/replay, durable observer storage and account-scoped public observer service implemented. Release requires the reviewed migrations and separate API/assets procedures below. Inventory baseline: master
`dba3304`, November 21, 2026. Repository findings below do not establish live
deployment, archive completeness or a source's historical availability.

## Eight planned PRs

| PR | Deliverable and acceptance | Dependencies / release |
| --- | --- | --- |
| 1 | Shared source envelope, availability selector, cursor lifecycle, adapter conformance suite, initial source inventory. Late publication, correction, rewind and stale-response tests. | This foundation; dormant asset modules, no production switch. |
| 2 | Editable versioned Event Builder, two materially different event classes, Results and linked session replay. Fix every as-of count including bullish duration. Portable rules, save/reopen, synthetic isolation and outcome-boundary checks. | 1; offline asset preview first. |
| 3 | Durable observer, immutable rules/events/outcomes, transactional checkpoints and idempotent restart. Same evaluator as replay. Controlled recording through live and replay yields matching identities/outcomes. | 1–2; API/D1 and immutable R2 evidence, no new capture owner or settlement cron. |
| 4 | Chain/Historical and raw/adaptive/anchored skew adapters and reader switches. Source and request-count parity, old-reader rollback. | 1; reuse existing payloads, exact-revision asset release. |
| 5 | All-bot historical decisions and fills, allowlisted projection, partial fills, opening positions and truncation. Test more than 100 fills and overnight carry. | 1,3; separate private export/API effects from browser changes. |
| 6 | Public evidence readers: Order Flow/gamma, news, Squeeze, credit, scanner and other eligible feeds. Versioned adapter fixtures and per-source coverage. | 1,3; qualified readers may release separately; no new provider connection. |
| 7 | Private journal and Trainer. Enforce cutoff server-side and preserve blind-session clock, reveal, cache/auth separation and original execution semantics. | 1,3; dedicated privacy and actual response checks before activation. |
| 8 | Complete class aggregates, filters, distributions, comparisons, and remove duplicate clocks only after last-consumer inventory and rollback. | Qualified integrations; no minimum event count or elapsed-time gate. |

Eight is the planning baseline, not a reason to combine incompatible service
releases. Split a PR if review or release boundaries require it, recording the
reason. Each integration includes its own compatibility and rollback checks;
PR 8 must not become a gate holding already qualified readers. Missing archive
evidence blocks that source's historical lane, not the usable instrument.

## Account observation and retained public ingestion

`/api/one-clock/research` derives its namespace from the authenticated human
account. GET reopens immutable definitions or reads one session/instrument as of
an explicit cutoff. POST saves a version or activates/pauses it with server times,
origin checks and the existing write fence. Neither arbitrary observations nor
private sources can be submitted. Activation intervals are retained separately;
pausing stops new triggers while existing outcomes continue. Switching a version
ends the old interval. This access does not authorize journal, credit or Kalshi.

The initial source registry supports recorded underlying prices and published
reference skew from QQQ/SPY/IBIT chain artifacts. The source descriptor supplies
its instruments, fields, version, owner, normalizer and revision policy; the
Builder obtains fields from this registry. New sources do not create an account-
specific reader, cutoff implementation or timer. Squeeze and remaining inputs
are not yet enabled for this observer; their separate replay readers remain.

The existing 15-minute API invocation runs bounded, shared ingestion before
account evaluation. It rotates two account/session/instrument jobs and up to four
rule versions per job, with an aggregate 800 evaluation-statement budget and a
single-rule retry when a group exceeds that budget. Failed rules retain an
as-of attempt receipt and advance the rotation; other rules can continue. A
depleted invocation budget defers work without recording a false rule failure. At most two public scope
reads occur, each with two 1,000-object metadata pages, 20 artifact pairs,
4 MiB of bodies and 1 MiB per object. Accounts in the same scope reuse the read.
Initial/older unprocessed evidence is labelled backfill, never upgraded by retry.
Catch-up and tenant rotation add delay; this is periodic archive observation,
not a claim of instant alerts or a complete source event stream.

Each fixed-day metadata rescan discovers late keys before an earlier cursor.
Both CSV and sidecar ETag/upload times identify a retained artifact revision;
stable source record IDs and increasing observer-retained revisions support
corrections and reversion to previously seen bytes. The two source clocks,
publication and actual observer receipt, remain separate. SQL generation checks
commit observations, coverage and version progress atomically. Processing time
never rewinds when old evidence arrives. Rule evaluation starts only after all
configured source pages in that scope are caught up; a failed source does not
stall unrelated scopes. Sources and rules are bounded at 2,000 retained artifact
versions and 10,000 normalized records per session, 100 rule versions per account,
1,000 activation intervals per account, 100 simultaneously active accounts and
1,000 active rule versions. Exceeding a bound reports failure without a false
completion checkpoint.

The latest two NYSE sessions are revisited. Jobs recover directly from active
definitions after missed sessions. Activation gates rising-edge admission before
the once-per-session quota; anchors and condition state retain eligible session
history, so activation itself does not manufacture an edge. Older sessions remain provisional:
arbitrarily late changes and versions overwritten between polls cannot be
recovered automatically. Existing retained versions are preserved. A native,
immutable publication outbox from each existing publisher remains the durable
successor for complete, low-latency handoff; no second capture owner is added.
The calendar is an explicit NYSE policy exported from the existing producer
library for 2024–2030, with early closes. Unknown dates fail closed, and CI checks
calendar parity. Updating its policy uses `scripts/one-clock-calendar.py`.

The Builder's **Save to account**, **Observe saved version**, and **Pause
observation** controls are distinct from local synthetic fixture saves.
**Observed Results** follows the shared cursor, includes observer receipt gating,
retains trigger/endpoint collection lineage, and exposes coverage independently
from zero events. Per-rule attempts distinguish evaluated, failed and not-yet-
evaluated states at the selected cutoff. Live refresh retains the committed
session and instrument; draft edits apply only through Read cutoff or Live.
Inspecting a receipt does not manufacture a source replay at an
earlier time. Full historical replay linkage, rich aggregate filters/comparisons,
remaining source migrations and native publication handoff are still open.

Release: independently review and apply only `0004_one_clock.sql` then
`0010_one_clock_ingestion.sql`; deploy API with `ONE_CLOCK_OBSERVER=enabled`, then
the staged assets. With no activated definitions, the service performs no source
reads. Roll back to the prior separated API/assets or disable the observer flag;
retain new tables and evidence. Existing producers, gateway identities, account
execution and settlement ownership are unchanged. No KV operations.

`node scripts/one-clock-observer-preview.mjs` runs the actual account APIs against
an in-memory store and synthetic records on an isolated loopback port. Its fixed
fixture identity exists only in that harness. Browser checks exercised account
save/activate/pause and two recorded fixture occurrences rewinding to one pending
occurrence, then zero before the trigger receipt. This is not live source proof.

## One adapter, shared selection

The offline instrument is `/one-clock.html`. Its bundled SYN-1 records are
hand-authored engineering fixtures, never historical or prospective market
evidence. The blank form supports multiple AND/OR conditions, explicit rising
or first-per-session transitions, missing-input behavior, and zero or multiple
numeric outcomes. Browser saves create immutable versions; export retains the
portable rule. Results switch classes and versions and reconcile counts with
the ledger at the cursor. A separate explicit full-session view is retrospective.
The linked Nov. 9 prototype remains a separately reviewed reconstruction.

`one-clock-events.js` evaluates observations in availability order using one
incremental source index. Outcome endpoints separately qualify by event time,
availability, tolerance and session close. A late eligible endpoint can change a
missing outcome to measured without changing the trigger identity; the durable
observer must preserve that as an attributed revision. The same pure evaluator
accepts an entire archive or only records that have arrived. No strategy or order
execution runs from this page. Account persistence and periodic public-source
activation are described above; full categorical/comparison statistics and
remaining reader migrations are subsequent slices.

The observer storage in `backend/one-clock-observer.js` reuses this evaluator.
It atomically commits observations, event/outcome history and source checkpoints
to D1 with optimistic generation protection and exact batch-id retry receipts.
The additive migration and trusted boundary are described in the
[backend guide](../backend/README.md#one-clock-observer-storage-not-activated).
It has no client ingestion route. The account service uses shared public
ingestion and activation windows. Controlled recordings test parity and restart;
production activation and natural event/outcome receipts need their own release
evidence and are not inferred from fixture success.

`site/shared/one-clock.js` has no network, storage or timer. The application
owns one clock for a view and registers source adapters once. A live refresh
advances its cutoff using an injected `now`; replay holds its explicit cutoff.
The existing fetch cadence drives it. Consumers share `clock.read(source)`
within a generation, rather than starting extra polling loops.

An adapter declares `id`, `version`, `owner`, `maxAgeMs`, and
`read({session,instrument,mode,origin,cutoff,generation,signal})`. It returns
normalized observations. Adapter readers remain responsible for access control,
bounded source requests, digest verification, actual coverage and safe payload
projection. Private records must be filtered on the server before transmission;
the browser selector is not an authorization boundary. Blind Trainer sessions
must not receive full future archives or globally scrubbable controls.

Every observation has:

| Field | Meaning |
| --- | --- |
| `source`, `id`, `revision` | Stable source identity, immutable record identity and positive revision. Duplicate delivery is harmless; differing contents under the same identity/revision fail. |
| `session`, `instrument` | Source-qualified session and instrument, not inferred from the currently open page. Calendars/session bounds belong to the adapter. |
| `eventAt` | Observation time in epoch milliseconds or an offset-qualified timestamp. A snapshot-construction fallback must be explicitly labelled. |
| `availableAt` | Earliest proven availability in the adapter's declared view. Unknown is null, never substituted with download time or a filename. |
| `origin` | Exactly one of recorded, reconstructed or synthetic. Selection uses one explicit cohort. |
| `evidence` | Stable source artifact identity. It is not necessarily public or a navigable URL. |
| `value` | Allowlisted observed content; null represents unavailable/retracted content. |
| `details` | Optional allowlisted provenance/coverage specific to this record, without future summaries. |

The selector requires both event and availability times at or before the cutoff,
then picks the highest eligible revision for each identity. Rewind restores an
earlier eligible revision. Missing publication is unavailable even in a
reconstruction: the reconstruction must explicitly supply its assumed timestamp
and origin. Age uses observation time; publication age is shown separately.
Retractions do not resurrect prior values. Session/instrument/origin changes or
entry into replay stop owned live subscriptions and abort old requests; a
transport that ignores abort still cannot publish a stale response.

All counts, labels, rows, charts and aggregates must derive from the selected
rows, never from the raw full-session array. Full-session analysis is an explicit
separate view. No later fill, closure, bullish-duration total or outcome may leak
into the replay view. An outcome's own measurement and availability times must
both qualify independently of its trigger.

## First adapter

`one-clock-market-readings.js` consumes the output of
[`research/market_readings_series.py`](../research/market_readings_series.py).
That existing reader verifies canonical digests and preserves the legacy schema;
the adapter does not duplicate Python's serialization/hash implementation. It
filters mismatched readings, unknown schemas, invalid digests and session keys.
It requires the containing snapshot's publication and the reading's publication
when present. Source observation and construction-time fallback stay distinct.
Legacy records stay labelled. This PR does not publish a new export or claim
that the Nov. 9 cache is a complete observed history.

## Adding a stream

1. Add its source-native normalization and bounded loader under the owning
   component. Reuse existing capture and archive owners; never create a second
   feed solely for One Clock.
2. Register the adapter with explicit version, freshness and session policy.
   Preserve event, first-seen, publication, import and revision times separately
   where the source provides them. Do not promote an inferred time to fact.
3. Run `tests/helpers/one_clock_conformance.mjs` against normalized fixtures;
   test the source-specific normalizer too. Include before/at/after publication,
   unknown time, duplicate delivery, correction, rewind, missing/partial data,
   session/instrument isolation and failed transport. An immutable source without
   revisions still needs a fixture proving the common revision selector.
4. Connect consumers to the shared result, compare against their existing reader,
   check request counts, and retain a reversible switch until parity and rollback
   pass. Record source coverage and the last consumer before retiring old logic.

New streams require one mapping and evidence checks. They should not require
another cutoff algorithm, age timer, replay loop, cancellation mechanism or
outcome scorer. Explicit source differences remain visible instead of being
hidden behind a purported universal timestamp.

## Source inventory and migration order

| Sources / readers | Existing owner and evidence | Availability / missing evidence / recovery | Planned successor |
| --- | --- | --- | --- |
| Live chains, prices, human header, daily Historical | `market_data/live_market`, `site/js/data-fetch-cycle.js`, `historical-panel.js`; intraday CSV/sidecars, daily manifest/CSV | Sidecar publication can prove a reading became available; CSV filename alone cannot. Daily close is eligible after publication. Reuse digest-checked export; old CSV is labelled reconstruction. | PR 4; keep daily lookup and existing live cadence. |
| QQQ/SPY reference skew; IBIT adaptive skew; anchored bot skew | `site/shared/oi-skew.js`, `reference-oi-skew-meters.js`, `crassus/oi_skew_publisher.py` | Published reference, browser adaptive calculation and recorded bot decision are distinct versions. Actual anchor and decision require runner evidence. Test resets, missing pairs and stale inputs. | PR 4; never replace actual decisions with a reconstruction. |
| Every bot's decisions, declines, failures and fills | `crassus/audit.py`, `archive.py`, `backend/bots.js`; roster is runtime membership, not the static strategy list | Private decision ledger and fill join; current `session_fills` is latest 100 per bot/current NY day. Need allowlisted historical export with truncation and opening positions. Absent export is unavailable, not flat/no-trade. | PR 5; one schema across roster, no per-bot synchronization. |
| Order Flow, gamma and linked strike views | `site/order-flow.html`, `research/oa241/build_replay.py`, existing Tradier artifacts | Execution, first observation and publication differ; bounded live tail is not whole-session coverage. Preserve opening coverage and late prints. | PR 6; cursor supplies frame and age, old model is parity reference. |
| News and existing hypotheses | `market_data/news/reader.py`, `join.py`, `research.py` | Reuse cutoff/revision/backward-price semantics and author/import/commit history. Free text needs an explicit measurable rule to score. | PR 6; retain annotation IDs and published views. |
| Squeeze shortlist | Existing scheduled latest-success/latest-attempt artifacts and dated runs | Needs publication index; newest/later outcome never fills an earlier hole. Nov. 9 coverage unproven in prior review. Use labelled fixture or another eligible session. | PR 6; preserve existing scanner/MCP consumers. |
| Credit tape | Existing landed credit API/owner gate and capture exports; recheck current owner guide | Execution and publication timestamps plus historical export coverage must qualify. No new capture connection or weakened access gate. | PR 6; coordinate with current source owner. |
| Scanner and one-off studies | `market_data/options_scanner/returns.py`, `windows_live.py`, `windows_replay.py` | Qualified endpoint/tolerance/session policies; active versus final and best-later-exit remain distinct. Preserve frozen counterfactual policy versions. | PR 6/8; replace repeated selection/scoring only after parity. |
| Private journal and context | `backend/trade-journal.js`, `trade-journal-context.js`, `site/js/trade-journal-loads.js` | Dedicated passkey; full-day caches and sampling can reveal future fills/closures. Filter before response; separate replay and retrospective cache keys. | PR 7; preserve account/financial ledger ownership. |
| Trainer and simulations | `research/options_replay/engine.py`, `crassus/replay.py` | Forward-only hidden dates, own fills/fees/reveal/journal; original synthetic/event-tape/historical origin retained. | PR 7; share archive eligibility underneath existing clock constraints. |
| Prediction-market evidence / Strategy Archive | Overlapping PR #299 and issue #298 at baseline | Integrate landed contracts when available, retaining exact source/strategy version, population and evaluation plan. Their existence is not One Clock measurement. | PR 6/8; no prerequisite for other sources. |

For the bot inventory, enumerate current membership at the authorized source when
building PR 5, then join every member to the same decision/fill contract. Static
strategy registration includes dormant strategies and cannot prove live roster
or archive coverage. A new bot using the existing audit format should require
no new clock code. Persist per-source coverage observations with their own
availability time; do not attach today's completeness conclusion to an earlier
cursor.

## Verification and release

The main dashboard shares one committed selection across Live, Historical and
Automated. Choose Replay in the Session clock control, set the session,
instrument and New York time, and apply it. Navigating between tabs preserves
the instant. Live displays the eligible archived chain. Automated displays
public fill receipts and carried inventory, keeping live balances, marks and
paper-trading controls outside the replay. Historical combines the sources
and links to the private journal at the same instant. A selected instant from
Event Results opens the same dashboard clock with millisecond precision.

Returning to Live resumes the current roster on the Automated tab. The
QQQ/SPY/IBIT meters use one shared age tick; their acquisition schedules and
the account quote service still own their respective live fetches. Dashboard
readers can subscribe to the window event `one-clock-selection`, with the
immutable selection in `event.detail`, and use
`window.oneClockDashboard.snapshot()` on mount. A live-mode notification
releases the fixed cutoff.

The Historical tab now has an explicit **One Clock · as of cursor** reader,
alongside the original daily retrospective view. `/api/one-clock/chain` accepts
only session, QQQ/SPY/IBIT instrument and epoch-millisecond cutoff. It derives
fixed intraday keys, reads at most two 1,000-object index pages and twelve
candidate sidecars (at most twelve CSVs), with 1 MiB/object, 4 MiB total and
2,000 CSV-row bounds. An incomplete index is unavailable, never sampled as
complete. Results contain only allowlisted market fields. No storage writes,
account access, new capture, latest-snapshot fallback or cron is introduced.

The returned current CSV and sidecar versions must both have been uploaded by
the cursor, and the sidecar must report a published snapshot. Their ETags bind
the evidence identity; source publication and current archive-revision times
remain separate. Replaced objects cannot reconstruct earlier bytes. A missing
sidecar remains unavailable. The readings digest is retained as provenance,
not claimed verified; the sidecar has no CSV-byte digest. Published reference
skew and reconstructed IBIT adaptive skew use the same eligible parent snapshot.
Historical anchored bot state remains explicitly unavailable because the runner
projection is latest-only, not a historical publication ledger.

This is a read-only Historical consumer: it never writes `lastData`, quote
interests, paper state or execution caches. Existing live capture/execution
paths keep their cadence; their display synchronization remains a subsequent
integration check, not an implication of adding this replay reader. Selecting
the daily view cancels the pending replay response and restores the original
reader. API deployment must precede asset activation; an old API returns an
explicit unavailable/error state, never substituted live data. Rollback can
select the daily view or restore the previous separated asset artifact; the
additive read-only endpoint may remain during an asset rollback.

`node --experimental-sqlite scripts/one-clock-preview.mjs` starts a read-only
loopback fixture preview on a free port, prints its URL, and denies mutations.
Its Nov. 9 synthetic QQQ/SPY/IBIT snapshots become eligible at 09:30:01.500 and
09:35:01.500 New York. It reuses the preview gateway/CSP and cannot access
production capture. Browser QA verified eligible chain/reference skew, rewind
before availability, reconstructed IBIT adaptive output and daily-view rollback.
The existing optional Playwright test was unavailable locally; use CUA for
browser verification. The standard loopback test requires its own free port8787.

Run `node --test tests/one_clock*.test.mjs`, the existing no-runtime-KV guard,
and `python tests/test_changed_paths.py`; integrations also run their owning
reader suites. The foundation tests require no network or storage bindings.
Observer writes will require D1 transactional restart tests and KV tripwires.
Zero Workers KV operations applies to every path, including retry/backfill and
rollback. Follow [SITE_RELEASES](SITE_RELEASES.md) and
[RELEASE_BOUNDARIES](RELEASE_BOUNDARIES.md). A merge is not deployment; a release
is not proof of natural market observations. Zero events is a valid launch.

## Bot fill reader (PR 5, consumer slice)

The Historical One Clock view reads a selected public PAPER bot at the same
instrument/session/cursor as the chain. The names-only roster is explicitly
current membership; it excludes the same hidden strategies as `/api/bots` and
never sends current balances, closures, fills, aliases or strategy state to replay.
The adapter pages the complete retained D1 trade ledger, not the roster's last
100 session fills. Each page examines at most 40 terminal R2 receipts (64 KiB
each), from a ledger bounded to 10,000 entries; no listing or writes. Future
event rows are excluded before pagination. Continuations bind to the eligible
ledger version, so edits require a clean reload and future appends do not alter
an earlier replay. R2 terminal upload times gate visibility; bodies uploaded
after the cursor are not read. Receipt owner and the complete public fill
projection must match the D1 entry. Public identities are hashes, not request IDs.

Prior-session entries retain their original event session as carry evidence in
the requested replay scope. Opening quantity means known at New York midnight;
current quantity includes only cursor-visible receipts. Quantity inventory has
no inferred marks, cash, fees, strategy attribution or P&L. Missing/oversized/
mismatched receipts, including legacy settlements without receipts, invalidate
both inventory totals; visible fills remain inspectable. Page/budget failures
do not display partial totals. Current record closure is not historical closure.

The November 21 public roster contained 34 accounts across smoke/Phelps,
Canopus, Looking Glass, QQQ/IBIT/SPY OI variants, max-pain, momentum, put/call,
QQQ gated strangle, SPY SMA200 and Trump variants. This adapter uses the roster
contract rather than an embedded list. Decision, no-trade, failure and historical
strategy-assignment publication remain **unavailable**, not inferred from fills.
The private Crassus decision archive is in a separate bucket; the latest public
OI projection drops intermediate decisions. A durable generic producer handoff
and observer activation remain open, separately from this read-only consumer.

Rollback: choose Daily retrospective or leave the bot unselected; existing live
Automated and execution paths are untouched. Release API before assets through
the exact-revision gates. `one_clock_bots.test.mjs` covers 105 fills, overnight
carry, partial exits, publication rewind, gaps, privacy and pagination races.
The read-only preview offers `synthetic_bot` at Nov 9 09:35 (104 session fills,
10 opening contracts and 109 current contracts); rewind before 09:30 removes
session fills while retaining the known carry.

## Public evidence: Squeeze publication reader (PR 6, first slice)

Historical One Clock accepts any ticker; chain coverage remains independently
limited to QQQ/SPY/IBIT. The registered `shortlist` adapter shares the committed
cursor, request cancellation and immutable envelope. Its fields are published
top-ten membership/rank and three producer scores. Missing evidence is not a
negative membership observation. The same event evaluator tests a first-per-
session appearance rule, separate from the skew class.

`/api/one-clock/squeeze` reads only the existing scheduled producer namespace.
It requires an immutable finished publication receipt whose `latest` result is
`advanced`, matching run/commit identities, a verified manifest hash and verified
results bytes/hash. It applies the producer's comparison-eligible, rank-sorted
top-ten policy. Execution completion or an archive-only/superseded run cannot
establish appearance. Availability conservatively includes the terminal receipt
and all retained object upload times. A later overwritten object cannot supply
earlier evidence. The projection contains no future outcomes or arbitrary report
fields; full input archives are not sent to the browser.

Bounds: two 1,000-object listing pages, 20 finished-publication candidates per
selected day/cursor, 1 MiB per object, 4 MiB total bodies. Incomplete listing,
unknown upload time, unmatched publication, hash failure or missing qualifying
object makes the source unavailable; it does not select an older run as current.
No KV, storage writes, extra producer, provider connection or scheduling change.
The original Squeeze tab remains the independent current reader/rollback.

This slice does not claim Nov 9 real Squeeze coverage. Its fixture has ABC first
published at 09:30:01.500 New York; earlier replay is unavailable, QQQ has a
qualified absence after publication, and the chain still works independently.
The observation's event time is the conservative proven publication-state time;
scan start/finish are retained separately. State advances preserve the producer's
exact `(started_at, run_id)` ordering, including overlapping finishes and delayed
terminal receipts. Started-only or failed attempts may conceal a pointer advance
and therefore make coverage unavailable; an unknown terminal status also fails.

The two listing pages are a **lifetime namespace bound**, not a daily partition.
This first reader deliberately fails closed beyond that bound. Before claiming
automatic source scaling, add the generic durable publication catalog to observer
ingestion: persist verified source identity/revision, publication time and session,
resume listing with transactional checkpoints, and query the session index here.
New source descriptors must reuse that catalog rather than raise list limits or
require manual daily indexes. This remains open integration work, not a finished
scalability claim. Interrupted publisher attempts likewise need verified recovery
evidence; do not delete them or infer successful publication during repair.

News, Order Flow/gamma, credit and scanner readers remain separate open source
integrations. News must reuse its collector/archive revision and classification
rules; its publication mode is expressly retrospective. Credit must retain the
owner gate and cannot reuse the public market projection route. Scanner active
snapshots and finalized best-later-exit reports are distinct source products.

## Automatic publication catalog

`one-clock-source-catalog.js` is the shared metadata ingestion/checkpoint service.
Descriptors in `one-clock-catalog-sources.js` supply one source id, version,
owner, fixed R2 prefix and native key-to-group/kind mapping (one each of
`started`, `finished`, `failed` per publication attempt). The service performs
one 100-object page per registered source per existing API cron invocation.
It reads metadata only and never writes R2 or contacts a market provider.
Entries and continuation commit in one D1 transaction; generation conflicts
roll back the entire page. Failed pages retry their checkpoint; three consecutive
listing failures restart the scan automatically while retaining all catalog
records and the last completed coverage watermark. Completed scans repeat, so
new UUID keys sorting before a prior cursor are discovered without manual indexes.

The watermark is the **start** of the last completed full scan. A query after
that watermark is `catalog-catching-up`, not a false complete history. Queries
use a bounded upload-time range and an indexed interval lookup to select the
requested day's attempts plus older unresolved/failed attempts, including
finished receipts without a start and reversed receipt order. Gap intervals are
decomposed into disjoint dyadic millisecond buckets; a cutoff uses 54 bucket
lookups without scanning completed history or future attempts. Each affected
group replaces its spans inside the same transaction as the entries/checkpoint,
using one JSON insert (not one statement per span). At most 301 statements and
212 spans per group are needed per 100-object page. Completed prior days do not consume today's
20-group read budget. The existing content verifier still reads exact immutable
objects, checks hashes, and gates every byte by publication time; indexing never
upgrades a missing publication to fact. Source gaps remain explicit.

The API flag `ONE_CLOCK_CATALOG=enabled` selects this index. Apply only the
reviewed additive `0005_one_clock_catalog.sql` before deploying that API revision;
it changes no account/journal tables, R2 artifacts, capture process or cron
expression. The existing cutover fence skips indexing as well as settlement.
Catalog failures are isolated from settlement. Roll back to the prior API
revision to restore the bounded direct reader; retain additive index tables.
Observer rules/events in `0004` are a separate activation and remain dormant.

Validation includes 2,004 lifetime objects with a two-object session query and
SQLite query-plan checks for indexed range/bucket searches, randomized
direct-qualification parity at every boundary before and after late receipts,
transaction rollback/restart, concurrent pages, unknown metadata, descriptor
version mismatch, stale coverage, and automatic continuation recovery. Source
indexing creates no event registrations or trading decisions. Additional sources
reuse this scheduler and transactional service; no source-specific timer or
manual daily index is part of the extension contract.

## Private journal recorded replay

Owen Trades occurrences can open the signed-in journal at one fill. The trading day and the Results evaluation cutoff travel separately, so a later import can qualify an earlier execution. The browser resolves the position and execution against the journal revision visible at the cutoff. It shows the supported underlying and exact option quotes sampled at two-minute intervals around the fill, within four minutes either side. Each sample uses the existing publication-qualified chain reader. Missing and stale snapshots leave gaps, and the Results cutoff bounds the latest eligible observation. The fragment carries the private identity within the browser. Ordinary journal links and full retrospective context remain available.

The journal retains its dedicated passkey authorization on every data route.
`cutoff` selects the latest private D1 payload committed by that instant before
any fill filtering, totals, context sampling or response. Additive migration
`0009_one_clock_journal.sql` seeds current payloads at migration time and captures
subsequent content/status changes transactionally with the existing importer.
It never backdates availability to a Notion edit or broker timestamp; old payloads
not previously retained are unavailable. There is no new sync timer or Notion write.

The private page registers one journal adapter with the shared clock. Its recorded
view excludes later executions, revisions, closure, fees and free-text retrospective
notes. Current and recorded projections share the same average-cost book calculation.
Combination legs project separately; an expiration without a retained intraday time
is applied only after its entire New York date, once the payload is available.
The selected journal date and as-of date are separate, so a prior day's expiry
can appear once known without assigning it the last fill's date. Unsupported
eligible revisions remain visible outside the financial totals.
Recorded context bypasses the full-session cache and sampling plan, reading only
the existing publication-qualified chain adapter at the selected cutoff. This is
one snapshot, not a complete premium path; missing exact contracts remain gaps.
For a later as-of date the reader keeps the original event-day prefix and gates
publication by the later cutoff; it does not substitute future-session quotes.
The explicit Full retrospective remains the rollback display. Sync is disabled
while viewing recorded history so its current summary cannot leak into that view.

Apply only reviewed migration 0009 before releasing this API. Rolling back the
API/assets leaves additive private history retained. It does not affect account
execution, passkeys, public bot identity, settlement or event-observer activation.
Trainer forward-only/blind-date integration is described below.
Validation uses synthetic private revisions, fees/partials/expiry/combinations,
auth-before-read and context-cache exclusion, response races and browser rewind.
No private production journal payload is needed for those checks.

## Trainer source eligibility

The Python binding in `research/options_replay/one_clock.py` implements the same
envelope and as-of revision contract as the JS core. Both consume
`tests/fixtures/one_clock_contract.json`; equal-time identity ordering is explicit
UTF-16 code-unit order rather than host locale. Trainer adapts each immutable pack
once, indexes latest eligible readings at publication boundaries in linear storage,
and keeps its own server clock, dwell, reset attempts, fees, ledger and reveal.
Board, charts, candles, volume and price marks use the same eligible source rows.
Publication delay never refreshes observation age or creates a tradable old quote.

Legacy archive/event-tape playback is explicitly reconstructed availability, with
the existing snapshot-boundary assumption visible. It is not evidence of historical
public availability. Simulation stays synthetic; explicit recorded sources with
missing availability remain unavailable. Source dates/keys and all future payloads
remain on the server until reveal. No global scrub control is added.

Trainer deployment is separate from API/assets: preserve both QQQ/SPY stores,
resume states and gateway identity, verify service activity, then update the reviewed
consumer files through their existing lifecycle. No producer restart, new feed,
archive write or rewrite of saved decision-state hashes belongs to this slice.


## Results across retained sessions

The authenticated `/api/one-clock/results` reader loads an explicit range of at
most 31 calendar days and one or all registered public instruments. Latest
retained event revisions, source coverage and rule attempts are gated by the
same cutoff. A result exceeding 10,000 occurrences or 4 MiB of retained payloads fails
explicitly before payload retrieval; the complete response is capped at 8 MiB.
No silent truncation or complete-statistics claim is made. Private namespaces are selected
from the session, never from query parameters. Historical revisions survive
later retractions. The separate synthetic fixture remains a separate cohort.

`site/shared/one-clock-results.js` projects one filtered occurrence table. Class,
version, instrument, collection, outcome state and text search affect that table
and every count/statistic together. Measurement choices include the rule-local
outcome and horizon; pooling requires the same source, field, measure, horizon,
tolerance and maximum age. Incompatible/absent measurements are explicitly
unscored. Numeric summaries use measured outcomes for mean, median and histogram;
categorical endpoints retain boolean/string/number types and use measured
outcomes for counts and rates. Pending/missing rows never enter a denominator.

Configured comparisons report occurrence-weighted A-minus-B mean differences
(or a chosen category-rate difference in percentage points). One influence sum
per session preserves covariance between groups sharing a session. The standard
error uses G/(G-1) times the sum of squared session influence differences. Both
groups need two measured sessions to estimate between-session uncertainty;
descriptive estimates are still available earlier. No significance verdict,
confidence interval or predictive claim is emitted. Independent sessions are an
assumption; few sessions, serial dependence and adaptive exploration remain
limitations. This is a direct session-cluster sandwich calculation, following
the clustering principle described by [Cameron and Miller (2015)](https://facultysites.dss.ucdavis.edu/research/Cameron_Miller_JHR_2015_February.pdf),
not an assertion that market sessions satisfy its assumptions.

Live refresh retains committed dates/instruments and current filters. Replay
links carry an exact millisecond trigger cursor to the read-only Historical
view, without passing archived data into trading globals. A source replay does
not pretend a later observer receipt existed at trigger time. Missing source
evidence is shown as unavailable. Roll back this Results API and asset slice
together to the compatible observer release; it adds no schema migration,
publisher, cron or KV operation.

The Order Flow tape selector keeps the current view as its default. Time & Sales, the QQQ price chart, the ten-second strike footprint, the mirrored premium ladder and the event feed share the displayed minute and contract filter. All optional views receive the whole collected strike universe. Zooming the main tape or profile changes its viewport. Inspect available prints opens the published live tail or archive through the selected cursor with the contract filter applied. Each optional view scrolls within its panel. The price chart fits the full minute across the viewport. Scroll vertically to browse five strikes at a time. Scroll horizontally through strike columns in the footprint, or vertically through strike rows in the mirrored ladder. Its header shows the visible and available strike ranges. When the live display cap shortens the minute, the summary shows the earliest retained print time. Ticket highlighting begins at $10,000 in premium. Archive rows without provider sequences retain their archive row number.

The November 23 recorded close freezes the public QQQ recent-print feed through its last recorded print (15:59:56.507 ET), including the session contract roster and the original sequences. The matching QQQ chain comes from the publication-qualified One Clock chain reader at that cutoff. The capture keeps only the chain fields the page consumes. Its 201 recorded strikes span $500–$900. The September 25 trade archive covers 16 contracts at eight strikes ($740–$747). The chart, footprint and ladder also show strikes from the chain snapshot at the replay cursor. At 09:31 ET that snapshot spans $709–$775. Strikes outside the trade archive have unavailable individual trade data.

### QQQ opposite-color candle breach

Choose **QQQ opposite-color candle breach** under **Study type** in Event Builder.
Save the version to your account and choose **Observe saved version**. The observer
uses its existing periodic schedule to fetch one shared QQQ regular-session
one-minute chart per requested session and retain finalized OHLC values. It
uses the first retained finalized version of each minute. The receipt can be
later than the bar close. Missing minutes break adjacency and leave exact
five-minute outcomes unknown. A positive change means movement in the breach
direction. Each eligible pair can be reconstructed from its retained inputs;
saved-study replay returns the trial ledger including non-events.

The three QQQ candle dates in **Replay dataset** use the raw bars from
research/qqq_candle_breach and a reconstructed bar-close cursor. Their original
publication times are unknown. Observed Results uses actual retained receipts.

## Mapped company headlines

In Event Builder choose **Mapped company headline**, choose a ticker, save the account definition and activate observation. The event is one archived story revision during a New York trading date. Premarket and after-hours revisions qualify. Its clock is the latest of the observation, archive and archive-mode event timestamps. Publication-mode headlines cannot trigger it. The observer reads the existing dedicated public News index and its bounded archive/all pages on demand. It can use the retained title in older generations. Redelivering the same item and revision leaves one occurrence.

The other input is the selected symbol's row in the existing price strip. Only rows with the correct provider instrument and symbol, a finite positive price, provider observation time, collector receipt and R2 upload time qualify. Dated daily fallbacks have no intraday observation time. Thirteen allowlisted companies and ETFs have a configured strip route. TSM, BP and SHEL support headline tracking only. A configured route can also lack timely samples. The latest eligible quote available by the story supplies the baseline; a baseline older than two minutes is labelled stale. Each return uses the earliest retained observation at or after its target, available by the cursor, including a following session for the one-day outcome. An endpoint observation at exactly 24 hours past the target qualifies; an observation even one millisecond later is outside the window. If the cutoff passes this deadline with no eligible retained quote, the outcome reads as missing, including for paused or expired study sessions. A later receipt may resolve the outcome only when its observation occurred inside the window. The observer samples its existing feed periodically, so intermediate quotes can be absent. Both legs retain their identities and timestamps.

Observed Results uses the observer's commit time. Replay uses original source availability within the evidence ultimately retained. Replaying a quote one millisecond before its R2 receipt withholds the return. On-target and delayed measurements have separate statistics. The delayed group shows actual elapsed time beside each return. Saved-study replay accepts a session and cutoff, and a single occurrence can be replayed from Results. No new cron, migration, price subscription or bucket writer is required.

### Inside the QQQ minute

Open the intraminute order-flow view from One Clock or the QQQ Order Flow page.
Inspecting a recorded QQQ occurrence links to its exact receipt cutoff and, for
a strike encounter, preselects that strike. The chart aligns call and put flow
with the underlying midpoint, bid/ask and optional trade range. Select a
100-millisecond interval or a one-second bin for the signed-contract imbalance,
subsequent one-second price response and tape. Use the receipt cutoff to replay
what had arrived by that instant. The bundled Nov 13 sample opens offline.
See [Tradier operations](../market_data/tradier/OPERATIONS.md#qqq-intraminute-order-flow)
for source coverage and live activation.

#### Alkalus flow classification (preliminary)

Each selected interval also carries a preliminary Alkalus classification —
golden time, working copper, or lead time — from the
[Preliminary Guideline — Alkalus](https://app.notion.com/p/3bb3c519147b8186863ecf7abc7635f1)
taxonomy, as applied in the
[QQQ Aug 13 2026 $732 Call — PPI Band-Walk Scalp](https://app.notion.com/p/3bb3c519147b81cfa485cb7b026771e7)
retrospective (13-minute hold, +18.39%, classified working copper; holding
toward $733 would have been a new wager). The taxonomy classifies time inside
a convex position by what the held interval actually converted risk into, not
by duration and not by the move after exit. This drilldown applies it to
option *flow*, descriptively only: it carries no option premium repricing, so
there is no theta, convexity payoff, or entry friction to measure, and the
label is not a trading signal.

Inputs, all from the owned interval only: the first-to-last observed quote
midpoint displacement inside the bin, the bin's last observed quote spread
(ask − bid) as the friction unit, and the bin's signed contract pressure.
Threshold comparisons allow only floating-point roundoff at the quote price
scale so exact decimal boundaries retain their intended class. Non-finite
midpoints are unclassified. Lead includes a visible reason distinguishing
balanced signed flow, adverse movement and movement below one spread.
Thresholds, in spread units, with signed pressure agreeing in sign with the
displacement: golden time at 5 spreads or more, working copper at 1 to under
5 spreads, lead time below 1 spread — or whenever the signed flow did not
agree with the displacement, since displacement against the flow is
non-conversion, not a separate adverse class in this preliminary scheme. The
interval is unclassified when no midpoint was observed, when it holds no
option prints, when the spread is not finite and positive, or when a recorded
gap overlaps it. The midpoint change one second later is shown separately as
a next-interval observation and is never an input to the classification:
later acceleration belongs to a different interval and cannot retroactively
reclassify the owned one, so gold appearing after the exit does not turn a
correct copper exit into a mistake.
