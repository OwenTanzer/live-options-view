# Durable Crassus ledger and private R2 archive

The runner can keep **both its ledger and pending execution intents on a Railway
persistent volume**, then archive ledger records to a separate private R2 bucket.
Uploading to R2 does not make ephemeral local state durable. Both parts are needed:
volume persistence supports execution recovery; R2 supplies a second ledger copy.

## Configuration

Defaults remain `crassus/logs` and `crassus/state` (`/app/logs`, `/app/state` in the
container). Set `CRASSUS_DATA_ROOT=/data/crassus` to move both beneath that root.
The `STOP` sentinel also moves to `/data/crassus/state/STOP`. Mount a persistent
volume at `/data` **before enabling this path**. The application does not provision
or verify a volume: a path on the container filesystem is still ephemeral.
Only one runner may use a data root. File and directory fsync require POSIX storage
support, as provided by the Linux deployment; failures must not be ignored.

R2 archival is disabled unless any `CRASSUS_ARCHIVE_*` setting is present. When
configuring it, supply all of:

| Variable | Value |
| --- | --- |
| `CRASSUS_ARCHIVE_BUCKET` | Dedicated **private** bucket name |
| `CRASSUS_ARCHIVE_ACCOUNT_ID` | R2 account's 32-character lowercase hexadecimal ID |
| `CRASSUS_ARCHIVE_ACCESS_KEY_ID` | Bucket-scoped access credential |
| `CRASSUS_ARCHIVE_SECRET_ACCESS_KEY` | Its secret, managed in Railway variables |
| `CRASSUS_ARCHIVE_PRIVATE` | Literal `true`, acknowledging verified private access |

Use credentials that permit object reads and writes in this bucket. The program
uses no delete operations. No collector bucket or credentials are inherited;
if `R2_BUCKET_NAME` is present, an identical archive bucket is rejected. The
acknowledgement does **not** establish bucket privacy: verify that R2 public access,
custom domains, and Worker routes do not expose the bucket. Never put ledger data
in the public market-data bucket under a supposedly private prefix.

Partial configuration or an invalid checkpoint fails startup before account login.
Network/upload failures are logged and retried independently of trading. Do not
put credentials in command lines, repo files, task descriptions, or diagnostic logs.

## Deployment and migration checklist

This change does not mount a volume, create a bucket, change Railway variables, or
deploy anything. Before the first storage migration:

1. Preserve and independently verify a fresh copy of existing `/app/logs` and
   `/app/state`, including records after the previous historical backup. Record
   source deployment, sizes, hashes, and capture timestamps. Do not assume an old
   backup includes today's later writes.
2. Arrange a controlled cutover with no concurrent writes while capturing the final
   ledger and pending intents. Stop trading gracefully under an approved operational
   window, capture and verify the final files **before** an action that replaces the
   container. If that final capture is unavailable, do not proceed or claim full
   preservation. A live copy alone has a race at the cutover boundary.
3. Provision the persistent volume and seed `/data/crassus/logs` and
   `/data/crassus/state` with the verified final source files before starting the
   runner against `CRASSUS_DATA_ROOT=/data/crassus`. Keep filenames, contents, and
   ledger modification times (cross-run recovery uses mtime ordering). Preserve the
   original backup. Do not mount over `/app/logs` or `/app/state`: an empty mount
   hides source files. Do not simply change the data root and start with empty state.
4. Check pending intents and the STOP sentinel deliberately; a migrated STOP file
   keeps trading stopped until intentionally removed. Configure the private bucket
   credentials. Deploy the reviewed code, then confirm startup recovery resolves
   pending executions before ordinary decisions and both data directories use the
   volume. No stale intent snapshot is restored automatically.
5. Observe `archive_uploaded` and `archive_status`; verify backlog drains. Download
   actual R2 objects and check their SHA-256 and reconstructed record coverage.
   Test a controlled restart and an R2 outage in staging before production cutover.

## Archive format and recovery guarantees

Objects have this layout:

```
crassus-ledger/v1/<persistent-namespace>/<ledger-filename>/<start>-<end>-<sha256>.jsonl
```

Offsets are zero-based bytes with an exclusive end. Batches end on complete JSONL
records, target 1 MiB, and can exceed that target by at most one record. An
individual record over 8 MiB fails visibly. A poll uploads at most four batches,
then waits 30 seconds. Reads are bounded; malformed records fail rather than being
skipped. An incomplete trailing line remains local and contributes to backlog,
including if an old run ended with a torn record. Operator investigation is needed
for persistent incomplete/corrupt tails; no automatic repair discards evidence.

`state/archive/checkpoint.json` contains the namespace, bucket, per-file verified
byte offsets, any pending upload range and checksum, and last successful upload.
The pending range is atomically saved and fsynced **before** upload; an ambiguous
request or restart therefore retries exactly the same immutable object, even after
new ledger appends. Conditional PUT prevents overwrites. The worker downloads the
stored object and checks its length, metadata, and **actual SHA-256** before
persisting progress. It does not rely on ETags or uploader-supplied metadata alone.
R2 supports conditional PUT via its [S3 compatibility API](https://developers.cloudflare.com/r2/api/s3/api/).

SDK requests use 3-second connect and 10-second read timeouts and no automatic
retries; the next background poll retries. Socket timeouts are not a strict
wall-clock deadline. Shutdown waits at most one second for this daemon worker;
unacknowledged work remains local for the next process. An upload may finish after
shutdown starts; its saved pending range makes replay safe. `--once` does not
promise to drain an archive backlog before exit. Archive work never changes the
ledger-write-before-intent-finalize ordering.

`archive_uploaded` reports acknowledged bytes, SHA-256 and success time.
`archive_status` reports remaining local bytes and last success time.
`archive_failed` reports the exception type without arbitrary SDK response text.
SDK wire debugging is suppressed even under `--verbose`. Missing tracked files,
truncation, invalid checkpoints and verification mismatch are errors, not permission
to reset progress. A persistent failure on an older file can prevent later files
from being archived; alert on archive failures, growing backlog, and absent archive
status events. This code supplies events, not an external alert/watchdog service.

For manual restore, list objects within one namespace and ledger filename, verify
each SHA-256, sort by starting byte, require contiguous nonoverlapping coverage
from zero, and concatenate unchanged bytes. If the checkpoint was lost, a fresh
namespace can contain duplicate evidence; do not combine namespaces blindly.
The archive retains every local ledger and never deletes local data, so volume
capacity must be monitored. Pending execution state stays on the persistent
volume and is **not** periodically restored from R2: a stale snapshot could replay
already acknowledged work. Loss of the volume still requires explicit execution
reconciliation and recovery, even when the ledger archive is complete.
