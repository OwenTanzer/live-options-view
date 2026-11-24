# Private Kalshi personal research — release handoff

This subsystem grants access to a single configured GitHub principal for private
trading research and isolated simulated fills.

## Credit-origin contract (supervisor's independent patch)

The main app requires exactly:

```
GET https://prolific-essence-production.up.railway.app/api/private/identity
Cookie: __Host-cds-session=<existing opaque session>

200 application/json
Cache-Control: private, no-store
{"github_id":125096971}
```

The credit service must validate its existing server-side session and return the
numeric GitHub identity established by its OAuth callback. Missing, expired,
revoked or unauthorized sessions return 401. Never derive identity from a
request parameter, forwarded user-ID header, login name or browser assertion.
No token, email, display name, session metadata or market data belongs in the
response. Do not return CORS headers. Do not log cookies. Reject methods other
than GET. The main app rejects any other ID, including a numeric string.

This endpoint belongs on the existing credit branch, not in this checkout.
The deployed credit-origin change is tracked in PR #308. Do not
add it to `backend/credit-tape.js`'s browser proxy allowlist. The main API calls
the fixed HTTPS host server-side, forwarding **only** one opaque session cookie,
without redirects; it never forwards the normal Options View session, flow
cookie, credentials or browser-provided identity headers. Tokens stay HttpOnly.
No dedicated OAuth app, Kalshi API key or new secret is needed in the main app.

`/kalshi.html` is a public empty shell. Sign in opens the existing Credit Tape
GitHub flow in a new tab. Return and press **Check sign-in**. Sign out uses the
existing same-origin Credit Tape POST logout and verifies that introspection
now refuses. Every private API call independently checks identity. Ordinary
Options View login, Object A seats, alternate Workers hostnames, fixture preview
and copied user-ID parameters are not entitlement.

## Main-app release and configuration

This is a reviewed mixed API/assets change. Follow [SITE_RELEASES](SITE_RELEASES.md)
and use the protected lanes, exact revision and normal supervisor approval.
No bare Wrangler deploy, combined Worker, collector restart or Railway change.

1. Record actual serving API/assets revisions and existing DB/cron bindings.
   Source declares `DB` → `live-options`, database ID
   `94c7c9ad-228b-471b-b760-48cd18c58d1b`. That is repository configuration;
   the supervisor must compare the current provider binding before migration.
2. Apply `backend/migrations/0006_kalshi_private.sql` once to that DB through
   the supervisor's authorized migration procedure. It adds only private
   evidence/state/budget tables. Existing accounts/sessions remain intact.
3. Independently verify credit-origin identity introspection: Owen numeric ID,
   no session 401, other user denied, revoked session 401, no sensitive response
   fields and no redirect/token forwarding. Retain only redacted verdicts.
4. API release uses `deploy.yml` with the exact tested revision and
   `wrangler.api.toml`. Source config sets `KALSHI_PERSONAL_ENABLED="1"` and
   `KALSHI_ENTRIES="1"` under Owen's explicit personal activation authority.
   Code defaults closed if settings, private DB or identity proof are absent.
5. Release assets through `deploy-staged.yml`, then read back `/__environment`
   and the served asset revision. Check phone sign-in, Discover, exact terms,
   book and sizes, one owner-requested paper entry, refresh/reload, archive and
   sign-out. Do not create a venue order or a synthetic production fill.
6. In the actual owner browser, verify `/api/kalshi/portfolio` is separate from
   the ordinary Options View balance. In an anonymous/other-user browser,
   confirm all private data endpoints deny; directly test Workers aliases,
   `/r2-proxy/` and `/browser-data/` cannot retrieve these records. Keep quote
   bodies/screenshots and cookies out of GitHub, Git, Notion and release logs.

The API owns two schedules. The quarter-hour trigger retains settlement and existing One Clock jobs. The minute trigger runs only private event-market capture when `KALSHI_MINUTE_CAPTURE=1`. Disable that flag to stop minute sampling independently. Supervisor owns deploy,
live OAuth acceptance and production paper-trial observation. Source tests
are not evidence those production steps occurred.

## Source and executable evidence

