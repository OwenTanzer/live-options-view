# Strike approach/retreat test: frozen spec and unseen-archive coverage

Status: **frozen before any outcome on an unseen session is computed.** No outcome run exists. This replaces the old G5c design (`PROTOCOL.md` at `0e1d8cc`) for any future run. Sept 25 and the 34 already-viewed sessions (Sept 25 – Nov 16) are exploratory only: they were used here for inputs and geometry, and for planning dispersion. No new outcome was read from them. Revised 2026-11-19 after review: 74 anchors, reconnect and late-start handling, session weighting, equivalence bounds, prior-move quote rule, retrospective control label.

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
- **Primary:** classes A and B in section 3 (4 sessions).
- **Sensitivity only:** class C. It is reported beside the primary result and never pooled into it.
- **Exploratory, never pooled:** Sept 25 and every session in the old 34-session sample.
- **Reconnects:** the recorder does not backfill missed transactions after a reconnect (Tradier summary, `limitations`). Cumulative flow after a gap is incomplete for the rest of the session, and it cannot be reconstructed. So:
  - a session with any reconnect is sensitivity only (class C);
  - in the sensitivity run, its anchors with t at or after the first gap start are dropped. This is not limited to anchors that overlap the gap.
- **Late starts:** each session's late start is reported. Flow at t misses the prints before the stream was ready.
  - Primary allows a late start of at most 5 s (Sept 21: 2.3 s, Sept 22: 3.2 s), and the result is also shown without those two sessions.
  - Larger late starts are class C.
- **Accrual of future sessions:**
  - **Primary:** Tradier status `complete`, no reconnect, late start ≤ 5 s, and chain snapshots for the universe.
  - **Class C:** anything else that has prints and snapshots.

### Anchors
- Every 5 minutes from 09:45 to 15:50 ET: **74 per session**.
- **Available session:** passes the session rules above. **Eligible anchor:** in an available session, and passes every rule below. Both counts are reported separately, with each anchor failure counted by its first failing reason.
- An anchor is eligible only if all of these hold:
  - **Quote at t:** a QQQ quote at or before t, at most 5 s old.
  - **Quote at t + 5 min:** the same rule.
  - **Prior move:** a QQQ quote within ±5 s of t − 15 min and at or before t. Its mid is S_{t−15}. At 09:45 this is the first quote in 09:30:00–09:30:05. It is available at t.
  - **Chain snapshot:** the latest snapshot at or before t is at most 120 s old, with same-day gamma and spot for all 16 contracts.
  - **Flow:** no reconnect gap at or before t + 5 min in the session (class C only; primary sessions have none).

### Information at t: strike selection
- **Inputs:** only prints timestamped ≤ t, the snapshot above, and settled OI from the latest snapshot ≤ 09:30.
- **Spot:** S_t is the mid of the latest QQQ quote ≤ t.
- **Flow gamma per strike:** computed as in the Order Flow view (merged `fcd7794`). The 100% participation point estimate is `base`, and the scenario endpoints are `low` and `high`, with unknown-side volume assigned to each side. Sign agreement does not depend on participation, so 50% gives the same strata.
- **Eligible strikes:** covered strikes with |S_t − K| ≤ $2.00.
- **Stratum A (scenarios agree):** among eligible strikes whose `low` and `high` have the same nonzero sign, K\*_A has the largest |base|. Its gamma sign is the sign of `base`.
- **Stratum U (sign undetermined):** if the eligible strike with the largest |base| has endpoints that cross zero, it is K\*_U. There is no sign prediction.
- **Ties:** smaller |S_t − K| first, then the lower strike.
- **Strata are never pooled.** One anchor can count in both.
- **Strike selection is live-available:** it uses nothing after t.

### Outcome over the next five minutes
D_K = |S_{t+5} − K| − |S_t − K|, using quote mids as above. Negative means approach.

### Price-only control, matched on distance and time of day
- **Control levels (retrospective):** for an event with signed offset x = S_t − K\*, the pool comes from the same session, at eligible 5-minute anchors u ≠ t with |u − t| ≤ 60 min. Each u contributes the integer level L_u for which:
  - |(S_u − L_u) − x| ≤ $0.10 (same side, same distance);
  - sign(S_u − S_{u−15}) = sign(S_t − S_{t−15}), with both prior moves under the quote rule above. A zero move matches only zero.
  - L_u uses no options data and need not be in the universe.
  - Anchors u > t, and outcomes ending after t, are **not available live**. This control is labelled **retrospective**: a benchmark for the association, not a trading rule.
- **Control value:** the mean of D_{L_u} over the pool. An empty pool makes the event unusable, and that is counted.
- **Event statistic:** Δ = D_K\* − control.
- **Causal sensitivity:** the same control restricted to u + 5 min ≤ t, so it uses only outcomes known at t. Reported beside the primary result, never replacing it.
- **Secondary control:** the reflected level 2S_t − K\* at the same instant, Δ_R = (D_K\* − D_R)/2. The level is live-available, has the same distance and time exactly, and has mean zero if direction is independent of K\*'s side.

### Estimand, resampling and minimum counts
- **Session-weighted (primary):** for each session, average Δ over its events in the cell, then average over sessions with equal weight. A session with one event counts as much as one with forty.
- **Event-weighted (reported, not used for a verdict):** the plain mean over events.
- **Resampling:** 95% session-cluster bootstrap, 2,000 resamples, seed 20261118.
  - The unit is a whole session. Each event's Δ, and every control value it used, are computed once from the original session and carried intact into each resample. Controls are never recomputed and never shared across sessions.
  - Percentile intervals.
