# Single-stock historical backfill collector

Backfills, then maintains, per-stock historical candles for the 101
NASDAQ-100 listings from Tiingo, and publishes them to R2 as
`nasdaq/candles/<TICKER>/<interval>.json` — one file per interval
(1m, 3m, 5m, 9m, 15m, 30m, 1h, 2h, 4h, 1d, 1w), plus one
`snapshot_provenance.v1` sidecar per ticker
(`nasdaq/candles/<TICKER>/provenance.json`). The Stocks panel reads the latest digest-verified interval and links to the same symbol and interval in Event Builder. On the One Clock page choose a ticker and trading session under **Replay a stock candle archive**. The session cursor follows each acknowledged revision. Later corrections and withdrawals take effect at their own receipt times. Previously saved interval and receipt files can also be inspected offline. The publisher retains immutable interval bytes and commits their revision history through a conditional manifest update. Repeating an unchanged backfill preserves its original receipt. The flat interval keys are compatibility copies. Exchange session hours accompany the receipt where the repository calendar covers the date.

Run from the repository root:

```sh
python -m market_data.single_stock_historical_backfill universe
python -m market_data.single_stock_historical_backfill backfill --out backfill-out
python -m market_data.single_stock_historical_backfill backfill --out backfill-out --symbols AAPL,MSFT
python -m market_data.single_stock_historical_backfill update --out backfill-out
python -m market_data.single_stock_historical_backfill status --out backfill-out
python -m market_data.single_stock_historical_backfill backfill --publish   # opt-in R2 write
```

## Sources

| Data | Provider | Notes |
|---|---|---|
| 1-minute bars | Tiingo `/iex/{ticker}/prices`, `resampleFreq=1min` | Three newest pages of 10,000 bars each (~75 trading days). Tiingo caps a response at 10,000 bars and returns the *newest* bars in the window, so paging walks backward with `endDate`; pages are merged deduped by timestamp (see `tiingo.py`) |
| Hourly bars | Tiingo `/iex/{ticker}/prices`, `resampleFreq=1hour` | Back to 2017 (~15,478 bars for AAPL, 2 pages). Wall-clock-hour buckets labelled by hour start in UTC, ~6 per trading day |
| Daily bars | Tiingo `/tiingo/daily/{ticker}/prices` | From 2000 (one request; 6,764 bars for AAPL). Carries volume and split-adjusted fields |
| Derived intervals | Aggregated by the collector, never fetched | 3m/5m/9m/15m/30m from 1m (09:30 ET session-open bucketing), 2h/4h from 1h (same-day groups only), 1w from 1d (Monday–Sunday, volume summed). See `resample.py` |

Auth: `TIINGO_API_TOKEN` in the environment only (token in the Authorization header). Free Starter limits: 50 requests/hour, 1,000/day,
500 symbols/month. No other account or auth.

## Universe

