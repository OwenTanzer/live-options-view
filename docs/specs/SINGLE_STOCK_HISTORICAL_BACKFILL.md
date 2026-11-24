# Single-stock historical candle backfill contract

Producer: `market_data/single_stock_historical_backfill/` (new in this
PR). Consumer: the site NASDAQ-100 panel (`site/js/nasdaq-panel.js`,
PR #391) — which reads the cached interval files from R2 first and
falls back to a live Yahoo Finance fetch only when a cache entry is
missing. This specification is repository documentation, not a served
asset.

## Directory-name note

The work was scoped as `market_data/single-stock-historical-backfill/`,
but a hyphenated directory is not a valid Python package name, and
every collector in this repository is an underscore package imported
as `python -m market_data.<collector>` (`nasdaq_collector`,
`crypto_collector`). The package is therefore
`single_stock_historical_backfill`. The R2 paths it publishes do not
carry the collector name, so nothing downstream sees the difference.

## Artifacts

| Object key (R2 bucket `qqq-options-chain-data`, expected — see below) | Contents |
|---|---|
| `nasdaq/candles/<TICKER>/<interval>.json` | One interval file per ticker per interval (below), `schema_version: 1`, `dataset: "single_stock_candles"` |
| `nasdaq/candles/<TICKER>/provenance.json` | `snapshot_provenance.v1` sidecar for the ticker (below) |

Eleven interval files per ticker (1m, 3m, 5m, 9m, 15m, 30m, 1h, 2h,
4h, 1d, 1w), 101 tickers: 1,111 interval files plus 101 sidecars
when the backfill is complete. Publication is per ticker as it
completes, and opt-in (`--publish`). No artifact is published yet;
the collector is not deployed or scheduled, and no R2 object exists
until a maintainer deploys it and the first `--publish` run succeeds.
The panel reads whatever the publisher writes; it never publishes,
starts a collector, or deploys.

## Sources (Tiingo, token-authenticated)

- **1-minute bars:** `https://api.tiingo.com/iex/{ticker}/prices`
  with `startDate`, `endDate`, `resampleFreq=1min`. Three newest
  pages of 10,000 bars each (~75 trading days).
- **Hourly bars:** same endpoint with `resampleFreq=1hour`, back to
  2017 (~15,478 bars for AAPL, 2 pages).
- **Daily bars:** `https://api.tiingo.com/tiingo/daily/{ticker}/prices`
  with `startDate`, `endDate`, from 2000 (one request; 6,764 bars for
  AAPL).

Auth: `TIINGO_API_TOKEN` in the environment only (token as query
param `token`). Free Starter limits: 50 requests/hour, 1,000/day,
500 unique symbols/month. Who owns the Tiingo token in the repo
environment is an open question (see below).

### What Tiingo actually returns (verified live 2026-11-23)

- A response holds at most **10,000 bars**. When the requested window
  holds more, Tiingo returns the **newest** 10,000 bars — a
  `startDate`-only 1-minute request from 2026-08-10 returned bars
  starting 2026-10-19, not 2026-08-10. Paging therefore walks
  **backward**: each page sets `endDate` to the first (earliest) bar
  date of the previous page, and pages merge deduped by timestamp.
  The `endDate` day is returned in full, so consecutive pages overlap
  on that day; dedupe by timestamp is what makes the merge exact.
  A page that adds no new bars ends the walk (guards the case where
  the history is an exact multiple of 10,000).
- 1-minute bars: 390 per trading day (09:30–16:00 ET, regular session
  only), labelled by minute start in UTC. Three pages reach back
  ~75 trading days (AAPL: 2026-10-19 back to 2026-09-14 on page 2;
  a third page covers 2026-08-10–2026-09-14).
- Hourly bars: wall-clock-hour buckets labelled by hour start in UTC
  (~6 per trading day, first 10:00 ET). 2017-01-01 to 2026-11-23 is
  15,478 bars for AAPL in 2 pages (10,000 + 5,478).
- Daily bars: one request from 2000-01-01 returned 6,764 bars for
  AAPL (first 2000-01-03, last 2026-11-23, close 349.08 — matching
  the connector's earlier verified close). **Correction to the plan:**
  the plan estimated ~2,600 daily bars; the live count is 6,764.
  Daily bars carry `volume` and split-adjusted fields
  (`adjClose`, `adjVolume`, `divCash`, `splitFactor`).
- **Intraday bars carry no volume.** The intraday response keys are
  `date`/`open`/`high`/`low`/`close` only — there is no `volume` key
  at all, not even null. No volume field is stored on any intraday
  bar or any interval derived from one. Daily bars and the weekly
  bars derived from them carry volume. The panel's volume strip
  therefore appears only where data has volume (daily/weekly), as
  the tracker already does.
- Omitting `startDate` does **not** mean "from the beginning": a
  1-minute request with no `startDate` returned only the most recent
  session day (390 bars for AAPL on 2026-11-23). The 1-minute walk
  therefore always sends an explicit `startDate` (today minus 120
  calendar days, ~85 trading days). A placeholder such as
  `0000-01-01` is rejected with HTTP 400 — this was caught by the
  first CLI smoke run (HTTP 400 on the first request) and fixed
  before the re-run.
- One observed anomaly: the first (oldest) hourly bar of a capped
  page can be a flat stub (O=H=L=C) on a market holiday — 2020-07-03
  (observed Independence Day, market closed) appeared as a single
  flat bar at 16:00Z with O=H=L=C=364.36, the previous close. Bars
  are stored exactly as returned, never dropped or corrected.
- Tiingo does **not** carry NDX (404). The index is out of scope
  here; the site's NDX chart stays on Yahoo Finance `^NDX` (PR #391).

## Universe

The universe is the live NASDAQ-100 list, resolved on every run
by `collector.resolve_universe` and mirroring PR #391's collector
(same endpoint, same parse/diff helpers, same fallback rule):

- Re-fetch Nasdaq's official `list-type/nasdaq100` API on every
  run; collect/backfill only the live members.
- The 101 listings (100 companies) are embedded in
`market_data/single_stock_historical_backfill/symbols.py` — the same
list as `market_data/nasdaq_collector/symbols.py` (PR #391, unmerged
when this was written), re-verified live against Nasdaq's official
`list-type/nasdaq100` API on 2026-11-23 (`totalrecords: 101`, API
date "Nov 23, 2026"): the live symbol set is identical to the
embedded set. This collector is self-contained on master and does not
import the unmerged package. Alphabet Inc. appears twice — GOOGL
(Class A) and GOOG (Class C) — hence 101 listings for 100 companies.
- The embedded list is the fallback and the diff baseline: when
  the live list fetch fails, or returns an empty/garbled response,
  the run collects the embedded 101 and records
  `membership_source: "embedded_fallback"` with the error in every
  sidecar's `universe` block — a bad fetch never empties the
  backfill. Membership changes (added/removed with symbol and
  name, plus the resolution date) are recorded in the same block
  and in the run summary's `universe` block. A ticker that leaves
  the index is not backfilled or updated further, but its stored
  files and checkpoint entries are kept; a ticker that enters it
  is backfilled from scratch. Re-verify against the same endpoint
  after each annual reconstitution (December) or announced change;
  `symbols.validate()` fails closed on a wrong count or a duplicate
  symbol in the embedded list, while the live list is validated
  for uniqueness only (`validate_unique`), since its count
  legitimately differs after a reconstitution.

## Interval file shape (`nasdaq/candles/<TICKER>/<interval>.json`)

```json
{
  "schema_version": 1,
  "dataset": "single_stock_candles",
  "symbol": "AAPL",
  "interval": "9m",
  "base": false,
  "derived_from": "1m",
  "source": "Tiingo (api.tiingo.com)",
  "bars_count": 3250,
  "first_bar": "2026-08-10T14:30:00Z",
  "last_bar": "2026-11-23T20:51:00Z",
  "generated_at": "2026-11-23T21:00:00Z",
  "bars": [{"t": "2026-08-10T14:30:00Z", "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.5, "bars": 9}]
}
```

- `base` is true for 1m/1h/1d (published exactly as Tiingo returned
  them) and false for derived intervals; `derived_from` names the
  base (null for bases).
- Intraday bars: `{"t", "open", "high", "low", "close"}` — no
  `volume` key, ever. Derived intraday bars add `"bars"` (the number
  of base bars folded in).
- Daily bars: `{"t", "open", "high", "low", "close", "volume",
  "adj_close", "adj_volume"}`. Weekly bars: the same plus `"bars"`,
  with `volume` summed from the daily base.
- `t` is ISO-8601 UTC. Intraday bars are labelled by period start
  (a 1-minute bar at 20:59Z covers 20:59–21:00Z); daily bars are
  midnight UTC on the bar date.

## Derived intervals (never fetched)

| Derived | From | Rule |
|---|---|---|
| 3m, 5m, 9m, 15m, 30m | 1m | Bucketed from the 09:30 ET session open of each bar's own ET date (a 9m bucket is always 09:30–09:39, 09:39–09:48, …). Only regular-session bars are aggregated; out-of-session bars are dropped, never folded in. Mirrors `market_data/nasdaq_collector/candles.py` |
| 2h, 4h | 1h | Consecutive hourly bars grouped within the same ET session day (2h: 10:00–12:00, 12:00–14:00, 14:00–16:00; 4h: 10:00–14:00, then the trailing 14:00–16:00 pair). Groups never cross a day; a partial trailing group is kept as the live, still-forming bar |
| 1w | 1d | ISO calendar weeks (Monday–Sunday): open = first bar's open, close = last bar's close, high/low = extremes, volume = sum of daily volumes. A partial current week is kept |

## Pacing, checkpoint, resume

- A full backfill is 6 requests per listing (3 + 2 + 1) = **606
  requests** for 101 listings. At 50/hour the first fill takes
  **~12.1 hours minimum**; the daily cap (606 < 1,000) does not bind
  a single backfill, but it does bind a second full backfill on the
  same day — which is why completed symbols are checkpointed and
  never re-fetched.
- `<out>/.backfill-checkpoint.json` holds the request log (last 25
  hours) and per-symbol, per-base progress (`status`, `pages`,
  `bars`, `first_bar`, `last_bar`, `requests`, `completed_at`).
  Writes are atomic (temp file + rename) after every symbol+base.
  A run resumes by skipping completed symbols. The checkpoint is
  local state only and is never published.
- The rate limiter counts requests in the last hour/day and sleeps
  until the oldest request ages out of the hour window. A 429 is
  retried once after the server's Retry-After (capped at one hour);
  a second 429 aborts the run with the checkpoint intact (the
  checkpoint is saved in a `finally`, and the CLI reports the abort
  as a clean message, not a traceback).
- The limiter's memory is the checkpoint file: two runs with
  different `--out` directories do not share budget knowledge, and
  the token's hourly budget is shared with every other user of the
  same Tiingo token. Do not run two backfills (or a backfill plus
  unrelated Tiingo calls) in the same hour and expect the limiter
  to know. The 2026-11-23 smoke runs hit HTTP 429 this way: ~20
  requests from this work plus earlier verification calls against
  the same token exhausted the 50/hour budget, and a fresh probe
  request also 429'd — the budget, not the code, was exhausted.
- One ticker is processed at a time, sequentially. Concurrency would
  not help: the hourly budget is the binding constraint.
- Steady state (`update`): only bars newer than the newest stored
  bar per base are fetched — ~3 requests per listing per day at
  most, usually fewer (a base with no new bars still costs one
  request to confirm). A ticker with no stored files is backfilled
  instead of updated.

## Session model

US equity sessions, weekdays 09:30–16:00 America/New_York. Tiingo
1-minute bars are already regular-session only (390/day). The
resampler drops any out-of-session bar rather than folding it in.
Bars are stored exactly as Tiingo returns them — including the flat
holiday stub bar noted above — never corrected, dropped or invented.

## Provenance

One `snapshot_provenance.v1` sidecar per ticker
(`nasdaq/candles/<TICKER>/provenance.json`):

```json
{
  "schema": "snapshot_provenance.v1",
  "dataset": "single_stock_candles",
  "artifact": "nasdaq/candles/AAPL/",
  "symbol": "AAPL",
  "constructed_at": "2026-11-23T21:00:00Z",
  "archive_uploaded_at": null,
  "latest_published_at": null,
  "publication": {"status": "not_published",
                  "reason": "local run: --publish not given"},
  "source": "Tiingo (api.tiingo.com), token-authenticated, free Starter",
  "as_of": "2026-11-23T20:59:00Z",
  "universe": {"source": "Nasdaq official list-type/nasdaq100 API",
               "source_url": "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
               "as_of": "2026-11-23", "listings": 101, "companies": 100,
               "membership_source": "live", "added": [], "removed": [],
               "resolution_as_of": "2026-11-23", "error": null},
  "files": {"1m": {"key": "nasdaq/candles/AAPL/1m.json",
                   "derived_from": null, "bars": 29250,
                   "first_bar": "2026-08-10T13:30:00Z",
                   "last_bar": "2026-11-23T20:59:00Z",
                   "tiingo_requests": 3, "tiingo_pages": 3},
            "...": "one entry per interval"},
  "coverage": {"intervals": 11, "bars_total": 86231,
               "first_bar": "2000-01-03T00:00:00Z",
               "last_bar": "2026-11-23T20:59:00Z"},
  "notes": ["Intraday bars (1m/1h and every interval derived from them) carry no volume: ...",
            "Derived intervals are aggregated by the collector, never fetched: ..."]
}
```

Counts in this example are illustrative (the 1m/1d entries use the
verified live figures: three 1-minute pages reaching ~75 trading
days, 6,764 daily bars from 2000).

One sidecar per ticker rather than one per file: eleven files per
ticker would otherwise mean 1,111 sidecar objects for the universe.
The sidecar still carries the same claims per file (bar count,
first/last bar, base or `derived_from`, Tiingo requests/pages). A
missing sidecar means unknown provenance. A ticker Tiingo does not
carry is recorded `source_unavailable` in the checkpoint and the run
summary, with no files — a price is never substituted, carried
forward, or invented.

## R2 publication

Opt-in only (`--publish`). Environment (**unconfirmed against live
service config** — these names are the collector's expectation, not a
verified live deployment):

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `TIINGO_R2_BUCKET` — bucket holding the `nasdaq/` prefix, expected
  to be the same `qqq-options-chain-data` bucket that holds
  `nasdaq/latest.json` (unconfirmed)

Each ticker is published as it completes (eleven interval files,
then its sidecar); a sidecar failure is raised, not swallowed. The
local sidecar is rewritten as `published` only after the R2 writes
succeed.

## Verification (local, 2026-11-23)

- Collector unit tests: 33 pass (`python -m pytest
  tests/test_single_stock_historical_backfill.py`). `node --check`
  and `py_compile` clean. `python tests/test_changed_paths.py`:
  79 OK.
- Live Tiingo smoke runs (read-only, no `--publish`): the paging
  mechanism was verified directly — a 1-minute request from
  2026-08-10 returned exactly 10,000 bars starting 2026-10-19, and
  a second page with `endDate=2026-10-19` returned 10,000 bars
  2026-09-14→2026-10-19; an hourly request from 2017 returned
  10,000 bars from 2020-07-03 and a second page with
  `endDate=2020-07-03` returned the remaining 5,478 bars back to
  2017-01-03 (15,478 total). A full CLI smoke run for AAPL and MSFT
  (8 requests) completed both tickers: 1h 15,478 bars
  (2017-01-03→2026-11-23, 2 requests), 1d 6,764 bars
  (2000-01-03→2026-11-23, 1 request), all eleven intervals derived
  (2h 7,739, 4h 5,159, 1w 1,404), provenance sidecars written,
  checkpoint request log accurate. The 1m base in that run returned
  only one session day (390 bars) because no `startDate` was sent —
  the bug the smoke run caught; fixed the same day.
- **Not yet verified live:** the CLI's 3-page 1-minute walk after
  the `startDate` fix. The re-run (AAPL only, 6 requests) hit HTTP
  429 because the token's 50/hour budget was already exhausted by
  the day's verification calls (~20 from this work plus earlier
  calls against the same token). The paging itself is proven live
  (above) and in unit tests; the CLI re-run is one command once the
  hourly budget resets:
  `python -m market_data.single_stock_historical_backfill backfill
  --out backfill-out --symbols AAPL --max-requests 6` — expect 1m
  ~29,250 bars in 3 requests. Until that runs, the 1m three-page
  walk is verified by mechanism, not by the CLI end to end.
- No R2 artifact published, no staged site preview read back. No
  producer restarted or redeployed. No live service ran on the basis
  of repo HEAD alone.

## Panel contract (stacks on PR #391)

The panel change applies on top of PR #391 (`cian/nasdaq-panel`,
`site/js/nasdaq-panel.js` at `215a4def`), which does not exist on
master yet — it is delivered with this PR as a patch against that
file, to be applied after #391 merges. The panel:

