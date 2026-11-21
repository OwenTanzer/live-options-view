# Bitcoin monthly futures options — implementation and release record

November 21, 2026. Local work on `steve/monster-hunt-btc-paper`, based on
`0a3853df0692e8c9bdff762fc0267fc4f9eda447`. This is a disabled-by-default paper
product, not a broker order integration. No production change has been made.

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

**Live evidence remains pending.** This isolated implementation did not authenticate
to a brokerage or change the running feed. No actual instrument identity, exchange
quote, account coverage, licensed display audience or approved final-mark delivery
source was obtained. There is no observed entitlement rejection to quote; a fixture
403 is only a test. Fixture symbols and quotes are synthetic and must never be
presented as weekend market observations. Confirm the provider's actual BTC field
values, price units and monthly identity before enabling entries.

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
approval. Existing holders retain access to their positions after removal from the
entry audience. `BTC_OPTIONS_ENTRIES=1` independently permits new buys for that
audience; absent/zero blocks new entries while closes and settlement remain enabled.
The collector route also requires the existing `X-Live-Quote-Key`. The public quote
route refuses BTC keys/provider future-option symbols. Neither browser-data proxy
allowlist exposes the chain or settlement records. The disposable preview explicitly
allows the same-origin BTC API route; it never falls back to production.

The existing `/api/paper-trade` reservation/lease and D1 compare-and-swap mutation
are reused. Orders are long-only, exactly one contract per ticket, market or
immediately marketable limit. Cash and held quantity are checked inside the mutation.
There is a per-account cap of 64 distinct held BTC contracts. The server re-resolves
the selected key through the collector and rechecks freshness before mutation.
Only the execution side needs a positive price, size >= 1 and exchange/receipt times
within fifteen seconds; a missing opposite side is permitted, a crossed book is not.
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
4. Separately deploy the collector through its existing lane at an authorized safe
   lifecycle boundary. Record existing feed identity, subscription counts and fresh
   provider events before/after, reconnect behavior and any real observation gap.
   Do not start a duplicate collector or stream. Confirm the bounded BTC window and
   held-contract recovery alongside healthy QQQ/SPY/IBIT collection.
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
