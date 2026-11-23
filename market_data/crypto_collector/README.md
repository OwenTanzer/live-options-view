# Crypto snapshot collector

The collector publishes the hourly spot-crypto snapshot consumed by
the website's Crypto panel and the Market MCP (`crypto_snapshot`
source). Run `python -m market_data.crypto_collector` from the
repository root; [__main__.py](__main__.py) delegates to
[cli.py](cli.py), following the
[options scanner](../options_scanner/README.md) layout. It uses the
[shared Python image](../../deployment/python/README.md).

## Session model: continuous, no close

Crypto trades 24/7. This collector has no equity session, no
09:30–16:00 ET window and no holiday calendar. It runs one
collection per hour, forever (`loop`, default interval 3600 s), and
every artifact carries `"session_model": "continuous_24_7"`. Do not
reuse equity-session freshness rules here: a row is fresh while its
own provider observation is under two hours old, stale after that,
unavailable when its source could not be read.

## What it fetches (public only, no auth)

| Provider | Coins | Fields |
|---|---|---|
| Coinbase Exchange public API (`/products/<SYMBOL>-USD/ticker` + `/stats`) | 30 coins labelled `coinbase` | Price + last-trade time; 24h open/high/low/volume; 24h change from `(last - open) / open` |
| CoinGecko public markets API (`/coins/markets`, one call) | 9 coins labelled `coingecko`: USDT, USDC, TRX, XMR, LEO, WBT, DAI, CC, GRAM | Price, 24h high/low/change, volume, market cap, rank, 1h/7d change |
| CoinGecko markets (same call) | All 39, metadata only | Rank, market cap, 1h/7d change for the Coinbase coins |
| CoinGecko `/global` | — | Total market cap, BTC dominance (best-effort) |
| alternative.me Fear & Greed (`/fng/?limit=1`) | — | Value + classification + its own as-of time (best-effort) |

No account, no API key, no Coinbase MCP credential. The tracker's
Coinbase *account* connection is a different thing and stays
read-only; this collector never uses it.

## The 39 coins

Fixed universe in [coins.py](coins.py), in market-cap rank order as
observed 2026-11-23 22:06 UTC: the 39 coins of the live Coinbase
Price Tracker — the 30 it prices from Coinbase plus the 9 it prices
from CoinGecko (USDT, USDC, TRX, XMR, LEO, WBT, DAI, CC, GRAM). It
is not the top 39 by market cap: the tracker's Coinbase set reaches
down to rank 85 (ATOM). Ranks are re-read live from CoinGecko on
every run; the fixed order is the fallback and the order the tests
assert.

All 30 `coinbase`-labelled coins have a live Coinbase Exchange USD
pair (verified 2026-11-23 22:06 UTC). A row whose source cannot be
read at run time is published as `source_unavailable` with an
explicit reason, never priced from the other source and never
silently dropped. GRAM is priced from CoinGecko even though Coinbase
lists TON-USD for the same network token — the tracker prices it
from CoinGecko, and the two sources are never substituted for each
other.

## What the outputs mean

- `crypto/latest.json` (`crypto_snapshot.v1`): 39 rows by live
  market-cap rank. Every row carries `source` and `price_basis`
  (`coinbase` or `coingecko`), `provider_observed_at` / `as_of`,
  `freshness` (`fresh` / `stale` / `unavailable`) and `status`
  (`ok` / `partial` / `source_unavailable` with `status_reason`).
  A missing source shows as `source_unavailable` with a null price;
  a Coinbase price is never replaced by a CoinGecko price or vice
  versa. Rank, market cap and 1h/7d change are CoinGecko *metadata*
  (`metadata_source`), distinct from the price basis.
- `crypto/latest.provenance.json` (`snapshot_provenance.v1` sidecar,
  same shape as the main collector's): `constructed_at`,
  `archive_uploaded_at` (null until a dated archive copy is
  written), `latest_published_at`, `publication.status`, and the
  per-coin `price_basis` / `provider_observed_at` / `freshness`.
  A missing sidecar means unknown provenance.
- `market`: total market cap, BTC dominance, Fear & Greed with its
  own as-of time. Fear & Greed is best-effort: when the feed cannot
  be read it is `{status: "unavailable", value: null}` — no value is
  fabricated.
- `patterns`: the read-only trend-pattern section (same detection
  list as the tracker: sharp 24h moves, momentum alignment/
  divergence, 24h high/low tests, wide-range volatility, elevated
  volume, sustained 7-day trends, jumps since last update, broad
  rally/selloff breadth, BTC-vs-alts divergence, Fear & Greed
  extremes, market-cap/dominance shifts), deduped within 90 minutes.
  Detection is pure logic in [patterns.py](patterns.py); a pattern
  that is still true after the window re-appears.

## Operator commands

```sh
python -m market_data.crypto_collector smoke
python -m market_data.crypto_collector run --out ./crypto-out
python -m market_data.crypto_collector run --out ./crypto-out --previous ./crypto-out/crypto/latest.json
python -u -m market_data.crypto_collector loop --publish   # deployed shape
```

`run` without `--publish` writes only local files. `--publish`
uploads `crypto/latest.json` and the sidecar to R2 bucket
`qqq-options-chain-data` via boto3; it needs `R2_ENDPOINT_URL` and
`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` in the environment
(`CRYPTO_R2_BUCKET` overrides the bucket). A failed publish raises
and leaves the previously published artifact in place. Do not
restart, redeploy or manually launch a producer to test a change;
fixture tests are inventoried in [Collector CI](../../.github/workflows/collector-ci.yml).

## What this collector does not do

No trading, no alerts, no favourites persistence, no Crassus, no
One Clock crypto fields, no BTC options, no news joins, no VIX/VVIX,
no 90-day chart history. The full artifact contract is
[docs/specs/CRYPTO_SNAPSHOT.md](../../docs/specs/CRYPTO_SNAPSHOT.md).
