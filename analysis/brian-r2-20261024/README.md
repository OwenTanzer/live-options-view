# Increment 1: data access and first empirical tests

Brian — October 24, 2026. Research only; no trading, source writes, collector changes, infrastructure creation or deployment.

## Findings

Three families were investigated: own-price continuation/reversal, variance conditioning versus coefficient memory, and NVDA-to-QQQ lead-lag. Two option schemas were inspected for feasibility; no option prediction or signed-flow test was run.

All directional forecasts lost to a zero-return forecast. The six original own-price forecasts had MSE ratios 1.0121–1.0470 and sign hit rates 48.82–49.14%. Adding NVDA in a separately declared matched comparison reduced MSE by only 0.28–0.31% relative to price/time-of-day controls; every paired daily bootstrap interval includes zero. Those models still lost to zero return, with MSE ratios 1.0091–1.0192. This is not evidence for a tradable lead-lag edge.

Variance forecasts beat the prior-60-session unconditional mean in this validation period. The lowest MSE ratio was 0.8351 for the seasonal 60-session fit; its matched nonseasonal counterpart was 0.8538. But the seasonal-versus-nonseasonal difference has a five-day block-bootstrap interval spanning zero. QLIKE instead favors nonseasonal conditioning and the shorter 20-session coefficient window. About 89.5% of the seasonal 60-session model's cumulative daily MSE benefit comes from its five most beneficial days. The benefit remains positive after excluding those days or April 2025, but is much smaller. The plot and monthly tables make that concentration visible.

These results distinguish score-dependent historical performance from an explanation of structural change. They do not identify a unique state/coefficient mechanism or warrant future transfer. The time-of-day feature replaces a longer variance feature; this is an equal-dimension comparison, not a pure additive causal ablation. A seasonal-only benchmark remains an important next control.

## Verified access and coverage

Read-only access works through the existing public R2 host discovered by OptionsView marketMCP, using Python Requests with certificate verification enabled. All four current QQQ source files are byte-identical to the September 16 preserved extract used to freeze the experiment. Authenticated broad listing remains unavailable: the old Cloudflare OAuth token returns 401 with Requests, and the old Railway lookup reports unauthorized/login required. Earlier urllib probes returned DNS, 429, 403 or expired-certificate errors. Those are route-specific failures, not evidence that the bucket is absent. No secrets were printed.

Bucket: `qqq-options-chain-data`. `download-manifest.json` records exact keys, SHA-256, bytes, retrieval time, ETag and Last-Modified. Last-Modified describes archive upload, not original event availability. The source objects remain unchanged.

| Source | Materialized coverage | Meaning and timing limits |
|---|---|---|
| `history/QQQ/5/{2023,2024,2025,2026}.json` | 65,084 bars, 838 sessions, April 3, 2023–August 5, 2026 | Unversioned `t,o,h,l,c,v`; epoch-second timestamps, interpreted in America/New_York. Five-minute OHLCV, not individual prints or order flow. No original ingest/release/correction timestamps. |
| `history/NVDA/5/{2024,2025,2026}.json` | 38,825 bars, 500 sessions, August 9, 2024–August 7, 2026 | Same schema and timing limitations. Intraday returns avoid overnight split jumps, but historical adjustment/vintage policy remains unverified. |
| Actual QQQ/NVDA intersection | 38,669 exact common timestamps across 498 sessions, August 9, 2024–August 5, 2026 | This is the intersection, not the union. Both sources cover all 10,682 scored validation origins. |
| `history/QQQ/options_0dte/manifest.json` | 334 listed dates, April 1, 2025–August 4, 2026 | Manifest overlap: 2 discovery, 166 validation and 166 final dates. This is date-list overlap, not verified usable option-feature coverage. Only April 1 body inspected. |
| `history/QQQ/options_0dte/2025-04-01.json` | 250 same-day-expiry contracts | Bid/ask present, but IV and all five Greek fields are missing in all 250 rows. No quote observation/ingest timestamps. EOD expired chains cannot be treated as information available earlier that day. No skew/term backtest claimed. |
| `intraday/20261023/snapshot_161411543301.csv` | One QQQ snapshot, 134 unique 0DTE contracts | Collection time 16:14:11.543301 ET; archive Last-Modified 20:14:12 UTC. No crossed quotes; 52 zero bids; median relative spread 12.17%; one missing Last. IV/Greeks present, but individual quote ages unavailable. This post-16:00 sample is schema/quality evidence only. |
| `intraday/spy/20261023/snapshot_161413479278.csv` | One SPY snapshot, 132 unique 0DTE contracts | Collection time 16:14:13.479278 ET; Last-Modified 20:14:14 UTC. No crossed quotes; 56 zero bids; median relative spread 7.44%. Quote ages unavailable. No historical QQQ/SPY panel established. |
| `intraday/prices.json` | One current published strip, 18 labels | Asynchronous cross-assets; source receipt time differs from provider observation. CSV copies omit per-asset times. A common row does not establish simultaneous or fresh prices. |

QQQ and NVDA have no duplicate timestamps or missing OHLCV fields. QQQ has 830 sessions with 78 bars and eight with 43 bars; no off-session rows under the nominal 09:30–16:00 filter. The exact early-close/closing-print convention is unresolved. Removing the two short validation sessions leaves 10,624 origins/166 days and essentially unchanged rankings (best variance ratio 0.8352). Exchange-calendar completeness, corrected-data vintages and corporate-action policy are not fully verified.