The universe is the live NASDAQ-100 list, resolved on every run
(`collector.resolve_universe`, mirroring PR #391): Nasdaq's official
`list-type/nasdaq100` API is re-fetched and only the live members
are collected. The 101 listings (100 companies) are embedded in
`symbols.py`, taken from the same API and re-verified live on
2026-11-23 (`totalrecords: 101`, API date "Nov 23, 2026") — the
live symbol set is identical to the embedded set. Alphabet Inc.
appears twice — GOOGL (Class A) and GOOG (Class C) — which is why
there are 101 listings for 100 companies. The embedded list is the
fallback and the diff baseline: a failed, empty or garbled list
fetch collects the embedded 101 and records
`membership_source: "embedded_fallback"` in every sidecar's
`universe` block. Membership changes (added/removed with symbol
and name, plus the resolution date) are recorded in the same block
and in the run summary; a leaver keeps its stored files and
checkpoint entries, an entrant is backfilled from scratch.
`universe` prints the live list with its changes. Re-verify
against the same endpoint after each annual reconstitution
(December) or announced change; `symbols.validate()` fails closed
on a wrong count or a duplicate in the embedded list.

## Interval files

`nasdaq/candles/<TICKER>/<interval>.json`
(`schema_version: 1`, `dataset: "single_stock_candles"`):

- `symbol`, `interval`, `base` (true for 1m/1h/1d, false for derived),
  `derived_from` (null for bases, else the base interval), `source`,
  `bars_count`, `first_bar`, `last_bar`, `generated_at`.
- `bars[]`: intraday bars are `{"t", "open", "high", "low", "close"}`
  (plus `"bars"` on derived bars — the number of base bars folded
  in). **No `volume` field on any intraday bar**: Tiingo intraday
  responses carry no volume at all, so none is stored — not even null.
  Daily bars are `{"t", "open", "high", "low", "close", "volume",
  "adj_close", "adj_volume"}`; weekly bars carry summed `volume`.

## Pacing and resume

A full backfill is 6 requests per listing (3 + 2 + 1) = 606 requests
for 101 listings, so the first fill takes ~12 hours at 50
requests/hour; the daily cap (606 < 1,000) does not bind a single
backfill. `pacing.py` keeps the request log and per-symbol progress
in `<out>/.backfill-checkpoint.json` (atomic writes after every
symbol+base). A run resumes by skipping completed symbols; `--force`
re-fetches completed symbols. Pacing is per HTTP request (the client
acquires the limiter inside every request, because one fetch can
page into several requests — acquiring per fetch would undercount
up to threefold). A 429 is retried once after the server's
Retry-After and then aborts the run with the checkpoint saved (the
checkpoint is written in a `finally`, and the CLI reports the abort
as a clean message, not a traceback). `update` fetches only bars
newer than the newest stored bar per base, so the steady-state cost
is ~3 requests per listing per day (one per base), and usually
fewer — a base with no new bars still costs one request to confirm.

The limiter's memory is the checkpoint file: two runs with different
`--out` directories do not share budget knowledge, and the token's
hourly budget is shared with every other user of the same Tiingo
token. Do not run two backfills (or a backfill plus unrelated Tiingo
calls) in the same hour and expect the limiter to know.

## Session model

US equity sessions, weekdays 09:30–16:00 America/New_York. Tiingo
1-minute bars are already regular-session only (390/day). The
resampler drops any out-of-session bar rather than folding it in.
Bars are stored exactly as Tiingo returns them — including the
occasional flat stub bar on a market holiday at a page boundary
(observed 2020-07-03 on the hourly base) — never corrected, dropped
or invented. A ticker Tiingo does not carry is recorded
`source_unavailable` with no files; a price is never substituted,
carried forward, or invented.

## Provenance

Every ticker gets one `snapshot_provenance.v1` sidecar
(`nasdaq/candles/<TICKER>/provenance.json`) listing all eleven
interval files with bar counts, first/last bars, base or
`derived_from`, and the Tiingo requests/pages behind each base. One
sidecar per ticker rather than one per file: eleven files per ticker
would otherwise mean 1,111 sidecar objects for the universe. See
`docs/specs/SINGLE_STOCK_HISTORICAL_BACKFILL.md` for the full
contract, including the CI wiring a maintainer must add.

## R2 publication

Opt-in only (`--publish`). Environment (unconfirmed against live service
config — these names are the collector's expectation, not a verified live
deployment):

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `TIINGO_R2_BUCKET` — bucket holding the `nasdaq/` prefix (expected
  to be the same `qqq-options-chain-data` bucket as
  `nasdaq/latest.json`; unconfirmed)

Each ticker is published as it completes (eleven interval files, then
its sidecar); a sidecar failure is raised, not swallowed. The local
sidecar is rewritten as `published` only after the R2 writes succeed.

## Tests

```sh
python -m pytest tests/test_single_stock_historical_backfill.py
```

Offline: universe count, Tiingo paging (10,000-bar cap, backward
`endDate` walk, overlap dedupe), intraday/daily bar parsing
(including the absent intraday volume field), pacing budgets and
checkpoint resume, resampling (including 9m bucketing from 1-minute
bars, 2h/4h same-day grouping, weekly volume sums), provenance
shape, publish keys and the backfill/update flows with a fake Tiingo
client. No test touches a provider or R2.
