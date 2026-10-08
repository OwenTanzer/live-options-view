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
it as current. `COLLECT_SPY=0` reports `disabled`.

## Configuration and load

- `COLLECT_SPY` (default on): `0` disables SPY collection without a deploy of
  new code.
- No new credentials. The same tastytrade OAuth session and DXLink feed carry SPY.
- Added provider load: one extra option-chain request per session. **Current
  code subscribes every returned SPY strike**, calls and puts, even though
  `STRIKE_WINDOW=33` limits only snapshot rows when SPY spot is available.
  With no spot, even that row filter is inactive. For `N` distinct SPY strikes,
  the added load is `2N` option symbols and `8N+4` event subscriptions (four
  event types per option and four for the SPY underlying). The collector logs
  the actual session count. An offline 300-strike fixture yields 600 option
  symbols and 2,404 event subscriptions. Batches of 200 are transport chunks,
  **not** a subscription or provider rate cap. There is no configured SPY
  subscription maximum yet; provider limits and live chain size are unverified.
- Added R2 load at the existing 60 s cadence: per SPY snapshot, 4 writes
  (CSV, `vwap_state.json`, `momentum_log.jsonl`, `latest.json`) and 1 read
  (momentum log). Plus one `first.csv` per day and one RVOL baseline write at
  session end. Cadence and limits are unchanged.
- SPY's snapshot runs right after QQQ's in the same loop, so each cycle
  (and QQQ's actual spacing) grows by SPY's write time (about 1-2 s). The
  missed-snapshot check allows 60 s of slack past the expected time.

## Verification status

- Fixture/local: `python tests/verify_collector_spy.py` (13 checks: key
  layout, symbol-bound chain parsing, SPY-only chain request with no weekly,
  payload identity, cross-symbol volume-delta/VWAP isolation, missing SPY
  spot, RVOL warm-up and foreign-baseline rejection, per-symbol restart
  recovery). The existing collector suites pass unchanged against
  `QQQ_SESSION`.
- Deployed: **not yet observed.** Live SPY support should not be claimed until
  `intraday/spy/latest.json` is seen updating with SPY rows during a session.

