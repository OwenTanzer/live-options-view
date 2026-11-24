# Crypto candle history and One Clock

Options View can replay Cian’s delivered crypto history and collect new candles from providers. A delivered ZIP has a fixed universe, provider selection and coverage manifest. A new collection resolves the current panel universe. The two paths have different evidence and availability times.

## Delivered history

The Crypto Backend page in Notion holds successive ZIP snapshots. On Linux, `market_data.crypto_candle_cache.archive import` reads the manifest-selected JSON members and supplementary CoinGecko daily samples, validates counts and date ranges, and installs a private SQLite database identified by the SHA-256 of the ZIP. Reimporting identical bytes preserves the existing database and its availability timestamp. Each different delivery has a separate snapshot. See the [Linux operating procedure](../../ops/panel-minute/README.md#historical-crypto-archive).

The archive reader supports the delivered 1m, 1h and 1d series. The manifest determines the source independently for each coin and interval. Coinbase rows supply OHLC and volume. Tiingo rows supply the historical fields in the delivered record. CoinGecko market-chart rows supply a price at an instant. For those samples, both event and value time refer to that instant. For OHLC, value time is the start of the candle and event time is its close. Candles still open when the archive job began are withheld because the export lacks per-candle fetch times. Use the reader’s provider option to select an alternate source explicitly. The coverage view preserves the source batch progress flag where available. Missing series and missing timestamps remain gaps.

One Clock availability begins at the local import receipt. A later query can therefore retrieve observations from 2015, while a query cut off before the import returns no observations from that snapshot. Each returned observation identifies the ZIP digest and source member as evidence. The Crypto panel reads the generated site asset containing coverage and up to 24 Coinbase BTC hourly closes. Its cutoff applies to both coverage and prices. Full archive queries use the private database. The panel’s price forecast has its own [synthetic observation contract](CRYPTO_PREDICTIONS.md).

## New provider collections

The collector resolves the top 50 panel coins from the CoinGecko markets list, applies the tracker’s tokenized-asset exclusions, and checks the USD pair for new members. An incomplete universe response uses the embedded fallback and records that choice and the membership diff in the sidecar. A coin’s presence in an earlier ZIP is independent of membership in the current panel universe.

| Source | Input |
|---|---|
| Coinbase Exchange public candles | 1m, 1h and 1d USD candles. The provider returns timestamp, low, high, open, close and volume. Requests page backward in windows of up to 300 bars. |
| Tiingo crypto prices | An optional authenticated backstop when a Coinbase base fetch fails or returns no rows. The token is read from `TIINGO_API_TOKEN`. |
| CoinGecko public OHLC | A 30-day request supplies four-hour OHLC with timestamps at close. Six distinct four-hour slots from the same UTC day produce one reconstructed daily candle without volume. A day with a missing slot is omitted. CoinGecko’s longer automatic OHLC windows use four-day candles. [Provider reference](https://docs.coingecko.com/reference/coins-id-ohlc) |

The steady-state collector requests the stored provider and interval for each update. An unsupported pair fails before files are written. New captures record observation time after fetching the provider response. An identical completed observation keeps its original receipt. A changed completed candle requires a separate snapshot so an older cutoff can still recover its original value. A forming candle can be replaced on refetch. Backfill and seed require an empty destination for the coin.

## Files and time semantics

The local output layout is `crypto/candles/<COIN>/<interval>.json`, with a `snapshot_provenance.v1` sidecar at `crypto/candles/<COIN>/provenance.json`. Each interval file identifies the symbol, interval, provider, derivation base, generation time, number of stored bars, and first and last bar timestamps. Its schema version is 1 and its dataset is `crypto_candles`. The optional R2 publisher uses the same keys and writes each coin’s sidecar after its interval files. Archive import writes to private storage independently of that publisher.

Every candle records its UTC start `t`, OHLC, observation time `observed_at` and origin. Volume is included when supplied. Source candles use `recorded`. Locally aggregated candles use `reconstructed`. One Clock uses the continuous session, with `valueAt` at the start, `eventAt` at the close, and `availableAt` at the receipt or local derivation. A bar observed before close stays ineligible until a later fetch confirms it. Replaying later does not promote an early snapshot to a completed observation.

| Derived intervals | Constituents | UTC alignment |
|---|---|---|
| 3m, 5m, 9m, 15m, 30m | 1m | Multiples of the width since midnight |
| 2h, 4h | 1h | Even two- or four-hour boundaries since midnight |
| 1w | 1d | Monday 00:00 through the following Monday |

Each derived bar counts its constituents in `bars`. A missing slot sets `partial: true`, retained on the One Clock value, and causes partial coverage in the sidecar. A 03:00 constituent therefore belongs to the 02:00–04:00 bucket even if the 01:00 observation is missing. A missing base leaves its derived intervals absent. Calendar gaps between existing candles also contribute to partial coverage. “Full” over a file’s span does not establish coverage before the first observation or after the last.

The checked-in Coinbase BTC seed contains 378 completed bars across the three base intervals for offline tests. The archive tests, provider fixtures, One Clock fixtures and browser panel tests exercise separate paths. The release procedure for the generated public asset is described in [site releases](../SITE_RELEASES.md).
