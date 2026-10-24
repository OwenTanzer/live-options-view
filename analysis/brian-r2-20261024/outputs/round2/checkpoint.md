# Increment 2: conditioning versus forgetting

Final segment remains unscored. Validation is consumed. Research association only; no tradability or transfer claim. This increment fixes the earlier confounded seasonal specification while preserving all earlier code and results.

## Identifiable comparison

Protocol was frozen before this factorial run. All models use the same 10,682 QQQ origins across 168 validation sessions, 2025-04-03 through 2025-12-02. Target is the squared subsequent five-minute log return after the existing availability gap: a noisy conditional second-moment proxy, not observed latent variance. MSE and QLIKE are separate objectives; lower is better for each. QLIKE here is log(forecast)+target/forecast, so negative values are normal.

The unchanged state bank is r1², mean r1² over 6 bars, mean r1² over 12 bars. Feature history is 5/30/60 minutes; coefficient histories are 20 or 60 previous sessions; refits occur daily. Past-only scaling and ridge penalty 1 remain fixed. Labels in fitting are from prior completed sessions. A fixed equal-weight combination of the two forecasts has no learned aggregation memory.

Seasonal conditioning adds u and u², where u=(minutes since 09:30−195)/195. This compact basis permits asymmetry between session endpoints and smooth curvature. The choice used only discovery, not a validation basis search. Discovery's daily-normalized target profile averages 1.258, 0.849 and 0.938 over slots 60–150, 150–270 and 270–380 minutes. That retrospective descriptive profile is never an online feature. No alternate seasonal bases were searched. This is an additive state-conditioning test, not a test of every possible seasonal interaction.

| Bank | Coefficients including intercept | 20-session MSE / matched unconditional | 60-session MSE / matched unconditional | 20-session QLIKE | 60-session QLIKE |
|---|---:|---:|---:|---:|---:|
| Unconditional | 1 | 1.000000 | 1.000000 | -12.93511 | -12.62311 |
| Seasonal only | 3 | 1.005004 | 1.001419 | -12.88299 | -12.59557 |
| State | 4 | 0.886185 | 0.853809 | -13.31011 | -13.21094 |
| State plus season | 6 | 0.887987 | 0.856911 | 15.45085 | 208.87360 |

Ratios in each column have different matched-memory denominators; use absolute losses to compare memories. State MSE is 8.458234e-11 (20) versus 8.214275e-11 (60). Equal-memory state MSE is 8.315970e-11, ratio 0.873715 to the equal unconditional forecast; QLIKE -13.26589. Twenty sessions improves QLIKE relative to sixty while sixty improves point-estimate MSE. Neither is universally optimal.

Adding season raises MSE at both memories. The daily mean seasonal increment is +1.708e-13 at 20 sessions (five-day block 95% interval -3.308e-14 to 5.545e-13), and +2.949e-13 at 60 (-1.090e-14 to 8.940e-13). The seasonal-by-memory MSE interaction is -1.241e-13 (-6.030e-13 to 1.759e-13). No evidence here that this seasonal basis substitutes for faster coefficient adaptation. State20 minus state60 daily MSE is +2.420e-12 (-2.065e-13 to 7.163e-12); QLIKE difference is -0.10219 (-0.18146 to -0.02316). These are exploratory validation intervals, not multiplicity-adjusted discoveries.

## Numerical failures and episodes

The additive seasonal bank produces 1/10,682 negative raw forecasts at 20 sessions (May 9), and 3/10,682 at 60 (July 9). At floors 1e-14, 1e-12 and 1e-10, its 20-session QLIKE is approximately 2860.855, 15.451 and -13.003; its 60-session QLIKE is 22195.82, 208.874 and -10.995. Thus the apparent loss depends severely on the floor. We retain the primary 1e-12 convention and report the failure; we do not select a favorable floor. The state and unconditional banks never clip at any of those floors. Earlier increment-1 saved variance forecasts are also all above 1e-10. Equal mixtures average floored component predictions in this linear family.

A separately frozen exploratory log-link family changed both link and fitting objective to penalized QLIKE, with the same banks, memories and coverage. All 1,344 fits converged. Nevertheless, exp(linear raw-variance states) extrapolated disastrously on April 7: state60's largest forecast was 7.112e35 against target 2.031e-8, with rv6 160 training standard deviations from its mean. Its MSE ratio was 4.922e77. State20's ratio was 3.115e43. Lower QLIKE than unconditional does not rescue these pathological MSE forecasts. No caps, log-feature repair or retuning were applied. This family is stopped and excluded from final candidates. Optimizer convergence is not forecast reliability.

