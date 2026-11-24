# Market data

These services acquire or derive data for readers elsewhere in the repository.
They have separate lifecycles; a browser request does not start them.

| Component | Output and role | Entry from repository root |
|---|---|---|
| [Live market](live_market/README.md) | QQQ/SPY snapshots, price strip, health and in-memory live quotes | `python -m market_data.live_market` |
| [Single-stock historical backfill](single_stock_historical_backfill/README.md) | Per-stock Tiingo candle backfill for the 101 NASDAQ-100 listings (1m/1h/1d bases, eight derived intervals); `nasdaq/candles/<TICKER>/<interval>.json` + per-ticker provenance sidecar | `python -m market_data.single_stock_historical_backfill backfill` |
| [Tradier archive](tradier/OPERATIONS.md) | Daily time-and-sales archives; no identified in-repository consumer | `python -u -m market_data.tradier.launcher` |
| [Options scanner](options_scanner/README.md) | Session sweeps, contract rankings and active snapshots | `python -u -m market_data.options_scanner run` |

All use the [shared Python image](../deployment/python/README.md). Schedules,
activation and provider configuration are documented with each service.
[diagnostics.py](diagnostics.py) supplies shared bounded error redaction.
The scanner explicitly imports bucket helpers from [Tradier's probe](tradier/probe.py).
Component probes and replay utilities live beside their owners; they are not
routine startup steps. Run offline fixture tests before any credentialed command.