Read-only transport calls only `https://external-api.kalshi.com/trade-api/v2`.
There is no order/portfolio provider client and no Kalshi credential. Discovery
samples at most two open events per allowlisted series; weather takes three
outcomes per event, other subjects six. The 16 series in `SERIES_SUBJECT` include 15-minute Bitcoin, Ether, Solana and XRP
up/down contracts. Use **Update markets** to discover the next window after a
cutoff; price refreshes only read the currently visible tickers. Other series span
space, mathematics, music, games, geology, economics, climate, health and four
city daily highs. This is bounded discovery, not an exhaustive venue scanner.
No volume threshold excludes small contracts. Unsupported contract shapes stay
non-executable: only USD 1, non-provisional, non-multivariate binaries, known
tick ranges, exact terms/source and supported taker fees qualify.

The adapter reads market, event, series, event/series fee changes and order book. List
prices and last-trade prices never become executable quotes. It preserves
provider payloads privately with request/receive times. Rule availability is
conservatively the observation receipt; the original market creation date is
separate and is never used as proof of a later rule revision's publication.
The rule content digest excludes the receipt clock, so unchanged terms retain
their version across observations. The REST order book has
**no exchange timestamp or sequence**: `exchange_ts` and `sequence` remain null.
`provider_response_ts` is the HTTP Date, clearly labelled as such. A response
must arrive within five seconds, have no positive cache Age and have a valid
provider Date within ten seconds. Quotes expire after ten seconds, must precede
close, and are checked again inside the ledger mutation. Browser prices disappear
when expired or the tab loses visibility. This is a fresh REST snapshot, not
proof of the last individual order's exchange update time.

YES ask is one dollar minus the best NO bid; the opposite level supplies size.
NO is reciprocal. Fractional displayed size is rounded down to whole contracts;
the paper order itself must be a whole number. No depth walking, partial fills,
shorting, resting orders, synthetic quotes or client-supplied execution prices.
Each distinct request obtains a fresh server snapshot. The simulator assumes
no market impact; it does not decrement the actual venue book.

Fees use the official July 7, 2026 direct-member taker schedule,
`ceil_0.0001_USD(M × 0.07 × C × P × (1−P))`, implemented with BigInt arithmetic.
The freshly read series multiplier/type and effective event overrides are
retained on each fill. Cleared overrides restore series defaults. Incomplete
fee pagination, unknown types/precision and pending effective changes at commit
fail closed. `quadratic_with_maker_fees` uses its taker coefficient; no maker
execution is simulated. A valid future waiver expiry is honored. The prior
cent-rounded fee table is not the current direct-member convention.

