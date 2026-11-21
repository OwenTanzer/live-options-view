# Immutable publication handoff

This is the dormant first half of a two-PR delivery. It adds a public publication
contract, bounded consumer and additive migration `0011_one_clock_publications.sql`.
There is no registered production stream, scheduler hook, environment flag,
publisher change or automatic migration in this slice. The second PR must prove
the producer's crash behavior and integrate the existing scheduled observation
path before activation. Merging this library does not change live capture.

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

## Producer requirements for the second PR

1. Prepare and persist immutable evidence and exact commit bytes before publication.
2. Create the next sequential entry with `If-None-Match: *`. The commit is the
   authoritative publication point; publish mutable compatibility views afterward.
3. An optional conditional head is a restart optimization, never evidence that
   a missing sequence can be skipped. After a restart, probe consecutive entries
   beyond the head to recover a commit whose head update was interrupted.
4. Resolve an ambiguous PUT by checking the original sequence and publication UUID
   before retrying. A competing publisher cannot overwrite that sequence. Do not
   generate a new publication identity merely because an acknowledgement was lost.
5. Test failures before evidence, before/after commit, before/after head, concurrent
   publisher collisions, restart, late correction and retraction. Missing or corrupt
   evidence must not result in a claimed successful publication.
6. Read actual collector watch settings and lifecycle before merge/promotion. Keep
   the existing process, capture identity and schedule; no duplicate provider feed.

R2 documents atomic create-only conditional writes in its
[conditional operations contract](https://developers.cloudflare.com/r2/api/s3/conditional-operations/).
The existing collector's legacy latest/provenance ordering alone is not this contract.

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

The integration must retain collection labels and reconcile legacy/native overlap
before observer evaluation. Native commits published now cannot backdate old
archive availability or silently replace recorded event lineage.

## Release and rollback

Review/test this contract independently; leave it dormant. Apply only migration
0011 in the second PR's reviewed activation sequence, before enabling its consumer.
Disable that integration to stop ingestion while preserving retained history.
Never roll back or rewrite the producer's immutable log. No account, execution,
settlement, private bucket or Workers KV operation belongs to this library.

Fixture tests cover bounded continuation, missing head hints, old-session
corrections, retractions, cutoff rewind, corrupt/future/transport reads, transaction
rollback, competing consumers, identity conflicts, per-stream fairness, byte
reservation, cursor scope and strict public field/evidence validation.
