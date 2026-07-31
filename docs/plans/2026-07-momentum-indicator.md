# Time-series momentum live indicator + log

## Goal

Show a live time-series momentum indicator beside the options-chain header and on the QQQ price-strip tile (mirroring the VWAP/RVOL indicators added in #37/#38), and persist a public historical log of it -- there is currently no way to observe the momentum signal outside of the private, gitignored `momentum_qqq` trading bot's own local decision ledger.

## Design

1. `market_signals.py` gains `compute_time_series_momentum` -- a deliberate re-implementation of `crassus/crassus/momentum.py`'s `compute_momentum` (same status vocabulary: `no_data`/`warming_up`/`stale_anchor`/`ok`, same anchor-selection logic), not a shared import across the collector/crassus deployable boundary. This is a **display/log-only reference signal**, not a change to what any `momentum_qqq` account actually trades on -- each account still runs its own independently-configured `PriceHistoryTracker`. The two are the same kind of computation, not guaranteed to be bit-identical.
2. The collector accumulates a rolling window of recent spot observations (reusing the already-correctly-paired spot price/timestamp from `_resolve_underlying_spot`, see #38) and computes a momentum reading every snapshot cycle. Unlike VWAP, this needs no restart-recovery persistence -- a bounded real-time window self-heals within one lookback period after any restart.
3. The reading is published as a new `momentum` object on `underlying_market` (alongside `vwap`/`rvol`), and appended as one line to a rolling public per-day NDJSON log in R2 (`intraday/{date}/momentum_log.jsonl`) -- a queryable historical record independent of the private bot ledger.
4. `crassus/crassus/market.py`'s `UnderlyingMarket` gains `momentum_*` fields for parity with `vwap_*`/`rvol_*`, parsed the same way.
5. `docs/shared.js` gains a pure `formatMomentum()`, displayed beside VWAP/RVOL in the header and on the QQQ tile, reusing the same live/stale/fallback/closed state vocabulary.

## Verification

- `python tests/verify_collector_vwap_rvol.py` (root) -- momentum math (no_data/warming_up/ok direction up-down-flat/stale_anchor) and the collector wiring (payload shape, `momentum_log.jsonl` append), alongside existing VWAP/RVOL coverage.
- `python crassus/scripts/verify_vwap_rvol.py` -- `UnderlyingMarket.from_payload` parses the new nested `momentum` fields.
- Run all web suites and JavaScript syntax checks, including the new `tests/momentum_display.test.js`.
- Manual/visual: same caveat as #37/#38 -- collector.py talks to a live feed with no credentials available in development, so this is unit-tested via `market_signals.py`'s pure functions but not integration-tested end-to-end before deploy.

## Deferred

A visual momentum history chart in the existing History tab (`docs/index.html`'s `#history-panel`) is a natural follow-up now that `momentum_log.jsonl` exists, but building that chart UI is out of scope here. Unifying this display signal with `momentum_qqq`'s actual live trading computation (so the two are guaranteed identical) is a bigger, riskier change to already-merged trading logic and is intentionally not part of this PR.
