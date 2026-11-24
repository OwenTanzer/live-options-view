# Candle-cache backend

Local cache reader and writer for the R2 candle layout published by
`market_data/single_stock_historical_backfill/` (PR #393, unmerged at
build time): `nasdaq/candles/<TICKER>/<interval>.json` plus one
`snapshot_provenance.v1` sidecar per ticker. Every cached bar is
stamped with the One Clock observation envelope and selected as of a
chosen time. Nothing here publishes, schedules or deploys anything;
R2 publication is opt-in only.

## Layout

| File | Role |
|---|---|
| `envelope.py` | One Clock observation envelope for a bar: `source`/`id`/`revision`/`session`/`instrument`/`origin`/`evidence`/`eventAt`/`availableAt`/`value`, plus state chips (`recorded` / `reconstructed` / `synthetic`) and gap labels. Verified field-for-field against `site/shared/one-clock.js`. |
| `selector.py` | As-of selector mirroring One Clock's `selectAsOf`/`readingAsOf` (see its docstring for why it mirrors rather than imports), plus `gap_report` and `as_of_bars`. No change to One Clock's QQQ/SPY/IBIT fields. |
| `cache.py` | Reader/writer: interval files in the exact PR #393 shape, provenance sidecars, read-time envelope stamping, `read_as_of`, stale-cache reporting, `verify_tree`. |
| `store.py` | `CacheStore` abstraction: `LocalDirectoryStore` (atomic writes) and `R2Store` (boto3, lazy import; env config only, same names as PR #393). |
| `provenance.py` | `snapshot_provenance.v1` sidecar builder, with the additive `envelope` block (per-file origin/revision/available_at/evidence). |
| `resample.py` | Derived intervals from the three bases (copied from PR #393 so this package is self-contained on master). |
| `cli.py` | `seed` / `read` / `as-of` / `verify` / `publish` (see below). |
| `sample/aapl-seed.json` | Small seeded sample: AAPL only, three bases (1m: 390 bars, 1h: 300, 1d: 300) — live Tiingo bars, see below. |

The browser side lives in `site/js/candle-cache-one-clock.js`: a One
Clock source adapter that reuses the real core (`selectAsOf` /
`readingAsOf` from `../shared/one-clock.js`). It is not wired into
the NASDAQ-100 panel yet — that panel arrives in PR #391, which is
unmerged, and the wiring is a follow-up.

## Use

```sh
# Write the bundled AAPL seed sample into a local cache tree (no network).
python -m market_data.candle_cache_backend seed --out /tmp/candle-cache

# Read one interval file back.
python -m market_data.candle_cache_backend read --root /tmp/candle-cache --symbol AAPL --interval 1m

# Cache state as of a chosen time (epoch ms or offset-qualified ISO).
python -m market_data.candle_cache_backend as-of --root /tmp/candle-cache \
  --symbol AAPL --interval 1m --cutoff 2026-11-23T21:00:00Z

# Check files against the provenance sidecar.
python -m market_data.candle_cache_backend verify --root /tmp/candle-cache --symbol AAPL

# Upload one ticker to R2 (opt-in; needs the R2 environment, see below).
python -m market_data.candle_cache_backend publish --root /tmp/candle-cache --symbol AAPL
```

## Seeded sample

`sample/aapl-seed.json` holds AAPL only: 1m (390 bars, the full
2026-11-23 session), 1h (newest 300 bars, 2026-09-15→2026-11-23),
1d (newest 300 bars, 2025-09-16→2026-11-23). The bars are **live
Tiingo bars, not synthetic**: a subsample of the live fetch performed
2026-11-24T00:12:32Z by the PR #393 backfill smoke run, re-cut here
so the sample stays small. This build's own fresh fetch was not
possible — `api.tiingo.com` returned HTTP 429 (the shared token's
50/hour budget was exhausted by the day's verification calls). No bar
was invented, corrected or filled. The full backfill (~4.8M bars) is
NOT in the repo; see `docs/specs/CANDLE_CACHE_BACKEND.md` for where
it lives.

## R2 publication (opt-in, unconfirmed)

Environment only, never hardcoded (same names as PR #393):

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `TIINGO_R2_BUCKET` — bucket holding the `nasdaq/` prefix (expected
  to be `qqq-options-chain-data`; unconfirmed)

The sidecar is uploaded last; a sidecar failure raises, so a ticker
is never reported published without its provenance.

## Tests

```sh
python -m unittest tests.test_candle_cache_backend -v
node --test tests/candle_cache_one_clock.test.mjs
```
