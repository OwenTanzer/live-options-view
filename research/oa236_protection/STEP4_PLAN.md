# Step 4 (D4): frozen causal-economic comparison plan, v1 (proposal)

**Status:** a proposal for Chej and Owen to review. **Nothing in this plan has been run.** It fixes the arms, data, metrics, selection rule and success criterion *before* any evaluation. Once accepted, any change gets a new version number and leaves this one intact. The items marked **[confirm]** are assumptions the dispatch did not specify.

## 1. Question

For OI Skew's actual entry logic, does any of the exit families below materially improve **net economic outcome** over the unchanged exits on sessions that have not been used for development? And if the history cannot answer that, what finite shadow test can?

## 2. Arms (frozen)

Every arm uses the same entry logic, one contract per position, the EOD flatten (which always wins), observed quotes, and identical cost assumptions. Code and config hashes are recorded per arm.

| Arm | Exit policy | Source |
|---|---|---|
| **A0** unchanged | Strategy signal sell + EOD flatten (today's behaviour) | live code at the frozen revision |
| **A1** stop-only | A0 + the 35% entry-premium stop (full exit at the first executable bid ≤ 65% of own basis, each re-entry its own basis) | `exits.py` 1.2.0 (#234) |
| **A2** prior fixed take-profit | A1 + a **+50%** fixed premium take-profit **[confirm: the +50% placeholder `exits.py` carried before the 11-09 `None` decision]** | `exits.py` `profit_target_pct=0.50` |
| **A3** direction-free trailing | A1 + after net ≥ +20%, exit when bid ≤ 75% of the peak bid since entry **[confirm the 25% giveback]**; no direction input | small declared rule; arm level shared with A4/A5 |
| **A4** directional, no re-entry | A1 + `oa236-protect-v1` `protect_only` | #236 |
| **A5** directional, with re-entry | A1 + `oa236-protect-v1` `protect_reentry` | #236 |

**Layering [confirm]:** A2–A5 sit on top of the selected 35% stop, using the existing precedence (EOD flatten > stop > target/trail/protection > signal sell > re-entry). The un-layered versions (A2–A5 without the stop) are reported as secondary diagnostics only, never as selection candidates.

**Direction input:** the OA-229 challenger `oa229-direction-challenger-v1` at directional-move-lab 500a36e, unchanged.

## 3. Two stages, in order

### Stage 1: entries fixed (trade-level)
- Use the 75 actual round trips. Every arm starts from the same actual fill and basis.
- Each exit counterfactual uses observed snapshot quotes.
- A5's re-entries need quotes for the **new** contract after an exit, which is the first gap that must be filled (§5).
- Stage 1 attributes exits only; it cannot show portfolio effects.

### Stage 2: portfolio state and full cycles
- Re-run the **actual strategy decision function** (`oi_skew._decide_core`, the frozen revision) over each archived session's snapshots, at the runner's 5-minute cycle times. Each arm's exit layer runs in front of it.
- Carry the full state: account cash, one-at-a-time positions, cooldowns, re-entries, the EOD flatten and the session anchor reset.
- **Parity gate (must pass before any arm is compared):** A0 must reproduce the actual paper record.
  - Entries and exits on the same cycle and contract for **≥ 90%** of the actual round trips.
  - Every mismatch listed with its reason (cycle gaps, quote differences, logged-out days).
  - If the gate fails, Stage 2 stops and reports the mismatch, not arm results.

## 4. Costs (frozen)
- Fills: long exits at the snapshot **bid**, entries at the snapshot **ask**.
- Two scenarios: (a) the decision snapshot; (b) one snapshot later plus one tick of slippage.
- Fees are reported separately, never folded into the trigger:
  - **$0** (paper);
  - **$1.00 per contract per side** **[confirm the broker schedule]**.
- Results are always shown both gross and fee-net. No arm is described as "net" unless fees are included.

## 5. Data and the explicit gaps

| Need | Source | Gap and how it's handled |
|---|---|---|
| Held/exit/re-entry quotes | Collector chain snapshots, ~64 s cadence | No per-row quote time: snapshot age ≤ 90 s is a **proxy** for the live 15 s rule. A re-entry strike can be absent from the board, which makes that re-entry unavailable, never assumed. Snapshot bids are not independently observed executable fills. |
| Underlying bars | QQQ spot from each snapshot's first row | `history/QQQ/5` stops on 2026-08-05. Bar highs and lows come from about 5 samples (a proxy). |
| Runner cycles | Decision ledger, Aug 31 – Nov 9 | Aug 12–31 has no ledger, so cycles there are synthetic 300 s (flagged). Nov 4–6 the bot was logged out (643 × 401), so those days are excluded from A0 parity and marked. |
| Strategy state | Ledger metadata | Anchors reset on restarts (6 of 46 dates). Stage 2 replays anchors per actual run ID where known; otherwise the gap is flagged. |
| Exposure history | Steps 1–3 used Aug 12 – Nov 9 | **That whole window is development-exposed.** No part of it can serve as untouched evaluation. |

## 6. Metrics (all arms, same rows)
- **Expectancy per opportunity, per cycle and per session**, gross and fee-net, clustered by session.
- **Max drawdown** (session-sequence equity) and the worst session.
- **Giveback avoided** and **lost tail gains**, both relative to A0.
- **Winners cut short:** A0 winners where the arm exits for less.
- **Recovered continuation:** for A5, re-entries whose re-entered leg ends net-positive.
- **Churn:** *executed trades* per session and round trips per position chain. These are not phase transitions.
- **Largest-winner sensitivity:** each arm's result with A0's top 1, 3 and 5 winners removed, and the share of each arm's delta that comes from them.

## 7. Selection and success criterion (frozen before any untouched data)
1. **Selection on the development window (Aug 12 – Nov 9), Stage 2, fee-net at $1.00/side.** Choose the arm with the highest session-clustered expectancy, subject to max drawdown ≤ A0's. Only the six pre-declared arms are candidates, and no parameter is tuned. If no arm beats A0, then **A1 is carried forward only if** it beats A0 on drawdown. Otherwise the result is "no candidate".
2. **The selection, code hashes and this criterion are frozen and sent before the first untouched session.**
3. **Material improvement on the untouched window.** The selected arm counts only if all four hold:
   - (a) mean paired session P&L vs A0 ≥ **+$10/session**, fee-net (E, the same placeholder as the v6 protocol) **[confirm]**;
   - (b) a one-sided paired permutation test over sessions gives p < 0.05, with the session-dependence caveat stated;
   - (c) max drawdown is no worse than A0 by more than 10%;
   - (d) the sign of the improvement survives leave-one-session-out and removal of the single largest winner.

Each arm is then reported as **supported**, **unsupported** or **insufficient evidence**.

## 8. If history is insufficient: finite shadow test (no live trading, no new writer)
- **Window:** the next **60 NYSE sessions** after the freeze (fixed N, the same as the v6 protocol, with no adaptive stopping for success).
- **Mechanics:** after each session closes, run the frozen Stage 2 replay locally over that session's archived snapshots, read-only. This needs no runner change, no broker, no KV and no new production writer. Append one JSONL decision ledger row per arm per cycle, with input hashes.
- **Checkpoints:** every 10 sessions, report data quality only (snapshot coverage, parity misses). There are no interim performance claims. Stop early only on data failure.
- **At session 60:** evaluate exactly §7.3 and report supported, unsupported or insufficient.

## 9. Not authorized by this plan
- No merge, deployment, restart, activation or trading change.
- No new collector or production writer.
- No KV or provider writes.
- Read-only R2 GETs of archived snapshots are the only data access.
