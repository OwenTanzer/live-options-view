# Step 4 (D4) packet, v6: bounded correction of v5

This supersedes `STEP4_PLAN_v5.md`; v1–v5 are kept unchanged. It applies Chej's review of v5 (2026-11-14, 12:55 PM) to the same packet, and nothing more:

- an explicit comparison population, with a predeclared session denominator;
- Tier 1 labelled as exploratory, with every censoring case named;
- the exact observations Tier 2 needs, and a disposition for each;
- the interpretation rules (primary comparison, multiplicity, zero or negative references, drawdown and churn denominators) fixed before any test or ratio is used.

**Unchanged:**
- Nothing has been run and nothing is authorized: no evaluator, no freeze, no shadow start, no activation.
- The arms, layering, numeric gates and any shadow duration remain **proposals**.
- D1–D3 stay as accepted.

Every number below comes from `d4_inventory.py` (schema v2; read-only, computes no arm outcome).

## 1. Populations (fixed before any outcome is computed)

| Population | Definition | Size | Use |
|---|---|---|---|
| **P-desc** | every actual round trip | 75 trips / 33 trade days | Descriptive only. Never paired, never tested. |
| **P-ledger** | trips on or after 2026-08-31 (ledger exists) | 61 trips / 27 trade days | Descriptive. Skew-dependent logic (A1g, A5) is computable only here. |
| **P-primary** | P-ledger trips on **eligible sessions** (§2) | **60 trips / 26 trade sessions** | The **only** population for paired arm comparisons. Every arm uses this exact subset. |
| **S-elig** | eligible NYSE sessions, zero-trade days included (§2) | **46 sessions** (26 with actual trips, 20 without) | The Tier 2 session denominator. |

**Strata** (each reported separately, never pooled into P-primary):
- 7 trips are pre-settlement-fix and pre-ledger, with unattributed exits.
- 7 trips are post-fix but pre-ledger, with unattributed exits.
- The remaining 61 are post-fix and ledger-covered: 48 signal exits and 13 EOD flattens.
- The one ledger trip outside S-elig (2026-11-04, error share 42%) is listed but excluded from P-primary.

**Tier 1 results are trade-day-conditional.** They are stated per P-primary trip and per trade session ("conditional on the actual bot trading that session"), never per eligible session. No unconditional per-session claim is made from Tier 1.

## 2. Predeclared session denominator (coverage only; proposal criteria)

- **Window:** all NYSE sessions from 2026-08-31 to 2026-11-09 (Labor Day excluded), which is **50 sessions**.
- **Eligibility:** a session is eligible when it has **≥ 75** market-hours ledger cycles, a **maximum gap ≤ 630 s** (two 300 s cycles plus 30 s), and an **error share ≤ 20%** (transport or runner errors). That gives **46**.
- **Excluded (4):**
  - 2026-08-31: no market-hours cycles (the ledger starts after the close);
  - 2026-11-04 (42%), 11-05 (100%) and 11-06 (100%): error share above 20%, because the bot was logged out.
- These criteria use coverage only and were fixed without looking at any arm outcome.
- **Before Aug 31:** 2026-08-12 to 08-30 has no ledger. Those sessions are outside S-elig and carry only the 14 descriptive trips.

## 3. Tier 1 (identical entries): exploratory only, with censoring named

Tier 1 replays each arm's exits on P-primary's actual entries. It **cannot** establish counterfactual portfolio exposure, re-entry exposure or churn, and this does not change with more sessions of the same design. Every trip-arm row carries these labels, and every table reports the share of rows that carry each one:

| Label | Meaning |
|---|---|
| `C1 fallback_actual_exit` | The arm's exit was still pending at the actual exit. The actual fill is used. |
| `C2 reentry_censored_at_actual_exit` | A5's re-entered leg is closed at the actual exit time, at that snapshot bid. Anything after that is unobserved. |
| `C3 no_counterfactual_rebuy` | The arm exited before the actual exit, and the strategy's re-buys after that point are unobserved (the bot was still holding). This applies to every early exit in A1–A5. |
| `C4 guard_removal_only` | A1g can only remove later actual same-side entries. It cannot model the A1 re-buys it would have prevented, because C3 hides them. |
| `C5 quote_gap_over_90s` | A decision falls inside a snapshot gap longer than 90 s (3 trips have one). |
| `C6 latency_fill_beyond_path` | The next-snapshot fill falls outside the recorded path, so C1 applies. |

Tier 1 churn is reported as a **censored lower bound** (C2 and C3). It never enters a gate.

## 4. Tier 2 (full cycles): exact missing observations and their disposition

Tier 2 replays the actual `oi_skew._decide_core` (strategy_version 1.0.0) at the actual cycle times of every S-elig session, with each arm's exit layer in front of it.

