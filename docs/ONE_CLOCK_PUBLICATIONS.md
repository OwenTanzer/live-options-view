# Immutable publication handoff

This handoff uses three separately reviewed PRs. The first supplies the immutable
contract/consumer and migration `0011_one_clock_publications.sql`. The second adds
source handoff receipts in `0012_one_clock_handoff.sql` and integration into the
existing observer invocation, without watched producer changes. The third supplies
the existing collector's publication adapter and its fault tests. The producer
contract below is required before activation; merging the API alone does not prove
those producer guarantees or enable capture.

Publication requires `ONE_CLOCK_PUBLICATIONS=enabled` on the relevant service.
The API configuration enables its consumer after reviewed schema application;
the collector defaults off until deliberate enablement. Collector promotion is
a continuous-service replacement: merging watched files can deploy even while
publication is disabled. The API/schema release must precede that watched merge.

## Register once, synchronize through the shared consumer

A trusted server descriptor declares `id`, `owner`, `audience: public`, instruments,
session basis (`new-york` or `utc`), and an allowlisted scalar field schema for
each output source. Private streams cannot use this registry or public bucket.
The owning adapter remains responsible for qualifying source freshness and
projecting source facts; a schema-valid number alone does not prove a fresh quote.
New streams supply a descriptor and publication integration, not another timer,
tenant scan, settlement cron or archive inventory. Existing artifact adapters
remain available for historical evidence until explicit cutover reconciliation.

Each stream is `<descriptor-id>/<instrument>`, across all sessions. It publishes
consecutive entries at `publications/v1/<stream>/entries/000000000001.json` onward.
An entry has schema `one-clock.publication.v1`, stream, sequence, UUID publicationId,
owner, previousDigest (null for sequence 1), offset-qualified createdAt, records,
and evidence references. The digest is SHA-256 of the exact previous entry bytes,
avoiding cross-language JSON serialization assumptions. Never reuse a sequence,
overwrite an entry or reserve a sequence before its commit. Retractions and late
corrections append to the current log even when their event belongs to an old day.

Records declare source, stable id, positive revision, session, eventAt and an exact
allowlisted value object (or null retraction). The normalized identity includes
the native stream prefix so legacy artifact revisions cannot collide. A record
cannot migrate to another session/instrument; corrections increase its revision.
Commit upload time from R2 is availability, not the event timestamp, local clock,
filename or ingestion time. Commit availability cannot move backwards along a log.
Observer receipt time is retained separately and also gates as-of reads.

Evidence references contain key, SHA-256, byte count, content type and schema. Keys
must be content addressed under that stream's blobs prefix. The compact commit
is the authoritative public projection. This reader validates references but
does not download their bodies: its receipts explicitly say publisher-attested.
It must never claim full evidence-body verification. The publisher integration
must create and verify immutable evidence before committing its projection.

## Producer commit and recovery

1. Prepare and persist immutable evidence and exact commit bytes before publication.
2. Create the next sequential entry with `If-None-Match: *`. The commit is the
   authoritative publication point; publish mutable compatibility views afterward.
3. An optional conditional head is a restart optimization, never evidence that
   a missing sequence can be skipped. After a restart, probe consecutive entries
   beyond the head to recover a commit whose head update was interrupted.
4. Resolve an ambiguous PUT by checking the original sequence and publication UUID
   before retrying. A competing publisher cannot overwrite that sequence. Do not
   generate a new publication identity merely because an acknowledgement was lost.
5. Immutable intent objects bind UUIDs to exact sequence/bytes before commit. Durable
   per-record heads prevent repeated/decreasing revisions and identity moves from
   poisoning the consumer log. Repair every committed record head before advancing
   the stream head. Concurrent writers can collide; they cannot overwrite a commit
   or silently move an existing intent to a different sequence.
6. Read actual collector watch settings and lifecycle before merge/promotion. Keep
   the existing process, capture identity and schedule; no duplicate provider feed.

