# Bitcoin monthly futures options — implementation and release record

November 21, 2026. PR295 shipped as `9f2ea06`. The observed-side-clock candidate
is prepared separately on merged master `68bc428`, including the subscriber
revocation fix from PR301. This remains a disabled-by-default paper product,
not a broker order integration. The operator owns publication and rollout;
local candidate tests do not establish deployment or live acceptance.

## Source and identity

The collector uses its existing production tastytrade read token and price-lane
DXLink connection. `GET /futures-option-chains/BTC` supplies instrument definitions;
`GET /instruments/futures/{symbol}` verifies the deliverable, expiration and five
bitcoin notional multiplier. After restart, exact held provider symbols are
resolved through `/instruments/future-options/{symbol}`. A stable `BTCOPT:` key is
the first forty hex characters of SHA-256 of the exact provider instrument symbol.
Symbols are never reconstructed using an equity/OCC convention. The normalized
contract retains provider and streamer symbols, security ID when supplied, future,
expiry, cutoff, strike, put/call and multiplier. Missing monthly or deliverable
facts fail closed. Weekly, micro and Bitcoin Friday products are excluded.

The authenticated November 21 production response uses `End-Of-Month`, product
code `BTC` and product exchange `CME`. It reports `American` exercise and
`is-exercisable-weekly=true` even for the standard monthly. These vendor flags
are not classification or exercise authority: CME's cryptocurrency option FAQ
specifies European exercise and monthly delivery into an immediately expiring
future. Discovery and held-symbol lookup both require the exact product fields,
matching option/future expiry and five-bitcoin size. A null future maturity date
falls back to its verified expiration date. Fixtures reproduce these fields with
synthetic instruments; no early-exercise behavior is added.

Public documentation checked on November 21:

