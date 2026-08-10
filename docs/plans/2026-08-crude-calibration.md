# EIA STEO + OVX crude calibration feed

## Goal

Answer, mechanically instead of by hand, a recurring question about crude oil: has EIA's Short-Term Energy Outlook (STEO) revised its crude price/balance forecast since its last release, and how does that compare to current crude-oil implied volatility (OVX)? The motivating example was a debate transcript arguing whether WTI would settle above a fixed price threshold within six months, driven by the Iran/Strait of Hormuz conflict -- both sides repeatedly pulled numbers by hand from consecutive months' STEO PDFs and disputed the meaning of an options-implied-volatility figure. This feed is unrelated to the QQQ 0DTE option chain that is this repo's primary purpose -- it's a standalone macro data source added because the same collector/R2/viewer plumbing already exists here.

## Design

1. `crude_calibration.py` -- pure parsing/comparison logic (no I/O), mirroring `market_signals.py`'s separation of concerns: normalizes raw EIA v2 STEO API rows into a `SteoVintage` (one release's forecast curve across future months), diffs two vintages for a given forecast period (`compare_vintages`), and buckets an OVX reading into `calm`/`elevated`/`high` (`classify_ovx_regime`).
2. `collector.py` adds `OVX` to `PRICE_TICKERS`/`YF_SYMBOL_MAP` (same DXLink-guess-with-yfinance-fallback shape as `KOSPI`), and a new `eia_steo_loop()` that polls `api.eia.gov`'s v2 STEO API every 6h, entirely gated on an optional `EIA_API_KEY` env var. EIA's API only serves the *current* published forecast, not a queryable history of what a prior release forecast for the same future month -- so vintage-over-vintage comparison is built up locally, one release snapshot per poll, in a rolling `baselines/eia_steo_vintages.json` log (same shape as the existing RVOL baseline). Runs as its own daemon thread from `main()`, independent of the QQQ market session, since STEO has nothing to do with market hours.
3. `docs/shared.js` gains pure `fmtSteoDelta()`/`findRevision()` formatting helpers; `docs/index.html` gets an `OVX` price tile and a small `#steo-panel` showing the nearest forecast period's Brent/WTI/balance figures, their delta vs. the prior release, and the OVX regime badge. The panel stays hidden entirely if `macro/eia_steo.json` doesn't exist (i.e. `EIA_API_KEY` isn't configured).

## Series ID verification (2026-08)

The two price series (`BREPUUS`, `WTIPUUS`) were guessed from public EIA documentation and confirmed correct with a live authenticated call. The balance series was *not* correctly guessed on the first pass -- `PATC_WORLD` looked plausible from its name but is actually **world liquid fuels consumption** (~100+ million bbl/d), not a balance figure at all. The correct series, found via `api.eia.gov/v2/steo/facet/seriesId`, is `T3_STCHANGE_WORLD` ("Net Inventory Withdrawals, Total World Crude Oil and Other Liquids"). Its sign convention (positive = withdrawal/draw, negative = build) was cross-checked against a real number rather than assumed: the API's Apr-Jun 2026 values average ~5.08 million bbl/d, matching the debate transcript's own "the realized Q2 draw was 5.1 million barrels per day" almost exactly.

## Verification

- `python tests/verify_crude_calibration.py` -- STEO row parsing (multi-series grouping, bad-value handling), vintage comparison (shared/absent periods, partial-missing-value deltas), and OVX regime bucketing, using the debate's own real figures as fixture data where it made the test more concrete (see `test_compare_vintages_computes_deltas_for_shared_period`).
- `node tests/steo_calibration_display.test.js` -- `fmtSteoDelta()`/`findRevision()` sign/unit formatting and missing-data lookup.
- Existing `stage2_verification.py`, `verify_collector_vwap_rvol.py`, and full JS suite re-run clean (nothing in the existing price-strip/session/CSV wiring changed shape, only gained one more ticker).
- `node --check` + inline-script parse check on `docs/index.html`.
- Not verified: whether `$OVX.X` actually resolves on tastytrade/DXLink (may always fall through to the yfinance `^OVX` fallback, same open question as the existing `KOSPI` guess).

## Deferred

- A visual history chart of STEO revisions over time (mirroring the momentum log's `docs/plans/2026-07-momentum-indicator.md` follow-up) is a natural next step once `baselines/eia_steo_vintages.json` has accumulated a few months of real releases, but is out of scope here.
- No attempt was made to source a true WTI-futures-options implied volatility (the debate's own "45-day front-month quote," a different tenor/instrument than OVX's 30-day USO-options measure) -- that would need a paid data source (CME DataMine, a broker API) and is a bigger change than this PR.
