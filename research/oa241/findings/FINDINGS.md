# Gamma Exposure and Order Flow: findings (repaired, exploratory)

This report supersedes `FINDINGS_superseded_9a282b4.md`; those numbers are not accepted.
- **Repairs:** listed in `CHANGES.md`, and frozen at `166814a` before anything was rerun.
- **Repaired features:** `b655f75`.
- **Protocol:** frozen at `0e1d8cc` and preserved. Deviations are recorded in `ERRATA.md`.
- **Status of these results:** the Nov 2–16 holdout was already viewed in `9a282b4`, so every result here is **exploratory, not confirmation**.

Regressions are associations, not proof of dealer hedging. Attribution and participation scenario bounds are not confidence intervals. Intervals are 95% session-bootstrap percentile intervals (2,000 resamples of whole sessions).

```sh
python research/oa241/findings/features.py ARCHIVE SNAPSHOTS SESSIONS.json FEATURES   # G4
python research/oa241/findings/analysis.py FEATURES > research/oa241/findings/results.json
python -m unittest tests/test_oa241_features.py
```

## Verdicts (exploratory)

| Question | What the repaired estimates support | Baseline | Central effect | Interval | Sample |
|---|---|---|---|---|---|
| **Volatility** (G5a): does signed within-day dealer gamma predict lower next-30-minute variance? | **No useful out-of-sample gain detected.** The predefined useful effect (≥ 2% LOSO MSE reduction) lies outside the interval. The coefficient itself is undetermined in sign: a small damping or amplifying association is not ruled out. | M2: price controls, unsigned activity, OI concentration, session intercepts | LOSO MSE reduction −0.2%; coefficient × IQR (19–111 $bn) = −0.003 log-RV | MSE reduction −0.8% … +0.2%; coefficient × IQR −0.096 … +0.063 log-RV | 372 windows, 34 sessions |
| **Continuation/reversal** (G5b) | **Unresolved.** Predicted sign, but the interval spans both zero and the 0.02% useful effect. | sign since previous close, time of day | −0.012% of price across IQR | −0.044% … +0.015% | 408 anchors, 34 sessions; hourly non-overlapping 204: −0.011% (−0.044 … +0.014) |
| **Strike attraction** (G5c) | **Unresolved.** Point estimate has the opposite sign; interval spans the ±$0.02 useful effect. | distance-matched control strike | long-minus-short contrast +$0.063 per 5 min (attraction predicts < 0) | −$0.088 … +$0.236 | 949 events, 30 sessions; 728 anchors had no matched control |
| **Downside warning** (D3) | **Unresolved.** The interval spans both the useful benefit (≤ −0.0049 Brier) and no benefit. | price returns, realized variance, time of day, unsigned activity | Brier 0.0973 vs 0.0980 (−0.0007) | paired daily −0.0053 … +0.0029 | holdout 748 anchors, 11 sessions, 77 positives |

### G5a detail
- **Unsigned option activity** is the one addition that clearly improves on the price controls out of sample: LOSO MSE 8.2% better (+2.4% … +14.0%).
- **Opening-OI concentration** is undetermined out of sample: LOSO −3.6% (−10.1% … +5.8%). It has a negative in-sample coefficient (−0.0011 per $bn; −0.0022 … −0.0001).
- **Signed G_within** (central attribution, 100% participation): coefficient −0.000029 per $bn. Only 44% of per-session fits are negative.
- **Sensitivity:** every attribution endpoint and total-range substitution includes zero. Without the Oct 5–8 discovery days, the M3-vs-M2 LOSO reduction is −0.4% (−1.4% … +0.04%).
- **Superseded pooled-intercept fit:** M3-vs-M2 −0.8%. The frozen session-intercept fit changes the level of the holdout MSE (M2 0.92 vs 0.34 pooled) but not the conclusion. The holdout M3-vs-M2 is −0.8%.

