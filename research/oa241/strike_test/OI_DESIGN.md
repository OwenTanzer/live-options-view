# OI-gamma strike test: provenance and revised design (paper only)

This is an **unsigned association** test: is a 0DTE strike with large open-interest gamma approached more, or less, over the next five minutes than a price-only level at the same distance? It makes **no claim about dealer positioning or hedging**.

**Sources:** only existing local manifests, configs and reports, plus the already-saved `snapshot_depth.json`. No new archive read or listing, no outcome, no collection change, no PR #270 change. The useful threshold stays **$0.02 per 5 minutes**.

## 1. Provenance of the 58 candidate dates (Jun 23 – Sept 18)

Per-date detail is in `provenance.csv`.

**Category rules:**
- **Exposed:** a study read that date's QQQ intraday price path and computed a forward-price outcome.
- **Untouched:** every local record shows the date was outside every such study.
- **Unknown:** the records cannot tell.

| Study | Local record | Candidate dates touched | What was read |
|---|---|---|---|
| **OA-233** move capture | `oa233-evidence/handback/session_manifest_v2.csv`, `freeze_oa233.json` | **58 of 58**: dev split, handling `evaluated` | ~1-min snapshot underlying and option quotes; QQQ $2–3 moves over 30–120 min |
| **OA-229** direction (Skew + Direction step 1) | `directional-move-lab` `research/oa229-direction-v1/reports/comparison-train-calibration-v1.json`, README | 24: Jul 1–29 train/calibration (19 of them have 0DTE snapshots); Jul 30 – Aug 5 fixture test sessions, which the README says earlier OA-229 research had already exposed | 5-min QQQ bars; 30-min forward moves |
| **Skew + Direction** steps 2–3 (35% stop, OA-236 protection) | `oa236_protection/STEP4_PLAN.md` §5: "Steps 1–3 used Aug 12 – Nov 9 … development-exposed" | 25: Aug 12 – Sept 18 | snapshot spot and option quotes on bot trips |
| **OA-239** history | `REPORT.md` (H1), H2–H4 reports | 29: Jun 23 – Aug 5, descriptive only (5-min RV, range, trend efficiency in the H1 atlas). H2–H4 used 2011–2025 only. | descriptive price summaries, no forward outcome |
| **OA-237** Options Replay | qualification (`qualify.py`: parity, 97/116 days) | all, as inputs | spot and option mids for a consistency check, no forward outcome |
| **OA-241** | `results.json`, `SPEC.md` | 0 | — |

**Result:**

| Classification | Dates |
|---|---|
| Untouched | **0** |
| Exposed | **58** (all, through OA-233; 24 also through OA-229, 25 through Skew + Direction steps 2–3) |
| Unknown | 0 by local record. Exposure outside these records cannot be excluded: work by other agents or terminals, earlier OA-229 runs in another workspace, and Replay sessions played by people. |

**Consequence:** the 58 dates are **exploratory at best**. The later dates are no better:
- **Sept 21 – Nov 10:** OA-233 holdout or development, Skew + Direction, and OA-241 G5c.
- **After Nov 9:** the Skew + Direction D4 window, which needs its own certified freeze.
- **From Nov 16:** OA-237's S3c held-out forward sessions.

So an untouched primary sample can only be **forward sessions after a freeze that Chej dates and certifies**, coordinated with the D4 and OA-237 forward sets.

**Input flags on candidate dates** (from the noon sample):
- Jun 23: 661 s snapshot gap, and OI arrived late (OA-239 H1).
- Jul 1 and Jul 31: snapshot gaps of 122–124 s.
- Jul 10, Aug 14, Aug 21 and Sept 4: gamma missing on 12–14 0DTE rows.
- Jul 16: positive OI on only 26 of 134 rows.

## 2. Revised design

### Snapshots and timestamps
- **Snapshot time** is the ET time in the filename `snapshot_HHMMSS…`, which the collector writes. The file's write and receipt lag are not recorded (see §3).
- **At t** (anchors every 5 minutes, 09:45–15:50 ET, 74 per session): use the latest snapshot with filename time ≤ t, if it is **≤ 120 s old**.
- **At t + 5:** use the latest snapshot with filename time ≤ t + 5 min, if it is ≤ 120 s old and **later than the snapshot used at t**.
- **At t − 15:** use the latest snapshot with filename time ≤ t − 15 min, if it is ≤ 120 s old. At 09:45 that means a snapshot from 09:28:00–09:30:00. If none exists, the 09:45 anchor has no prior move and is unusable.
- **Spot:** the snapshot's `UnderlyingPrice`, which must be a single value across its 0DTE rows. Never the `QQQ` column, which OA-237 found can disagree.
- **Frozen feed:** a snapshot is unusable if its spot and every 0DTE mid equal the previous snapshot's for 3 or more consecutive snapshots (OA-237 found such a stretch on Nov 2, 14:22–14:33).
- **Consistency gate:** the snapshot must pass OA-237's frozen put-call parity check (tolerance $0.15 plus half spreads) at strikes within $2.
- **The anchor is unusable** if any of the t − 15, t or t + 5 snapshots fails these rules. Each failure is counted by reason.

