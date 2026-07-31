# VWAP + RVOL plan

Issue: #37

## Goal

Add canonical underlying-level VWAP and RVOL data to the live market pipeline, then display it beside the options-chain header and beneath the QQQ price-strip tile, with the same values available to `momentum_qqq` as optional confirmation gates.

## Design

1. Collector computes session VWAP as a spot-price-at-snapshot × volume-delta-since-last-snapshot approximation (QQQ's own cumulative session volume was already tracked in feed state but unused), reset at session start and recoverable after a mid-session restart via a small per-day R2 object.
2. Collector maintains a self-bootstrapping RVOL baseline: 5-minute time-of-day buckets, a 20-trading-day rolling window, folded into a cross-day R2 object once per session at session end so a mid-session crash can never corrupt it with a partial day. A bucket with fewer than 5 days of samples reports `insufficient_history` rather than a fabricated multiple.
3. Both are published as one new `underlying_market` object on the existing `intraday/latest.json` payload -- the same object the browser and the Python bot already read directly from R2 with no Worker mediation, so no new endpoint is needed for "one canonical record."
4. `crassus/crassus/market.py` gains an `UnderlyingMarket` dataclass parsed from that block; `crassus/crassus/vwap_rvol.py` is a new, separate pure-evaluation module (mirrors `momentum.py`'s "math separate from decision logic" split) that never fabricates a verdict when the data isn't yet trustworthy.
5. `momentum_qqq` gains two optional params, both off by default: `vwap_confirmation_required` and `rvol_floor`. Either can only veto an already-supported direction, never manufacture one. When the gate's own data isn't trustworthy yet (RVOL still bootstrapping, or a stale/missing snapshot), a held position is retained rather than closed -- the same "absence of evidence isn't evidence against" treatment already established for `momentum_qqq`'s own stale-snapshot handling.
6. `docs/shared.js` gains a pure `formatVwapRvol()` used by both the options-chain header and the QQQ tile's new second line, reusing the existing ticker `live`/`stale`/`fallback`/`closed` state vocabulary rather than inventing a new one.

### Correctness fixes from review

- **Spot/timestamp pairing**: `underlying`'s resolution (bid/ask mid → last → the `_last_spot` fallback) is now a single pure function, `_resolve_underlying_spot`, that returns the price and its *own* observation timestamp together, branch for branch -- previously `spot_ts` was derived independently from the DXLink feed state even when the price itself had fallen back to a non-DXLink source (e.g. yfinance), so a stale/unrelated timestamp could get paired with a fallback price. The yfinance fallback path (which has no trustworthy provider timestamp) now correctly carries `spot_ts=None`, so it always classifies as not-`"live"`.
- **Provider-event timestamp ordering**: `accumulate_vwap` now takes the provider's own observation timestamp (not the collector's wall-clock read time) and rejects any tick whose timestamp isn't strictly newer than the last one actually folded in, leaving the accumulator untouched rather than risking a delta computed against an out-of-order or duplicate delivery.
- **Partial-session VWAP**: `underlying_market` now carries `vwap_session_started_at` and `vwap_partial_session` -- `True` when the accumulator started meaningfully after the session's official open (a restart without full recovery, or a delayed process start), so a VWAP that doesn't cover the whole session-to-date is explicitly flagged rather than looking identical to a full one.
- **Stale confirmation data treated as actionable**: `vwap_rvol.evaluate_gate` now checks `underlying_market.freshness` before `rvol_status` -- RVOL's own bucket status can read `"ok"` while the feed powering the *current* spot/VWAP comparison has gone stale, and previously the gate would evaluate and act on that stale comparison anyway. A new `"stale_source"` status makes this explicit, and `momentum_qqq`'s audit metadata now includes `vwap_gate_freshness` so every decision's trust in the data is visible in the trail, not just its status.

## Verification

- `python scripts/verify_vwap_rvol.py` (crassus) -- data-contract parsing, gate evaluation, and `momentum_qqq` integration, including a regression check that omitting both new params changes nothing.
- `python tests/verify_collector_vwap_rvol.py` (root) -- VWAP accumulation/session-reset/restart-recovery and RVOL baseline load/finalize math against a fake S3, alongside the existing (previously CI-unwired) `tests/stage2_verification.py`.
- Run all web suites and JavaScript syntax checks, including the new `tests/vwap_rvol_display.test.js`.
- Manual/visual: collector.py talks to a live tastytrade/DXLink feed with no credentials available in development -- the collector-side computation is unit-tested via the extracted pure functions in `market_signals.py` but cannot be integration-tested end-to-end before deploy.

## Deferred

True tick-level VWAP (weighting by each trade's own price instead of spot-at-snapshot) is deferred -- it needs each DXLink `Trade` event's price captured alongside `dayVolume`, a bigger change to the feed-ingestion layer. RVOL's baseline is mechanically correct on merge day but isn't materially trustworthy until roughly a trading week has actually elapsed in production; that's a deployment-time reality the code reports honestly (`insufficient_history`), not something a first PR can shortcut. Backfilling the baseline from a historical-data vendor, per-strike VWAP/RVOL, and propagating VWAP/RVOL onto the 10s `prices.json` cadence are all explicitly out of scope.
