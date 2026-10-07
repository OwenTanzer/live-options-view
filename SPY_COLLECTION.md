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
baseline tagged with another symbol is not loaded.

The shared macro price strip (`intraday/prices.json`, `_last_prices`) stays
restored from QQQ only. SPY is subscribed for its own spot and volume but is
**not** added to `PRICE_TICKERS`, so the price strip and `health.json` symbol
counts are unchanged.

`health.json` stays QQQ-only: SPY has its own counters and missed-snapshot
tracker, so SPY activity can't mask a missed QQQ snapshot. SPY failures
show up in logs and in `intraday/spy/latest.json` going stale.

## Warming up and missing data

- RVOL: SPY starts with no baseline. Buckets report `no_data` /
  `insufficient_history` until `RVOL_MIN_DAYS_REQUIRED` (5) completed sessions
  have been folded into `baselines/spy_rvol_buckets.json`.
- Momentum: `warming_up` until its lookback has samples, as for QQQ.
- VWAP: `vwap_partial_session` is true when collection starts after the open.
- If the SPY chain fails to load at session start, SPY is skipped for that
  session (logged) and QQQ continues unchanged.

## Configuration and load

- `COLLECT_SPY` (default on): `0` disables SPY collection without a deploy of
  new code.
- No new credentials. The same tastytrade OAuth session and DXLink feed carry SPY.
- Added provider load: one extra option-chain request per session; about
  134 extra option symbols (STRIKE_WINDOW ±33 around spot, calls and puts) ×
  4 event types, plus SPY's own 4 underlying event types, on the existing
  websocket (subscriptions are already sent in batches of 200).
- Added R2 load at the existing 60 s cadence: per SPY snapshot, 4 writes
  (CSV, `vwap_state.json`, `momentum_log.jsonl`, `latest.json`) and 1 read
  (momentum log). Plus one `first.csv` per day and one RVOL baseline write at
  session end. Cadence and limits are unchanged.
- SPY's snapshot runs right after QQQ's in the same loop, so each cycle
  (and QQQ's actual spacing) grows by SPY's write time (about 1-2 s). The
  missed-snapshot check allows 60 s of slack past the expected time.

## Verification status

- Fixture/local: `python tests/verify_collector_spy.py` (8 checks: key
  layout, symbol-bound chain parsing, SPY-only chain request with no weekly,
  payload identity, cross-symbol volume-delta/VWAP isolation, missing SPY
  spot, RVOL warm-up and foreign-baseline rejection, per-symbol restart
  recovery). The existing collector suites pass unchanged against
  `QQQ_SESSION`.
- Deployed: **not yet observed.** Live SPY support should not be claimed until
  `intraday/spy/latest.json` is seen updating with SPY rows during a session.

## Website (docs/index.html)

- A QQQ/SPY selector in the chain header drives the heading, page title,
  price, tier, VWAP/RVOL/momentum line, rows and visible-contract quotes. The
  choice is remembered per browser (`localStorage`, best-effort).
- QQQ's `intraday/latest.json` is still fetched every cycle whatever is
  selected. Paper trading, expiry settlement marks and the QQQ price tile use
  only QQQ data; SPY is fetched in addition only while SPY is selected.
- Every snapshot is validated for the requested symbol before rendering
  (`validateChainPayload` in shared.js): its top-level symbol,
  `underlying_market.symbol` and every contract root must match. A mismatch,
  a missing SPY publication (404) or a failed first load is shown as an
  explicit unavailable state, never as the other underlying's data.
- Switching clears the chain immediately. Each request carries a selection
  token (`ChainSelection`), so a reply that arrives after a switch is dropped
  rather than rendered or cached.
- The SPY chain is view-only (no paper tickets or position badges). Its OI
  colors are relative to SPY's own snapshot, because `derived/OIranges.csv` is
  calibrated on QQQ only. The header says so.
- The historical date view stays QQQ-only.
- Tests: `node tests/chain_underlying.test.js` (helpers, plus the shipped
  fetch/render cycle under a DOM shim: QQQ→SPY→QQQ, delayed and stale replies,
  mislabeled/mixed payloads, missing SPY, transient failures). A local browser
  run with fixture snapshots confirmed the selector, rows, quotes and that the
  QQQ tile keeps QQQ readings while SPY is shown.
