# Strike test: decision brief

Built only from the accepted spec (`SPEC.md`, `b056427`), the existing OA-241 reports, and one new read-only, inputs-only listing of the chain-snapshot archive (`snapshot_depth.json`). It reads no price column, opens no holdout and computes no outcome.

## What the current archive supports

| Question | Supported now | Not supported | Minimum new evidence that would change the verdict |
|---|---|---|---|
| **Signed flow gamma** (stratum A: long vs short) | The finding that the sign is undetermined: 1 scenario-agreeing anchor in 2,514 viewed, all 1,678 old K\* picks uncertain | Any long/short or attraction/repulsion verdict | ≥ 100 events per sign from ≥ 10 primary sessions. At the viewed rate that is not reached by accrual alone. It needs a source of clearer signs: narrower signing, or future flow or added strikes that turn out to agree more often (not ruled out). |
| **Flow-gamma magnitude vs price-only level** (stratum U) | A frozen, testable spec. 4 primary sessions (Nov 17, Nov 18, Sept 21, Sept 22) and 4 for sensitivity. | Any verdict: below the 10-session minimum | ≥ 10 primary sessions for a labelled result. About 330–790 to resolve ±$0.02 per 5 min. |
| **Old G5c strike attraction** | Exploratory "unresolved" only (opposite sign, 728 anchors without a control, all near-strike visits dropped) | Evidence either way | None. Superseded by the spec. |
| **Wider strike universe** | Recovering the ~⅓ of viewed anchors with no covered strike within $2 | A reliable sign on its own | Covered-strike agreement rates from a wider universe |

## Feasible 0DTE follow-up with inputs we already record

**Candidate:** an open-interest gamma strike test that uses chain snapshots only, with no prints.

**Inputs exist** (`snapshot_depth.json`):
- 101 trading days with same-day-expiry snapshots, Jun 23 – Nov 18;
- 58 of them before Sept 21, which is earlier than any OA-241 outcome analysis;
- 46–74 strikes per day, against the Tradier universe's 8;
- Gamma on every 0DTE row, and settled OI on most;
- 5 days with a snapshot gap over 120 s.

**Design:** this is stratum U's design with the flow input swapped out.
- **Selection:** at each anchor, K\*_OI is the 0DTE strike within $2 with the largest |Σ OI × gamma × 100 × S²|, using the latest snapshot ≤ t (≤ 120 s old).
- **Spot:** that snapshot's UnderlyingPrice, because pre-Sept quotes don't exist.
- **Outcome:** D_K = |S_{t+5} − K| − |S_t − K|.
- **Price-only comparison:** the same retrospective matched control (same session, ±60 min, offset within $0.10, same prior 15-minute move). Also the causal and reflected variants.
- **Statistics:** two-sided, session-weighted, whole-session bootstrap.

**Go/no-go evidence check (inputs only, before any outcome):**
- Go only if both hold:
  - the eligible sessions (Jun 23 – Sept 18) give a planning half-width ≤ the useful effect;
  - each session has ≥ 40 eligible anchors.
- At the old dispersion ($0.046–0.072 at 30 sessions), 58 sessions give a half-width of about $0.033–0.052. That is a detectable effect of about $0.05–0.07 per 5 minutes at 80% power.
- So it is **no-go at the $0.02 useful effect**. It is **go only as a coarse screen** if Owen and Chej accept a useful effect of about $0.05.
- **Prerequisite:** confirm that no other study (for example OA-239 or Skew + Direction) has read those June–Sept QQQ intraday paths for a strike-level outcome.

## Recommendation

- At $0.02 no follow-up is feasible.
- **The binding input is the number of independent sessions.** It is not a missing field: the snapshots already provide wide strikes, gamma and OI.
- The OI-gamma screen is the only test the current inputs can run. It can detect only effects about 2.5–3.5× the useful threshold, so I would run it only if a coarse screen at about $0.05 is what you want.
