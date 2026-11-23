# Removing Cloudflare KV from bot operations (issue #208)

Workers KV was the wrong store for account and session state: no conditional
write, no transaction, no uniqueness constraint, and a daily *write* quota that
the production runner exhausted (`KV put() limit exceeded for the day`). This
document records what replaces it, why, how the cutover is executed, and what is
and is not true about the rollback. The runtime target is **zero KV operations**;
the legacy namespaces stay in place as a read-only archive until a separate,
deliberate retirement.

## Two revisions, not one

The change is deliberately staged, because a single revision cannot both serve
KV and provide a place to run the copy:

| Revision | Commit | What it does | KV in the request path? |
| --- | --- | --- | --- |
| Additive | `1c760ac` (patches 0010–0014) | Adds `backend/storage.js`, both schema files, the operator route, the cutover fence, the parity/bounded machinery, and the barrier hardening (fail-closed fence, epochs, drain and stabilization). Handlers still serve KV. | Yes — this is the old runtime, unchanged except for one fence check |
| Switch | `929feea` (patch 0015) + record (patch 0016) | `auth`, `common`, `bots`, `paper-trade` and `settlement` go through the storage contract. | No — zero KV, measured |

Deploy the additive revision first, run the copy under the fence, and deploy the
switch when `finalize` succeeds. Do not point the copy at a deployment that has
already switched: after the switch, KV is no longer written and the copy has no
single writer to freeze.

## Inventory: every runtime KV dependency

