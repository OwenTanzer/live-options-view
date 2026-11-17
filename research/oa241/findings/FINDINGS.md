# Gamma Exposure and Order Flow: findings

Reproducible account of whether estimated exposure adds information about subsequent volatility, continuation or reversal, and strike movement, and of whether opening open interest plus flow gives advance downside evidence. The definitions were frozen before any outcome was computed:

| Freeze | Commit | Time |
|---|---|---|
| Protocol | `0e1d8cc` | 18:22 PT |
| Feature version and analysis code | `6026ca9` | 18:44 PT |

The results were computed after both. Regressions are associations, not proof of dealer hedging. Scenario bounds are not confidence intervals.

```sh
python research/oa241/findings/features.py ARCHIVE SNAPSHOTS SESSIONS.json FEATURES   # G4
python research/oa241/findings/analysis.py FEATURES > research/oa241/findings/results.json
```

`results.json` has every number below.

## Verdicts

| Question | Verdict | Baseline | Effect (central) | 95% session-bootstrap interval | Sample |
|---|---|---|---|---|---|
| **Volatility** (G5a): does signed within-day dealer gamma predict lower next-30-min variance? | **Null**: no out-of-sample information | M2 (controls, unsigned activity, OI concentration) | −0.002 log-RV across the interquartile range (IQR) of G (21–100 $bn); LOSO MSE **0.6% worse** (useful = ≥ 2% better); holdout unchanged (0.0%) | coefficient × IQR: −0.079 … +0.052 log-RV | 372 windows, 34 sessions |
| **Continuation/reversal** (G5b): does higher gamma predict less continuation? | **Unresolved**: predicted sign, but the interval spans both zero and the useful effect | sign since previous close, tod | −0.011% of price across the IQR (useful ≥ 0.02%) | −0.048% … +0.007% | 408 anchors / 34 sessions; hourly non-overlapping 204: −0.013% (−0.050 … +0.007) |
| **Strike attraction** (G5c): do long-gamma strikes attract price relative to distance-matched strikes? | **Unresolved**: opposite-sign point estimate, interval spans the useful effect | distance-matched control strike | long-minus-short contrast **+$0.060** per 5 min (attraction predicts < 0; useful ≥ $0.02) | −$0.061 … +$0.200 | 956 events / 30 sessions (721 had no matched control) |
| **Downside warning** (D3): do opening OI plus flow improve on price and unsigned activity? | **Unresolved**: holdout interval spans both useful benefit and no benefit | price returns, RV, tod, unsigned activity | Brier 0.0954 vs 0.0980 (−0.0026; useful ≤ −0.0049) | paired daily −0.0107 … +0.0038 | holdout 748 anchors, 11 sessions, 77 positives |

**G5a in detail:**
- The signed component G_within (passive dealer, central attribution, 100%) has coefficient −0.000020 per $bn, with interval −0.0010 … +0.0007.
- Only 44% of per-session coefficients are negative.
- Unsigned activity improves on the price controls out of sample (LOSO MSE 4.3% better).
- Opening-OI concentration has a negative in-sample coefficient whose interval excludes zero (−0.0011 per $bn; −0.0022 … −0.000001). It still makes LOSO MSE 0.7% worse, so it is not an out-of-sample gain.
- Results are the same without the Oct 5–8 discovery days, and every attribution or total-range endpoint substitution also includes zero.

**G5b in detail:** quartile means of the continuation measure M are not monotone: +0.035, −0.007, +0.063 and −0.034% (102 anchors each).

**G5c in detail:**
- Long-gamma reference strikes: Δ +$0.017 (−0.060 … +0.090), n = 725.
- Short-gamma reference strikes: Δ −$0.043 (−0.152 … +0.049), n = 231.
- Unsigned-OI reference strikes: Δ +$0.022 (−0.039 … +0.077).
- Without discovery days, the contrast is +$0.080 (−0.048 … +0.234).

**D3 in detail:**
- **Alert threshold:** 0.13, chosen on development F1.
- **Holdout performance at that threshold:** precision 16%, recall 34%, 12.3 false alerts per session.
- **Event rate:** 4.2% of anchors in development (65 positives in 23 sessions).
- **Power:** the observed standardized daily effect is d ≈ 0.20. OA-225's table needs about 197 independent evaluation sessions at that d, versus 11 available, and about 295–589 under its illustrative serial dependence.

## Sample and coverage

**Signed sample.** The staged OA-178/179 sample (Sept 10, 11, 14, 15 and 16) has no trade prints or preceding quotes (G2), so the pilot itself is unresolved. As the frozen substitution, the signed tests use the 34 Tradier sessions with status `complete`, no partial reasons, and zero gap and late-start seconds:
- **Excluded by that rule:** Sept 21 and Sept 22 (late start of 2.3 s and 3.2 s), and Sept 24, Sept 28, Sept 30 and Oct 29 (partial). Sept 3 has no summary.
- **Discovery days:** Oct 5–8 stay in as development, flagged.
- **Untouched holdout:** Nov 2–16.

**Universe.** 16 same-day contracts at 8 strikes selected at 09:29, about ±4 points around spot. Exposure is only for that universe, with no non-0DTE expiries and no full-chain concentration.

**Gamma, spot and OI.** Gamma and spot come from ~1-minute chain snapshots (≤ 120 s old at use). Settled OI is the latest snapshot at or before 09:30. Its clearing date is not in the source.

**Rejected anchors.** 36 G5a windows were dropped for a missing one-minute price. No realized variance was zero.

**Signing.** OA-180 combined: unknown volume is carried in the attribution endpoints, and agreement between methods is not accuracy. Opening inventory is unknown, so the conditional total range assumes zero settlement-to-open change.

## D1: October 7–8 case reconstruction (discovery only)

**October 8.**
- **The drop:** the largest 15-minute drop, −0.82%, starts at 12:42. Spot is 753.99, then 749.84 at 12:52 and 747.80 at 12:57.
- **Before the drop:** in the 30 minutes before onset, 30-minute signed put flow was *negative* (net inferred put selling, about −2k to −10k contracts). Unsigned activity was already high (about 330–515k contracts per 30 min).
- **During the drop:** net inferred put buying appears only *after* onset: +2.2k at 12:47, +14.8k at 12:52, +16.2k at 12:57. Call flow turned strongly negative over the same window.
- **Verdict:** the flow accompanied and followed the decline; nothing preceded it by ≥ 5 minutes in the predicted direction. The automated flag listed pre-onset put-flow changes, but they were swings within net selling, not put buying.
- **Universe:** the captured strikes were 751–758, so the 748–750 put cluster from the OA-225 preliminary review (34,418 OI) and the 745 put were **outside the archived universe** and cannot be reconstructed.

**October 7.** The largest 15-minute drop, −0.33%, starts at 09:33 in the opening minutes, with flow building from zero. It cannot separate opening state from concurrent flow.

**What the archive cannot resolve:**
- strikes outside the 8 selected;
- participant identity and opening/closing status;
- aggressor side is inferred, not observed;
- two selected days cannot establish predictive validity.

## Recommendation

1. **Volatility.** Treat the signed within-day exposure as not adding volatility information at this sample size. Unsigned activity is the only feature here with out-of-sample value.
2. **Continuation and strike attraction.** These need more sessions or a wider captured strike universe before a decision. The current intervals are about 2–5 times wider than the useful effects.
3. **Downside warning.** A confirmatory test needs about 200 evaluation sessions at the observed effect size. Separately, the collector's ±4-strike universe excludes the strikes that motivated the hypothesis. Widening the captured universe is a precondition, and it is a separate collection decision.
