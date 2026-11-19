# Strike approach/retreat test: frozen spec and unseen-archive coverage

Status: **frozen before any outcome on an unseen session is computed.** No outcome run exists. This replaces the old G5c design (`PROTOCOL.md` at `0e1d8cc`) for any future run. Sept 25 and the 34 already-viewed sessions (Sept 25 – Nov 16) are exploratory only: they were used here for inputs and geometry, and for planning dispersion. No new outcome was read from them.

## 1. What the two visits to 744 show (Sept 25, exploratory, inputs only)

`sept25_744.json` records each 5-minute anchor. QQQ was within $0.50 of 744 at 23 of 74 anchors: 09:45–10:00, 11:05–13:00 on and off, and 14:30–15:40.

1. **The old reference strike jumps around.** "Largest |flow gamma| within $2" picked 742, 744, 745 and 746 during those visits. It did not stay on the strike price was visiting. Late in the day the largest value is mostly 0DTE gamma growing near spot. That makes the old pick close to "nearest strike", which a price-only rule can make without any options data.
2. **Every pick has an undetermined sign.** All 74 Sept 25 picks have scenario endpoints that cross zero, and no anchor has any eligible strike whose scenarios agree. The old test still assigned "long" or "short" gamma from the central attribution alone.
3. **The old control fails exactly at a visit.** The control had to be another covered strike within $0.50 of K\*'s distance. With $1 strikes, none exists when spot is within $0.25 of K\*. On Sept 25 that happened at 13 anchors, and the control was missing at all 13: 09:50, 15:00, 15:05, 15:25, 15:30 and 15:40 at 744, plus 7 more at other strikes. So the old design dropped the events that look most like a visit.

Across all 34 viewed sessions (`explored_inputs.json`, inputs at the anchor only):

| | Count |
|---|---|
| Anchors with a QQQ quote | 2,514 |
| No covered strike within $2: spot left the fixed 8-strike universe | 836 |
| Old K\* events | 1,678 |
| Old K\* sign undetermined | **1,678 (100%)** |
| Anchors with any scenario-agreeing strike within $2 | **1** (Nov 6) |
| Old control missing | 728 |
| K\* within $0.25 of spot | 216; control missing for **all 216** |

**Consequence:** the old "long vs short gamma" contrast compared cells whose sign the data do not determine. Section 2 fixes this.

## 2. Frozen comparison

### Sessions
- **Unseen set (primary):** the sessions in section 3, classes A and B.
- **Sensitivity only:** class C.
- **Exploratory, never pooled with the unseen set:** Sept 25 and every session in the old 34-session sample.
- **Accrual:** a future session is added only if its Tradier summary is `complete`, or `partial` solely for reconnects with a total stream gap ≤ 30 s. Its late start must be ≤ 5 s, and it must have chain snapshots for the universe.

### Anchors
- Every 5 minutes from 09:45 to 15:50 ET, 75 per session.
- An anchor is used only if all of these hold:
  - a QQQ quote at or before t is at most 5 s old, and the same holds at t + 5 min;
  - the latest chain snapshot at or before t is at most 120 s old, with same-day gamma and spot for all 16 contracts;
  - no reconnect gap overlaps [t − 5 min, t + 5 min].
- Each failure is counted by reason.

### Information at t: strike selection
- **Inputs:** only prints timestamped ≤ t, the snapshot above, and settled OI from the latest snapshot ≤ 09:30.
- **Spot:** S_t is the mid of the latest QQQ quote ≤ t.
- **Flow gamma per strike:** computed as in the Order Flow view (merged `fcd7794`). The 100% participation point estimate is `base`, and the scenario endpoints are `low` and `high`, with unknown-side volume assigned to each side. Sign agreement does not depend on participation, so 50% gives the same strata.
- **Eligible strikes:** covered strikes with |S_t − K| ≤ $2.00.
- **Stratum A (scenarios agree):** among eligible strikes whose `low` and `high` have the same nonzero sign, K\*_A has the largest |base|. Prediction: positive → approach (Δ < 0); negative → retreat (Δ > 0).
- **Stratum U (sign undetermined):** if the eligible strike with the largest |base| has endpoints that cross zero, it is K\*_U. There is no sign prediction. The two-sided question is whether a large-|flow| strike is approached more than a price-only level at the same distance.
- **Ties:** smaller |S_t − K| first, then the lower strike.
- **One anchor can count in both strata.** Results are never pooled across strata.

### Outcome over the next five minutes
D_K = |S_{t+5} − K| − |S_t − K|, using quote mids as above. Negative means approach.

### Price-only control, matched on distance and time of day
- **Control levels:** for an event with signed offset x = S_t − K\*, the control pool is drawn from the same session, at 5-minute anchors u ≠ t with |u − t| ≤ 60 min. Each u that passes the anchor rules contributes the integer level L_u for which:
  - |(S_u − L_u) − x| ≤ $0.10 (same side, same distance);
  - sign(S_u − S_{u−15}) = sign(S_t − S_{t−15}), so the level sits on the same side of the prior 15-minute move as K\*;
  - L_u is chosen without any options data, and need not be in the universe.
