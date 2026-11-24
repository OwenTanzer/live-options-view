# Market data

These services acquire or derive data for readers elsewhere in the repository.
They have separate lifecycles; a browser request does not start them.

| Component | Output and role | Entry from repository root |
|---|---|---|
| [Live market](live_market/README.md) | QQQ/SPY snapshots, price strip, health and in-memory live quotes | `python -m market_data.live_market` |
| [Crypto snapshot](crypto_collector/README.md) | Hourly 39-coin spot prices, trend patterns and provenance sidecar | `python -m market_data.crypto_collector loop --publish` |
| [NASDAQ-100 collector](nasdaq_collector/README.md) | Hourly NASDAQ-100 (101 listings) + NDX snapshot, per-ticker news and candles; `nasdaq/latest.json` + provenance sidecar | `python -m market_data.nasdaq_collector collect` |
| [Tradier archive](tradier/OPERATIONS.md) | Daily time-and-sales archives; no identified in-repository consumer | `python -u -m market_data.tradier.launcher` |
| [Options scanner](options_scanner/README.md) | Session sweeps, contract rankings and active snapshots | `python -u -m market_data.options_scanner run` |
| [Crypto candle cache](crypto_candle_cache/README.md) | Historical crypto candles per coin per interval, One Clock as-of reads | `python -m market_data.crypto_candle_cache backfill --out <dir>` |
| [Crypto predictions](crypto_predictions/README.md) | Hourly next-hour crypto price estimates, One Clock synthetic observations | `python -m market_data.crypto_predictions predict --cache-dir <dir> --out <dir>` |

All use the [shared Python image](../deployment/python/README.md). Schedules,
activation and provider configuration are documented with each service.
[diagnostics.py](diagnostics.py) supplies shared bounded error redaction.
The scanner explicitly imports bucket helpers from [Tradier's probe](tradier/probe.py).
Component probes and replay utilities live beside their owners; they are not
routine startup steps. Run offline fixture tests before any credentialed command.

### One-shot price capture

Run `python -m market_data.panel_prices crypto /path/to/crypto/latest.json`
on the Coinbase-capable capture host, or replace the feed and path with
`nasdaq /path/to/nasdaq/latest.json` on the Nasdaq host. The command emits
a separate JSON document on stdout. It needs a recent, trusted hourly snapshot
for membership and enrichment. It has no scheduler, storage client or publish
command. The regular hourly collectors retain their existing interfaces.

In `panel_prices.v1`, `enrichment` holds the input snapshot with its original
timestamps, and `prices` holds independent USD observations by symbol. A
fresh mark requires a provider observation no more than 60 seconds old at
receipt. A date from a previous New York trading day is stale. A date without
an intraday timestamp on the same day has unknown freshness. The caller can
age timestamped marks further when displaying a saved document. Missing marks
carry a null price. The capture clock does not advance the observation clock.

Crypto uses one USD CoinGecko simple-price batch and a Coinbase Exchange
ticker per Coinbase symbol. Nasdaq requests watchlists in groups of 20.
When an undated or stale watchlist mark differs from the hourly primary
price, or a listing is missing, the collector attempts the per-symbol primary
quote. Each mark identifies its source. News, candles, membership discovery
and 24-hour enrichment are read from the hourly document.

A run admits new requests for up to 32 seconds, with four pool workers,
at most one additional CoinGecko batch on the coordinating thread, and at
least 0.2 seconds between planned starts per provider. Connect and socket
read timeouts are two and four seconds. These bound individual socket waits;
DNS and an in-progress response can outlast the admission window. A 429 or
503 suppresses further requests to that provider for the run. The returned
`retry_after` contains the provider header (or 60 seconds when absent);
a future scheduler must respect it across invocations. `calls`, `failures`
and `coverage` distinguish transport from measured price freshness.