- [tastytrade product list](https://tastytrade.com/learn/trading-products/futures/available-futures-products/)
  lists standard Bitcoin futures options. This does **not** establish any account's entitlement.
- [Detailed chain API](https://developer.tastytrade.com/reference/instruments/getFuturesOptionChainsSymbol/)
  defines provider instrument and streamer identities.
- [Future API](https://developer.tastytrade.com/reference/instruments/getInstrumentsFuturesSymbol/)
  defines the deliverable and notional multiplier.
- [CME cryptocurrency options rules](https://www.cmegroup.com/articles/faqs/frequently-asked-questions-options-on-cryptocurrency-futures.html)
  distinguish monthly delivery into immediately expiring futures from weekly delivery.
  The current schedule permits weekends/holidays, with Chicago-time maintenance
  16:00–16:02 Monday–Friday and 02:00–04:00 Saturday. Both server and collector use
  timezone-aware checks. Provider/exceptional closures still require fresh liquidity.

Authenticated production discovery returned 2,386 contracts; the deployed private
window returned 22 standard monthly five-bitcoin contracts with positive two-sided
prices and sizes. All 22 lacked usable normalized bid/ask source timestamps. The
operator verified account-specific chain and quote access; this does not confer
display rights on other site users. Owner identity, restricted audience activation
and live paper acceptance remain operator gates. No permitted final-mark importer
has been established. Fixture prices and fills are synthetic.

## Private quote service and execution

`BTC_OPTIONS_ENABLED=1` on the existing collector enables the private lane; absent
means disabled. It requires the existing continuous price lane. One background
discovery worker has bounded REST pagination and a five-minute cadence, with at
most four unknown held definitions resolved per attempt. The quote budget is 64
option instruments plus one deliverable future Quote. Up to eleven strikes (both
sides) are centered on the future quote, initially the median listed strike until
that quote arrives. Held instruments have priority. A fifteen-minute interest TTL
releases abandoned subscriptions; positions returning later request their exact
identities again. Budget overflow is explicit and never substitutes another strike.

Only product-owned Quote subscriptions are removed during window/series changes;
existing equity and price subscriptions are preserved. Reconnect invalidates BTC
state, and every Quote replaces missing sides/sizes rather than carrying them
forward. Bid/ask exchange times, receipt times, sizes and availability are separate.
There is no BTC chain archive or KV operation.

`GET /api/btc-options` requires a live paper session bound to the verified subscriber-owner
GitHub identity or an explicit owner passkey proof. The former `BTC_OPTIONS_USERS` username allowlist is ignored. Public
registration, a matching name, a journal login cookie, holdings, or an old execution
receipt never grant access. See [owner linking](#existing-owner-passkey-linking) below.
Revocation blocks fresh quotes and execution while preserving the stored ledger and
settlement obligations. `BTC_OPTIONS_ENTRIES=1` independently permits new buys;
absent/zero blocks new entries while entitled closes and settlement remain enabled.
The collector route also requires the existing `X-Live-Quote-Key`. The public quote
route refuses BTC keys/provider future-option symbols. Neither browser-data proxy
allowlist exposes the chain or settlement records. The disposable preview explicitly
allows the same-origin BTC API route; it never falls back to production.

The existing `/api/paper-trade` reservation/lease and D1 compare-and-swap mutation
are reused. Orders are long-only, exactly one contract per ticket, market or
immediately marketable limit. Cash and held quantity are checked inside the mutation.
There is a per-account cap of 64 distinct held BTC contracts. The server re-resolves
the selected key through the collector and rechecks freshness before mutation.
Only the execution side needs a positive price, size >= 1 and a qualifying execution
clock plus independently fresh receipt time within fifteen seconds; a missing
opposite side is permitted, a crossed book is not.
Quotes over one second in the future fail closed. Closed/maintenance/expired sessions
cannot fill. New entries must belong to the active nearest monthly series.

Premiums are USD per bitcoin; whole-contract amounts multiply by five. The explicit
simulation fee is **$2.50 per contract per side, $0 at expiration**. This is a model,
not a claim about broker/exchange fees. Every fill persists immutable contract
metadata, the source quote, side times/sizes, multiplier, modeled fee and cash delta.
The average-cost book allocates entry fees when quantity closes; realized gross,
fees, net realized and remaining entry-fee basis survive reload.

The Bitcoin selection is separate from equity snapshots. It uses the existing ticket,
ledger, positions and history. Switching products clears tickets and rejects old
in-flight responses; account changes clear private quote memory. Held contracts
refresh even while another chain is visible. Long liquidation uses a fresh executable
bid; unavailable bids do not fall back to midpoints or snapshots. Aggregate open/total
PnL shows unavailable when a BTC position cannot be marked.

Bitcoin is a peer in the existing chain selector. `/?chain=BTC` opens it directly
and preserves the selection through refresh and login. Rows display owned quantity,
side sizes, freshness and separate **Buy at ask** / **Sell / close at bid** actions.
These open the existing ticket for review; the row never submits an order itself.
Unavailable actions have visible reasons, and **Open trade ticket** remains available
for inspecting a contract. Quotes age out in the UI between polling responses.
Equity snapshot/session, IV, skew and Historical controls are hidden while Bitcoin
is active and restored on returning to an equity chain.

Signed-out visitors get **Sign in** to their normal paper account. Signed-in visitors
without an authorized source remain denied. When owner linking is configured, they
can deliberately link their existing owner passkey to the displayed current paper
account for this session. There is no trial username or account-switch recommendation.
Account switching clears private rows and tickets immediately, and delayed responses
from the previous account/view cannot restore them. Initial BTC quote loading waits
for authenticated session verification, independently of equity calibration loading.
Private BTC fills/source quotes are excluded from the persistent display cache;
older cached BTC entries are scrubbed on read. Holdings reload from the verified
account API. Late execution and settlement replies cannot mutate a replacement
account's displayed book.

### Existing owner passkey linking

This fallback API/auth flow is included with the mobile chain repair (#312). It uses
the already-supported journal WebAuthn credential store and pinned
`@simplewebauthn/server` verifier, with a new assertion ceremony. Paper accounts remain
ordinary password accounts. PR #310 subsequently supplied real GitHub owner verification;
the primary path below reuses that fixed service. CLI credentials are never reused.

The only supported principal for the current subscriber feed is the existing
invitation-provisioned journal **Owen** principal. That fixed server record is not
a paper username, form value, claimed GitHub handle, or email match. Jayden/Jacob
journal credentials do not own this subscription. Linking verifies possession and
user verification of the existing enrolled credential; it never enrolls a new key,
accepts an invitation, signs into the journal, or reads/reuses a journal session or
auth secret. [SimpleWebAuthn's verification contract](https://simplewebauthn.dev/docs/packages/server)
requires the saved challenge, expected RP/origin, credential public key and user verification.

The UI names the current paper account and requires **Link this session with owner
passkey**. `/api/btc-options/owner-link` accepts JSON POSTs with the exact production
Origin, rejects cross-site requests, and requires a live non-bot, non-closed paper
session. A random discoverable challenge reveals no credential/account list and is
bound in D1 to that session, account, purpose-specific table and configured source.
It expires in five minutes and is atomically consumed before verification, including
failed attempts. This prevents replay even for synced passkeys with a zero counter.
The credential counter compare-and-swap and proof insertion share a D1 transaction;
session validity is rechecked before inserting the proof. Journal assertions cannot
be replayed here, or vice versa.

The proof lasts at most eight hours and never beyond the paper session. It survives
page refresh but not logout/new login. **Unlink Bitcoin data from this session**
removes it and pending challenges. Existing journal credential revocation, account
deletion, paper logout, source-binding changes, and the source switch all revoke
access server-side. Normal login and unverified `/api/me` responses omit BTC fills
and their source quotes; the stored ledger is retained. The UI reloads that history
after proof, clears private state on rejection, and ignores an old ceremony after
an account switch. Public cache/storage is never used for proofs or private quotes.

Release prerequisites (operator-owned; this PR performs none):

1. Confirm that the existing enrolled Owen journal principal is the actual subscriber
   of the configured collector source, and retain the source/account-specific display
   rights and quote-verification evidence. Code inspection and fixtures do not prove
   live enrollment or entitlements. Do not enroll/recover credentials as a shortcut.
2. Apply additive `backend/migrations/0005_btc_owner_link.sql` after the existing
   account/session and journal migrations. It creates empty challenge/proof tables
   and revocation triggers; it seeds no identities or grants and uses no KV.
3. Through the separately reviewed API release, set `BTC_OWNER_LINK=enabled` and
   `BTC_OWNER_RIGHTS_RECEIPT` to the retained source-rights receipt reference only
   after prerequisite 1. These source-level gates are not paper-user allowlists.
   Proofs are bound to the receipt and `LIVE_QUOTE_ORIGIN`. Missing settings/schema
   fail closed. This replaces the legacy allowlist; existing paper sessions need
   GitHub binding or deliberate passkey proof. Keep entries separately controlled.
4. Release compatible assets through the staged lane, then have the owner try
   `https://options.moopertonic.net/?chain=BTC` in their normal paper account.
   Verify deny-before-proof, link, refresh, unlink/logout and revocation. Leave
   #312's independent site-only release available throughout preparation.

Disable `BTC_OWNER_LINK` to withdraw this source immediately. Roll back to a compatible
denying API/assets revision; do not restore the legacy username grant. Leave the
additive schema and trading ledger intact. No collector deployment is needed.

**Distinct-user blocker:** the assignment's tastytrade API agreement prohibits
displaying this subscriber feed to distinct third parties. No provider-authorized
cross-user grant or existing verifiable user-to-source adapter was found. Ordinary
users must remain denied. Their own future authorized source would require a real
provider grant, verified subject/account binding, permitted display scope and an
isolated source adapter with revocation—not another row granting this shared feed.
No speculative OAuth, text matching, secret reuse, or generic grant endpoint is added.

### Existing GitHub owner and normal paper sessions

PR #310 introduced `ownerIdentity()` in `backend/kalshi-access.js`. It forwards only
the existing opaque Credit Tape session to the fixed HTTPS identity endpoint and
accepts exactly numeric GitHub subject `125096971`. The shared verifier now uses
Cloudflare-supported `redirect: 'manual'`, rejects every redirect/Location response
and any status other than 200, bounds JSON, checks its content type, and fails closed
on wrong/expired identity or transport errors. Kalshi public reads use the same
supported redirect discipline. There is no new OAuth app, credential, key or issuer.

Selecting BTC or submitting ordinary paper login checks this already-established
GitHub identity without prompting for another authentication factor. A same-origin
JSON POST explicitly binds that current paper account/session, checking the displayed
account against the authenticated session as a race guard (never as identity proof).
The binding stores only the paper session hash, account ID, numeric GitHub subject,
source binding and expiry. It confers no authority by itself: quote reads, history,
buy/close submission and receipt replay revalidate the fixed upstream. There is no
positive identity cache or cross-session binding. Source rights gates still apply.
The passkey ceremony above remains available when GitHub is unavailable.

The phone UI offers **Sign in with GitHub** through the existing gateway route
`/credit-tape/auth/login?return=btc`. A short-lived HttpOnly navigation hint returns
an approved callback from the fixed Credit Tape landing page to `/?chain=BTC`.
It is not OAuth state, identity, a session, or an arbitrary return URL; the existing
upstream retains all OAuth approval and callback validation. If ordinary paper login
is still needed, the BTC selection remains visible. If both sessions are already
verified, the chain and actions load without a passkey or extra confirmation step.

Apply additive `0008_btc_github_link.sql` alongside the documented account/journal
and owner-link migrations before the separate protected API/assets release. It
seeds no users or grants. Logout/account deletion clears bindings. **Unlink** leaves
a session-scoped disabled marker so refresh cannot silently relink; the explicit
**Use verified GitHub for this paper session** action can restore it after a fresh
upstream check. Proof expiry, revoked GitHub sessions, changed source rights and
different people remain denied. Other paper sessions and the separate Kalshi ledger
retain their own balances and history. No production migration or release is performed
by the implementation/tests.

### Execution clocks and raw wire diagnostics

`bid_ts` / `ask_ts` remain provider side times, never synthesized from receipt.
The decoder accepts positive epoch milliseconds (number or numeric string) and
explicit timezone-bearing ISO strings. Missing, zero, nonfinite, boolean or
unparseable values are unusable. It does not guess seconds versus milliseconds.
A present parsed stale/future provider time fails closed; the fallback cannot
override it. [dxFeed Quote](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html)
defines per-side milliseconds with second precision. Aggregate Quote `time` and
REST `updated-at` are not per-side substitutes.

When provider side time is unavailable, `observed_side_change.v1` may qualify:

- The first complete price/size pair after subscription or reconnect is only a
  baseline. A later FEED_DATA frame must change that exact side's price or size.
  Unchanged repeats and opposite-side changes never advance its observation time.
- Missing or invalid sides clear that side's baseline and change. Multiple events
  for the same side within one frame, or a backwards clock, require a new baseline.
  Size withdrawal to zero is unavailable; a later positive changed size may qualify.
- A new connection epoch invalidates all prior observations on open (including
  silent redial), close, auth rejection and transport error. Resubscription also
  starts a baseline. Quotes and epoch/readiness are read under one lock.
- The API validates matching epoch, ordered baseline/prior/change timestamps and
  frame sequences, changed old/new pair, and equality with the current executed
  side. The change must be no older than fifteen seconds. Its time must be after
  the prior receipt and no later than the current receipt, which independently
  must be within fifteen seconds. Unchanged receipt updates cannot renew a change.
- A source-timed event clears fallback evidence, so a subsequent unchanged untimed
  event cannot evade a contradictory source clock. Missing evidence fails closed.

[dxFeed lasting-event semantics](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/LastingEvent.html)
deliver the latest conflated Quote at subscription, rather than queuing historical
changes. This design discards that initial value and unchanged reconnect replay;
it does not claim an exchange timestamp or measure upstream latency. A provider
silently replaying different historical values on an uninterrupted connection
cannot be distinguished without source time. Restrict this weaker, explicitly
process-observed clock to the authorized paper simulation; do not call it proof
of exchange freshness.

Fills retain `quote_clock_basis` (`provider` or `observed_side_change`), selected
`quote_ts`, nullable `quote_source_ts`, and the complete immutable `source_quote`.
The change evidence includes connection epoch, baseline/prior/current frame
sequence, timestamps and old/new price/size. The chain, ticket tooltip and history
label observed changes separately from provider time; legacy fills remain labeled
legacy source time. Bid liquidation uses the same qualification as selling.

The private collector response adds `wire_time_diagnostics` with schema
`btc_wire_time_counts.v1`, connection epoch, total ingested frame count and
option-only Quote event counts. Each `bidTime`/`askTime` field is counted by exact
JSON type/category: absent, null, boolean, number/string zero, positive, negative,
nonfinite, string ISO/unparseable, or other. Deliverable future and equity events
are excluded from these counts. Counts reset with the epoch; no raw values,
symbols, prices or credentials are logged or put in public health/archive data.

The retained production first-message log is truncated and may be a non-BTC event;
its absent fields cannot establish actual BTC wire types. Until the operator reads
these diagnostics from the existing stream, the precise cause of the 22 null
normalized side times remains unconfirmed. Do not describe synthetic zero-valued
fixtures as a production wire observation, or start a second stream to probe it.

## Final settlement

Standard monthlies model the net cash result of exercise into the immediately
expiring future: call `max(final - strike, 0) * 5 * quantity`, put
`max(strike - final, 0) * 5 * quantity`. The server uses its own cutoff clock.
The browser cannot supply a BTC final mark. D1 applies the exact contract/expiry
settlement once under account CAS; missing/invalid marks remain pending.

The final-mark reader is implemented, but **a licensed production mark importer is
not enabled or supplied by this change**. An authorized source owner must establish
the permitted official CME final source and separately provision immutable evidence
at this exact private operational R2 key:

`paper-trades/btc-final/<forty-hex-contract-key>/<YYYY-MM-DD>.json`

Required fields: `contract_key`, `future_symbol`, `exp`, numeric nonnegative `value`,
`kind: "official_final"`, `source: "CME"`, nonempty `source_ref`, and `published_at`
at/after contract expiry and not in the future. This schema is an administrative
trust boundary, not independent proof of a source's license or authenticity. Never
write daily closes, spot BTC, a weekly fixing, or guessed marks into this namespace.
No writer is added and no historical artifacts are rewritten.

The existing API settlement cron also checks human BTC holders using D1. The cron
schedule stays unchanged; a mark arriving outside its weekday window is retried on
the next scheduled run or authenticated `/api/settle` request. Equity settlement and
legacy default multipliers remain intact. Accounts with a BTC history are retained
on insolvency so an equity liquidation cannot erase pending BTC obligations.

## Original collector and execution-clock rollout

These are the original mixed-product rollout controls. For the separate owner-link
repair, use the migration/API/asset procedure above; its access policy supersedes
the original audience allowlist and it does not redeploy the collector.

This is a mixed collector/API/site change. Collector paths and its README are inside
the main collector's deployment scope: merging can trigger a producer rebuild.
Do not merge into an active collector session without the current continuity plan.

1. Pass exact-revision Web CI and Collector suite, including the mobile fixture and
   packaging and rollback checks.
2. Confirm account quote coverage, intended-audience display/use rights and permitted
   official final-mark source. Save sanitized evidence; retain any exact rejection.
3. Deploy backend readers first through protected `deploy.yml` / `wrangler.api.toml`,
   with entries disabled. No schema, binding or cron change is required by this code.
   Old collectors carry no change evidence, so the new API continues to reject
   their untimed quotes. Old APIs ignore the new evidence and also reject untimed
   quotes. Ship all three lanes before enabling owner acceptance; an old UI cannot
   explain the new clock. Ensure the PR301 subscriber fix is included.
4. Separately deploy the collector through its existing lane at an authorized safe
   lifecycle boundary. Record existing feed identity, subscription counts and fresh
   provider events before/after, reconnect behavior and any real observation gap.
   Do not start a duplicate collector or stream. Confirm the bounded BTC window and
   held-contract recovery alongside healthy QQQ/SPY/IBIT collection.
   Read option-only wire type counts from the authenticated private response, then
   observe baseline -> unchanged -> actual changed-side eligibility with source
   times still accurately null. Capture reconnect baseline rejection and receipt-
   age rejection without restarting a producer solely for a test. A natural
   reconnect or the authorized rollout can supply the baseline observation.
5. Publish exact-revision assets using protected `deploy-staged.yml` and the staged
   `payload/` layout in [SITE_RELEASES.md](SITE_RELEASES.md). No bare Wrangler deploy.
6. Enable only the approved audience, then new entries. First deployed acceptance:
   mobile weekend chain, exact contract/quote identities and sizes, one buy, reload,
   subsequent fresh bid close, fill IDs, cash movements, fees and net result. If no
   executable side exists, retain that observation and leave live acceptance pending.

Rollback starts by setting `BTC_OPTIONS_ENTRIES=0`. Preserve collector management,
the readers and settlement for existing positions. Asset rollback must retain a
way to manage those positions. Do not roll the API back to a revision that treats
futures options as equities or erase new ledger records. No combined-Worker restore,
new credentials, paid service, real order, KV operation or external message is part
of this assignment.

See [local verification receipt](BTC_PAPER_VERIFICATION.md) for exact tests and limits.
