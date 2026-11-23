# Extended price lane (#178): continuous collection and references

Issue: [#178](https://github.com/OwenTanzer/live-options-view/issues/178). The
lane extends the accepted 100-instrument price artifact beyond the QQQ/SPY
option-chain session. It is price-only work: option chains, the return scanner,
Tradier and trading keep their existing schedules.

## Architecture

`market_data/live_market/price_lane.py` runs one long-lived daemon thread,
started from `main()`. It owns:

- one tastytrade OAuth/DXLink connection with **price subscriptions only**
  (no option symbols are ever passed to `set_subscriptions`);
- the `intraday/prices.json` write, with `price_lane.writer_id` in the payload;
- `intraday/price_lane_health.json`, throttled to one write per 30 seconds and
  forced on failure;
- `intraday/price_references.json`, a bounded rolling-history companion.

`market_data/live_market/price_sessions.py` supplies the explicit per-family
session labels. `market_data/live_market/price_history.py` supplies the rolling
24-hour references. Neither module can start collection; they are pure
evaluation.

**One writer.** `runner.price_writer_plan()` is the only decision point.
`continuous_price_lane` starts the lane from `main()`; `in_session_legacy`
starts the old `prices_loop` inside `_run_session()`. The two paths cannot both
be selected, so a second publisher is not a configuration accident. The
artifact's `price_lane.writer_id` makes a foreign writer visible rather than
silent.

### Rollback

Set `PRICE_LANE_ENABLED=0` and redeploy the collector. The chain session
publishes prices again exactly as before. Off-hours equity/futures/crypto
coverage stops while the lane is disabled; there is no dual-writer fallback by
design. The lane does not change any option-chain setting, so rollback of this
feature is one environment variable.

## Supported sessions

| Family | Window (America/New_York) | Label |
|---|---|---|
| US equities/ETFs | 04:00–09:30, 09:30–16:00, 16:00–20:00 on NYSE trading days | `premarket`, `regular`, `afterhours` |
| Early closes | 13:00 on the verified dates in `EQUITY_EARLY_CLOSES` | `regular` then `afterhours`, `early_close: true` |
| CME energy/metals futures | Sunday 18:00–Friday 17:00, daily 17:00–18:00 break | `overnight`, `regular` inside 09:30–16:00, `maintenance` |
| Crypto | continuous | `continuous` |
| Spot FX | Sunday 17:00–Friday 17:00 | `overnight` |
| Daily series (Yahoo/Treasury) | retrieval, not a tick | `daily` |

Holidays come from the published exchange calendar; a non-trading weekday is
`holiday`, a weekend is `closed`. Calendar failure yields `unknown` and
`eligible: false` for equities — it never guesses an open market. Futures
holiday/early-close rules are an explicit table, not a weekday heuristic.

`market_data/live_market/price_sessions.py` is deliberately ET-wall-clock based,
so daylight saving changes move the UTC offset, not the 04:00/09:30/16:00/20:00
boundaries. `PriceLane` keeps its production clock in UTC for publication and
history, and converts each instant to ET before any calendar or session call.

Futures contracts are reconciled while the feed stays healthy. The lane
re-resolves on a bounded six-hour/ET-day cadence, compares the shared mapping
with its own subscription snapshot, and replaces only its own DXLink connection
when a roll or a chain-session resolution changes the active month. The payload
therefore names the contract the lane is actually subscribed to.

### Resolution integrity (November 9 repair)

The provider is the only authority for the active month, and a resolution
**gap is not a roll**. `resolve_futures()` builds a complete candidate mapping
off to the side and commits it atomically; a request that fails, returns no
items, or leaves a product without an eligible active contract preserves the
last good mapping and records a bounded retry (60 s doubling to 900 s). A
still-valid prior contract is carried for an unresolved product; an expired
prior is dropped rather than resubscribed.

The lane treats a snapshot that only *loses* contracts as a resolution gap: it
keeps its current subscriptions, records `contract_gap_reason` and
`contract_retry_at` in health, and retries on the bounded cadence. A preserved
map that looks unchanged does not clear that deadline — only the resolver
reporting `ok` does, so a degraded discovery keeps retrying across cycles. A
snapshot with a genuine change is actionable even while a retry is pending;
only a failed replacement feed suppresses another attempt. A replacement feed is
started before the old one is stopped, so a failed rebuild cannot leave the lane
with no subscriptions. Health now carries `contract_resolution` (state, last
error, retry, per-label symbols) alongside `contracts`.

The resolution token is refreshed rather than reused: an access token issued at
startup is long expired six hours later (tastytrade access tokens last 15
minutes). A renewal persists the new credentials for every later REST attempt
and for any rebuild; the live feed keeps the streamer token it connected with.

The live November 9 failure this repairs: the 17:54:07Z six-hour reconciliation
produced a null mapping, the lane rebuilt onto it, and every futures row stayed
frozen at its 17:53:5xZ print for the rest of the evening while publication and
health stayed green.

## Provider capabilities and cadence

The November 9 bounded probe verified routes, not sustained entitlement:
82 of 89 probed symbols returned prices, including seven dated futures and three
crypto pairs. Extended-hours and weekend continuity remains a **deployment and
market-time observation**, not something fixtures can certify.

Bounds:

- publication every `PRICES_SECS` (10 s), unchanged;
- 82 price symbols × 4 event types = 328 DXLink subscriptions, one websocket;
  during the chain session this is a second connection alongside the option
  feed, not a replacement for it;
- OAuth access tokens refresh on their own lifetime; auth attempts are capped at
  four per hour window;
- failures back off 60 s, 120 s, 240 s … capped at 30 minutes;
- the rolling-reference object holds at most 150 ten-minute buckets per crypto
  instrument (three instruments, roughly 10 KB);
- lane health writes are throttled to 30 s.

## References

Every row keeps its existing `reference`. Additive fields are:

- `market_session`: the family session label above;
- `change_basis`: `rolling_24h`, `previous_close_unverified`,
  `prior_day_close_same_contract`, `contract_reference_unverified`,
  `dated_or_unknown`;
- `rolling_reference` and `rolling_change_pct`: crypto only.

A rolling reference is returned only when a ten-minute sample exists within
30 minutes of the 24-hour mark, for the same instrument. Before that, the value
is `null` and the comparison is unavailable — never 0% and never a prior UTC
day wearing a 24-hour label. Futures changes require the same resolved contract;
an unresolved or rolled contract cannot produce a comparison.

### Extended-session price basis (November 9 repair)

A last trade is a **current** equity price only when it carries a provider event
time inside the active session. During premarket and after-hours a trade that
predates the session (or has no provider time at all) is last session's close,
and publishing it while the venue trades is the defect Owen reported: the live
November 9 artifact carried the 15:59:59 ET closing print for all 71 streaming
equity rows across 2.7 hours of after-hours while two-sided quotes advanced.

In `premarket`/`afterhours` the selector therefore uses a **qualified two-sided
quote** as the price when the trade is not from the current session:

- both sides present and ordered, and both collector receipts inside
  `EXTENDED_QUOTE_MAX_AGE_SECS` (the feed stale window);
- spread at or under `EXTENDED_QUOTE_MAX_SPREAD_PCT` — a 13% after-hours quote
  (IGV) is a placeholder, not a price;
- `price_basis: bid_ask_midpoint`, `observed_at` from the **older** side's
  provider time only when both sides carry one; when the feed supplies none,
  `observed_at` stays `null` with `provider_quote_time_unavailable` and the row
  is **unknown**, never live. Receipt time is never promoted to observation
  time.

If no qualified quote exists, the stale close is retained rather than replaced.
Regular-session rows are unchanged, and futures/crypto/FX/daily families keep
their provider-timed or explicitly dated selection.

### Extended trade prints (TimeAndSale)

The lane also probes the documented same-feed `TimeAndSale` route rather than
declaring the provider incapable. It subscribes trade prints for US equity/ETF
symbols only, on a **separate DXLink channel with its own `FEED_SETUP`**, so a
field or event-type rejection there cannot disturb the accepted quote/trade
feed. The channel number is **3**: dxLink requires odd numbers for service
channels. `PRICE_LANE_TIMESALE=0` disables the probe without reverting the
release.

A print is usable only when it carries its own provider `time` and a positive
price. Identity is dxFeed's unique per-symbol index (time plus sequence) or a
non-zero `tradeId`, preserved as an exact integer: identity fields are never
round-tripped through a float, and fractional ids are rejected, so adjacent
values above 2**53 cannot merge into one identity. `tradeId` 0 means
unavailable and never matches. The flags word supplies the transactional type
(NEW, CORRECTION, CANCEL), the extended-hours bit, the valid-intraday-tick bit
and the spread-leg bit; the explicit derived fields are accepted when present.
Non-valid ticks, spread legs, unknown transactional types and
corrections/cancellations without an identity are rejected. A NEW print moves
the price only forward in `(time, sequence)` order — a later print in the same
millisecond wins on its higher sequence, an equal or lower sequence at the same
time is rejected, a duplicate identity is not a new print, and an equal-time
event without a sequence on either side fails closed rather than being guessed
later. A CORRECTION applies only to the print it identifies (an unrelated or
older correction cannot rewind the latest), and a CANCEL removes the print it
identifies even when it carries no price. A matching correction that cannot be
accepted — non-valid tick, spread leg, non-positive or non-finite price —
invalidates the print it supersedes instead of leaving the old price selected.
The collector receipt is stored beside the print but never substitutes for the
provider time.

Selection uses a print only in premarket/after-hours, only when the print's
provider time is inside the active session, only when the provider did not mark
it regular-session, and only when it is newer than the snapshot Trade event.
Otherwise the qualified-quote or stale-close path above is unchanged. Health
reports `timesale.requested/subscribed/error/counts` and the observed event
fields, so a deployment states whether prints arrive with real timestamps
instead of inferring it. Local access evidence (no session credential, Railway
unauthorized) is in `docs/evidence/timesale-probe/2026-11-09-access.md`.

Provider evidence, and what it does not prove: the lane subscribes Quote, Trade,
TradeETH and Summary, and requests `bidTime`/`askTime` on Quote and `time` on
both trade types. In the observed 2.7-hour after-hours window no TradeETH event
arrived for any of the 71 streaming equity rows, and QQQ's chain-session
provenance at 16:14 ET records bid/ask provider times as null while receipts
advance. That is consistent with two documented facts but is not proof of
entitlement. dxFeed defines TradeETH as the extended-hours last trade and states
that during extended hours the Trade event updates only day volume and turnover;
its price stays at the regular close
(https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/TradeETH.html).
dxFeed's Quote does carry `bidTime`/`askTime`
(https://get-prod-web-rt.dxfeed.com/realtime/javadoc/com/dxfeed/event/market/Quote.html).
tastytrade's DXLink documentation, however, lists Quote, Trade, Summary, Greeks,
TimeAndSale, Profile and Candle, and notes that which types a feed returns
depends on the feed and token
(https://developer.tastytrade.com/docs/concepts/streaming/).

So the honest statement is narrower: this feed and window produced no
extended-hours trade prints and no observable quote timestamps, and the
mechanism matches the documented semantics. Whether TradeETH is unavailable on
this gateway, unattributed to this token, or simply quiet is not established.
Feed health now reports event counts and the exact field names observed per
event type, so the deployment answers that instead of inference. If further
same-feed investigation is wanted, TimeAndSale is the documented stream to test
before any provider change; no paid provider is recommended on this evidence.

## Verification

Offline: `python -m unittest discover -s tests -p test_price_lane.py` covers
boundaries, holidays, early closes, DST, maintenance weekends, reconnect
backoff, auth budgets, chain isolation, rolling warm-up/persistence and payload
compatibility. `node --test market_mcp/overview-v2.test.js` covers the additive
consumer fields.

Deployment acceptance still requires observed automatic publications and
authenticated reads during premarket, after-hours, an overnight futures session
and a crypto weekend, plus reconnect/restart recovery. Record actual revisions,
timestamps and artifacts; local fixtures and off-session probes are not
sustained operational acceptance.