### OI and gamma: missing versus zero
- **OI** is settled and fixed for the session: use the value in the latest snapshot at or before 09:30. OA-239 found that OI refreshes around 06:00–06:35 and stays frozen during regular hours.
  - **Blank or non-numeric:** missing.
  - **Zero:** possibly missing. OA-171 serialized unknown values as zero, and on Jul 20 OI changed from 0 to populated after 12:00.
  - A contract missing at 09:30 stays missing all day. Values that arrive later are never used for anchors that day (causal rule).
  - A session where any in-window contract's OI changes during regular hours is flagged.
- **Gamma** comes from the snapshot at t.
  - **Blank, non-numeric or negative:** missing.
  - **Exactly 0 at a strike within $2:** treated as missing, because it is implausible that close to spot.
- **Strike eligibility at t:** |S_t − K| ≤ $2, and both the call and the put have usable OI and gamma.
  - **Primary:** a zero-OI contract makes the strike ineligible.
  - **Sensitivity:** zero OI counts as zero.
  - A missing input is never treated as zero.
- **Strike magnitude:** G_K = Σ over {call, put} of OI × gamma × 100 × S_t². It is unsigned: no call/put sign is assigned.
- **K\*:** the eligible strike with the largest G_K. Ties go to the smaller |S_t − K|, then the lower strike. Selection uses nothing after t.

### Outcome
D_K = |S_{t+5} − K| − |S_t − K|, with spot from the t and t + 5 snapshots. Negative means approach.

### Price-only control (no options data)
- **Retrospective matched control (primary):** for x = S_t − K\*, the pool comes from the same session's usable anchors u ≠ t with |u − t| ≤ 60 min.
  - Each u contributes the integer level L_u with |(S_u − L_u) − x| ≤ $0.10.
  - The prior 15-minute moves must match: sign(S_u − S_{u−15}) = sign(S_t − S_{t−15}), with both from snapshots under the rules above. A zero move matches only zero.
  - The control value is the mean of D_{L_u}. An empty pool makes the event unusable.
  - It uses anchors after t, so it is labelled **retrospective, not available live**.
- **Causal variant (sensitivity):** the same control restricted to u + 5 min ≤ t.
- **Reflected level (secondary):** 2S_t − K\* at the same instant, with Δ_R = (D_K\* − D_R)/2. It is live-available.
- **Event statistic:** Δ = D_K\* − control.

### Estimand and verdict (threshold unchanged)
- **Estimand:** session-weighted mean Δ. The event-weighted mean is reported but gets no verdict.
- **Interval:** whole-session bootstrap, 2,000 resamples, seed 20261118, with control values carried intact. Percentile 95% interval.
- **Minimum:** ≥ 100 events from ≥ 10 sessions. Otherwise **not testable**.
- **Verdicts (two-sided):**
  - **different from control:** the interval excludes 0 and |mean| ≥ $0.02;
  - **equivalent:** the whole interval lies inside ±$0.02;
  - **unresolved:** anything else.
- **Primary sample:** certified forward sessions only.
- **Exploratory:** the 58 dates and every later exposed date. They are never pooled with the primary sample, and are reported only if a run is approved.

### Go/no-go evidence check (before any outcome)
- **Go** only when all three hold:
  - ≥ 10 certified forward sessions have passed the input rules;
  - ≥ 100 events from them;
  - a planning half-width ≤ $0.014 (≈ 80% power at $0.02), from a dispersion estimate that Chej accepts.
- The old flow-gamma variance is **not** accepted as that estimate.
- Until such an estimate exists, the check cannot return go. The default is **no-go**.

## 3. What remains unknown
1. **Exposure outside local records:** other agents' and terminals' work, earlier OA-229 runs, and Replay sessions people have played.
2. **Snapshot timing:** whether the filename time is the request time or the write time, and how stale `UnderlyingPrice` is relative to it.
3. **Per-date OI arrival:** whether each date's OI was settled before 09:30 (Jun 23 was not), and which zeros are real.
4. **Why some 0DTE rows lack gamma** on Jul 10, Aug 14, Aug 21 and Sept 4: added strikes, or provider gaps.
5. **Dispersion of Δ for an OI-selected K\*:** unknown. Estimating it would need outcomes, which means an approved, dispersion-only run on exposed dates or the accrual of forward sessions.
6. **Sharing forward sessions** with the D4 and OA-237 S3c untouched sets: whether one forward session can serve several frozen tests. That is Chej's decision.
7. **Accrual time:** at about 1 session per trading day, 10 sessions is 2 weeks. Resolving ±$0.02, if the dispersion resembles the old flow value, would take years.
