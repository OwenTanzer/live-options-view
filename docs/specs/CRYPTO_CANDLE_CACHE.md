# Crypto candle cache contract

Producer: `market_data/crypto_candle_cache/` (new in this PR).
Consumer: the site Crypto panel (`site/js/crypto-panel.js`, PR
#389) — which reads the cached interval files from R2 first
through One Clock's as-of selector (panel patch in `panel/`, to
be applied after #389 merges). This specification is repository
documentation, not a served asset.

## Directory-name note

The work was scoped as `market_data/crypto-candle-cache/`, but a
hyphenated directory is not a valid Python package name, and every
collector in this repository is an underscore package imported as
`python -m market_data.<collector>` (`crypto_collector`,
`nasdaq_collector`, `single_stock_historical_backfill`). The
package is therefore `crypto_candle_cache`. The R2 paths it
publishes do not carry the collector name, so nothing downstream
sees the difference.

## Artifacts

| Object key (R2 bucket `qqq-options-chain-data`, expected — see below) | Contents |
|---|---|
| `crypto/candles/<COIN>/<interval>.json` | One interval file per coin per interval (below), `schema_version: 1`, `dataset: "crypto_candles"` |
| `crypto/candles/<COIN>/provenance.json` | `snapshot_provenance.v1` sidecar for the coin (below) |

Eleven interval files per coin (1m, 3m, 5m, 9m, 15m, 30m, 1h, 2h,
4h, 1d, 1w), 50 coins: 550 interval files plus 50 sidecars when
the backfill is complete. Publication is per coin as it completes,
and opt-in (`--publish`). No artifact is published yet; the
collector is not deployed or scheduled, and no R2 object exists
until a maintainer deploys it and the first `--publish` run
succeeds. The panel reads whatever the publisher writes; it never
publishes, starts a collector, or deploys.

The full backfill data lives **outside this repository**, in the
parallel Crypto Backend job (`~/workspace/crypto-backend/` and its
Notion page "Crypto Backend"). This package is the contract, the
seed, and the steady-state update path — not the backfill data.

## Sources

Three sources, one per coin per base, with the source that actually
supplied each base recorded in the provenance sidecar:

- **Coinbase Exchange public candles** (`api.exchange.coinbase.com`,
  `/products/<SYM>-USD/candles`, no key, no account): the three
  bases (1m/1h/1d) for the 32 Coinbase-labelled coins. A response
  is `[epoch, low, high, open, close, volume]` — low/high before
  open/close — newest first, at most 300 bars per request. Paging
  walks backward in capped 300-bar windows, deduped by timestamp
  (mirroring the stock backfill's Tiingo walk): a window holding
  more than 300 bars is refused with HTTP 400, not truncated
  (caught live 2026-11-24 — a 1-minute request over 24 hours
  failed outright, and the first version of this client, which
  walked backward from `end` over the whole window, lost both the
  1m and 1d bases of a live BTC backfill smoke run to that 400;
  the capped-window walk is the fix, regression-tested).
  2026-11-24: BTC hourly bars (latest bar 2026-11-24T00:00:00Z,
  close 61204.79); USDS-USD and USD1-USD tickers both resolve.
  Deep paging was verified by the parallel Crypto Backend job the
  same night: BTC daily bars walk back to 2015-07-20 (4,145 bars),
  so Coinbase — not Tiingo — is the depth source for
  Coinbase-labelled coins.
- **Tiingo crypto prices** (`api.tiingo.com/tiingo/crypto/prices`,
  `tickers=btcusd`, `startDate`/`endDate`, `resampleFreq=1min` /
  `1hour` / `1day`, token as the `token` query param from the
  environment only, `TIINGO_API_TOKEN`): the deep-history backstop,
  from December 2017. The route exists — an unauthenticated request
  returns `{"detail": "Please supply a token"}`, not a 404
  (checked 2026-11-24) — but **no authenticated Tiingo crypto call
  has been made from this repository**; the backend job has made 0
  Tiingo requests so far, and the shared free Starter budget
  (50/hour, 1,000/day, shared with the stock backfill job) is not
  spent here to prove a shape the job will prove once. The bar
  shape in `tiingo.py` is Tiingo's documented crypto-prices shape
  and the parser is defensive (missing `volume` → no volume key).
  Tiingo is tried only when a Coinbase base fetch fails or returns
  fewer bars than the requested window. It is a named, recorded
  backstop for history depth, never a silent same-day price
  substitute: the snapshot collector's fixed source labels still
  govern the panel's spot prices.
- **CoinGecko public OHLC** (`api.coingecko.com/api/v3/coins/<id>/ohlc`,
  `vs_currency=usd`, `days=365`, no key): the **1d base only**, for
  the 18 CoinGecko-labelled coins. A response is
  `[epoch_ms, open, high, low, close]` — no volume, ever.
  Granularity is server-chosen by `days` and was verified live
  2026-11-24: `days=7` for bitcoin returns bars 4 hours apart, so
  only the long window maps onto a calendar base — `days=365`
  returns daily bars (the backend job's TRX read: 366 daily bars,
  2025-11-25 → 2026-11-24). CoinGecko-labelled coins therefore have
  **no 1m/1h base and no intraday interval derived from one**: their
  3m/5m/9m/15m/30m/1h/2h/4h files are absent, and the sidecar
  records them as `coverage: "absent"` with the reason "CoinGecko
  free OHLC has no 1m/1h series". A missing interval is a labelled
  gap, never a bar copied from another source or an interpolated
  price. The 1w file is derived from the 1d base.

Auth: only Tiingo needs a token, from the environment only. No
secret appears in any file in this tree. Free Starter is
personal/internal use only; redistribution needs a commercial
licence. Who owns the Tiingo token in the repo environment is an
open question (see below).

## Universe

The universe is the live top 50 by market cap, resolved on every
run by `collector.resolve_universe` and mirroring the Coinbase
Price Tracker (which is itself dynamic top 50 since 2026-11-23)
and the snapshot collector (PR #389, same rule, same tokenized
exclusion):

- Re-fetch the CoinGecko markets list (top 100, ordered by market
  cap) on every run; exclude tokenized assets under the tracker's
  standing rule — Figure Heloc, Tether Gold, tokenized funds/yield
  coins (the seven CoinGecko ids in
  `coins.TOKENIZED_EXCLUDED_IDS`); take the first 50.
- Probe each entrant's source live: a Coinbase Exchange USD pair
  that answers means `coinbase`, otherwise `coingecko`. Coins
  already in the embedded list keep their verified source label
  (USDT stays `coingecko` even though a USDT-USD pair exists — see
  the correction below).
- The embedded 50 in
`market_data/crypto_candle_cache/coins.py` — the tracker's 39
(PR #389, same source labels) plus the 11 the tracker added when
it expanded to the top 50 (USDS and USD1 from Coinbase; USDE,
RLUSD, USDG, PYUSD, RAIN, MemeCore, OKB, Falcon USD, United
Stables from CoinGecko). Ranks are CoinGecko market-cap ranks at
verification time: the 39-coin ranks from the snapshot collector's
verification (2026-11-23 22:06 UTC), the 11 added coins and moved
ranks from the backend job's top-50 read (`last_updated`
2026-11-24T00:13:48Z). Tokenized assets in the raw CoinGecko top
50 (Figure Heloc, Tether Gold, tokenized funds/yield coins) stay
excluded under the tracker's standing rule, so this is the
tracker's 50, not the raw top 50 — the backend job's raw list
carries those assets and uses the symbol `TON` for
the-open-network, while this package keeps the tracker's `GRAM`
symbol, as the snapshot collector does. `coins.validate()` fails
closed on a wrong count or a duplicate symbol.
- The embedded 50 is the fallback and the diff baseline: when the
  CoinGecko list is missing or partial (fewer than 50 usable coins
  after exclusion), the run collects the embedded 50 and records
  `source: "embedded_fallback"` with the error in every sidecar's
  `universe` block — a bad fetch never shrinks or empties the
  cache. Membership changes (added/removed with symbol, rank,
  source and the resolution date) are recorded in the same block.
  A coin that leaves the top 50 keeps its stored files (they are
  never deleted); a coin that enters it is backfilled from
  scratch. Ground truth from the tracker's first live
  reconciliation (2026-11-24): USDD (rank 51), BFUSD (55) and HTX
  (57) entered and ASTER, DOT, ATOM left; the live resolver
  reproduced exactly that diff on a live read the same day.
- A CoinGecko-labelled entrant gets the 1d base only, exactly like
  any other CoinGecko-labelled coin (free OHLC has no 1m/1h
  series); its intraday intervals are absent by design, labelled
  as such, never interpolated.

One correction to record: the backend job found a live Coinbase
pair for USDT (`USDT-USD`, 2,030 daily bars from 2021-05-04), but
the snapshot contract prices USDT from CoinGecko, so this package
keeps USDT CoinGecko-labelled. Whether the cache should follow
the snapshot label or the live pair is an open question (below).

## Interval file shape (`crypto/candles/<COIN>/<interval>.json`)

```json
{
  "schema_version": 1,
  "dataset": "crypto_candles",
  "symbol": "BTC",
  "interval": "1h",
  "base": true,
  "derived_from": null,
  "source": "coinbase",
  "source_label": "Coinbase Exchange public candles (api.exchange.coinbase.com), no key",
  "bars_count": 168,
  "first_bar": "2026-11-17T00:00:00Z",
  "last_bar": "2026-11-23T23:00:00Z",
  "generated_at": "2026-11-24T00:32:44Z",
  "bars": [{"t": "2026-11-23T23:00:00Z", "open": 61176.89, "high": 61515.0,
            "low": 61063.21, "close": 61189.97, "volume": 368.52,
            "observed_at": "2026-11-24T00:32:44Z", "origin": "recorded"}]
}
```

- `base` is true for 1m/1h/1d (published exactly as fetched, plus
  the envelope below) and false for derived intervals;
  `derived_from` names the base (null for bases).
- Every bar carries the One Clock envelope: `t` is the **source
  time** (the bar start as labelled by the source); `observed_at`
  is the **observed time** (when this collector fetched the bar);
  `origin` is the **state chip** — `"recorded"` for a base bar
  fetched from a provider, `"reconstructed"` for a bar derived
  locally. `"synthetic"` is never written by this collector: a
  price is never invented, carried forward, or zero-filled.
- Bars carry `volume` when their source carries it (Coinbase bars
  do, on every interval, so derived crypto bars sum it). A base
  without volume (CoinGecko daily) yields bars with no `volume`
  key — never null, never zero — and derived bars from it likewise.
  Derived bars add `"bars"` (the number of base bars folded in);
  a base bar never carries it.
- The still-forming latest bar is stored with `"forming": true`.
  It is a real bar, but it is not yet an observation (see One
  Clock below) and is never served as a price.
- `t` is ISO-8601 UTC. Intraday bars are labelled by period start;
  daily bars are midnight UTC on the bar date. A re-fetch that
  changes a stored bar's OHLC is a new revision of the same bar:
  the writer refuses to silently overwrite it unless `--force` is
  given.

## Derived intervals (never fetched)

| Derived | From | Rule |
|---|---|---|
| 3m, 5m, 9m, 15m, 30m | 1m | Bucketed from 00:00 UTC of each bar's own UTC date (a 9m bucket is always 00:00–00:09, 00:09–00:18, …). A partial trailing bucket is kept as the live, still-forming bar |
| 2h, 4h | 1h | Consecutive hourly bars grouped within the same UTC day (2h: 00:00–02:00, 02:00–04:00, …; 4h: groups of four). Groups never cross a UTC day; a partial trailing group is kept |
| 1w | 1d | ISO calendar weeks (Monday–Sunday): open = first bar's open, close = last bar's close, high/low = extremes, volume = sum of daily volumes where present. A partial current week is kept |

The one deliberate difference from the stock backfill's resampler
(PR #393) is the session: **crypto trades 24/7, so there is no
09:30 ET session open and no out-of-session bar to drop**.
Buckets align to 00:00 UTC instead. That is the whole difference
— the aggregation code mirrors the stock resampler's structure,
including the rule that 1h is a fetched base, never a derivation
from 1m.

## Session model

Continuous, 24/7 — recorded in the contract, not smoothed over.
There is no equity session, no session open/close, no holiday
calendar, and no "market closed" state. The snapshot collector
(PR #389) already records this as `session_model:
"continuous_24_7"`; the candle cache uses the same string in its
sidecar and the One Clock session `"continuous"`. A bar is never
dropped for falling outside market hours, because there are none.

## One Clock synchronisation

Verified against the real code: `site/shared/one-clock.js`
(master `901f1fec`) exports `instant`, `observation`,
`observationKey`, `selectAsOf`, `readingAsOf`,
`createSourceRegistry`, `createClock`; origins are `recorded` /
`reconstructed` / `synthetic`; modes are `live` / `replay`. No
change is made to One Clock's own QQQ/SPY/IBIT fields, and no
change is made to `one-clock.js` itself. The cache is a new
adapter that speaks its existing language:

- **Observation mapping** (`market_data/crypto_candle_cache/one_clock.py`,
  mirrored in `site/js/crypto-candles.js`): each cached bar maps to
  an observation with `source: "crypto_candle_cache"` (the adapter
  id), `instrument` the coin symbol, `session: "continuous"`,
  `id: "<COIN>|<interval>|<bar start>"`, `evidence` the R2 key,
  `origin` the bar's state chip, `revision: 1`, and `value` the
  bar's OHLC (plus `volume` / `bars` where present). The provider
  (Coinbase / Tiingo / CoinGecko) travels in the `provider` field,
  never by relabelling the source.
- **Times**: `eventAt` is the bar's **close** (start + interval
  width) — the moment the bar's value became final. `valueAt` is
  the bar's start. `availableAt` is `observed_at` (when the
  collector fetched the bar). That ordering is forced by One Clock
  itself (`valueAt` must not follow `eventAt`; `availableAt` must
  not precede `eventAt`) and it has a real consequence: a bar is
  only selectable once it has closed.
- **Forming bars**: a bar fetched before its close has
  `availableAt < eventAt`, which `observation()` rejects — so
  forming bars are **not** observations. They are reported
  separately as `forming` and never served as a price. A bar
  observed early stays forming at later cutoffs too: it can only
  become an observation when a later fetch observes it after its
  close. (This was caught by the JS test while building the panel
  patch: a 00:00 bar fetched at 00:30 is forming at 06:00 as well.)
- **Selection**: the reader serves candles through `selectAsOf` /
  `readingAsOf` (Python port `select_as_of` / `reading_as_of`, same
  filter rules, same highest-revision-wins dedupe, same conflict
  error on two different encodings of one revision, same
  `fresh` / `stale` / `unavailable` reading states). The as-of
  cutoff selects only completed bars whose availability is at or
  before the cutoff, in the `recorded` origin cohort by default;
  derived bars are selectable in the `reconstructed` cohort.
- **Gaps, labelled honestly**: `detect_gaps` reports every place
  where consecutive bar starts are further apart than 1.5x the
  interval width as a **price gap** (`missing_bars` is the count of
  whole bars absent), and `coverage_label` reports a requested
  window as **"coverage partial"** when bars do not span it or
  contain a price gap. Both labels travel next to the reading; the
  selection still returns what exists, and the gap is stated, not
  smoothed over. A missing interval file is "coverage partial"
  with the reason (e.g. "no cached candles yet"), never an empty
  chart.

## Pacing, checkpoint, resume

- Backfill defaults: 1m covers 7 days, 1h covers 90 days, 1d
  covers the source's full depth (Coinbase paging to the product's
  first bar; CoinGecko 365 days). These are overridable
  (`--days-1m`, `--days-1h`). The deep first fill for the universe
  runs in the parallel Crypto Backend job, not here.
- Tiingo's budget is the binding constraint when the backstop is
  used: 50/hour, 1,000/day, shared with the stock backfill job and
  every other user of the same token. The limiter counts requests
  in the last hour/day and sleeps until the oldest request ages out
  of the hour window. A 429 is retried once after the server's
  Retry-After (capped at one hour); a second 429 aborts the run
  with the checkpoint intact.
- `<out>/.cache-checkpoint.json` holds the per-source request log
  (last 25 hours) and per-coin, per-base progress. Writes are
  atomic (temp file + rename) after every coin+base. A run resumes
  by skipping completed coins. The checkpoint is local state only
  and is never published. Two runs with different `--out`
  directories do not share budget knowledge.
- Coinbase and CoinGecko are keyless but rate-limited; both clients
  space requests and a 429 aborts the run with the checkpoint
  intact. The backend job made 579 Coinbase requests in one night
  without hitting a limit — an observation, not a quota.
- One coin is processed at a time, sequentially.
- Steady state (`update`): only bars newer than the newest stored
  bar per base are fetched and merged. Stored forming bars are
  dropped before the merge (a forming bar's OHLC is still moving)
  and re-fetched. A coin with no stored files is backfilled
  instead of updated.

## Provenance

One `snapshot_provenance.v1` sidecar per coin
(`crypto/candles/<COIN>/provenance.json`):

```json
{
  "schema": "snapshot_provenance.v1",
  "dataset": "crypto_candles",
  "artifact": "crypto/candles/BTC/",
  "symbol": "BTC",
  "constructed_at": "2026-11-24T00:32:44Z",
  "archive_uploaded_at": null,
  "latest_published_at": null,
  "publication": {"status": "not_published",
                  "reason": "local run: --publish not given"},
  "session_model": "continuous_24_7",
  "as_of": "2026-11-23T23:00:00Z",
  "files": {"1h": {"key": "crypto/candles/BTC/1h.json",
                   "derived_from": null, "source": "coinbase",
                   "bars": 168, "first_bar": "2026-11-17T00:00:00Z",
                   "last_bar": "2026-11-23T23:00:00Z",
                   "requests": 2, "coverage": "full", "reason": null},
            "...": "one entry per interval, plus absent intervals"},
  "coverage": {"intervals": 11, "bars_total": "sum of bars over the eleven files",
               "first_bar": "2026-08-26T00:00:00Z",
               "last_bar": "2026-11-23T23:00:00Z"},
  "notes": ["Crypto trades 24/7: buckets align to 00:00 UTC, ...",
            "Every bar carries its source time (t), observed time ...",
            "Derived intervals are aggregated by the collector, ...",
            "CoinGecko-labelled coins have no intraday base: ..."]
}
```

Coverage is stated per interval: `"full"`, `"partial"` (file
exists but does not span the window, or contains a price gap), or
`"absent"` (no file, with a reason). One sidecar per coin rather
than one per file: eleven files per coin would otherwise mean 550
sidecar objects for the universe. A missing sidecar means unknown
provenance. A coin whose labelled source fails is recorded
`source_unavailable` in the run summary, with no files — a price
is never substituted, carried forward, or invented.

## R2 publication

Opt-in only (`--publish`). Environment (**unconfirmed against live
service config** — these names are the collector's expectation, not
a verified live deployment):

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `CRYPTO_R2_BUCKET` — bucket holding the `crypto/` prefix,
  expected to be the same `qqq-options-chain-data` bucket that holds
  `crypto/latest.json` (unconfirmed)

Each coin is published as it completes (interval files, then its
sidecar); a sidecar failure is raised, not swallowed. The local
sidecar is rewritten as `published` only after the R2 writes
succeed.

## Seed

`market_data/crypto_candle_cache/seed/BTC/` holds a small seeded
sample — BTC only, the three bases, 378 real bars fetched live
from Coinbase public candles on 2026-11-24T00:32:44Z (6 requests):
1m 120 bars (2026-11-23T22:32 → 2026-11-24T00:31), 1h 168 bars
(2026-11-17T00:00 → 2026-11-23T23:00), 1d 90 bars (2026-08-26 →
2026-11-23). Last bars: 1h close 61,189.97, 1d close 61,189.97.
The still-forming latest bar of each base was dropped, so every
seeded bar is a completed bar, gap-free over its own span. `seed`
copies these files into R2 layout under `--out`. No other coin, no
other data, is checked in.

## Verification (local, 2026-11-24)

- Collector unit tests: 41 pass (`python -m pytest
  tests/test_crypto_candle_cache.py`), also under
  `python -m unittest`. `py_compile` clean. `node --check` clean
  on both panel JS files.
- Live Coinbase fetch (seed, no `--publish`, 6 requests): 1m 120,
  1h 168, 1d 90 bars, all gap-free, all enveloped
  (`observed_at` 2026-11-24T00:32:44Z, `origin: "recorded"`).
  Live route checks the same night: USDS-USD and USD1-USD tickers
  resolve; CoinGecko `bitcoin/ohlc?days=7` returns 4-hour bars
  (server-chosen granularity); Tiingo
  `/tiingo/crypto/prices?tickers=btcusd` returns "Please supply a
  token" (route exists, no authenticated call made).
- Panel patch: verified to apply cleanly onto pristine #389 files
  (fetched via the GitHub API, no clone) with `patch -p1
  --dry-run`, then applied for real in a scratch layout — the
  applied files are byte-identical to the patch files, and both
  `node tests/crypto_panel.test.js` (the existing #389 test) and
  `node tests/crypto_candles.test.mjs` (new) pass there.
- **Not yet verified live:** no R2 artifact published, no staged
  site preview read back. No producer restarted or redeployed. No
  live service ran on the basis of repo HEAD alone. No
  authenticated Tiingo crypto call has been made from this
  repository (see Sources). `python tests/test_changed_paths.py`
  was not run against a real master checkout (no clone was made);
  the new paths fall in already-covered lanes (`market_data/**` →
  collector_ci, `tests/**` → web_ci, docs/`README.md` → no lane),
  and the harness's expectations were read, not assumed — see
  BUILD_REPORT.md.

## Panel contract (stacks on PR #389)

The panel change applies on top of PR #389 (`cian/crypto-panel`,
`site/js/crypto-panel.js` at `81c7ca3a`), which does not exist on
master yet — it is delivered with this PR as a patch in `panel/`,
to be applied after #389 merges. The panel:

- reads `crypto/candles/BTC/1h.json` from the same public-data
  origin (`BROWSER_DATA`) first, as of now, through the real
  `selectAsOf` / `readingAsOf` (new ESM module
  `site/js/crypto-candles.js`, which imports them from
  `site/shared/one-clock.js`);
- renders one line: bar count, latest close with its as-of time,
  the reading state, the provider, and the coverage label, with
  price gaps stated next to it;
- renders a missing cache entry (404), a malformed file, or an
  empty bars list as "candles unavailable" with the reason — never
  a chart of nothing, never a price without its as-of time;
- needs no `site/index.html` change except the `#crypto-candles`
  element and the script hashes: the Crypto tab and panel wiring
  already exist in #389. Both `?v=` hashes are the first 16 hex
  chars of the new files' sha256 (the convention the existing
  panel test pins — verified: sha256 of #389's crypto-panel.js
  starts `4ac1f1cbdebcdf92`, its `?v=` value).
- draws no chart: a candlestick chart over the cached intervals,
  per-coin selection, and interval switching are future scope —
  the read path this patch proves (R2 file → observations → as-of
  selection) is what a chart would consume.

## CI wiring (maintainer)

The GitHub App token cannot create or update workflow files (403,
`workflows` permission missing — same blocker as the Crypto Panel
PR #389, the NASDAQ-100 panel PR #391, and the single-stock
backfill PR #393). A maintainer with workflow access must add the
following steps; they are **not** in this PR.

1. In `.github/workflows/collector-ci.yml`, add a step that runs
   the new collector tests (same Python job as the existing
   collector tests):

   ```yaml
   - name: Crypto candle cache tests
     run: python -m pytest tests/test_crypto_candle_cache.py -q
   ```

   (If the job installs test dependencies, `pytest` must be among
   them; the collector itself needs only the standard library plus
   `boto3` for `--publish`, both already in
   `deployment/python/requirements.txt`.)

2. In `.github/workflows/web-ci.yml`, add a step that runs the new
   panel test (same pattern as the other panel tests, e.g.
   `node tests/crypto_panel.test.js`):

   ```yaml
   - name: crypto candle cache panel (R2-first as-of candles)
     run: node tests/crypto_candles.test.mjs
   ```

   The test file is delivered with the panel patch (it requires
   `site/js/crypto-candles.js` from the patch) and joins `tests/`
   when the patch is applied.

## Open questions

- **Where the full backfill data lives:** in the parallel Crypto
  Backend job (`~/workspace/crypto-backend/`) and its Notion page
  "Crypto Backend" — not in this repository and not in this PR.
  Who publishes that data to R2, on what schedule, and does this
  collector's `update` then take over steady state, or does the
  backend job keep owning it?
- **Who owns the Tiingo token and the Coinbase/CoinGecko routes?**
  The token lives in the `custom.tiingo` connection today. Which
  secret holds `TIINGO_API_TOKEN` for the deployed collector, and
  who rotates it? Free Starter is personal/internal use only;
  redistribution needs a commercial licence. Coinbase and CoinGecko
  are keyless public routes — who owns them if they change, and is
  the CoinGecko daily-only limitation acceptable long-term for the
  18 CoinGecko-labelled coins (no intraday candles at all)?
- **Is One Clock's as-of selector the right read path for a 24/7
  crypto panel?** The panel now reads through `selectAsOf` /
  `readingAsOf` with session `"continuous"`. That treats a candle
  bar as an observation that becomes selectable when it closes —
  which is honest, but it means the panel never shows the
  still-forming bar, and a replay-mode (`mode: "replay"`) reading
  is available but unwired. Is the as-of read the panel's primary
  path, or should the panel also offer a plain latest-bar read?
- **USDT's source label:** the backend job found a live
  `USDT-USD` Coinbase pair (2,030 daily bars from 2021-05-04),
  but the snapshot contract prices USDT from CoinGecko and this
  package keeps it CoinGecko-labelled. Should the cache follow the
  snapshot label or the live pair?
- **Unconfirmed R2 env vars:** `CRYPTO_R2_BUCKET` (and the shared
  `R2_ENDPOINT_URL` / key pair) are unconfirmed against live
  service config, as with PRs #389, #391 and #393. Is the bucket
  `qqq-options-chain-data`, and does the publisher's key have write
  access to the `crypto/candles/` prefix?
- **First authenticated Tiingo crypto call:** the route exists but
  no authenticated call has been made from this repository. The
  first call should be recorded here (shape, volume field, depth)
  before any Tiingo-sourced bar is treated as established — and it
  spends the token budget the stock backfill job shares.
- **Backfill windows:** the defaults (1m 7 days, 1h 90 days, 1d
  full depth) are the repo collector's windows. The backend job
  fetched 1m for 1 day and 1h for 90 days per coin. Which windows
  does the first fill actually use, and who runs it?