- **Minimum counts per cell:** at least 100 events from at least 10 distinct sessions. Otherwise the cell is **not testable**. The cells are:
  - stratum U;
  - stratum A, positive gamma;
  - stratum A, negative gamma.
  The signed contrast needs both stratum A cells to pass.
- **Breakdowns:** by session third (09:45–11:55, 12:00–13:55, 14:00–15:50), because late-day picks are close to the nearest strike. Descriptive only.

### Verdicts (threshold $0.02 per 5 minutes)
- **Stratum U, two-sided, on the session-weighted mean Δ:**
  - **different from control:** the interval excludes zero and |mean| ≥ $0.02 (sign reported as approach or retreat);
  - **equivalent:** the whole interval lies inside (−$0.02, +$0.02);
  - **unresolved:** anything else.
- **Stratum A, on the signed contrast C = mean Δ(positive) − mean Δ(negative):**
  - Each mean is session-weighted over sessions with at least one event of that sign.
  - Bootstrap resamples whole sessions and recomputes both means. If more than 5% of resamples lack either sign, the verdict is unresolved.
  - The $0.02 threshold applies to C only. The two per-sign means are reported with intervals but get no verdict.
  - **supported:** C < 0, the interval's upper bound < 0, and C ≤ −$0.02;
  - **opposite sign:** C > 0, the interval's lower bound > 0, and C ≥ +$0.02;
  - **equivalent:** the whole interval lies inside (−$0.02, +$0.02);
  - **unresolved:** anything else.
- **Each verdict reports** available sessions, eligible anchors, events, unusable events, and sessions contributing events, all by reason. Class C and the causal-control sensitivity are reported alongside.
- **Interpretation:** associations only, not proof of dealer hedging. Scenario endpoints are not confidence intervals.

## 3. Unseen-archive coverage (`coverage.json`)

Sessions with no outcome ever computed: they are listed as excluded in every `results.json`, or were recorded after Nov 16. Inputs only were checked (summary, universe, every regular-session chain snapshot). No print or quote after the open was read.

| Class | Session | Tradier status | Prints (all 16 contracts) | Preceding quotes (p95) | Snapshots with all gamma + spot | Note |
|---|---|---|---|---|---|---|
| A: recorded after the analysis | Nov 17 | complete | yes, ≥ 661 each | 995 ms | 341/341, no gap > 120 s | universe 660–667 |
| A | Nov 18 | complete | yes, ≥ 2,086 each | 1,036 ms | 347/347 | universe 657–664 |
| B: never analysed | Sept 21 | complete, 2.3 s late | yes, ≥ 938 | 1,013 ms | 370/370; two gaps of 121–123 s near 12:00 | anchors in those gaps fail the snapshot rule |
| B | Sept 22 | complete, 3.2 s late | yes, ≥ 447 | 1,034 ms | 380/380 | |
| C: sensitivity only | Sept 28 | partial: 1 reconnect, 13.6 s gap | yes, ≥ 981 | 1,086 ms | 379/379 | no backfill; anchors from the gap on dropped |
| C | Oct 29 | partial: 1 reconnect, 1.9 s gap | yes, ≥ 1,769 | 1,178 ms | 367/367 | as above |
| C | Sept 24 | partial: 256 s late start | yes | 1,005 ms | 380/380 | universe picked 09:34; first 4 min of flow missing |
| C | Sept 30 | partial: 258 s late start | yes | 1,342 ms | 380/380 | as above |

**Count:**
- **Available primary sessions:** 4 (A: 2, B: 2), so at most 296 anchors before eligibility.
- **Sensitivity:** 4 more (class C).
- The exact number of eligible anchors needs the quote checks at t and t + 5. Those read prices, so they are counted only in the sealed run.

**What is missing:**
- **Sessions:** this is the binding gap.
- **Universe width:** the fixed 8-strike universe left a third of the viewed anchors with no covered strike within $2.
- **Sign:** scenario-agreeing cells are almost absent.

## 4. Can the unseen archive support the test? **No.**

- **Stratum A (signed): no, and neither waiting nor a wider universe fixes it on its own.**
  - The viewed inputs had 1 agreeing anchor in 2,514, so the unseen sessions are expected to give about 0 events per sign, against a need for 100 events from 10 sessions in each.
  - The obstacle is unknown-side volume. A wider strike universe would recover the third of anchors that have no covered strike, but it would not by itself make the scenario endpoints agree.
  - Only narrower signing (less unknown-side volume) could. That is a collection decision for Owen.
- **Stratum U (magnitude vs price-only control): no, at the current size.** These planning numbers use only the dispersion (interval widths) of the old viewed G5c, not its effects:
  - the old single-stratum Δ had an interval half-width of $0.046–0.072 at 30 sessions;
  - resolving a $0.02 effect with ~80% power needs a half-width near $0.014, about **330–790 sessions**;
  - at ~1 session per trading day that is **1.3–3 years** of accrual;
  - available: 4 primary sessions, 8 with sensitivity.
  - **Caveat:** the matched control, the recovered near-strike events and session weighting will change the variance, so these figures are approximate.

A run now could only be a feasibility check of the pipeline on the 4 primary sessions, labelled as such. It would not be evidence.

## Reproduce

```sh
node research/oa241/strike_test/sept25_744.js > research/oa241/strike_test/sept25_744.json
python research/oa241/strike_test/explored_inputs.py FEATURES_DIR > research/oa241/strike_test/explored_inputs.json   # repaired features b655f75
R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
  python research/oa241/strike_test/coverage.py CACHE_DIR > research/oa241/strike_test/coverage.json        # read-only
```