Sources: [order book](https://docs.kalshi.com/api-reference/market/get-market-orderbook),
[series fees](https://docs.kalshi.com/api-reference/market/get-series),
[event overrides](https://docs.kalshi.com/api-reference/events/get-event-fee-changes),
[official fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf),
[official market/settlement fields](https://docs.kalshi.com/api-reference/market/get-market).

## Private ledger, replay and settlement

All records stay in existing private D1. There are **zero R2 or KV operations**
on this path, and no plaintext records in a nominally private public-bucket
prefix. The immutable namespace is `kalshi:personal:125096971`. Account creation
requires validated OAuth identity, uses a name unavailable to public registration,
and has no password. Both ordinary password login and ordinary session resolution
explicitly reject this account kind. Initial paper cash is $10,000; no Kalshi
balance is read or changed.

The shared account CAS writer serializes balance/position changes and request-ID
deduplication. Retries return the original fill without requoting. Fills preserve
principal, exact rules/version, quoted sides, displayed size, fee inputs,
request/receive/provider clocks and an immutable archive digest. The raw evidence
is stored once in a private archive row pinned by the fill.
Failed archival blocks the debit. A competing close cannot oversell. New entries
reserve D1 row capacity for subsequent exits; capacity failures never discard
provenance. Archived snapshots are digest-verified and individually replayable;
they are never relabelled executable or used for a current fill. There is no
continuous book reconstruction between REST samples. Numerical options comparisons
remain explicitly unavailable where no matched settlement basis exists.

The cron visits at most two selected/traded markets per invocation, round-robin,
and retains settled-market checks for corrections. Official settlement is read
separately so a missing book or unsupported current fee does not block it.
Only `finalized`, non-provisional records with an official YES/NO result,
matching $1/$0 payout and valid settlement time can settle. Official early close
time supersedes the entry's scheduled cutoff. Delayed, disputed, void, scalar
or unsupported results remain pending; this code never guesses a refund. An
official correction appends a cash adjustment and retains original fills and
prior resolution evidence. A never-traded market does not create a holding.

Interactive and settlement bounds: shared 60 provider reads/minute, 2,000/day for those clients; six detail reads and six order
attempts/minute; one discovery/minute; 400 archived observations/day; 100 KB
per evidence document; seven-day observation retention; latest 100 observations
listed; 20 lifetime traded markets and 1,000 ledger records per paper trial.
New buys also stop before the account reaches 500 KB, preserving exit headroom
under D1's per-row limit. Full fill provenance survives observation pruning.
Plan a deliberate account rollover when trial capacity is reached, never reset
cash or erase prior fills as routine cleanup. Budgets persist across tabs/restarts.
Cron uses at most fourteen provider reads per tick; it captures only while enabled.
No existing producer, capture universe or provider subscription is touched.

## Checks and rollback

```
node --test tests/kalshi.test.mjs
node tests/kalshi_browser.test.mjs
node tests/prediction.test.mjs
node tests/prediction_ui.test.mjs
python tests/test_changed_paths.py
```

Browser tests inject conspicuously labelled synthetic data into disposable SQLite
and exercise actual handlers at phone/desktop sizes; screenshots go to the OS
temporary directory, never into the repository. Fixtures cannot be selected by
production configuration. Run the affected API, BTC, equity, no-KV, capture and
asset/preview regressions from Web CI on the final reviewed revision.

Under Owen's existing personal-read authority only:
`node scripts/check-kalshi-public.mjs --personal-read-only` checks the real public
adapter using transient memory. It emits schemas/counts/verdicts, no prices,
contract identities, quote records, credentials, paper orders or production writes.
The November 21 15:51:49 UTC check found 61 candidates across 12 selected series
and a valid sized book; it confirmed absent exchange timestamps and the supported
current fee convention. This is public transport conformance, not authenticated
production acceptance or a fill receipt.

For entry rollback set `KALSHI_ENTRIES="0"` in the API release configuration;
authorized exits, history and official settlement continue. For full emergency
pause set `KALSHI_PERSONAL_ENABLED="0"`; this stops capture and settlement as well
as access, preserving D1 data. Restore the same principal and compatible reader
to resume. Keep credit identity validation working for ordinary entry rollback.
Asset rollback uses a compatible separated artifact and never rolls back D1,
erases history or reintroduces the combined Worker/KV.

## Local review receipt — November 21, 2026

- At `37ba9bcdc4430bd777eb6fa6575d27ec568b7c5e`, an isolated LF checkout passed
  102 tests across the private lane, prediction fixtures/UI, D1, auth/logout,
  Worker logic, ordinary paper trading, multi-underlying, account closure, BTC
  options, no-runtime-KV, capture boundary, credit proxy, bounded R2 proxy and
  settlement cron suites. All thirteen private-lane tests and both rendered
  phone/desktop workflows passed again after the rule-availability correction.
- Changed-path checks passed 79/79. `git diff --check` passed. Phone rendering
  was visually inspected; synthetic evidence is prominently labelled, controls
  fit the viewport, stale prices disappear and logout clears private content.
- The isolated asset/preview/deployment run passed 84/88. Four local environment
  failures remain for final-head CI: `jq` is absent for two selector tests;
  the historical combined-checkout hash differs under Windows auto-CRLF; port
  8787 is occupied for the loopback test. An earlier run also saw exact-revision
  failures while concurrent supervisor commits changed HEAD; the isolated run
  removed those. No running preview/service was stopped to force a pass.
- The real public read-only conformance check passed again at 15:56:46 UTC after
  series fee-change handling was added. No identity was forged, no fill was
  created, no production DB was touched, and no quote record was printed/saved.
- This receipt does not attest deployment, credit-origin endpoint availability,
  a completed owner OAuth round trip, or a production paper fill. Those remain
  the release supervisor's live acceptance responsibilities. No outward message
  or Notion update was sent in this implementation pass.

## Sign-in return

Kalshi starts the shared GitHub login in the same tab with `?return=kalshi`.
The API proxy records a ten-minute HttpOnly, Secure, SameSite=Lax navigation
hint using the existing desk-return cookie. Only fixed `kalshi` and `btc` keys
map to local destinations; a plain Credit Tape login clears the hint and
returns to `/credit-tape/`. Callback denial is never rewritten. The upstream
service still owns OAuth state, PKCE, owner authorization and session creation.
Callback and logout clear the hint. No upstream service restart is required.

## Contract chain

The owner desk shows a 12-contract page with YES/NO bids, asks and whole-contract
sizes in cents. YES is green; NO is red. Each row has quantity and direct paper
buy/sell buttons. Sell is enabled only for sufficient held quantity; all actions
also require a fresh, open, sized display book. The server independently fetches
rules, fees and a fresh execution book for every new order. A display response
never authorizes a fill. Unknown-response retries keep their execution ID.

`POST /api/kalshi/quotes` accepts at most 16 distinct supported tickers and uses
one [batch book request](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks).
Identity, same-origin and maintenance fences apply before the route. Response
identities must match exactly; malformed/crossed books yield unavailable rows.
Missing sides stay unavailable; they never become zero-priced quotes. Decimal
sizes round down using the existing adapter. This read does not archive every
chain page or change the background selected market.

The visible page refreshes every ten seconds, pauses while hidden or after five
minutes without interaction, and stops automatic retries after errors. Refresh
prices resumes it. Display prices expire after ten seconds and disappear on tab
hiding. Limits are ten chain requests/minute plus the unchanged global provider
shared 60/minute ceiling and interactive 2,000/day cap. Existing account/order limits, fee checks, owner
identity, archive/settlement writes, and paper-only execution remain unchanged.
No producer, schema or credential changes. Deploy the API first, then assets;
old assets remain compatible with the new API. Roll back assets independently.

## One Clock replay

Open the private Kalshi archive from the historical cursor to inspect retained captures through that millisecond. The page keeps the existing owner sign-in and reads only the archive endpoint. Both the paginated index and evidence-by-ID lookups apply the cutoff on the server. The receipt records capture time. Retention and the source’s sampling policy limit coverage. The standalone live view keeps its existing order and quote behavior.


## Event-market data on the One Clock cursor

Every successful live discovery and displayed batch quote is archived before
the response is returned. Detail and paper-fill evidence keep their separate
400/day budget. Display archives allow up to 2,000 receipts/day. Historical
requests use the authenticated archive only. Receipt time gates both the index
and the individual record. A scheduled minute is a sampling label, not the
time at which a quote became available.

With the minute trigger enabled, the API samples all unexpired tickers in its
supported discovery catalog in batches of 16 even with the browser closed.
The catalog refreshes each quarter-hour. Failed series and rolling crypto
series with no open window retry on the next tick. Each minute retains a
catalog reference, per-batch receive times, best bids/asks and displayed sizes.
Missing batches, rejected books and discovery failures remain visible gaps.
A delayed trigger cannot create a historical book using today's live price.
Ordinary observations expire after seven days; ledger-linked fill evidence is pinned.

The scheduled client reserves provider reads once per invocation. Its separate
18,000/day cap covers discovery and books across the 16 selected series while
the 60/minute limit remains shared. Apply additive migration
`0015_kalshi_minute_capture.sql` before enabling this API revision. It creates
two reusable capture lease slots and an index linking retained minutes to their
catalog. A 15-second lease permits an abandoned capture to be reclaimed in the
same minute. Ownership is checked when publishing. A catalog stays available
until its last dependent minute ages out. This adds no venue credentials,
public catalog publications, KV, or producer processes. Validate the minute
and quarter-hour triggers separately during protected API release, then use the
private One Clock replay on both sides of a crypto rollover.