Individual trade prints, trade-side classifications and book depth were not located in the materialized panel. `VolDelta` is a change in cumulative option volume, not signed flow. Greeks/exposures and snapshot-derived VWAP are not order-book measurements. No full order-flow claim is made. Quote staleness cannot be measured from the archived CSVs; zero missing fields do not establish freshness. The latest QQQ snapshot even has different underlying and price-strip QQQ values, reinforcing the need for separate source timestamps.

The September inventory listed 74 intraday date directories from June 23–September 16, 2026 and 599 objects on September 16. This is historical inventory evidence only, not a fresh exhaustive listing. Current source catalog confirms QQQ/SPY snapshots and cross-market strip, but provides no general historical index. Weekly-chain/term coverage is still unverified.

## Frozen design and search ledger

Read Musef's completed research, especially How to use, fifth-pass conditioning-versus-forgetting experiment and final intraday audit. Applied its separation of feature/state, coefficient and aggregation memories rather than implementing adaptive machinery first.

- Discovery: 502 sessions, April 3, 2023–April 2, 2025.
- Validation: 168 sessions, April 3–December 2, 2025. Now consumed development evidence.
- Final: 168 sessions, December 3, 2025–August 5, 2026. No final outcomes constructed or scored. Files were loaded for coverage/hash checks, so the precise claim is unscored final predictions, not that the files were never opened.

Frozen before forecast results: own-price direction and variance families, 20/60 prior-session coefficient windows, daily refits, fixed ridge penalty 1, past-only feature standardization, and equal forecast mixtures. Four regressors including intercept in each initial bank. Direction compares r1/r6/rv6 against r1/r6/r1*sqrt(rv6); variance compares r1-squared/rv6/rv12 against r1-squared/rv6/cos(time-of-day). State spans are five, thirty and sixty minutes, independent of training history. Aggregation uses fixed equal weights, with no learned loss memory.

Revision 1 made feature dimensions and latency gap explicit before any scores were computed. After these results, a separately frozen cross-asset family compared five-coefficient models with identical own return, variance and time-of-day controls, replacing own 30-minute return with NVDA's five-minute return. No asset sweep occurred. Four fitted streams and two fixed mixtures were added. Overall: 12 fitted streams, six mixtures, three simple baselines; no hyperparameter search beyond the declared memories. Episode, paired-bootstrap and full-session audits reuse validation and are explicitly exploratory.

At source bar index i, assumed bar-start timestamp t: bar i closes t+5 minutes and is available at t+6. Forecast target is log(c[i+2]/c[i+1]), covering t+10 to t+15, maturing at t+16 under the same release-delay assumption. There is a four-minute gap from origin to target start. No current-day labels enter training. Consecutive-bar checks require an uninterrupted prior hour and two future bars in the same session. The QQQ/NVDA join is backward as-of on t+360 seconds with zero tolerance; unmatched rows are dropped. No nearest-future joins or retrospective states.

Adjacent five-minute targets do not overlap in return increments but share boundary prices; features/training overlap heavily. Uncertainty is therefore reported by day, with a five-day circular block sensitivity for the memory/conditioning contrasts. Neither interval accounts fully for search, changing dependence or all measurement error. One squared five-minute return is a noisy variance proxy, not latent integrated variance. No executable quote/fill/cost model supports a tradability claim.

## Reproduction and verification

The isolated branch is `brian/r2-backtest-20261024`, based on existing Options View revision `c96b2a6a0f8a266c09648799c225a347edd8b959`. Original checkout and collectors are preserved. The research folder contains scripts, aggregate tables, manifests, protocol, environment and plot; raw data and per-origin forecasts remain ignored in `work/private/`.

Run from the research folder with Python, NumPy, pandas, Requests and matplotlib installed:

```text
python work/download_panel.py
python work/freeze.py
python work/test_baselines.py
python work/cross_asset.py
python work/options_feasibility.py
python work/episode_audit.py
python work/paired_audit.py
python work/verify_results.py
```

Rerunning freeze recreates the same date partition from source timestamps; retained published protocol records the original freeze. Seed is 20261024. `environment.json` records exact local runtime/package versions. No random model fitting; randomness is only in uncertainty resampling. The verifier independently recomputes target returns, matches current versus preserved QQQ hashes, checks identical cross/price validation origins, confirms no final prediction rows, and reports full-session sensitivity. Plot visually inspected. A fresh-directory run of the packaged baseline and cross-asset scripts reproduced all four score/daily-loss tables exactly after canonical LF normalization; `reproduction.json` records their hashes. Current bytes reproduce the preserved source identity; future mutable objects may differ, so verify hashes before accepting a rerun.

Publication: [draft PR 144](https://github.com/OwenTanzer/live-options-view/pull/144). No merge. The Notion event and Brian's Room retain the handoff and findings.

## Next discriminating work

1. Add a past-only seasonal-only variance benchmark and a balanced-budget additive state comparison; test a single frozen candidate on the final period only after fixing the choice and loss. Preserve the consumed-validation label.
2. Obtain actual intraday archive indices and repeated session coverage outside the frozen final period. Require quote/receipt timestamps or explicitly label the result an availability-assumption sensitivity. Compare near-ATM options and source ablations on matched origins; do not infer skew or term evidence from the null-IV EOD sample.
3. Check the small NVDA effect under a predeclared extra-lag/placebo and five-day block uncertainty before spending final data. If it continues to miss zero return, abandon this directional specification.

—Brian 🦡


Increment 2: [factorial conditioning/memory audit, failed tests and frozen round-3 plan](outputs/round2/checkpoint.md). Final remains unscored. See outputs/round2/reproduction.json for 13 exactly reproduced tables.