All validation days remain in the main scores. Five days account for 77.37% of state20's total daily MSE benefit and 87.47% of state60's; both sets are April 4, 7, 8, 9 and 10. State20 improves MSE on 135/168 days and state60 on 148/168. QLIKE benefit is less concentrated, with top-five shares about 40.1% and 40.0%. Monthly results and both favorable and adverse episodes are saved, without dropping inconvenient days. Day/block uncertainty is preferable to treating individual bars as independent evidence.

## Corrected NVDA specification and stop

NVDA predictor construction now uses only current/past observations and contiguous past intervals. Its own future target no longer determines eligibility. Evaluation requires observable QQQ targets and a backward availability join with current/past NVDA features. A truncation test confirms that removing later NVDA observations does not alter already available predictor rows. The complete data happen to produce the identical 10,682-row evaluation set, and current-spec predictions agree with historical outputs to 1e-16. The logical bug is fixed even though it did not change this sample.

The single predeclared extra-five-minute NVDA lag yields MSE/zero of 1.018163 (20 sessions), 1.008930 (60), and 1.012469 (equal). Current NVDA yields 1.019153, 1.009064 and 1.013102; matched price controls 1.022328, 1.011932 and 1.015955. The existing four-coefficient directional specification replaces QQQ r6 with NVDA r1 (or its lag), retaining QQQ r1, rv6 and cosine; it is a matched-size replacement, not a pure add-one-feature attribution. All lose to zero, and block intervals for differences from matched price controls include zero. Stop rule triggered: no further search or final-segment test for this directional specification. An extra past lag is a lag-sensitivity diagnostic, not proof of causality or an independent placebo experiment.

## Timestamp and source audit

On discovery plus validation, QQQ has 52,015 rows/670 days and NVDA 25,600/330; both have zero duplicate timestamps, zero non-300-second within-day intervals, and no missing OHLCV. QQQ has 663 sessions of 78 bars and 7 of 43; NVDA 326 and 4. UTC epoch timestamps convert to America/New_York with DST, with observed full sessions 09:30–15:55. Short sessions include a 13:00 stamp, so early-close/bar-boundary convention is not fully resolved. No overnight feature windows are permitted. The bar-start interpretation and 60-second release allowance remain assumptions, not verified historical first-available timestamps. The source writer/version and revision history are insufficient to certify live availability. Original source hashes and price coverage remain in increment 1; no final outcomes were constructed here.

Twelve bounded public R2 object reads were made, using existing access and certificate verification. No authentication bypass or collector/source changes. Bucket qqq-options-chain-data, public host pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev. Detailed keys, decoded-body SHA256, schemas and missingness are in options-discovery.json and source-followup.json.

* **Intraday option snapshots:** six actual CSVs, two each on September 10, 11 and 14, first at/after 09:30 and 09:35 Eastern. All six match the historical source index's hashes. Each has 134 rows (804 total observations), 36 columns, no duplicate contracts/crossed quotes/expired rows. Each has 11–15 zero bids; median relative spreads range 2.94%–9.47%. The historical index lists 2,986 files over five sessions, but only these six were retrieved this increment: the index is not proof of complete usable coverage. Fields include options prices/Greeks/IV, unsigned Volume and nonnegative VolDelta, and cross-asset marks. VolDelta is not aggressor-signed flow. Only TradeDate is in-row; snapshot filename time does not establish per-quote event time, ingestion time or first availability. Bid/ask ages and synchronized cross-asset availability cannot be reconstructed from these CSVs. Thus no defensible intraday executable quote backtest or causal lead-lag join is claimed.
* **Weekly chains:** actual files on September 8 at 09:30 and September 11 at 15:30 for September 11 expiry, 96 and 77 rows, 18 columns. Volume is missing in 77/96 and 5/77 rows. The manifest lists seven weekly expirations; this is not seven verified panels. Current collector code omits stale/unquoted contracts, creating selection/coverage changes, and does not archive quote ages in the CSV. Current code's freshness gate does not prove historical deployment or original per-contract freshness. Contract survivorship and quote timing remain unresolved.
* **Prints/quotes:** schema-v2 Tradier capability sample, September 3 only. Part000 contains 13,270 timesales, 7,959 top-of-book quotes and one heartbeat for QQQ plus 16 same-day-expiry options, strikes 710–717, selected near spot 713.46. Receipt range is 13:41:55.459–13:44:54.968 UTC (09:41–09:44 Eastern); provider trade timestamps span 13:41:55.423–13:44:54.835. Median/p95/max receipt minus provider lag is 89/226/859ms; no negative lag, duplicate symbol-sequence pair, cancel or correction in this part. Quote-age field is missing for 25 prints; median/p95/max available age is 871/1488.8/2153ms. Source code uses the newer side's provider timestamp in the preceding received quote; this is not the older-side age of both bid and ask. Quote records retain separate biddate/askdate, enabling a stricter audit in future work. No aggressor-side ground truth, multi-leg grouping, participant identity or book depth. Whole-run summary reports 30 minutes, 138,485 prints, 79,846 quotes and one cancel; those counts are summary evidence, not independently inspected full-run rows. The preserved events are normalized/enriched, not byte-exact wire data.

