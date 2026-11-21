# One Clock implementation and extension contract

Status: shared clock, offline authoring/replay and durable observer storage implemented; no production observer activation. Inventory baseline: master
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
execution runs from this page. D1 persistence, live activation, source ingestion,
full categorical/comparison statistics and reader migrations are later slices.

The observer storage in `backend/one-clock-observer.js` reuses this evaluator.
It atomically commits observations, event/outcome history and source checkpoints
to D1 with optimistic generation protection and exact batch-id retry receipts.
The additive migration and trusted boundary are described in the
[backend guide](../backend/README.md#one-clock-observer-storage-not-activated).
It has no public ingestion route or running source consumer yet. Controlled
recordings test parity and restart; production source activation and natural
event/outcome receipts remain outstanding.

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
