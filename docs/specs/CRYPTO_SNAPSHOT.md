# Crypto snapshot: data contract

The crypto collector publishes an hourly spot-crypto snapshot for
the website's Crypto panel and the Market MCP `crypto_snapshot`
source. This file covers the artifact only. Collection is
[market_data/crypto_collector/README.md](../../market_data/crypto_collector/README.md);
the panel is `site/js/crypto-panel.js`; the MCP tool is
`market_mcp/consumer.js` (`crypto_snapshot`).

## Artifact layout

R2 bucket `qqq-options-chain-data`:

| Artifact | Writer | Readers and meaning |
|---|---|---|
| `crypto/latest.json` | Crypto collector, hourly | Latest 39-coin snapshot; Crypto panel and MCP. A missing file means no publication yet, never an equity snapshot. |
| `crypto/latest.provenance.json` | Crypto collector, after `latest.json` | `snapshot_provenance.v1` sidecar: `constructed_at`, `archive_uploaded_at` (null until a dated archive copy exists), `latest_published_at`, `publication.status`, per-coin `price_basis` / `provider_observed_at` / `freshness`. A missing sidecar means unknown provenance. |

No dated archive tree is defined in this version; `archive_uploaded_at`
stays null until one is. Do not invent archive keys from the equity
`intraday/YYYYMMDD/` layout.

## Payload

`crypto/latest.json` has `schema_version: "crypto_snapshot.v1"` and:

- `constructed_at` / `generated_at`: ISO-8601 UTC build time.
- `session_model: "continuous_24_7"`, `cadence: "hourly"`. Crypto
  has no equity session close; no field in this artifact means a
  session open/close.
- `coins`: 39 rows, sorted by live CoinGecko market-cap rank. The
  fixed universe is
  [market_data/crypto_collector/coins.py](../../market_data/crypto_collector/coins.py):
  30 `coinbase` rows and 9 `coingecko` rows (USDT, USDC, TRX, XMR,
  LEO, WBT, DAI, CC, GRAM) — the 39 coins of the live Coinbase Price
  Tracker, not the top 39 by market cap (the tracker's Coinbase set
  reaches down to rank 85, ATOM).
- Per-coin fields: `rank`, `symbol`, `name`, `source`, `price_basis`,
  `metadata_source`, `currency` (`USD`), `price`, `high_24h`,
  `low_24h`, `change_24h_pct`, `change_1h_pct`, `change_7d_pct`,
  `volume_24h` (USD — Coinbase stats volume arrives in base units
  and is converted at the ticker price, matching CoinGecko's
  USD `total_volume`), `market_cap`, `provider_observed_at`, `as_of`,
  `freshness` (`fresh` / `stale` / `unavailable`), `status`
  (`ok` / `partial` / `source_unavailable`), `status_reason`, `gaps`.
- `market`: `total_market_cap_usd`, `btc_dominance_pct`,
  `fear_greed` (`value`, `classification`, `as_of`, `status`,
  `source: "alternative.me"`), `status`, `status_reason`.
- `patterns`: read-only trend-pattern rows (`id`, `dedupe_key`,
  `kind`, `severity`, `title`, `detail`, `coins`, `detected_at`),
  deduped within 90 minutes. Same detection list as the tracker;
  see the collector README.
- `counts`: `coins`, `available`, `partial`, `source_unavailable`.

## Isolation: no substitution

`source` and `price_basis` always agree, and a row's price comes
only from its own source:

- A `coinbase` row with a missing/failed Coinbase product publishes
  `price: null`, `status: "source_unavailable"`, `freshness:
  "unavailable"` — it is never priced from CoinGecko.
- A `coingecko` row with a missing/failed CoinGecko entry publishes
  the same shape with `price_basis: "coingecko"`.
- Rank, market cap and 1h/7d change are CoinGecko *metadata*
  (`metadata_source: "coingecko"`) for every coin, including
  Coinbase coins. They are not the price basis; when CoinGecko
  metadata is missing those fields are null and the row reports the
  gap. A consumer must reject a row whose `price_basis` disagrees
  with its `source` (the MCP validator does this as
  `incompatible_schema`).

## Freshness

Freshness is judged against the provider's own observation time
(`provider_observed_at`), never against an equity session clock: a
row is `fresh` while its observation is under two hours old
(hourly cadence), `stale` after that, `unavailable` with no
observation. Snapshot `constructed_at` is build time, not provider
observation time. Fear & Greed carries its own `as_of`; when the
feed cannot be read it is `{status: "unavailable", value: null}` —
no value is fabricated.

## Consumers

- The Crypto panel reads `crypto/latest.json` only; it never
  computes or re-derives a price, and shows an explicit
  stale/source-unavailable state instead of a substituted number.
- The MCP `crypto_snapshot` tool returns the same rows with the
  same field/freshness shape as `market_context`. It does not
  change `market_context`'s QQQ/SPY-only contract.
- No consumer in this version joins crypto prices to news, options
  chains, One Clock or Crassus.

## Open questions for reviewers

- Is hourly the right cadence, or should the crypto collector run
  more often?
- Who owns the CoinGecko source (rate limits, attribution) and the
  Fear & Greed feed?
- The 39-coin universe matches the live Coinbase Price Tracker
  (30 Coinbase + 9 CoinGecko, verified 2026-11-23 22:06 UTC); all
  30 `coinbase`-labelled coins have a live Coinbase Exchange USD
  pair.
- Should a dated archive tree (`crypto/YYYYMMDD/…`) be added, and
  who owns its retention?
