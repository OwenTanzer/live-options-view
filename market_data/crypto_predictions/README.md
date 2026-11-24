# Crypto predictions

Hourly next-hour price estimates for the top-50 crypto, published
as One Clock observations with `origin: "synthetic"` — estimates,
never recorded prices. The contract is
[`docs/specs/CRYPTO_PREDICTIONS.md`](../../docs/specs/CRYPTO_PREDICTIONS.md).

## What it publishes

| Object key (R2 bucket `qqq-options-chain-data`, expected — unconfirmed) | Contents |
|---|---|
| `crypto/predictions/<COIN>.json` | One prediction file per coin, `schema_version: 1`, `dataset: "crypto_predictions"`: the live estimate for the next hour plus retained past predictions (up to 7 days), kept as historical evidence so each prediction can be judged after its hour |
| `crypto/predictions/<COIN>/provenance.json` | `snapshot_provenance.v1` sidecar for the coin |

One horizon only (1h ahead), one prediction per coin per hour.
A prediction stays in One Clock after its target hour passes,
with its value intact — it is never retracted. A reader judges
a passed prediction by its `eventAt` / `availableAt` and its
value's `target_start` / `target_end`, which mark it as a past
prediction, not a current price. Live prices and future estimates
stay in separate cohorts: the candle cache selects `origin: "recorded"` /
`"reconstructed"`, predictions select `origin: "synthetic"`, and
neither selection ever sees the other's rows.

## Estimator

`estimator.py` is a pure function from recent completed 1h bars:
last-hour momentum, 24h/7d moves, hourly volatility, last-hour
range, and BTC correlation for non-BTC coins. No live-service
dependency, no clock, no I/O. The estimate is a drift blend
(`0.5` last hour, `0.25` per-hour 24h rate, `0.25` per-hour 7d
rate) clamped to ±3 hourly-volatility sigmas, with a one-sigma
range around it and a confidence from window completeness and
volatility. The exact formula and the signal list are in the spec;
`basis` in every published value names the signals actually used.

## Sources

Predictions are computed from the candle cache's 1h bars
(`market_data/crypto_candle_cache/`, PR #395) — no new provider
calls, no Tiingo tokens. A coin with no 1h file (the 18
CoinGecko-labelled coins, 1d base only) has no prediction by
design and is labelled absent, never interpolated from daily
bars. Pegged stablecoins estimate flat by construction; the spec
records that their estimates carry no information.

## Commands

```sh
python -m market_data.crypto_predictions universe
python -m market_data.crypto_predictions predict --cache-dir cache-out --out pred-out [--symbols BTC ETH] [--publish]
python -m market_data.crypto_predictions status --out pred-out [--symbols BTC]
python -m market_data.crypto_predictions seed --out pred-out   # checked-in BTC seed only
```

`--publish` is opt-in and needs `R2_ENDPOINT_URL`,
`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`, `CRYPTO_R2_BUCKET`
(all unconfirmed against live service config). Without it, files
are written locally and the sidecar says `not_published`.

## One Clock

Every prediction is an observation — session `"continuous"`,
`eventAt` at the target hour's start (the close of the last
completed 1h bar), `valueAt` the same instant, `availableAt` the
producer run time. The reader (`predictions.py`) maps files to
observations and serves them through the candle cache's Python
port of `selectAsOf` / `readingAsOf` (`site/shared/one-clock.js`,
unchanged). The still-forming latest bar never enters the
estimator window. No change to One Clock's own QQQ/SPY/IBIT
fields.

## Seed

`seed/BTC/predictions.json` holds a small seeded sample — BTC
only, seven hourly predictions computed from the real checked-in
BTC 1h seed bars (see `seed/README.md`). It is a seed for local
testing and for the `seed` command, not the backfill: no other
coin, no other data, is checked in.

## Tests

```sh
python -m pytest tests/test_crypto_predictions.py -q
python -m unittest tests.test_crypto_predictions -v
```