- reads `nasdaq/candles/<TICKER>/<interval>.json` from the same
  public-data origin (`BROWSER_DATA`) first, for the selected stock
  and interval;
- falls back to the live Yahoo Finance chart fetch only when the
  cache entry is missing (404), malformed, empty, or the fetch fails;
- maps the file's `t` field to the panel's `time` field and carries
  the no-volume shape through unchanged — the volume strip already
  renders only where data has volume, so intraday charts show no
  strip and daily/weekly charts do;
- keeps the NDX index chart on Yahoo Finance `^NDX` (unchanged);
- needs no `site/index.html` change except the script hash: the
  Stocks tab and panel wiring already exist in #391, and the R2 path
  uses the same data origin. The `?v=` hash on the
  `js/nasdaq-panel.js` script tag must be recomputed from the new
  file contents (first 16 hex chars of its sha256) — the panel test
  asserts it.
- also fixes a bug found in #391's panel while building the R2-first
  load: `renderCandles` read only the nested `{SYM: {interval:
  bars}}` shape, while `loadCandles` caches by flat `"SYM|interval"`
  keys — so every interval fetched on demand (Yahoo or R2) rendered
  as "Candles unavailable" even after a successful fetch. The patch
  reads the flat key first and the nested shape second, so both the
  old fixture shape and the live cache render. Covered by the new
  panel test.

