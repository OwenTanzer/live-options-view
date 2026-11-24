# Prediction seed (BTC only)

`predictions.json` is the seeded sample for
`market_data/crypto_predictions/`: BTC only, seven hourly
predictions computed from the real checked-in BTC 1h seed bars
(`market_data/crypto_candle_cache/seed/BTC/1h.json`, 168 bars,
fetched live from Coinbase public candles 2026-11-24T00:32:44Z).

Each prediction is computed causally — only from bars that had
closed by its target hour's end — for the target hours starting
18:00, 19:00, 20:00, 21:00, 22:00, 23:00 on 2026-11-23 and
00:00 on 2026-11-24 (UTC). At the seed generation time
(2026-11-24T00:32:44Z) the 18:00–23:00 target hours had passed,
so those six predictions are retained as historical evidence:
revision-1 value rows only, never retracted. The 00:00
prediction is live.

All rows carry `availableAt` 2026-11-24T00:32:44Z — the actual
seed generation time, not a simulated hourly run time. No
available time is invented: a prediction generated at 00:32
becomes selectable at 00:32, whatever its target hour.

Regenerate byte-identically with:

```sh
python -m market_data.crypto_predictions seed --out <dir>
```

The output file lands at `<dir>/crypto/predictions/BTC.json`;
copy it here only if the checked-in file is being refreshed.
