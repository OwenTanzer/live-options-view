# Crypto predictions: data contract

Hourly next-hour price estimates for the top-50 crypto, published
as One Clock observations with `origin: "synthetic"`. This file
covers the artifact only. The producer is
[`market_data/crypto_predictions/`](../../market_data/crypto_predictions/);
it computes estimates from the candle cache's 1h bars
([CRYPTO_CANDLE_CACHE.md](CRYPTO_CANDLE_CACHE.md), PR #395). The
panel read path is `site/js/crypto-predictions.js` (stacks on PR
#389).

**Estimates only. Nothing in this artifact is a price, a forecast
to trade on, or a signal. A prediction is a computed guess about
one hour ahead, labelled as such everywhere it is shown.**

## Artifact layout

R2 bucket `qqq-options-chain-data` (expected — unconfirmed, same
as the candle cache):

| Artifact | Writer | Readers and meaning |
|---|---|---|
| `crypto/predictions/<COIN>.json` | Prediction producer, hourly | The coin's live estimate for the next hour, plus retained past predictions (up to 7 days) kept as historical evidence. A missing file means no prediction has been published for the coin, never a live price. |
| `crypto/predictions/<COIN>/provenance.json` | Prediction producer, after the prediction file | `snapshot_provenance.v1` sidecar: `constructed_at`, `archive_uploaded_at` (null until a dated archive copy exists), `latest_published_at`, `publication.status`, estimator signals, live/historical counts, coverage. A missing sidecar means unknown provenance. |

No dated archive tree is defined in this version; `archive_uploaded_at`
stays null until one is. Do not invent archive keys from the equity
`intraday/YYYYMMDD/` layout.

## Payload

`crypto/predictions/<COIN>.json` has `schema_version: 1`,
`dataset: "crypto_predictions"`, `horizon: "1h"` and:

- `generated_at`: ISO-8601 UTC producer run time.
- `coverage`: `"current"` (the latest row's target hour has not
  passed), `"historical"` (every target hour has passed, but the
  value rows are retained as historical evidence), or `"absent"`
  (no estimate could be computed — see below).
- `observations`: One Clock observation rows (see below), oldest
  first. `observations_count` must match.

## The One Clock envelope

Every prediction is an observation, using the real envelope names
from `site/shared/one-clock.js` (`source`, `id`, `revision`,
`session`, `instrument`, `origin`, `evidence`, `eventAt`,
`availableAt`, `value`):

| Field | Value |
|---|---|
| `source` | `"crypto_predictions"` (the adapter id) |
| `instrument` | The coin symbol (`"BTC"`) |
| `session` | `"continuous"` — crypto trades 24/7, the same session string the candle cache uses |
| `origin` | `"synthetic"`, always. An estimate is computed, not observed |
| `id` | `"<COIN>\|1h\|<target hour start ISO>"` — one identity per coin per target hour |
| `revision` | `1`, always. A passed prediction is retained at
  revision 1; no revision-2 null row is ever published |
| `eventAt` | The target hour's start (= the close of the last completed 1h bar: the moment the estimate was made), epoch ms |
| `valueAt` | The same instant as `eventAt` |
| `availableAt` | The producer run time, epoch ms; must be at or after `eventAt` (One Clock rejects availability before the event) |
| `evidence` | The R2 key `crypto/predictions/<COIN>.json` |
| `value` | The estimate (below). Always an estimate: this adapter
  never emits a `null` value. (One Clock itself still allows
  `null` as a retraction for other adapters.) |

`value` for a live estimate:

- `estimated_price` — the estimated close at the end of the target hour.
- `direction` — `"up"` / `"down"` / `"flat"` on the sign of the
  estimated move, with a `0.05%` dead band for flat.
- `pct_move` — the estimated percent move from the last close.
- `range_low` / `range_high` — one hourly-volatility sigma around
  the estimate; the range always strictly contains the estimate.
- `confidence` — `0.05`–`0.95`, from window completeness and
  hourly volatility. It measures how much data and how calm a
  history the estimate rests on, **not** a probability that the
  estimate is right.
- `last_close` — the last completed 1h bar's close the estimate
  is measured from.
- `data_window` — `{bars_1h, returns, first_bar, last_bar,
  hours_24h, hours_7d}`: the bars actually used.
- `basis` — the signals actually used, from: last-hour momentum,
  24h move, 7d move, hourly volatility, last-hour range, BTC
  correlation.
- `signals` — the computed signal values
  (`momentum_1h_pct`, `move_24h_pct`, `move_7d_pct`,
  `hourly_volatility_pct`, `last_hour_range_pct`,
  `btc_correlation` or `null`).
- `target_start` / `target_end` — the target hour, ISO-8601 UTC.

## The estimator

`market_data/crypto_predictions/estimator.py` is a pure function
from recent completed 1h bars — no live-service dependency, no
clock, no I/O:

```
drift = 0.5 * momentum_1h
      + 0.25 * (move_24h / hours_24h)
      + 0.25 * (move_7d / hours_7d)
drift clamped to ±3 * hourly_volatility (0.2% floor)
estimated_price = last_close * (1 + drift)
range = estimated_price ± max(hourly_volatility * last_close,
                              0.1% * last_close)
confidence = completeness * max(0, 1 - volatility / 0.04),
             clamped to [0.05, 0.95]
```

- Window: the last 24 hourly returns (25 closes); the 24h move
  over up to 24 bars, the 7d move over up to 168 bars, with the
  hours actually used reported in `data_window`.
- BTC correlation is the Pearson correlation of the coin's hourly
  returns with BTC's over the aligned window (minimum 8 pairs).
  It is `null` for BTC itself and when too few aligned pairs
  exist; the basis then omits it.
- Only *completed* 1h bars enter the window. The still-forming
  latest bar is excluded, exactly as the candle reader excludes
  it from selection — an estimate never rests on a bar that has
  not closed.

## Retention — predictions are never retracted

A prediction stays in One Clock after its target hour passes,
with its value intact, as historical evidence against which
the prediction can be judged. Nothing retracts it: the
producer never publishes a revision-2 null row, and a passed
prediction is never removed until it ages out of the file
(past predictions are carried for 7 days, then pruned — a
bound on file size, not a retraction). A reader judges a
passed prediction by its `eventAt` / `availableAt` and its
value's `target_start` / `target_end`, which mark it as a past
prediction, not a current price.

What `readingAsOf` returns for a passed prediction, honestly:
the selector returns the latest row as of the cutoff with its
value intact, so the state is `fresh` while its age
(`cutoff − valueAt`) is within the caller's `max_age_ms` and
`stale` once it is older — never `unavailable` merely because
the hour passed. `unavailable` means no prediction exists at
all as of the cutoff (or the estimate is older than the
caller's bound in a reader that treats that as unavailable).
A consumer that shows only the *live* next-hour estimate must
therefore check that the returned row's target hour has not
passed at the cutoff (`target_end > cutoff`); the panel does
exactly that, and renders anything else as "no live estimate",
with the reason naming the retained history. The daily PR
batch (see the PR body) scores retained predictions against
the actual prices that followed — that judgement is the
reason they are kept.

## Cohorts: live prices and estimates never mix

- The candle cache selects `origin: "recorded"` /
  `"reconstructed"`; predictions select `origin: "synthetic"`.
  A reader that selects the recorded cohort never sees a
  prediction as a price, and a reader that selects the synthetic
  cohort never sees a live price as an estimate.
- The panel renders estimates in a separate "Next hour estimate"
  section, labelled `ESTIMATE ONLY / RESEARCH ONLY`, never in
  the price table, never as a live price, never a forecast to
  trade on.
- No change to One Clock's own QQQ/SPY/IBIT fields, and no
  change to `site/shared/one-clock.js`.

## Absence

- A coin with no 1h file produces no prediction file. The 18
  CoinGecko-labelled coins have a 1d base only (CoinGecko free
  OHLC has no 1m/1h series), so they have no estimate by design
  and are labelled absent in provenance — never interpolated
  from daily bars.
- A coin whose 1h file holds fewer than 2 completed bars likewise
  produces no estimate, with the reason recorded.
- A missing BTC 1h file degrades the other coins to
  no-correlation estimates (the basis says so) rather than
  failing the run.
- Pegged stablecoins (USDT, USDC, USDS, USDE, RLUSD, USDG,
  PYUSD, DAI, USDF, U, USD1) estimate flat by construction:
  near-zero volatility and near-zero moves. Their estimates carry
  no information; reviewers may exclude them from display.

## Consumers

- The Crypto panel reads `crypto/predictions/<COIN>.json` through
  the real `selectAsOf` / `readingAsOf` (`site/js/crypto-predictions.js`,
  origin `"synthetic"`, 2h staleness bound like the snapshot).
  A passed prediction is not shown as the live next-hour
  estimate: the read path checks the returned row's target hour
  and reports "no live estimate" when it has passed, naming the
  retained history as the reason. The retained rows themselves
  stay readable through `selectAsOf` for judging.
- No consumer joins predictions to news, options chains, One
  Clock equity fields, Crassus, or the private BTC lane. No
  trading, no alerts.
- The Market MCP does not expose predictions in this version.

## CI wiring (maintainer)

The GitHub App token cannot create or update workflow files (403,
`workflows` permission missing — same blocker as the Crypto Panel
PR #389, the NASDAQ-100 panel PR #391, the single-stock backfill
PR #393, and the candle cache PR #395). A maintainer with workflow
access must add the following steps; they are **not** in this PR.

1. In `.github/workflows/collector-ci.yml`, add a step that runs
   the new prediction tests (same Python job as the existing
   collector tests):

   ```yaml
   - name: Crypto prediction tests
     run: python -m pytest tests/test_crypto_predictions.py -q
   ```

   (If the job installs test dependencies, `pytest` must be among
   them; the producer itself needs only the standard library plus
   `boto3` for `--publish`, both already in
   `deployment/python/requirements.txt`.)

2. In `.github/workflows/web-ci.yml`, add a step that runs the new
   panel test (same Node job as the existing panel tests):

   ```yaml
   - name: Crypto predictions panel tests
     run: node tests/crypto_predictions_panel.test.mjs
   ```

## Open questions for reviewers

- **Retention (decided 2026-11-23, at Cian's direction):**
  predictions are retained as historical evidence, not
  retracted — a passed prediction stays in One Clock with its
  value intact so it can be judged against the actual price.
  The open question that remains is whether 7 days of history
  per coin is the right bound, or whether the daily PR batch
  archive should keep them longer.
- Is an hourly estimate the right product, or does a synthetic
  cohort in One Clock invite readers to treat estimates as
  prices? The cohort separation and the estimate-only labelling
  are the mitigations; reviewers should say whether they are
  enough.
- Should predictions be backtested/calibrated before any wider
  display (e.g. hit rate of the direction call, range coverage)?
  No calibration exists yet — the estimator's weights are
  documented judgement, not fitted parameters.
- Should the Market MCP expose a read-only `crypto_predictions`
  source, like `crypto_snapshot`? Not in this version.
- Who owns the prediction producer's schedule, and the R2 env
  vars (`CRYPTO_R2_BUCKET`, shared `R2_ENDPOINT_URL`/key pair —
  unconfirmed against live service config)?
- Should pegged stablecoins be excluded from display (their
  estimates are flat by construction)?
- The seed covers BTC only. Who publishes the first full-universe
  run, and on what schedule?
