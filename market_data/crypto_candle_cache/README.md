# Crypto candle cache

R2-backed historical candles for the top-50 crypto, with One Clock
synchronisation. The contract is
[`docs/specs/CRYPTO_CANDLE_CACHE.md`](../../docs/specs/CRYPTO_CANDLE_CACHE.md).

## What it publishes

| Object key (R2 bucket `qqq-options-chain-data`, expected — unconfirmed) | Contents |
|---|---|
| `crypto/candles/<COIN>/<interval>.json` | One interval file per coin per interval, `schema_version: 1`, `dataset: "crypto_candles"` |
| `crypto/candles/<COIN>/provenance.json` | `snapshot_provenance.v1` sidecar for the coin |

Eleven intervals per coin (1m, 3m, 5m, 9m, 15m, 30m, 1h, 2h, 4h,
1d, 1w). Only the three bases (1m, 1h, 1d) are ever fetched; the
rest are derived locally. Crypto trades 24/7, so buckets align to
00:00 UTC — there is no equity session open and no session filter.

## Universe (dynamic top 50)

Every run resolves the live top 50 by market cap before
collecting (`collector.resolve_universe`): the CoinGecko markets
list (top 100) is re-fetched, tokenized assets are excluded under
the tracker's standing rule, and the first 50 are collected, with
each entrant's source probed live (a Coinbase Exchange USD pair
that answers means `coinbase`, otherwise `coingecko`). The
embedded 50 in `coins.py` is the fallback and the diff baseline,
used only when the live list is missing or partial. Membership
changes (added/removed with symbol, rank, source and date) are
recorded in every sidecar's `universe` block; a coin that leaves
the top 50 keeps its stored files, and a coin that enters it is
backfilled from scratch. CoinGecko-labelled entrants get the 1d
base only, like every other CoinGecko-labelled coin. `universe`
prints the resolved list with its changes; `backfill`/`update`
without `--symbols` collect the resolved universe.

## Sources

- **Coinbase** (public candles, no key): the three bases for the 32
  Coinbase-labelled coins. Paging walks backward, 300 bars a page;
  BTC daily reaches 2015-07-20 (verified by the parallel Crypto
  Backend job).
- **Tiingo crypto** (token-authenticated, `TIINGO_API_TOKEN` in the
  environment only): the deep-history backstop, from December 2017.
  The route exists (an unauthenticated request is refused with
  "Please supply a token", not a 404) but no authenticated Tiingo
  crypto call has been made from this repository yet.
- **CoinGecko** (public OHLC, no key): the 1d base only, for the
  CoinGecko-labelled coins. CoinGecko free OHLC has no 1m/1h series,
  so those coins' intraday intervals are absent by design and
  labelled as such — never interpolated.
- **CoinGecko markets** (public, no key): the top-100 list that
  resolves the live top-50 universe on every run (see above).

The full backfill data lives in the parallel Crypto Backend job
(`~/workspace/crypto-backend/`) and its Notion page "Crypto
Backend", not in this repository. This package is the contract, the
seed, and the steady-state update path.

## Commands

```sh
python -m market_data.crypto_candle_cache universe
python -m market_data.crypto_candle_cache backfill --out cache-out [--symbols BTC ETH] [--days-1m 7] [--days-1h 90] [--max-requests N] [--publish]
python -m market_data.crypto_candle_cache update --out cache-out [--symbols BTC] [--publish]
python -m market_data.crypto_candle_cache status --out cache-out [--symbols BTC]
python -m market_data.crypto_candle_cache seed --out cache-out   # checked-in BTC seed only
```

`--publish` is opt-in and needs `R2_ENDPOINT_URL`,
`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`, `CRYPTO_R2_BUCKET`
(all unconfirmed against live service config). Without it, files
are written locally and the sidecar says `not_published`.

## One Clock

Every bar carries its source time (`t`), observed time
(`observed_at`) and state chip (`origin`: `recorded` for a fetched
base bar, `reconstructed` for a locally derived bar; `synthetic`
is never written). The reader (`one_clock.py`) maps bars to One
Clock observations — session `"continuous"`, event at the bar's
close, value at its start, available at its fetch time — and serves
them through `select_as_of` / `reading_as_of`, a faithful Python
port of `selectAsOf` / `readingAsOf` in `site/shared/one-clock.js`.
The still-forming latest bar is stored with `"forming": true` and
is never served as a price. Gaps are labelled ("coverage partial",
"price gap"), never smoothed over.

## Seed

`seed/BTC/` holds a small seeded sample — BTC only, the three
bases, a few hundred real bars fetched live from Coinbase public
candles on 2026-11-24 (see `seed/README.md` for the exact windows
and fetch time). It is a seed for local testing and for the `seed`
command, not the backfill: no other coin, no other data, is
checked in.
```

## Tests

```sh
python -m pytest tests/test_crypto_candle_cache.py -q
```

Offline fixtures only; the seed files are the only live data, and
they are checked in. No provider calls, no R2, no clock dependence
beyond fixed fixtures.