### Exclusions (G5a, 36 of 408 anchors)
- **All 34 anchors at 10:00 are excluded.** 33 sessions have no regular-session QQQ quote at or before 09:30:00, which the first lagged one-minute return needs (`missing_forward_or_lagged_return`). The remaining session's prior dollar-volume window, (09:00, 09:30], lies before the open (`prior_volume_window_before_open`).
- **2 more anchors** fall in an Oct 8 quote gap at 11:46–11:50.

### G5b detail
Quartile means of the continuation measure M are not monotone: +0.025, −0.005, +0.056 and −0.020% (102 anchors each).

### G5c detail
| Reference strike | Δ per 5 min | Interval | n |
|---|---|---|---|
| Long-gamma | +$0.009 | −0.069 … +0.102 | 754 |
| Short-gamma | −$0.053 | −0.175 … +0.068 | 195 |
| Unsigned-OI | −$0.008 | −0.051 … +0.041 | 943 |

### D3 detail
- **Holdout operating point:** precision 16%, recall 42%, 14.9 false alerts per session, threshold 0.14 (chosen on development F1).
- **Development event rate:** 4.2% of anchors (65 positives in 23 sessions).
- **Sample needed:** the observed standardized daily effect is d ≈ 0.10. OA-225's table then needs about 785 independent evaluation sessions, against 11 available.

## What changed from 9a282b4

- **Signed exposure was wrong before.** G_within had used gamma from the wrong contracts, with seed-dependent misalignment: repaired-vs-superseded correlation was 0.30 on Sept 25 and about 0.91 on other days. The unsigned OI concentration is unchanged, because gamma and OI were permuted together.
- **Verdicts that moved:**
  - Volatility: from "null: no out-of-sample information" to "no useful out-of-sample gain detected; coefficient undetermined".
  - Downside improvement: shrank from −0.0026 to −0.0007 Brier.
- **Verdicts that held:** continuation and strike attraction stay unresolved.

## Sample and coverage

Sample, universe and limits are as in the superseded report. Key points:
- **Sample:** 34 complete Tradier sessions, Sept 25 – Nov 16, a labelled substitution for the unsupported Sept 10–16 pilot (G2). Sept 21 and 22 are excluded by the late-start rule (ERRATA E1). Oct 5–8 are flagged discovery days; Nov 2–16 is the holdout, now viewed.
- **Universe:** 16 same-day contracts at 8 strikes.
- **Inputs:** gamma and spot from ~1-minute chain snapshots; settled OI from the latest snapshot at or before 09:30.
- **Signing:** OA-180 combined, with unknown volume carried in the attribution endpoints.

## D1: October 7–8 (discovery only; unchanged by the repairs)

**October 8.**
- **The drop:** the largest 15-minute drop, −0.82%, starts at 12:42.
- **Flow:** inferred put flow was net selling before onset. Net put buying appears only at 12:47 and later (+2.2k, +14.8k, +16.2k contracts per 30 min), while call flow turned strongly negative. The flow accompanied and followed the decline. The automated pre-onset flags are swings within net put selling, not leading put buying.
- **Universe:** the captured strikes were 751–758, so the 748–750 put cluster and the 745 put sit outside the archived universe.

**October 7.** The largest 15-minute drop, −0.33%, starts at 09:33 and cannot be separated from the open.

**What the archive cannot resolve:**
- strikes outside the 8 selected;
- participant identity and opening/closing status;
- aggressor side is inferred, not observed.

## Recommendation

1. **Signed within-day exposure:** do not rely on it for volatility forecasting at this sample size. A ≥ 2% out-of-sample gain is not supported; smaller effects remain undetermined.
2. **Unsigned activity:** this is the feature with demonstrated out-of-sample value here.
3. **Continuation, strike attraction and downside warning:** these need far more sessions (downside: about 785 at the observed effect) and, for downside, a captured strike universe that includes the strikes that motivated the hypothesis. Widening the universe is a separate collection decision.