| KV key / namespace | Used by (before) | What it holds | Replacement |
| --- | --- | --- | --- |
| `USERS` → `user:<name>` | `auth.js`, `common.js`, `bots.js`, `settlement.js` | Account record: salt, PBKDF2 hash, iterations, `balance_cash`, trades, bot identity, margin fields | `users` table, record JSON preserved verbatim |
| `USERS` → `bot:<name>` | `bots.js`, `settlement.js` | Legacy membership marker | `bot_markers` table (R2 index remains authoritative, #102) |
| `USERS.list(prefix)` | `bots.js` reconciliation | Enumerating accounts/markers | `users.username` primary-key scan, paginated |
| `SESSIONS` → `sess:<token>` | `auth.js`, `common.js`, `settlement.js` | `{username, createdAt}` keyed by the raw bearer token | `sessions` table keyed by `SHA-256(token)` |
| `PAPER_TRADES` (R2) | `bots.js` index, artifacts | Public roster index and archives | **unchanged** |

## D1 versus SQLite-backed Durable Objects

Both are free-plan-capable transactional stores; the choice is about shape, not
price. Limits below are as independently verified for this review (D1 Free):
5M rows read/day, 100k rows written/day (index writes count), 500 MB per
database, 5 GB per account, 50 queries per Worker invocation.
Sources: <https://developers.cloudflare.com/d1/platform/limits/> and
<https://developers.cloudflare.com/d1/platform/pricing/>.

| | D1 | Durable Objects (SQLite storage) |
| --- | --- | --- |
| Model | One logical SQLite database, SQL over HTTP | One SQLite database per object, single-threaded per object |
| Consistency | Serialized single writer; `batch()` transactional | Strong per object; cross-object coordination is yours |
| Uniqueness | Native `PRIMARY KEY` / `UNIQUE` | Native within one object only |
| Free-tier shape | 5 GB per account, 5M rows read/day, 100k rows written/day | 5 GB, per-object row allowances plus request/duration billing |
| Latency | One hop per query; batch to amortise | Co-located with the routed request |
| Operational cost | A binding, two schema files, `wrangler d1 execute` | A class, namespace, routing, alarms for expiry |

**Chosen: D1.** The workload is ~31 accounts with one JSON record each, a
session per login, and a few writes per account per day. At a 300-second cycle
the dominant operation is a per-cycle `GET /api/me` read — roughly 31 × 288 ≈
9k row reads/day — and writes are tens to low hundreds per day (sessions, trades,
settlements). Both are orders of magnitude inside the read and write budgets,
and 31 records are a rounding error against 500 MB. What this data needs is
exactly what D1 gives cheaply: a uniqueness constraint for registration, a
conditional `UPDATE ... WHERE version = ?` for balance/position mutation, and a
transaction for trade + balance. A per-account Durable Object would add a routing
layer and a cross-object uniqueness problem (one object per user cannot answer
"is this username taken?") to buy latency a 300-second cadence never uses. It
would also make the migration a per-account copy with no global integrity check,
which is the opposite of what the cutover needs.

If the workload ever becomes latency-bound per account, a per-account SQLite DO
is the natural evolution; the storage module is the seam that makes that swap
possible without touching route code.

## Source of truth and write ordering

* **Runtime:** D1 is the only source of truth after the switch. `createStorage`
  throws if `DB` is unbound; there is no KV fallback that could hide a failure.
* **Registration:** one `INSERT ... ON CONFLICT DO NOTHING`. The UNIQUE primary
  key decides the winner, so two simultaneous registrations cannot both succeed.
* **Account mutation:** read record + version, apply the mutation in memory, then
  one `UPDATE ... SET record, version = version + 1 WHERE username = ? AND
  version = ?`. `changes === 1` commits; otherwise re-read and retry, bounded.
  This is a real compare-and-swap, replacing the confirming re-read the old code
  itself described as best effort.
* **Sessions:** `sessions.token_hash = SHA-256(raw token)`. The cookie carries
  the raw token; the database never holds a bearer credential. Expiry is
  enforced on read and the row is collected lazily; logout and settlement
  revocation delete by hash, and `deleteForUser` revokes every session.
* **Bot roster:** R2 stays authoritative (#102); `bot_markers` exists so legacy
  markers remain visible and retirable.

## The cutover barrier

Two stores cannot both be writers during a copy. The additive revision therefore
checks a fence that the migration route controls:

* `{"phase":"fence","action":"arm"}` writes `migration_state.fence` with a fresh
  **epoch** and deletes every prior phase checkpoint and the cutover record.
  From the next request, every mutating API route answers `503` with
  `Retry-After`, and the cron sweep logs `settlement_skipped_cutover_fence` and
  does not run. Reads keep working.
* **The fence fails closed.** If a DB binding exists and the fence row cannot be
  read — D1 outage, missing table, malformed JSON — `cutoverFenceActive` treats
  the deployment as fenced and blocks writes. An armed barrier must not evaporate
  because the store that records it is briefly unavailable; only an explicit
  `armed: false` reopens writes. This means the schema must exist before the
  additive revision is deployed, which the operator sequence does.
* **Epochs bind every result.** Each arm generates a new `epoch`; every phase
  checkpoint records the epoch it was produced under, and `finalize` rejects any
  phase whose epoch differs from the current fence. A pre-arm copy therefore
  cannot satisfy finalize, and a disarm/re-arm invalidates the earlier verify and
  stabilize instead of silently accepting them. Copy and reconcile also advance a
  `data_epoch`; verify and stabilize record the epoch they observed, and finalize
  refuses if data moved after they ran.
* **The fence is not instantaneous, and KV has no propagation bound.**
  Cloudflare documents KV propagation as "60 seconds or more"; that is not an
  upper bound, and a cached read can serve an older value. So the fence cannot
  by itself establish a single writer, and neither a timeout nor two back-to-back
  identical scans prove that in-flight writes have drained. What the machinery
  does instead is make the claim falsifiable, in three parts. The operator arms,
  waits a drain window (default 75s, `CUTOVER_DRAIN_SECONDS`) for requests that
  were already past the entry check. `stabilize` then computes a digest of the
  whole legacy source and requires a later scan to produce the same digest, and
  requires that digest to remain identical across a quiet window (default 120s,
  `CUTOVER_STABLE_SECONDS`), so an immediate cached re-read is not enough. And
  the parity proof is bound to that same snapshot: `verify` computes a digest
  over exactly the KV keys and values it compared, finalize requires it to equal
  the stabilization digest, and a legacy write that becomes visible after verify
  therefore fails finalize with `parity was proven against a different source
  snapshot than stabilization`. After the switch, `postcheck` recomputes the
  source digest and compares it to the finalized one; a late-visible write
  reports `legacy_source_changed_after_finalize` and the repair is another
  restart of the copy, reconcile, verify and stabilize cycle, not silence. If
  stabilization disagrees for eight passes it stops with
  `stabilization_did_not_converge`, finalize refuses, and the operator disarms and
  investigates. These are bounded, documented evidence and abort conditions, not
  a guarantee that no write exists.
* Copy, reconcile, verify and stabilize run under the fence. `finalize` refuses
  unless the fence is armed, every required phase completed under the current
  epoch, the drain window has elapsed, stabilization converged, no data moved
  after verification, and the parity mismatch total is zero.
* Disarm after the switch revision is live. The switch checks the same fence, so
  leaving it armed would block D1 writes too; disarming after finalize is allowed
  and annotated for that reason.

**Conflicts are detected, not merged.** A version-only merge cannot distinguish
"newer" from "another writer produced version N+1 with different contents". The
copy compares a canonical-JSON SHA-256 digest at equal versions, counts a
`conflict`, and refuses to finalize. It separately counts `d1_ahead` (D1 version
above KV after the fence), which can only mean a write landed outside the window.
Both are refusals, not silent skips.

**Deletion and revocation converge.** Under the fence a completed scan is
authoritative. Accounts absent from KV are deleted in D1 (a settlement
liquidation during the window), and D1 sessions whose hash the scan did not see
are deleted. A fresh sessions pass clears `migration_session_seen` first, so a
revoked session cannot be resurrected because an earlier pass remembered it.

## Bounded, resumable phases

Workers CPU is the other per-invocation budget: 10 ms on the Free plan. Verify
died in production with Cloudflare error 1102 against 38 accounts, 1,484 sessions
and 32 markers, at `max_records` 20, 5 and 1. An earlier version of this record
attributed that to the source-snapshot checkpoint growing past 200 KB. That
attribution was wrong and is corrected here: production `phase:verify` was `NULL`
after the failed calls, so the phase never persisted a first checkpoint and the
large later-state checkpoint cannot explain a first-call failure. What the size
data does show is that the first account alone is 333,634 bytes and the largest
is 1,139,632 (38 accounts totalling 7,457,764 bytes), so the first verify record
forces two canonical-JSON digests of a third of a megabyte.

The plausible mechanism for the first-call failure is therefore per-record
hashing cost, and the byte-equal fast path addresses it: when the stored D1 record
is byte-identical to the legacy raw value (the copy preserves it verbatim), both
canonical digests are skipped because equal bytes already prove equal content. The
controller measured this on a synthetic 352,473-byte, 576-trade record: 14.49 ms
wall on the old path versus 6.00 ms on the reviewed repair, with zero mismatch.
Local CPU granularity is coarse, so that is not a provider pass; a real provider
readback remains decisive.

The source snapshot is still a constant-size SHA-256 hash chain over the scan —
one link per key/value pair, 64 hex characters plus a count. That is scalability
hardening for later state, not the proven cause of the first 1102: on the same
shape it reduces a completion-time checkpoint from 206,491 bytes to 654 bytes with
the same 79 bounded calls. Both changes are kept.

The chain commits to the same information the entry list did (kind, identity and
raw value or "absent"). Scan order is deterministic — KV lists lexicographically
within a prefix and the phases walk users, then sessions, then markers — so
verify, stabilize and postcheck compute the same chain for the same source. A
source that only changed order would fail closed rather than be accepted.

KV's Free plan also bills `list` requests separately — 1,000 per day, a much
smaller allowance than the read/write budgets — and the copy walks a page one
record at a time. A naive adapter therefore issues one `list` per record, which a
275-session copy would exhaust in minutes. `createLegacyKvSource` memoises page
key lists for its own lifetime: it is constructed inside each `handleMigration`
call, so the cache is request-scoped and a new request always lists afresh; the
key is the binding, prefix and page token; only key names are held, never values;
and a failed list is evicted rather than remembered, so a retry can succeed.
Cursors, offsets and the source digest are unchanged — the cache changes how many
`list` calls a request makes, not what it observes.

D1 Free allows 50 queries per Worker invocation, so no phase may scan an
unbounded store in one request. Every phase processes at most `max_records`
(default 20) and stops at a query budget below 50, then returns
`continuation: true` with a checkpoint. A KV page is addressed by page token plus
a per-page offset, both persisted, so an interruption inside a page resumes at
the key after the last processed one; re-applying the few records after the last
checkpoint is safe because every write is idempotent. `status` returns redacted
projections only — counts, flags, epochs and the stabilization digest — because
the phase checkpoints hold cursors and the D1 stages advance by username, so
serialising them raw would leak identities from a route documented as
counts-only.

## Migration, cutover and rollback

Schema (both files; run twice, `wrangler d1 execute` takes one file per call):

```
wrangler d1 create live-options
wrangler d1 execute live-options --remote --file backend/migrations/0001_accounts_sessions.sql
wrangler d1 execute live-options --remote --file backend/migrations/0002_cutover_support.sql
```

Set `MIGRATION_TOKEN` as a separate secret (never `BOT_REGISTRATION_KEY`) and
deploy the additive revision (`7186f36`).

Copy, repeatedly, until each phase reports `done: true`. Each call does bounded
work; the loop is the operator's:

```
post() { curl -sS -X POST https://options.moopertonic.net/api/admin/migrate \
  -H "X-Migration-Token: $MIGRATION_TOKEN" -H 'Content-Type: application/json' -d "$1"; }
post '{"phase":"users"}'            # repeat until done:true
post '{"phase":"sessions"}'         # repeat until done:true
post '{"phase":"bot_markers"}'      # repeat until done:true
post '{"phase":"fence","action":"arm"}'       # new epoch; prior phase results are discarded
sleep 75                                         # drain for requests already past the entry check
post '{"phase":"users"}'            # final pass under the fence, current epoch
post '{"phase":"sessions"}'
post '{"phase":"bot_markers"}'
post '{"phase":"reconcile"}'        # repeat until done:true
post '{"phase":"verify"}'           # repeat until done:true; mismatch_total must be 0
post '{"phase":"stabilize"}'        # repeat until done:true and stable:true
post '{"phase":"status"}'           # counts, epochs and the stabilization digest
post '{"phase":"finalize"}'         # 200 only with epoch match, drain elapsed, stable, parity exact
```

Repeat `stabilize` until `stable: true`; each call performs bounded work, and the
phase needs full scans that agree *and* a digest that stays identical across the
quiet window (`CUTOVER_STABLE_SECONDS`, default 120s), so an immediate cached
re-read cannot satisfy it. `stable_wait_ms` says how long remains. If it reports
`stabilization_did_not_converge`, stop: disarm, inspect what is still writing,
and re-arm when the legacy store is quiet.

`verify` must run against the same source that stabilization proves: finalize
compares the two digests. If a legacy write becomes visible after `verify`, the
digests differ and finalize returns 409 with a repair instruction. The repair
repeats the copy with `restart` so a phase already marked done actually re-scans:

```
for p in users sessions bot_markers reconcile verify stabilize; do
  post "{\"phase\":\"$p\",\"restart\":true}"
  # then repeat without restart until done:true (stabilize also needs stable:true)
done
```

Then deploy the switch revision (`929feea` or later), confirm logins and the
roster read correctly, run `postcheck` (it recomputes the source digest and
compares it with the finalized one), and disarm:

```
post '{"phase":"postcheck"}'        # matches:true, or a repair instruction
post '{"phase":"fence","action":"disarm"}'
```

**Rollback, honestly.** The incident is exhausted KV writes, so a rollback that
depends on KV puts cannot be promised. The machinery reflects that:

* `{"phase":"reverse_preflight"}` performs one KV write+delete probe and records
  whether KV writes are possible *now*.
* `{"phase":"reverse_users"}` refuses with `409` when the probe failed, and says
  the recovery that actually works: **keep serving from D1; D1 is the rollback
  floor while KV writes are exhausted.** A forced run (`{"force":true}`) is
  allowed through, writes back only records whose D1 `version` is higher than the
  KV copy, and returns `503 kv_write_failed_midway` if a write fails — it never
  reports a partial reverse as success and never deletes D1 rows. The cursor is
  committed only after a successful put, so the row that failed is retried on the
  next call rather than skipped, and the whole invocation honours `max_records`
  instead of resetting the budget per page.
* Before the switch, rolling back is just redeploying the additive revision with
  the fence disarmed. After the switch, a code rollback must read D1; the KV
  archive is not a serving target while writes are unavailable.
* Sessions cannot be reversed from hashes; a KV rollback invalidates sessions
  created after the cutover and those clients log in again. Accounts, balances,
  positions and trades survive because they are in D1 and in the reverse set.

Nothing in this migration deletes a legacy key. Retiring the namespaces is a
separate operation after the cutover has been observed for long enough that the
reverse path is no longer wanted.

## Post-cutover cleanup: retiring KV and the migration route

This section describes the revision that follows a successful cutover
(verify + stabilize + finalize, switch deployed, postcheck matched, auth and
logout verified). It is preparation, not part of the cutover itself.

**What the cleanup revision changes.** The normal Worker no longer binds
`USERS` or `SESSIONS`, and it no longer mounts `POST /api/admin/migrate`. The
fence check moves to `backend/fence.js`, which the normal entry keeps as a
safety interlock; the migration machinery (`backend/migrate.js`) is reachable
only through the separate `backend/recovery.js` entry. A static test asserts the
production config has no KV namespace block, the normal entry imports neither
`migrate.js` nor the route, and no runtime module outside the recovery path
references `env.USERS`/`env.SESSIONS`.

**Legacy data is preserved.** Removing a binding does not delete a namespace.
The KV data stays in the account as a cold archive. It is not a serving target
and not a rollback promise: the incident that motivated this work was exhausted
KV writes, and a stale archive cannot be trusted with post-cutover writes. D1 is
the serving rollback floor — a code rollback means deploying a previous
D1-reading revision, not re-pointing at KV.

**Offline recovery procedure.** If the legacy archive is ever needed (for
example, to recover an account that only exists there), deploy the recovery
Worker deliberately:

```
wrangler deploy --config wrangler.recovery.toml
wrangler secret put MIGRATION_TOKEN --config wrangler.recovery.toml
# arm the fence, run the phases, verify, reconcile; then remove it
wrangler delete live-options-recovery
```

`wrangler.recovery.toml` binds the legacy namespaces, points at the same D1
database, mounts only the recovery entry, and has no cron triggers or asset
routes. It must never receive user traffic. Because it shares D1, recovery
writes land in the same accounts production serves; that is why recovery work
runs under the fence, exactly like the original copy.

**Deployment order.**

1. Cutover complete: `verify` and `stabilize` agree, `finalize` returned 200,
   the switch revision is live, `postcheck` reports `matches: true`, and login,
   read, logout and re-read have been exercised against the switch.
2. Deploy this cleanup revision to the normal Worker. Expect 404 on
   `/api/admin/migrate`; expect normal auth, roster and trading to keep working
   with no KV binding present.
3. Optionally delete the recovery Worker if one was deployed for the cutover;
   the namespaces themselves stay untouched.
4. Retiring the namespaces (deleting the data) is a separate, later decision that
   this work does not make.

**Recovery implications in one line.** After cleanup, recovery is an explicit
offline deployment with KV bindings and the migration token, not a runtime mode,
and the only always-on rollback floor is D1.

## Redacted acceptance criteria

Each item is checkable without exposing an account, a record or a secret.

1. **Zero KV at runtime.** `verify_paper_lifecycle` reports `kv_touches: 0`; the
   harness throws on any namespace access; a static scan shows
   `env.USERS`/`env.SESSIONS` only in the migration source adapter.
2. **Identity and content parity.** `verify` compares every KV account against the
   D1 row by identity and canonical digest, D1-only accounts, session owner and
   expiry, and markers. Equal counts with different accounts, altered records,
   missing sessions or missing markers produce non-zero mismatch counts.
3. **Fail closed.** `finalize` returns `409` unless the fence is armed, every
   required phase is complete, and the mismatch total is zero; an incomplete
   finalize never publishes a cutover record.
4. **Conflict detection.** Same-version, different-content rows are counted as
   conflicts; D1 newer than KV after the fence is counted as `d1_ahead`; both
   block finalize rather than silently skipping.
5. **Deletion and revocation converge.** A legacy account deleted during the
   window is deleted in D1; a revoked session is deleted in D1 and a repeated
   copy cannot resurrect it.
6. **Bounded and resumable.** Each call processes at most `max_records` and stays
   under the query budget; interruption inside a page resumes from the persisted
   token+offset and completes without loss or a repeated full scan.
7. **Concurrency is real.** Two simultaneous registrations of one username yield
   exactly one success; two simultaneous mutations lose nothing; a stale version
   cannot overwrite a newer record.
8. **Rollback is not fiction.** A failed preflight refuses the reverse with the
   D1-floor recovery note; a failed mid-reverse reports `503` and leaves D1
   intact; a successful reverse writes only D1-newer records; the failed row is
   retried rather than skipped; `max_records` bounds the whole call.
9. **The barrier fails closed and is epoch-bound.** An unreadable fence state
   blocks writes; every required phase and the stabilization result carry the
   current epoch, so a pre-arm copy or an old verify cannot finalize.
10. **Stabilization is evidence, not assumption.** Finalize requires the drain
   window, a digest that stays identical across the quiet window, and a parity
   proof taken against that same snapshot; it records the digest and aborts after
   a bounded number of disagreeing passes. A late legacy write that becomes
   visible after verify cannot ride a newer stable snapshot into finalize.
11. **Post-switch drift is detected.** `postcheck` re-hashes the legacy source
   and reports `legacy_source_changed_after_finalize` when it differs from the
   finalized digest, with the repair sequence instead of silent loss.
12. **Status is redacted.** It exposes counts, flags, epochs and a digest, never a
   cursor, username or session token.
13. **Nothing is destroyed.** Legacy keys remain readable throughout; retirement
   is a separate decision.

## Evidence

`tests/verify_bounded.test.mjs` (4 checks): a production-shaped source keeps the
verify checkpoint under 4 KB, the digest is deterministic across a restart,
verify and stabilize agree through finalize, and a late legacy write still fails
finalize after the chain change.

`tests/kv_page_cache.test.mjs` (5 checks): one list for repeated reads of the
same page, prefix and token isolation, a new adapter seeing a fresh listing while
the old one keeps its cache, a failed list being retried, and the real migration
copy issuing one list per page per request.

`tests/storage_d1.test.mjs` (12 checks): unique registration under concurrent
inserts, byte-exact legacy record round-trip, no lost update, stale-version
rejection, token hashing/expiry/revocation, migration insert semantics, no KV
fallback, idempotent schema.

`tests/migrate_d1.test.mjs` (29 checks): route authorization, bounded copy across
45 accounts with continuation, interruption mid-page resume, identity/content
parity with equal counts, altered record at equal version, required-phase refusal,
fence required for reconcile/verify, full finalize happy path, same-version
conflict, `d1_ahead`, account-deletion convergence, session-revocation
convergence, session owner/expiry mismatch, exhausted-KV reverse refusal and
forced failure, successful reverse, disarm semantics, canonical digest stability,
expired-session skip, the fence stopping mutating routes and the cron sweep while
reads flow, fail-closed fence on a throwing or malformed state, pre-arm copy and
re-arm epoch invalidation, drain-window refusal, stabilization catching a late
write, redacted status, reverse retry of a failed row, whole-invocation `max_records`
on reverse, the exact late-write-after-verify reproduction (finalize 409, then a
restart repair reaches 200 with the newer record), postcheck detecting a write
visible only after finalize, and a quiet window that rejects two immediate
identical scans.

`verify_paper_lifecycle` (Python): register, login, session, buy, deferred
re-entry, sell, settlement, roster, restart — `kv_touches: 0`, balance 9740, R2
17 reads / 11 puts unchanged.

`verify_sma200_integration` (Python): passes after the fixture-isolation repair
described below.

Full web suite on the switch revision: build_stamp, browser_data,
historical_archive, worker_logic, preflight_worker_contract, account_closure,
bots_panel, live_quote_service, quote_instruments, quote_panel, quote_panel_dom,
order_logic, sma200_paper, multi_underlying, storage_d1, migrate_d1 — all pass.

### The SMA200 integration failure

The suite failed at `after_buy.trades[0]` (`IndexError`, no trades) on this host
before any of this work. Root cause: the strategy resolves its data root at
import time as `CRASSUS_DATA_ROOT` or CWD, and the test runs from `crassus/`, so
it read the real cache `crassus/state/sma200/SPY_daily.json` plus a persisted
`crassus/state/sma200/sma_integration.json` from an earlier run instead of the
synthetic bars written to its temp directory. With a real SPY series (close
664.12, SMA 596.4) the synthetic bullish crossing never occurs, so no fill is
submitted. Smallest repair, test-only: pin `sma200.DEFAULT_STATE_DIR` to the
run's temp state before executing. No production code changed.

## Not done here, and not claimed

Chej provisions the real D1 database id and performs the cutover; this work does
not deploy, mutate production records, rotate credentials, purchase a plan or
delete a legacy namespace. The `wrangler.toml` database id is a placeholder. The
quota figures above are the independently verified ones supplied for this review;
they still deserve a dashboard check at cutover time.
