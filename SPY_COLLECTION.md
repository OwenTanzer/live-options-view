# SPY collection: data contract (#121)

The collector publishes SPY's 0DTE chain and underlying context next to QQQ's,
over the same tastytrade/DXLink session. This file covers collection only. The
website and market MCP consume it in follow-up changes.

## Artifact layout

QQQ keeps every key it had before SPY was added, so existing consumers
(Crassus, synthetic days, diag page, website, market MCP) cannot read SPY
data by accident. SPY uses a parallel tree:

| Artifact | QQQ (unchanged) | SPY |
|---|---|---|
| Latest snapshot | `intraday/latest.json` | `intraday/spy/latest.json` |
| Archived snapshot | `intraday/YYYYMMDD/snapshot_HHMMSSffffff.csv` | `intraday/spy/YYYYMMDD/snapshot_HHMMSSffffff.csv` |
| Session-open mirror | `intraday/YYYYMMDD/first.csv` | `intraday/spy/YYYYMMDD/first.csv` |
| VWAP restart state | `intraday/YYYYMMDD/vwap_state.json` | `intraday/spy/YYYYMMDD/vwap_state.json` |
| Momentum log | `intraday/YYYYMMDD/momentum_log.jsonl` | `intraday/spy/YYYYMMDD/momentum_log.jsonl` |
| RVOL baseline | `baselines/qqq_rvol_buckets.json` | `baselines/spy_rvol_buckets.json` |
| Nearest-weekly archive | `raw/weekly/...` | not collected |

`intraday/spy/` never matches a QQQ `intraday/YYYYMMDD/` prefix, so per-day
listings (`restore_state`, archive readers) stay single-symbol.

## Payload

`intraday/spy/latest.json` has the same shape as QQQ's `intraday/latest.json`,
with these identity guarantees:

- top-level `"symbol": "SPY"`. QQQ's payload now also carries `"symbol": "QQQ"`;
  nothing else in it changed;
- `underlying_market.symbol` names the same underlying;
- `snapshot_key` points into `intraday/spy/YYYYMMDD/`;
- every row's `OptionSymbol` has the SPY root. Chain parsing drops any strike
  whose OCC root differs from the requested underlying, and logs a warning;
- `underlying_price` / `underlying_market.spot` come only from SPY's own
  DXLink quote. With no SPY quote they are `null`, never QQQ's price. SPY has no
  yfinance fallback, so its spot is either the live feed or a value restored
  from SPY's own archive with that archive's timestamp.

Timestamps, `snapshot_time`, `tier`, `expiration` and per-field freshness have
the same meanings as for QQQ.

## Isolation

All per-underlying state lives in `collector.UnderlyingSession`: volume deltas,
last spot, the first-snapshot guard, VWAP accumulator, RVOL buckets and
baseline, and the momentum window. Restart recovery reads only that symbol's
archive. Rows from another root are ignored. A `vwap_state.json` or RVOL
baseline tagged with another symbol is not loaded. At a new trading date,
SPY's in-process volume, spot, VWAP, momentum and first-snapshot state reset
before today's archive is restored, so each day gets its own `first.csv`.

The legacy macro price map (`_last_prices`) stays restored from QQQ only.
SPY is subscribed for its own spot and volume and gets its own entry in
`intraday/prices.json`, but is **not** added to `PRICE_TICKERS`. The existing
macro entries and `health.json` symbol counts are unchanged.

`health.json` retains its QQQ collector counters and adds a separate `spy`
block. SPY has its own counters and missed-snapshot tracker, so SPY activity
can't mask a missed QQQ snapshot. SPY failures show up in that block, logs,
and in `intraday/spy/latest.json` going stale.

## Warming up and missing data

- RVOL: SPY starts with no baseline. Buckets report `no_data` /
  `insufficient_history` until `RVOL_MIN_DAYS_REQUIRED` (5) completed sessions
  have been folded into `baselines/spy_rvol_buckets.json`.
- Momentum: `warming_up` until its lookback has samples, as for QQQ.
- VWAP: `vwap_partial_session` is true when collection starts after the open.
- If the SPY chain fails to load at session start, SPY is skipped for that
  session (logged) and QQQ continues unchanged. If today's expiration is
  absent, the error and `health.json` SPY block report
  `missing_today_expiration`; a next-day chain is never labeled 0DTE.

## SPY price tile and health

`intraday/prices.json` includes a `SPY` entry derived only from SPY's DXLink
quote. It carries the provider quote timestamp, or a null price/source when no
SPY quote is available. It never uses QQQ, yfinance or the legacy last-known
macro price map. The existing macro ticker list, QQQ CSV fields and legacy
`health.json` symbol counts stay unchanged.