The event part arrived HTTP-decoded despite its .gz suffix. Its saved SHA256 is explicitly a decoded-body hash, distinct from the manifest's compressed-object hash; compressed-byte identity was not independently verified. No raw proprietary bodies are committed.

**Actual joint coverage:** the tested QQQ/NVDA price panel ends August 5, before all newly sampled September option/print data. Their intersection with this tested panel is zero. Within the three sampled snapshot days, marks and options coexist in rows but their independent availability is unknown. The one-day print stream has QQQ and option quotes jointly but no sampled NVDA and no multi-session inference support. This source audit does not manufacture a multi-session flow edge.

## Frozen return point for increment 3

Keep only linear state20, state60 and their fixed equal mixture, with unconditional20, unconditional60 and equal as matched controls. No seasonal or positive-link candidate, and no NVDA direction candidate advances. The final segment remains 2025-12-03–2026-08-05 (168 sessions), opened once only after Chej's next direction. Daily prequential fits may use earlier final-session labels after maturity, with fixed model choices and no retuning. Compare MSE and QLIKE separately, matched coverage, raw clipping counts and fixed floor sensitivity, five-day block intervals, predefined monthly/episode summaries. Report both absolute losses and matched-baseline ratios. No declaration of universally optimal memory.

Before opening final outcomes, verify the frozen candidate list and availability sensitivity specification in round3-plan.json. Any change is a logged protocol amendment. If historical bar availability remains unverified, conclusions remain conditional on the documented timing assumption. Options/flow work should next establish multi-session receipt-time coverage and both-side quote ages, not fit a model to the 30-minute capability sample.

Code: work/round2. Numerical outputs: outputs/round2. Reproduction: reproduce.py reruns all three empirical scripts in a fresh private directory and compares every generated CSV, excluding regenerated protocol timestamps. All 13 tables matched exactly after LF normalization. Python 3.14.4, NumPy 2.4.5, pandas 3.0.6; SciPy version is recorded in positive-protocol.json, seed 20261024. Original increment-1 artifacts are preserved. This remains [draft PR 144](https://github.com/OwenTanzer/live-options-view/pull/144).

From analysis/brian-r2-20261024, first follow the root README's download and increment-1 reproduction steps to recreate ignored source objects plus validation-predictions.csv and cross-predictions.csv. Keep the committed frozen protocols; do not rerun freeze scripts as if making a new preregistration. Then run:

```text
python work/round2/factorial.py
python work/round2/nvda_check.py
python work/round2/positive_link.py
python work/round2/reproduce.py
```

The positive-link script writes its protocol timestamp on execution; preserve the committed original freeze as historical evidence. Source audit scripts additionally expect collector.py, scripts/moo144_tradier_collector.py, moo171_snapshot_analysis/r2_source.py and moo171_snapshot_analysis/out/source_manifest.json fetched from existing repository revision 26332bb0eba41618f4a8b941e74ff202c66b0fe0 into work/round2/collector.txt, flow-collector.txt, r2-source.txt and source-manifest.txt respectively. Their hashes are in diagnostics.json. Run options_discovery.py then source_followup.py to fetch the declared source keys, keeping bodies private; source_followup handles HTTP-decoded .gz content and caches reads. The current root manifest is mutable, so use the committed exact-key plans to reproduce the historical selection if it changes. Do not interpret changed source bytes as a reproduced historical result.

—Brian 🦡
