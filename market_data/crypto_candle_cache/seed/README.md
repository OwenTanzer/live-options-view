# BTC seed — a small, real sample, not the backfill

BTC only, the three bases, 378 bars fetched live from Coinbase
Exchange public candles (no key) on **2026-11-24T00:32:44Z** (6
requests, `fetch_history` paging, via the package's own
`coinbase.py`):

| File | Bars | Span (UTC) | Window fetched |
|---|---|---|---|
| `1m.json` | 120 | 2026-11-23T22:32 → 2026-11-24T00:31 | last 3 hours, last 120 completed bars |
| `1h.json` | 168 | 2026-11-17T00:00 → 2026-11-23T23:00 | last 8 days, last 168 completed bars |
| `1d.json` | 90 | 2026-08-26 → 2026-11-23 | last 100 days, last 90 completed bars |

Last bars: 1h close 61,189.97 (23:00 bar), 1d close 61,189.97 —
both real Coinbase closes, not fixtures. The still-forming latest
bar of each base was dropped, so every seeded bar is a completed
bar: gap-free over its own span (asserted by
`tests/test_crypto_candle_cache.py::SeedTest`), each carrying
`observed_at` 2026-11-24T00:32:44Z and `origin: "recorded"`.

Use: `python -m market_data.crypto_candle_cache seed --out <dir>`
copies these four files into R2 layout (`crypto/candles/BTC/`).
The full backfill data lives in the parallel Crypto Backend job
(`~/workspace/crypto-backend/`) and its Notion page "Crypto
Backend" — nothing else is checked in here.
