# Stocks reader

The Stocks button uses the shared tab registry. On-demand Yahoo fixture bars
use the same `symbol|interval` cache key for fetching and rendering; legacy
nested artifact candles remain readable.

Coarser intraday candles use elapsed-time buckets anchored at 09:30 in
America/New_York. Missing minutes do not move later bars into earlier buckets.
The time-zone conversion follows daylight saving time, and bars outside the
regular session are excluded from aggregation. A trailing partial bucket is
retained without inventing missing minute bars.

Offline checks: `node tests/nasdaq_panel.test.js`, `node tests/boot.test.js`,
`node tests/tab_switching.test.js`,
`node --test market_mcp/nasdaq_snapshot.test.js market_mcp/consumer.test.js`, and
`python -m pytest tests/test_nasdaq_collector.py -q`.
The dedicated Nasdaq workflow runs these plus News fixture checks.
