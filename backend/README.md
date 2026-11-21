# Website and paper-account backend

[backend/index.js](index.js) is the stable Cloudflare entry point. It dispatches to
these modules in the separate `live-options-api` service;
[wrangler.api.toml](../wrangler.api.toml) defines operational bindings and cron.
The public asset gateway uses [wrangler.assets.toml](../wrangler.assets.toml)
through the [staged release procedure](../docs/SITE_RELEASES.md).
The browser's implementation lives separately in [site/](../site).

| Responsibility | Source |
|---|---|
| Allowed archive access and live-quote proxy | [browser-data.js](browser-data.js) |
| Accounts and sessions | [auth.js](auth.js) |
| Bot roster and membership reconciliation | [bots.js](bots.js) |
| Quote-backed execution and request records | [execution.js](execution.js), [paper-trade.js](paper-trade.js) |
| Expiration settlement and scheduled sweep | [settlement.js](settlement.js) |
| Shared helpers | [common.js](common.js) |
| Owen-only Kalshi research, D1 archive and paper ledger | [kalshi.js](kalshi.js), [release and identity contract](../docs/KALSHI_PRIVATE.md) |

## Boundaries worth preserving

### One Clock observer storage (not activated)

[`one-clock-observer.js`](one-clock-observer.js) supplies trusted ingestion and
as-of reads over additive migration `0004_one_clock.sql`. It is deliberately not
mounted as an unauthenticated ingestion route. An owning adapter must authorize
the namespace and source, project an allowlisted payload, prove availability and
declare prospective/replay/backfill collection before calling it. A namespace is
an internal access boundary, never a client-supplied substitute for authentication.

Rule versions and observation revisions are immutable. Source observations,
event/outcome revisions, source checkpoint and batch receipt commit in one D1
batch. A generation trigger rejects concurrent stale evaluations; retry with the
same batch identity after rereading state. A failed batch rolls back completely.
There are no Workers KV, provider-feed, account-execution or settlement calls.
`observedEventsAsOf` filters observer revision time as well as source cohort and
session, so later corrections do not appear on rewind. First ingestion retains
its collection mode; backfill cannot silently become a prospective observation.
Each saved event also carries `lineage.inputs` (current and anchor receipts)
and `lineage.outcomes` (baseline and endpoint receipts plus measurement collection).
A prospective trigger can have a backfilled outcome; consumers must retain both
labels. Receipt times and original collection modes survive repeated delivery.

Bounds: 500 observations per delivery, 10,000 observation revisions per scoped
session and 100 registered versions per namespace. Exceeding a bound fails before
checkpoint commit; it is not a partial-success receipt. Integrations must establish
their workload against these bounds and preserve opaque source cursors rather
than infer completeness from the latest timestamp. Counting ongoing matches is
a reader projection, not a durable write on every tick.

