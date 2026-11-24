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
| `crypto/latest.json` | Crypto collector, hourly | Latest 50-coin snapshot; Crypto panel and MCP. A missing file means no publication yet, never an equity snapshot. |
| `crypto/latest.provenance.json` | Crypto collector, after `latest.json` | `snapshot_provenance.v1` sidecar: `constructed_at`, `archive_uploaded_at` (null until a dated archive copy exists), `latest_published_at`, `publication.status`, per-coin `price_basis` / `provider_observed_at` / `freshness`, and the `universe` block (below). A missing sidecar means unknown provenance. |

No dated archive tree is defined in this version; `archive_uploaded_at`
stays null until one is. Do not invent archive keys from the equity
`intraday/YYYYMMDD/` layout.

## Payload

`crypto/latest.json` has `schema_version: "crypto_snapshot.v1"` and:

- `constructed_at` / `generated_at`: ISO-8601 UTC build time.
- `session_model: "continuous_24_7"`, `cadence: "hourly"`. Crypto
  has no equity session close; no field in this artifact means a
  session open/close.
- `coins`: 50 rows, sorted by live CoinGecko market-cap rank. The
  universe is dynamic — see "Universe" below — and is the live top
  50 by market cap, not a fixed list.
- `universe`: `{source, as_of, coins, membership_source, added,
  removed, error}` — `source` is `"live"` or
  `"embedded_fallback"`; `added`/`removed` are membership-change
  records (`symbol`, `name`, `coingecko_id`, `rank`, `source`) with
  the resolution date in `as_of`, so the panel can show what
  entered/left the top 50. On a fallback run both lists are empty
  and `error` names the failed list fetch.
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

## Universe

The universe is the live top 50 by market cap, resolved on every
run by `collector.resolve_universe` and mirroring the Coinbase
Price Tracker (which is itself dynamic top 50 since 2026-11-23):

- Re-fetch the CoinGecko markets list (top 100, ordered by market
  cap) on every run; exclude tokenized assets under the tracker's
  standing rule — Figure Heloc, Tether Gold, tokenized funds/yield
  coins (the seven CoinGecko ids in
  `coins.TOKENIZED_EXCLUDED_IDS`); take the first 50.
- Probe each entrant's source live: a Coinbase Exchange USD pair
  that answers means `coinbase`, otherwise `coingecko`. Coins
  already in the embedded list keep their verified source label
  (USDT stays `coingecko` even though a USDT-USD pair exists).
- The embedded 39-coin list in
  [market_data/crypto_collector/coins.py](../../market_data/crypto_collector/coins.py)
  (30 `coinbase`, 9 `coingecko`: USDT, USDC, TRX, XMR, LEO, WBT,
  DAI, CC, GRAM — the tracker's 39 as verified 2026-11-23 22:06
  UTC) is the fallback and the diff baseline: when the CoinGecko
  list is missing or partial (fewer than 50 usable coins after
  exclusion), the run publishes the embedded 39 and records
  `source: "embedded_fallback"` with the error — a bad fetch never
  shrinks or empties the snapshot.
- Membership changes are recorded in the snapshot's `universe`
  block (and the provenance sidecar) with symbol, rank, source
  and date. Ground truth from the tracker's first live
  reconciliation (2026-11-24): USDD (rank 51), BFUSD (55) and HTX
  (57) entered and ASTER, DOT, ATOM left; the live resolver
  reproduced exactly that diff against the embedded 39 on a live
  read the same day.

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

## Predictions read path

Hourly next-hour estimates live in a separate artifact —
`crypto/predictions/<COIN>.json`, contract
[CRYPTO_PREDICTIONS.md](CRYPTO_PREDICTIONS.md) — and are read by
the panel through `site/js/crypto-predictions.js`, an ESM module
that serves the files through One Clock's real `selectAsOf` /
`readingAsOf` with `origin: "synthetic"`, session `"continuous"`,
2h staleness bound like the snapshot:

- A prediction is an estimate of the next hour's close, never a
  live price. The panel renders it in its own "Next hour
  estimate" section, labelled `ESTIMATE ONLY / RESEARCH ONLY`,
  with estimated price, direction/% move, range, confidence, data
  window, basis, and estimate/target times — never in the price
  table, never as a live price, never a forecast to trade on.
- A passed prediction is retained in the file as historical
  evidence, never retracted — but it is not shown as the live
  next-hour estimate: the read path checks the returned row's
  target hour and the section says there is no live estimate,
  with the reason naming the retained history and its target
  hour, so the prediction can still be judged after the fact.
- The estimate cohort never mixes with the live-price cohort:
  the snapshot selects recorded prices, the predictions module
  selects synthetic estimates, and a row of the wrong origin is
  a malformed file, never silently served. BTC only in this
  version (the coin with a checked-in prediction seed);
  per-coin expansion is future scope.

## Open questions for reviewers

- Is hourly the right cadence, or should the crypto collector run
  more often?
- Who owns the CoinGecko source (rate limits, attribution) and the
  Fear & Greed feed?
- The 50-coin universe is dynamic (live top 50 by market cap,
  tokenized assets excluded; see "Universe" above). The tracker's
  first live reconciliation (2026-11-24) had USDD, BFUSD and HTX
  entering and ASTER, DOT, ATOM leaving; all entrants probed
  CoinGecko-sourced (no Coinbase USD pair answers for them).
- Should a dated archive tree (`crypto/YYYYMMDD/…`) be added, and
  who owns its retention?
