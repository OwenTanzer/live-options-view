# Crypto candle history

Cian’s historical crypto ZIPs are immutable archive inputs. Each import has a private SQLite snapshot with per-series source, kind, count and date range. Coinbase and Tiingo candles retain their price fields. CoinGecko market-chart observations retain their single price. Replay begins at the import receipt, including for bars whose exchange dates are years earlier. The panel publishes coverage and a small BTC hourly sample from the selected snapshot.

```sh
python -m market_data.crypto_candle_cache.archive import crypto-backend.zip snapshots
python -m market_data.crypto_candle_cache.archive read DATABASE ETH 1h --as-of 2026-11-24T08:01:00Z --limit 24
python -m market_data.crypto_candle_cache.archive panel DATABASE
```

The import prints the database path to substitute for `DATABASE`. Use the corresponding delivery’s database for a historical query. The operational instructions cover regenerating the panel asset. [Linux archive operations](../../ops/panel-minute/README.md#historical-crypto-archive)

## Provider cache

The collector writes per-coin interval files and a provenance sidecar under `<out>/crypto/candles/`. It supports 1m, 1h and 1d bases where available and eight UTC-aligned derived intervals. A missing constituent labels a derived bucket partial. Exchange and Tiingo candles can include volume. The separate CoinGecko public OHLC collector builds a daily candle from six complete four-hour constituents per UTC day. Coverage follows the source actually available for each interval.

```sh
python -m market_data.crypto_candle_cache universe
python -m market_data.crypto_candle_cache backfill --out cache-out --symbols BTC ETH
python -m market_data.crypto_candle_cache update --out cache-out --symbols BTC
python -m market_data.crypto_candle_cache status --out cache-out --symbols BTC
python -m market_data.crypto_candle_cache seed --out fresh-seed
```

Backfill and seed initialize an empty coin destination. Update follows the stored interval and provider, preserves first availability for unchanged completed bars and fails on a conflicting completed observation. Store a revision in a separate snapshot. Optional R2 publication uses `--publish`, `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `CRYPTO_R2_BUCKET`. Keep internal-only data in the private archive.

The offline unit tests cover collection, aggregation, gaps and replay. The BTC seed contains 378 real Coinbase bars collected in November 2026. [Full file and replay contract](../../docs/specs/CRYPTO_CANDLE_CACHE.md)