- **Control value:** the mean of D_{L_u} over the pool. If the pool is empty, the event is unusable and counted.
- **Event statistic:** Δ = D_K\* − control.
- **Secondary control:** the reflected level 2S_t − K\* at the same instant, Δ_R = (D_K\* − D_R)/2. It has the same distance and time exactly, and mean zero if direction is independent of K\*'s side.

### Statistics and verdicts
- **Stratum A:** mean Δ for positive and for negative K\*, and the contrast (positive − negative). Prediction: contrast < 0.
- **Stratum U:** mean Δ, two-sided.
- **Breakdowns:** by session third (09:45–11:55, 12:00–13:55, 14:00–15:50), because late-day picks are close to the nearest strike.
- **Intervals:** 95% session-cluster bootstrap, 2,000 resamples, seed 20261118.
- **Useful effect:** |Δ| ≥ $0.02 per 5 min, and the interval must exclude zero.
- **Verdict labels:**
  - **supported:** predicted sign, interval excludes zero, useful effect met;
  - **opposite sign**;
  - **null within precision:** interval excludes the useful effect;
  - **unresolved:** interval too wide;
  - **not testable:** fewer than 100 events or 10 sessions in the stratum.
- **Each verdict reports** event, unusable-event and session counts by exclusion reason.
- **Interpretation:** associations only, not proof of dealer hedging. Scenario endpoints are not confidence intervals.

## 3. Unseen-archive coverage (`coverage.json`)

Sessions with no outcome ever computed: they are listed as excluded in every `results.json`, or were recorded after Nov 16. Inputs only were checked (summary, universe, every regular-session chain snapshot).

| Class | Session | Tradier status | Prints (all 16 contracts) | Preceding quotes (p95) | Snapshots with all gamma + spot | Note |
|---|---|---|---|---|---|---|
| A: recorded after the analysis | Nov 17 | complete | yes, ≥ 661 each | 995 ms | 341/341, no gap > 120 s | universe 660–667 |
| A | Nov 18 | complete | yes, ≥ 2,086 each | 1,036 ms | 347/347 | universe 657–664 |
| B: never analysed | Sept 21 | complete, 2.3 s late | yes, ≥ 938 | 1,013 ms | 370/370; two gaps of 121–123 s near 12:00 | excluded only by the old zero-late-start rule |
| B | Sept 22 | complete, 3.2 s late | yes, ≥ 447 | 1,034 ms | 380/380 | as above |
| B | Sept 28 | partial: 1 reconnect, 13.6 s gap | yes, ≥ 981 | 1,086 ms | 379/379 | anchors overlapping the gap dropped |
| B | Oct 29 | partial: 1 reconnect, 1.9 s gap | yes, ≥ 1,769 | 1,178 ms | 367/367 | as above |
| C: sensitivity only | Sept 24 | partial: 256 s late start | yes | 1,005 ms | 380/380 | universe picked 09:34; first 4 min of flow missing |
| C | Sept 30 | partial: 258 s late start | yes | 1,342 ms | 380/380 | as above |

**Count:** 2 strictly unseen sessions (A), 6 with B, 8 including C. Every one has the prints, preceding quotes and fresh chain snapshots the spec needs.

**What is missing:**
- **Sessions:** this is the binding gap.
- **Universe width:** the fixed 8-strike universe left a third of the viewed anchors with no covered strike within $2.
- **Sign:** scenario-agreeing cells are almost absent.

## 4. Can the unseen archive support the test? **No.**

- **Stratum A (signed): no, and waiting will not fix it.** The viewed inputs had 1 agreeing anchor in 2,514, so the 8 unseen sessions are expected to give about 0 events. Even ~250 sessions a year would give only a few. The obstacle is unknown-side volume, not sample size. Making stratum A testable needs narrower signing scenarios or a wider strike universe; both are collection decisions for Owen.
- **Stratum U (magnitude vs price-only control): no, at the current size.** These planning numbers use only the dispersion (interval widths) of the old viewed G5c, not its effects:
  - the old single-stratum Δ had an interval half-width of $0.046–0.072 at 30 sessions;
  - resolving a $0.02 effect with ~80% power needs a half-width near $0.014, about **330–790 sessions**;
  - at ~1 session per trading day that is **1.3–3 years** of accrual;
  - available: 2 strictly unseen sessions, or 6–8 counting classes B and C.
- **Caveat:** the new matched control and the recovered near-strike events will change the variance, so these figures are approximate.

A run now could only be a feasibility check of the pipeline on classes A and B, labelled as such. It would not be evidence.

## Reproduce

```sh
node research/oa241/strike_test/sept25_744.js > research/oa241/strike_test/sept25_744.json
python research/oa241/strike_test/explored_inputs.py FEATURES_DIR > research/oa241/strike_test/explored_inputs.json   # repaired features b655f75
R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
  python research/oa241/strike_test/coverage.py CACHE_DIR > research/oa241/strike_test/coverage.json        # read-only
```
