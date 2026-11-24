# Market data

These services acquire or derive data for readers elsewhere in the repository.
They have separate lifecycles; a browser request does not start them.

| Component | Output and role | Entry from repository root |
|---|---|---|
| [Live market](live_market/README.md) | QQQ/SPY snapshots, price strip, health and in-memory live quotes | `python -m market_data.live_market` |
| [Tradier archive](tradier/OPERATIONS.md) | Daily time-and-sales archives; no identified in-repository consumer | `python -u -m market_data.tradier.launcher` |
| [Options scanner](options_scanner/README.md) | Session sweeps, contract rankings and active snapshots | `python -u -m market_data.options_scanner run` |
| [Candle-cache backend](candle_cache_backend/README.md) | Local reader/writer for the R2 candle layout (`nasdaq/candles/`), One Clock envelope stamping and as-of selection; seeded AAPL sample | `python -m market_data.candle_cache_backend seed --out <dir>` |

All use the [shared Python image](../deployment/python/README.md). Schedules,
activation and provider configuration are documented with each service.
[diagnostics.py](diagnostics.py) supplies shared bounded error redaction.
The scanner explicitly imports bucket helpers from [Tradier's probe](tradier/probe.py).
Component probes and replay utilities live beside their owners; they are not
routine startup steps. Run offline fixture tests before any credentialed command.