| # | Observation Tier 2 needs | Status (measured) | Disposition |
|---|---|---|---|
| M1 | The strategy's inputs at every cycle, including cycles where the actual bot was holding (imbalance, session-start anchor, band) | Present in the ledger. All 849 holding cycles carry them, and the anchor is recorded per cycle, so restarts and anchor resets are observed, not reconstructed. | **Available** |
| M2 | The exact chain snapshot each cycle used | 10,166 records name the key (from Oct 5); 9,853 carry the snapshot timestamp only (before Oct 5); 4 have neither | **Available.** The pre-Oct 5 cycles are matched by timestamp and labelled; the 4 cycles with neither are pending. |
| M3 | Full-chain snapshots for S-elig, covering both the contract the strategy would choose at each counterfactual entry and every quote path after that | Recorded keys are retrievable: 26/26 sampled with HEAD 200 on the public bucket. **Not assembled.** About 390 per session × 46. | **Missing, but obtainable** with read-only GETs. This is a data pull with no outcome computed. |
| M4 | QQQ 5-minute bars on S-elig's 20 zero-trade sessions (for A3–A5 direction) | Only the 33 trade days are assembled | **Missing, but obtainable** from the same snapshots (M3) |
| M5 | Executable quote time (the Worker's 15 s rule) | Not recorded in snapshots; the cadence is about 64 s | **Not obtainable historically.** Tier 2 stays a snapshot-proxy replay, never executable-fill evidence. |
| M6 | Replay parity with what actually happened | 60 P-primary trips (120 fills) to match cycle by cycle | **Required before any arm figure is read.** It is reported with the economic size of each mismatch, not as a pass/fail gate. |
| M7 | Real-venue fees | The paper venue charges none | **Not obtainable.** Shown as reporting sensitivity only. Trigger net equal to gross at $0 says nothing about real-venue net profitability. |

**Disposition on the exposed history:** Tier 2 is **feasible in principle once M3 and M4 are assembled**, as a proxy replay that has to pass the parity check (M6). Even then it is **exposed**: it can rank arms, but cannot accept one.

**Untouched data:** none is in hand.
- Sessions from 2026-11-10 to 11-14 are **not** proposed for any use. They predate any freeze, I can't certify they haven't been inspected, and Chej holds that access check.
- An untouched window can only consist of sessions after a freeze that Chej dates and certifies.
- No study length is proposed here. v5's 30-session figure stays a proposal, and **a Tier 1-only shadow would not supply Tier 2**.

## 5. Interpretation rules (fixed before any test or ratio is used)

**Primary comparison.** There is one: **A4 − A1** (directional protection without re-entry, against the selected 35% stop). It is computed on P-primary in Tier 1 (exploratory) and on S-elig in Tier 2. A4 is chosen as primary because it is D3's core question and the only directional arm with no re-entry of its own, so C2 never applies to it. It is still subject to C3.

**Secondary comparisons (exploratory):** A5 − A1, A1g − A1, A2 − A1, A3 − A1.
- p-values are Holm-adjusted across these four.
- No secondary result can be adopted without its own future primary test.
- A1 − A0 is descriptive only, because A1 is already the selected input.

**Paired unit.**
- Tier 1: the P-primary trip, clustered by trade session.
- Tier 2: the S-elig session. A zero-trade session counts as $0 for any arm that doesn't trade in it.

**No ratios to the reference P&L.** Every gate is a dollar difference, X − A1:
- **Expectancy:** the mean paired difference. Tier 1 is per trip (trade-day-conditional); Tier 2 is per S-elig session.
- **Largest winners:** the top-k set is fixed as A0's top 1, 3 and 5 trips on the population, and is never re-chosen per arm. The gate is on Δ_topk = X_topk − A1_topk in dollars, and the proposed tail gate is Δ_top5 ≥ −0.20 × max(A1_top5, $0). When A1_top5 ≤ $0 that becomes Δ_top5 ≥ $0, so a zero or negative reference never creates a ratio.
- **Drawdown:** the maximum peak-to-trough of cumulative P&L, ordered by session date then fill time over the population, on the same rows for every arm. The proposed gate is DD_X − DD_A1 ≤ 0.10 × DD_A1; if DD_A1 = $0, the gate is DD_X = $0.
- **Churn:** executed round trips. The denominator is S-elig sessions in Tier 2; in Tier 1 it is a censored lower bound per entry and never a gate. The proposed gate is churn_X − churn_A1 ≤ 0.5 × churn_A1; if churn_A1 = 0, the gate is churn_X = 0.

**Costs.** Fills as in v5 (bid exits, ask entries, next-snapshot primary, optimistic and pessimistic sensitivities). The reporting fee ($0 and $1.00 per contract per side) never changes a trigger. The $0 trigger fee describes the paper venue only.

**Verdicts:**
- Exposed-history results (Tier 1 or Tier 2) are labelled **exploratory / exposed** and can't satisfy any gate.
- Only an untouched Tier 2 result can be called materially better, and then only for the primary comparison.

## 6. The next decision (finite)

1. **Accept or correct §1–§5** as the frozen interpretation contract. This is a review decision, not a run.
2. **Separately, if wanted:** authorize the M3/M4 read-only data assembly for S-elig. That pull computes no outcome.
3. **Separately:** set and certify a freeze date before any untouched window is discussed.

Nothing else is proposed.

**Reproduce:** run `d4_inventory.py` (add `--check-keys` for the read-only HEAD sample) on the same private inputs; its output records their SHA-256 values.
