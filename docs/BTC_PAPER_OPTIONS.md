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

`GET /api/btc-options` requires an Options View session. `BTC_OPTIONS_USERS` is a
comma-separated explicit audience allowlist, to be configured only after licensing
approval. Holdings never confer quote entitlement: removing a user from this
allowlist blocks both fresh quote reads and paper execution, while retaining the
ledger and settlement obligations. `BTC_OPTIONS_ENTRIES=1` independently permits new buys for that
audience; absent/zero blocks new entries while closes for entitled holders and
settlement remain enabled. The initial live trial is restricted to the verified
subscriber's own site account, with no other resident granted access merely
because they can sign in.
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

## Review and rollout controls

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
   quotes. Ship all three lanes before enabling the owner trial; an old UI cannot
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