Apply the reviewed additive migration before activating an observer consumer;
retain current API/assets for rollback and leave new history intact when disabling
the reader. No migration, active source ingestion or production activation is
performed by this code change. See [One Clock](../docs/ONE_CLOCK.md) for staged
delivery. Tests use the real SQLite engine through the D1 shim, including atomic
failure, restart, duplicate delivery, late outcome, correction rewind, concurrent
generation and namespace isolation. D1's batch transaction behavior and service
limits are documented by [Cloudflare](https://developers.cloudflare.com/d1/platform/limits/).

The live-quote route proxies the collector's in-memory feed. Archive reads use
stored R2 artifacts; the public site's archive-base selection is explained in
[Architecture](../ARCHITECTURE.md#browser-and-data-boundaries).

Paper orders are execution requests, not arbitrary client-supplied fills.
The execution path validates quote freshness and the requested side, and persists
canonical request records. Read the implementation and fixture tests before
changing retry or identity handling.

The configured bot index mode is `r2`; the migration is not waiting to start.
Membership and account state are different records: a retained closed account
must not silently disappear from the roster. The dated
[cutover record](../site/operations/2026-09-28-bot-index-cutover.md) preserves rollout
evidence. Do not replay its migration steps as routine startup instructions.

The single settlement cron runs every fifteen minutes, 24/7. Prediction outcomes
are swept independently of browser activity; the legacy equity sweep remains
guarded to hours 12–23 UTC on weekdays. Browser-triggered and scheduled settlement can obtain
spot marks differently. Source availability and closed-account policy are
separate questions; a successful sweep is not proof that every historical mark
exists. See the [system map](../ARCHITECTURE.md#following-one-value).

[Web CI](../.github/workflows/web-ci.yml) and
[consumer CI](../.github/workflows/market-mcp-ci.yml) list offline checks.
Old design and migration instructions are linked from
[documentation history](../docs/HISTORY.md), not treated as current deployment instructions.

## Trade journal reader (OA-238)

[`trade-journal.js`](trade-journal.js) is a **read-only** mirror of the Trade
Retrospectives journal in Notion (data source
`2a32c187-f187-4570-9d53-05e611943df0`) for the private trade display at
`/trade-journal.html`. It reads the full data source with pagination, then each
page body (also paginated), because the fills exist only in the body. It
normalises fills into the shared execution model, refreshes changed rows in
place with a revision history, and flags rows that leave the source (`archived`,
`removed`, `removed_from_source`, `missing_unverified`) without hiding them. It
also records the last successful sync and the last read error. A row in an
unknown body format is stored and shown as unsupported, never guessed. A row is
only marked as having left the source after a complete scan.

| Route | Auth | Effect |
|---|---|---|
| `GET /api/trade-journal?date=YYYY-MM-DD` | journal passkey session | Positions with an execution on that America/New_York date, unsupported rows dated that day, totals, sync status |
| `GET /api/trade-journal/row?page_id=` | journal passkey session | One stored row, with `source_last_edited` (Notion) and `app_read_at` |
| `POST /api/trade-journal/sync` | journal passkey session + origin | Reads Notion and updates D1. Writes are paused by the cutover fence like the other mutating routes |
| `GET /api/trade-journal/context?position_id=&date=` | journal passkey session | Underlying and exact-option series for one position's window, read from existing R2 archives ([`trade-journal-context.js`](trade-journal-context.js)) |

A scan pages through the whole data source in a stable order (creation
time), reads the body of every new or changed row, and only then checks rows
that were not listed. Each sync does at most 20 query pages, 35 body requests
(+10 so the last body can finish its pages) and 5 removal checks. The query
cursor and the listed-but-unread rows are saved (`trade_journal_sync`,
`trade_journal_scan`), so the next sync resumes exactly where this one stopped.
A cursor Notion refuses restarts the scan, and the summary says so. Rows whose
removal could not be checked are flagged `missing_unverified` and re-checked on
the next finished scan. Nested blocks are read down to 6 levels; a deeper body
marks the row unsupported instead of being cut silently. Notion reports
`last_edited_time` to the minute, so a row is re-read until one read lands a
full minute past its edit minute.

A position that is not flat reports `open_qty`, `open_cost_basis`, fees paid
and `realized_gross_pnl` on any closed part. Its `gross_pnl`/`net_pnl` are
null, and the day totals keep it out of realised gross and net.

Market context is computed for one position on one New York day
(`&date=`, default the entry day). It reads densely within 5 minutes of each
fill that day, and samples the rest of the position's life inside that day's
09:30-16:15 session with the remaining budget: at most 40 snapshots (QQQ, SPY)
or 15 Tradier parts (IBIT). A position entered on an earlier day or exiting on
a later one is noted, and each day reads its own archive. Series come back as
segments. The page draws each one on its own and never joins across a gap.
Continuity is decided from the observations' own timestamps, never from list
adjacency: snapshots more than 150 s apart (scaled when sampling is thinned),
or Tradier parts whose recorded events do not meet within 30 s, start a new
segment, and the hole is named in `coverage`. A lone observation is a point,
not a line. Sparse samples appear as points or ticks, and unread stretches stay
blank. Other underlyings return explicit coverage gaps. Results
for finished sessions are cached in `trade_journal_context`. While the cutover
fence is armed the route still serves, but does not write that cache.

Settings this needs before it can serve (names only; values are operator-managed):

| Setting | Kind | Value |
|---|---|---|
| `NOTION_TRADE_JOURNAL_TOKEN` | Worker secret on `live-options-api` | Token of a Notion internal integration with only the **Read content** capability, shared with the journal and descendants; this reader queries Trade Retrospectives |
| `backend/migrations/0003_trade_journal.sql` | D1 migration on `live-options` | Additive: `trade_journal_rows`, `trade_journal_sync`, `trade_journal_scan`, `trade_journal_context` |

The token is sent only in the `Authorization` header to `api.notion.com`. It is
never logged, stored or returned. Receipt link destinations in page bodies are
dropped before parsing. The context route only lists and reads the existing
`PAPER_TRADES` archives. Tests: `node tests/trade_journal.test.mjs` and
`node tests/trade_journal_context.test.mjs` (synthetic data only).

### Journal release and rollback

Keep the source journal read-only. Its parent journal grant includes descendants.
The application queries the one configured retrospectives data source. Owen, Jayden and Jacob enroll independently at
https://options.moopertonic.net/trade-journal.html. A verified passkey authorizes
the human journal and its refresh only. A normal Options View login grants no access.
On an operator device with 1Password unlocked, run
`python scripts/prepare-journal-invitations.py --vault PRIVATE_VAULT` and save its
hash-only SQL for the migration step. It verifies each saved invitation by readback.
Each member receives only their own enrollment code through a private channel.
On the site, choose “Join with an invitation”, enter that code, and use “Save my
passkey” with 1Password. Subsequent visits use “Sign in with a passkey”.
Codes expire seven days after provisioning and are invalidated on enrollment.
To revoke, execute a targeted DELETE on trade_journal_members for that member.
The revocation trigger erases the invitation, public credential and sessions and
retains the member name. Repeating initial setup skips that member. To grant
access again, explicitly revoke first, then run the invitation script with
`--recover MEMBER`. Apply its single conditional UPDATE and verify one changed
row before sharing the fresh recovery item privately.
Never put private positions, credentials or screenshots in a release artifact.

1. Obtain Owen’s merge approval and pass the protected checks for the revision.
   Separately confirm release authority. Capture current API and asset versions,
   active traffic, bindings and the D1 recovery point immediately before release.
2. Apply the reviewed SQL file 0003 to live-options once. Verify the journal and authentication tables against the migration and apply
   the three hash-only enrollment statements. Existing accounts and sessions stay intact.
3. Use the protected API workflow with the reviewed revision. The staged
   setup version contains only the credential setup. Do not promote it as the
   implementation. Verify on the actual candidate the read-only secret binding,
   existing bindings by name, then confirm journal-passkey access and rejection
   of anonymous and ordinary Options View sessions.
4. Through the protected asset workflow publish the same tested revision.
   Open /trade-journal.html as the viewer and sync until the scan reports
   complete. Check the November 13 fills, fee revision, selection and context
   against the private acceptance record. Keep the TLT coverage gap visible.
5. If verification fails, use the recorded compatible prior asset and API
   versions through their respective protected rollback procedures (assets
   first if already published). Leave the additive cache tables in place.
   Review any cache removal separately. The source requires no reversal.

[Strategy Archive](../docs/STRATEGY_ARCHIVE.md) documents shared strategy records, membership, agent intake, review and release order.


## Prediction markets (#293)

`prediction.js` extends the authenticated paper ledger with long-only YES/NO
positions, reciprocal server prices, explicit fees, quote provenance and official
resolution/correction entries. Keys include provider, environment, series,
market and outcome. Original option/share records retain their interpretation.
The existing paper-trade reservations, leases and serialized D1 mutations remain
authoritative. Cash deltas use 1/10000 USD units. Corrections are immutable cash
adjustments; a negative balance blocks subsequent entries. Accounts with event
history are retained on legacy insolvency so corrections cannot be erased.

GET `/api/prediction/markets`, `/archive?batch=SHA256` and `/portfolio` require
an Options View session on every request and return no-store. The portfolio
continues during display rollback or feed outages. Private evidence uses a new
read-only `PREDICTION_DATA` R2 binding, never the public capture bucket or KV.
No production binding is configured here. Select `PREDICTION_ENVIRONMENT` and a
reviewed `PREDICTION_RIGHTS_RECEIPT` before attaching a licensed demo/production
source. Display and buys each need their own `PREDICTION_DISPLAY=enabled` and
`PREDICTION_ENTRIES=enabled`. Sales and settlement ignore the entry switch.
Fixtures additionally require `ENVIRONMENT=preview` and `ISOLATION=disposable`.
The local preview supplies synthetic books through a separate binding.

The 24/7 sweep scans D1 users, including humans and closed accounts, and reads
the private official-resolution index. Missing/disputed/unofficial outcomes stay
pending. Delisted terms persist in fills. Later revisions adjust amounts already
paid. The first rule supports entry-premium refunds on voids (fees retained) and
fixed per-contract fees. Another convention must be implemented before enabling
that series. This complete sweep suits the current small roster; a bounded D1
due queue is required before substantially scaling accounts. The serving caps
of 200 markets and 10,000 resolutions fail closed.

Run `node tests/prediction.test.mjs` plus the paper, worker, storage, closure,
multi-underlying, cron and boundary suites. See the independent
[producer/release guide](../market_data/prediction/README.md). A permitted
production event/quote/paper-fill/reload/resolution receipt remains outstanding;
synthetic acceptance is not licensed capture. No real-money routing exists.
## Bitcoin monthly paper product

See [implementation, source gaps and staged rollout](../docs/BTC_PAPER_OPTIONS.md).
The private reader, entry switch and final-mark reader are separate controls.
The [owner-link release](../docs/BTC_PAPER_OPTIONS.md#existing-owner-passkey-linking)
uses a fresh assertion by the existing subscriber-owner journal credential to
authorize the current normal paper session. Journal login does not itself grant
BTC access. The existing Credit Tape GitHub verifier from #310 also binds the owner’s
current normal paper session, revalidating numeric identity on each private request.
The legacy paper
username allowlist is ignored; unverified sessions and distinct users remain denied.
Apply additive migrations 0005 and 0008 and verify the source rights receipt before enabling
the separate API/asset release. This does not require a collector rollout.