R2 documents atomic create-only conditional writes in its
[conditional operations contract](https://developers.cloudflare.com/r2/api/s3/conditional-operations/).
The collector calls this writer before updating the compatibility `latest.json`.
If commit cannot be confirmed, latest does not advance and the legacy sidecar
records `one_clock_commit_failed`. If latest later fails, the immutable commit
remains valid. CSV and sanitized JSON evidence are content addressed and read
back before commit; optional non-finite JSON cells become null, with raw CSV
preserved separately. Price/skew projections require a live, nonfuture selected
underlier receipt. Snapshot construction remains the event-time basis; provider
event time is not invented. Python validates the same UTF-16 text bounds as JS
and normalizes wire timestamps to UTC milliseconds.

The collector uses a separate S3 transport with 1-second connect/read timeouts,
one SDK attempt, at most 40 requests and a 5-second cooperative wall budget per
snapshot. Budget checks run before requests and while reading bounded bodies;
an already-running socket operation may finish after the wall budget. This is
not an assertion of a hard process-level deadline. Recovery advances the head
after each repaired entry and examines at most eight entries per call, allowing
bounded continuation after a lost hint. No new thread, feed or timer is created.
Counts-only logs report symbol, commit sequence, request count and elapsed time;
failure is visible without logging credentials, record bodies or transport errors.

## Bounded consumer and retained history

`ingestRegisteredPublications` gives each registered stream a fair share of at most
192 GETs and 2 MiB of entry data per invocation, across at most 12 streams. Entries
are at most 16 KiB with 10 records and four evidence references. It reserves one
maximum entry's budget before each GET, reads consecutive keys, and never lists a
bucket or scans lifetime history. At three streams the GET allowance is 64 each;
actual catch-up depends on entry size and production rate. A descriptor is not
permission to exceed the shared budget. Integration must reconcile capacity with
the existing observer and catalog work before enabling another source.

Checkpoint, exact entry bytes/digests, normalized records and status commit in one
D1 transaction. Generation CAS rejects stale consumers. Corrupt/unknown/future
entries stop the cursor; a validated prefix can commit before a transport/schema
failure. A SQL identity violation rolls back the entire batch. The stream then
reports a transaction failure and retries without skipping the offending sequence.
One stream's failure does not consume another's allocation. No raw exception or
private transport detail is placed in public coverage. A missing next entry means
only `tail-at-check`, never session completeness or producer finality.

The paginated reader uses a stream/session index and an explicit, scope-bound
cursor. Every revision is gated by event time, availability and observer receipt.
It returns at most 100 records and preflights a 2 MiB payload limit. It does not
discard older revisions, silently truncate a complete result, or impose a lifetime
row-count cap. Consumers must follow every page before evaluating a full session.
Physical D1 storage remains finite; monitor capacity rather than pretending an
append-only audit archive has unlimited storage. See
[D1 platform limits](https://developers.cloudflare.com/d1/platform/limits/).

Collection labels are fixed at first receipt. Initial catch-up and old-event
corrections are backfill; repeated delivery never upgrades them. Native ingestion
runs once per existing observer invocation even with zero active accounts.

## Source composition and cutover

Each native descriptor declares outputs and an explicit per-output legacy
adapter supersession map. The evaluator composes every registered source;
replacing chain/price/skew does not suppress an unrelated news/shortlist adapter.
Two native owners for the same output/instrument are rejected. Fields and
instruments join the common rule registry through the descriptor.

Producer start is not the observer cutover. A transaction freezes the source's
already-retained legacy membership, native sequence and identity/revision mapping.
Legacy history and collection labels remain unchanged. Matching native baseline
values alias that retained identity without replacing it. A different qualified
baseline value or retraction becomes an explicit backfill correction on the
original legacy ID, available to this observer at cutover. Later native revisions
use the persisted offset on that same ID. Nonoverlapping native records retain
their native identity and original receipt/collection. Retries cannot recompute
offsets from a newer legacy maximum. Queries and writes have explicit record/byte
bounds and fail without claiming complete evaluation when exceeded.

The account scheduler still evaluates its latest two equity sessions. Older
corrections are retained and available for as-of reads, but automatic historical
reevaluation remains open quest work. Results compare each scope's latest native
receipt with its evaluation receipt and explicitly mark newer unevaluated evidence
as pending. Existing statistics continue to describe the retained evaluation;
they are not claimed to incorporate that correction. Cutoff lookups use indexed
seeks and at most 93 session/instrument scopes per Results request.

## Release and rollback

Review/test the exact integration revision independently. Apply only reviewed
migrations 0011 and 0012 before enabling the API consumer. Promote the compatible
API before the collector starts native publication. Stage producer enablement
without a separate restart only if provider readback establishes that capability;
then deliberately promote the exact reviewed collector revision once. Inspect
live watch triggers and the active deployment's resolved build manifest. A generic
service builder field can differ from the resolved Dockerfile build. Verify actual
replacement timing, health/quote freshness, producer identity and any capture gap.
Equity close alone is not a maintenance window for the continuous price lane.
No extra provider feed or parallel publisher process is part of this release.

For rollback, preserve immutable logs, identities and handoff tables. Disabling
the producer alone leaves native coverage at its last publication; the reader
reports that time and rule freshness expires normally. It must not silently
switch to advancing legacy views. Disabling the API native flag after a handoff
pauses evaluation and displays a current operational notice, while preserving
historical evaluation receipts. Re-enable the same native consumer to resume.
A return to legacy evaluation requires a separately reviewed reverse handoff;
there is no automatic identity reset. No account, execution, settlement, private
bucket or Workers KV operation belongs to this integration.

Fixture tests cover bounded continuation, missing head hints, old-session
corrections, retractions, cutoff rewind, corrupt/future/transport reads, transaction
rollback, competing consumers, identity conflicts, per-stream fairness, byte
reservation, cursor scope and strict public field/evidence validation. Producer
tests include ambiguous PUT, partial identity repair, restart, collisions, invalid
revisions, time/request budgets and transport configuration. Cross-language tests
consume real Python commit bytes in JavaScript and verify handoff corrections,
unrelated-source composition, rollback fencing and historical pending disclosure.