`intraday/health.json` adds a `spy` block with collection status/reason, SPY
spot price and observation time/status, SPY snapshot upload time and cadence.
Spot is `live` only with a provider observation time no older than the
collector's existing stale threshold; otherwise it is `stale` or `unavailable`.
The website must also check the health artifact's `updated_at` before showing
it as current. `COLLECT_SPY=0` reports `disabled`. The `subscription_coverage`
field records the fixed startup reference spot and timestamp, available and
selected strike counts, selected strike range, and current spot status. A
missing fresh spot reports `spot_unavailable`; a fresh spot outside the fixed
range reports `out_of_range`. The same coverage evidence appears in a SPY
latest snapshot when one is written. `subscription_delivery` gives the pending
event/symbol pair count and last delivery error. An interrupted batch reports
`subscription_pending`, retains unsent pairs for retry or reconnect, and does
not publish SPY snapshots until delivery completes; QQQ collection continues.

## Configuration and load

- `COLLECT_SPY` (default on): `0` disables SPY collection without a deploy of
  new code.
- No new credentials. The same tastytrade OAuth session and DXLink feed carry SPY.
- Added provider load: one extra option-chain request per session. The feed
  first subscribes to the SPY underlying. Once it observes a fresh, timed SPY
  spot, the collector selects up to 67 distinct nearest strikes (lower strike
  wins a tie) and adds their calls and puts. This window is fixed for the
  session, including reconnects. For `K = min(67, N)` distinct chain strikes,
  the added load is at most `2K` option symbols and `8K+4` event/symbol pairs
  (four event types per option and four for the SPY underlying): **134 symbols
  and 540 event/symbol pairs maximum**. An offline 300-strike chain selects 67
  strikes and reaches that bound. With no fresh spot during the bounded startup
  wait, no SPY options are subscribed and SPY option collection reports
  `spot_unavailable` or `stale_startup_spot`; QQQ continues. If spot later moves
  beyond the selected range, coverage reports `out_of_range`; the collector
  does not silently expand the subscription. `STRIKE_WINDOW=33` remains a
  snapshot row filter and may yield no rows when spot moves far away. Batches
  of 200 are transport chunks, **not** a provider rate cap. Provider limits
  remain unverified.
- Added R2 load at the existing 60 s cadence: per SPY snapshot, 4 writes
  (CSV, `vwap_state.json`, `momentum_log.jsonl`, `latest.json`) and 1 read
  (momentum log). Plus one `first.csv` per day and one RVOL baseline write at
  session end. Cadence and limits are unchanged.
- SPY's snapshot runs right after QQQ's in the same loop, so each cycle
  (and QQQ's actual spacing) grows by SPY's write time (about 1-2 s). The
  missed-snapshot check allows 60 s of slack past the expected time.

## Verification status

- Fixture/local: `python tests/verify_collector_spy.py` (17 checks covering
  layout, full OCC/streamer identity, exact SPY expiry, subscription count,
  deterministic cap, failed-batch retry and reconnect, fresh spot and coverage,
  payload and state isolation, quote/tile/health evidence, missing data, RVOL
  warm-up, per-symbol restart recovery and new-day `first.csv`). The existing
  collector suites pass against `QQQ_SESSION`.
- Deployed: **not yet observed.** Live SPY support should not be claimed until
  `intraday/spy/latest.json` is seen updating with SPY rows during a session.

## Website (docs/index.html)

- A QQQ/SPY selector in the chain header drives the heading, page title,
  price, tier, VWAP/RVOL/momentum line, rows and visible-contract quotes. The
  choice is remembered per browser (`localStorage`, best-effort).
- QQQ's `intraday/latest.json` is still fetched every cycle whatever is
  selected. Paper trading, expiry settlement marks and the QQQ price tile use
  only QQQ data; SPY is fetched in addition only while SPY is selected.
- The separate, read-only SPY price tile uses only SPY's timed DXLink quote
  from `intraday/prices.json` or the live quote service. A missing quote is
  unavailable, and an old observation is stale. No SPY share ticket is enabled.
  The SPY health badge reads `health.json.spy` and rejects an old health
  artifact; missing expiration, missing snapshots and missing spot remain
  visible rather than borrowing QQQ health.
- Every snapshot is validated for the requested symbol before rendering
  (`validateChainPayload` in shared.js): its top-level symbol,
  `underlying_market.symbol` and every contract root must match. A mismatch,
  a missing SPY publication (404) or a failed first load is shown as an
  explicit unavailable state, never as the other underlying's data.
- Switching clears the chain immediately. Each request carries a selection
  token (`ChainSelection`), so a reply that arrives after a switch is dropped
  rather than rendered or cached. A transient fetch failure keeps a valid
  cached snapshot visible with a stale/error state, even when its data was
  only seconds old. Older overlapping QQQ replies cannot overwrite newer
  chain, indicator or paper-settlement state.
- The SPY chain is view-only (no paper tickets or position badges). Its OI
  colors are relative to SPY's own snapshot, because `derived/OIranges.csv` is
  calibrated on QQQ only. The header says so.
- The historical date view stays QQQ-only.
- Tests: `node tests/chain_underlying.test.js` (helpers, plus shipped
  fetch/render, price and health functions under DOM shims: QQQ→SPY→QQQ,
  delayed and stale replies, fresh-cache failures, mislabeled/mixed payloads,
  missing SPY, and SPY tile/health unavailable and stale states). A local browser
  run with fixture snapshots confirmed the selector, rows, quotes and that the
  QQQ tile keeps QQQ readings while SPY is shown.