## CI wiring (maintainer)

The GitHub App token cannot create or update workflow files (403,
`workflows` permission missing — same blocker as the Crypto Panel PR
#389 and the NASDAQ-100 panel PR #391). A maintainer with workflow
access must add the following steps; they are **not** in this PR.

1. In `.github/workflows/collector-ci.yml`, add a step that runs the
   new collector tests (same Python job as the existing collector tests):

   ```yaml
   - name: Single-stock historical backfill tests
     run: python -m pytest tests/test_single_stock_historical_backfill.py -q
   ```

   (If the job installs test dependencies, `pytest` must be among
   them; the collector itself needs only `requests` and `boto3`,
   both already in `deployment/python/requirements.txt`.)

2. In `.github/workflows/web-ci.yml`, add a step that runs the new
   panel test (same pattern as the other panel tests, e.g.
   `node tests/squeeze_panel.test.js`):

   ```yaml
   - name: single-stock backfill panel (R2-first candles)
     run: node tests/nasdaq_panel_r2_first.test.js
   ```

   The test file is delivered with the panel patch (it requires
   `site/js/nasdaq-panel.js` from PR #391) and joins `tests/` when
   the patch is applied.

## Open questions

- **12-hour first fill and scheduling:** the first fill takes ~12
  hours paced at 50 requests/hour. Is it a one-off manual run, or a
  scheduled service? Which service owns it, and on what cadence does
  `update` run afterwards (hourly with the snapshot collector, or
  daily)?
- **Who owns the Tiingo token in the repo environment?** The token
  lives in the `custom.tiingo` connection today. Which secret holds
  `TIINGO_API_TOKEN` for the deployed collector, and who rotates it?
  Free Starter is personal/internal use only; redistribution needs a
  commercial licence.
- **Unconfirmed R2 env vars:** `TIINGO_R2_BUCKET` (and the shared
  `R2_ENDPOINT_URL` / key pair) are unconfirmed against live service
  config, as with PRs #389 and #391. Is the bucket
  `qqq-options-chain-data`, and does the publisher's key have write
  access to the `nasdaq/candles/` prefix?
- **No-volume intraday:** intraday bars have no volume, so the
  panel's volume strip disappears on intraday intervals (it already
  does this where data has no volume). Is that acceptable, or is a
  volume source wanted later? (Any volume source would be a new
  scope, not a fix to this collector.)
- **NDX stays Yahoo:** the index chart remains on Yahoo Finance
  `^NDX`. No change proposed here.
- **Per-ticker vs per-file sidecars:** one sidecar per ticker is
  the chosen shape (1,111 per-file sidecars otherwise). If reviewers
  want one sidecar per interval file, `provenance.py` /
  `publish.py` need the per-file variant instead.
- **Daily bar count correction:** the plan estimated ~2,600 daily
  bars from 2000; the live count for AAPL is 6,764. The backfill
  budget (606 requests) is unaffected — daily is still one request
  per ticker — but stored volume roughly doubles versus the plan's
  estimate.
- **Pending live re-run:** the CLI's 3-page 1-minute walk after the
  `startDate` fix (see Verification above). One command, 6 requests,
  once the token's hourly budget resets.
