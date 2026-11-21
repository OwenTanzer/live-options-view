# Prediction evidence (#293)

The owner-only production follow-up runs bounded public REST reads in the API
Worker with private D1 capture/ledger/replay. See the current
[Kalshi identity contract and release guide](../../docs/KALSHI_PRIVATE.md).
It shares the fixed-point model with this collector, but does not activate this
standalone producer or publish a shared feed. The original fixture producer
and licensing discussion below describe the broader, unactivated lane.

Independent Node producer, deliberately outside the shared Python image and all
existing capture processes. `collector.mjs` takes a private R2-compatible
`{get, put}` binding and normalized events. `run.mjs` runs **only synthetic
fixtures**, writes a new local archive directory, and reads no credentials:

```
node market_data/prediction/run.mjs events.json new-archive
node tests/prediction.test.mjs
```

The Dockerfile builds from repository root and has no network client or order
router. A licensed deployment supplies its read-only transport and private R2
binding to `Collector`; no Kalshi production or demo capture is enabled here.
Transport certification and the licensed production round trip remain release
acceptance, not fixture results. The documented
[Kalshi agreement §3](https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf)
does not grant this application's capture/replay/display rights. Keep collection,
retention, replay and authenticated display permission plus retention days and a
written receipt with deployment configuration. `Collector` requires all four
rights for either demo or production. No sample-count or performance gate.

## Contract and replay

First series is synthetic BTC spot, strictly above a fixed dollar threshold,
USD 1 payout, integer contracts. Prices use integer ten-thousandths of USD.
`kalshiSnapshot` consumes the documented `orderbook_fp.yes_dollars/no_dollars`
decimal strings. Unsupported fractional contracts or precision fails explicitly.
Rules name provider/environment/series/event/market, payout, increments, fee
convention, observation window, index, inclusivity, cutoff, source, published
time and version. Revised rules are retained. Current supported fees are a
declared fixed per-contract amount; production series using another fee formula
must add and test that convention before entry enablement.

Every normalized message has a unique `id`, `market_key`, `exchange_ts`,
`receive_ts`, and `kind`: rules, snapshot, delta, disconnect, status, resolution,
or observation. Deltas carry sequence, side, integer price and size change.
Snapshots carry a sequence plus the raw fixed-point book payload. Never invent
an exchange timestamp from the receive timestamp. Status supports open, halted,
closed, disputed, settled and delisted. An official resolution includes source,
ID, revision, exact rule version and YES/NO/VOID. Void refunds remaining entry
premium, not fees, under the explicit `refund_entry_cost` fixture rule.

The private prefix is `prediction/v1/{fixtures,demo,production}/`. Batches are
content-addressed, followed by an ordered manifest and current/resolution views.
Retries reuse the same digest; conflicting event IDs fail. Replay consumes
receive order and filters **both** timestamps at the cursor. Missing/corrupt
batches stop recovery. `restoreArchive` reads the manifest, verifies digests and
replays; restored books are unsynchronized until a new snapshot. `ingest` asks
the injected transport for a resnapshot on gaps/reconnects. A failed resnapshot
leaves the book blocked. Archive failure retains pending events and eventually
applies backpressure. The transport must flush after disconnect before waiting
for reconnection; the 15-second quote TTL bounds previously published books.

One owner per namespace is required. Do not run concurrent producers against
one manifest. Default bounds: 20 selected/held markets and 500 pending events;
flush at 100 in the fixture launcher. Subscription rollover must retain held
metadata until resolution and its correction-retention period, with a budget
for the next series. Health exposes last good sequence, gap/synchronization,
exchange lag, request count, archive failures, writes and batch bytes. Configure
private-bucket lifecycle retention from the license; never enable public R2
hosting or proxy these prefixes through browser-data. The retained manifest is
only valid while all referenced batches remain available. Rotate a completed
namespace generation before retention removes its batches; no silent gap replay.

## Evidence / One Clock

Every book/lifecycle observation emits `one-clock.prediction.v1` even without
positions. Explicit observation events can attach licensed options and spot
context. The catalog exposes the latest 200 observations; replay reconstructs
all observations from batch events. Numerical comparisons require matched index,
expiry, averaging window, currency, inclusivity, spot settlement and zero basis.
The discounted bracketing call spread uses executable bid/ask sides, positive
size and as-of timestamps. Futures/spot or quarterly/short-window mismatches
remain context with reasons, without a numerical gap. This is an interval
risk-neutral estimate; costs/risk premia/shared inputs remain caveats.

## Independent releases

1. Producer: obtain rights, configure a private bucket and one bounded owner,
   certify the transport with fixture restart/gap/rollover/weekend cases, then
   approve its independent deployment. Do not restart existing capture services.
2. Backend: add a **read-only** `PREDICTION_DATA` binding to that private bucket;
   no default production binding is committed. Select environment and rights
   receipt; keep display and entries disabled. Release through `deploy.yml`.
   The single API cron becomes 24/7 while equity sweep hours stay guarded.
3. Site: stage the exact tested revision through `deploy-staged.yml`. Enable
   display separately, then entries only after licensed acceptance.
4. Roll back entries with `PREDICTION_ENTRIES=disabled`; sales and official
   resolution continue. Disable display without hiding portfolio history.
   Asset rollback never restores a combined Worker. Keep backend event support
   and the private resolution source until held obligations/corrections end.

No merge, deploy, provider setting change, live order or licensed feed access is
part of offline engineering acceptance. Cross-branch Bitcoin futures-option
integration must preserve both instrument discriminators; this checkout never
modifies that worktree.
