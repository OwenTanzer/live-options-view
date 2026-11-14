# Step 4 (D4): causal-economic comparison plan, v2 (scoped revision; proposal)

Supersedes `STEP4_PLAN.md` (v1), which is left unchanged for the record. Revised on Chej's review of 2026-11-13 21:18.

**Status:**
- **Nothing in this plan has been run, and nothing is authorized by it:** no replay, no tests, no prospective study, no live action, no merge, no activation.
- **Decided input, kept fixed:** the selected **35% entry-premium stop** (`exits.py` 1.2.0, #234).
- **Proposals:** every other choice below is labelled **[proposal]**. None of them is an accepted decision or a quest gate.
- **Open for confirmation:** the five items in §6.

The plan has two separate parts:

- **Part A: historical feasibility and comparison** (§1–§5). This is the work within the current D4 scope.
- **Part B: an optional 60-future-session study** (§7). This is a new proposal. It is not authorized execution and it is not a completion requirement for D4. OA-233 stays separately paused and is unaffected.

---

## Part A: historical feasibility and comparison

### A1. Question
On the exposed history (Aug 12 – Nov 9), can the exit families be compared causally and economically against the unchanged exits, under one shared data and cost contract? If so, what does that comparison show? Because the window is development-exposed, the result counts as feasibility and comparison evidence, never as an untouched validation.

### A2. Arms

| Arm | Exit policy | Status |
|---|---|---|
| A0 unchanged | Strategy signal sell + EOD flatten | baseline |
| A1 stop | A0 + **35% entry-premium stop** (first executable bid ≤ 65% of the position's own basis; full exit; each re-entry has its own basis) | **fixed (selected)** |
| A2 fixed take-profit | +50% premium target | **[proposal]** (pre-11-09 placeholder) |
| A3 direction-free trailing | After the arm level, exit when bid ≤ 75% of the peak bid | **[proposal]** |
| A4 directional protection, no re-entry | `oa236-protect-v1` `protect_only`, frozen as defined in #236 | family fixed; its use here **[proposal]** |
| A5 directional protection with re-entry | `oa236-protect-v1` `protect_reentry`, frozen as in #236 | family fixed; its use here **[proposal]** |

**Layering [proposal]:** A2–A5 sit on top of A1 under the existing precedence (EOD flatten > stop > target / trail / protection > signal sell > re-entry). The un-layered variants are reported alongside. Neither form is preferred until §6 is confirmed.

### A3. Shared data contract (every arm, identical)

**Eligibility.** A session is eligible only if all four hold:
- there is ≥ 1 actual OI Skew round trip in the window;
- the collector chain snapshots cover the held contract's session;
- QQQ spot samples exist for regular trading hours;
- the runner cycle source is known: the ledger from Aug 31 on, synthetic 300 s cycles for Aug 12–31.

Ineligible sessions are listed with the reason. They are excluded from **every** arm, never from just one. Nov 4–6 (the bot was logged out, 643 × 401) is listed separately: those days are kept in the eligible set but flagged.

**No lookahead.** At decision time t, an arm may use only:
- snapshots whose snapshot time is ≤ t;
- completed 5-minute bars available at ≤ t;
- ledger skew readings timestamped ≤ t.

The OA-229 challenger is fed with `cutoff=t`, so any later bar is rejected. The trailing peak and the armed state use bids observed at ≤ t only. Nothing uses the actual exit time or price except A0, whose actual record *is* the baseline.

**Missing quotes are unavailable.** A decision needs an executable quote: a snapshot row for that contract, with the needed side present, aged ≤ the declared proxy limit. Without one, the action is **pending**: an exit retries at the next cycle, and an entry or re-entry does not happen. Nothing is ever interpolated, carried forward past the limit, or taken from a different strike. A re-entry strike missing from the board means no re-entry.

**Quote-age proxy [proposal]:** ≤ 90 s against the live 15 s rule. Snapshots carry no per-row quote time, so every result is stated as a snapshot-bid proxy, not as observed executable fills.

### A4. Baseline, cash and drawdown (defined once, applied to all arms)
- **Unit:** one contract per position, $100 premium multiplier, no position sizing.
- **Cash:** each arm starts every session at the same notional cash; this tracks P&L only. Cash never constrains an entry in either A0 or any arm. Any constraint would apply identically to all arms, so the comparison is P&L-on-identical-entries (Stage 1) or P&L-on-replayed-decisions (Stage 2).
- **Session P&L:** realized premium P&L of all positions closed in the session. The EOD flatten closes everything, so there is no carry.
- **Drawdown:** max peak-to-trough of the cumulative session P&L sequence over the eligible sessions in date order. It is computed identically for every arm, both gross and at each fee level in A6.
- **Baseline:**
  - Stage 1 baseline = **actual** A0 outcomes on the actual trips.
  - Stage 2 baseline = **replayed** A0.

  The two are never mixed in one comparison.

### A5. Two stages

**Stage 1, entries fixed:**
- Every arm starts from each actual fill and basis.
- Exits follow the arm, under A3's contract.
- A5's re-entries require post-exit quotes for the new contract; a missing quote means no re-entry.
- Stage 1 attributes exits only; it shows no portfolio effects.

**Stage 2, full cycles:**
- Re-run the actual `oi_skew._decide_core` (frozen revision) at the runner cycles over each eligible session's snapshots, with each arm's exit layer in front of it.
- One position at a time; cooldowns, re-entries, the EOD flatten, and anchor resets per known run.

**Baseline parity is reported, not gated [proposal].** Before comparing arms, replayed A0 is matched against the actual paper record, round trip by round trip: same cycle, same contract. Every mismatch is listed with its cause: cycle-time source, quote difference, logged-out day, anchor or restart, or an entry the replay did not make. The report also gives the **economic impact** of the mismatches: actual minus replayed P&L, per mismatch and in total. No threshold is treated as agreed; any parity threshold is an open item (§6).

### A6. Costs: trigger costs vs P&L fee sensitivity (kept separate)
- **Fills:** exits at the snapshot **bid**, entries at the **ask**. Latency variant **[proposal]:** one snapshot later plus one tick.
- **Trigger costs are part of an arm's frozen definition:**
  - the A1 stop is premium-only, with no fee in the trigger (as in #234);
  - `oa236-protect-v1` (A4/A5) arms at a **net** +20% and floors at a **net** +5%, where "net" subtracts a round-trip fee at the family's configured `fee_per_contract_per_side`;
  - A3's arm level is proposed as the same net +20% under the same trigger fee.
- **Unresolved assumption, named explicitly:** what trigger fee value the frozen net trigger uses. #236's replay used **0**, so its "net" equalled gross. This plan does **not** change the trigger fee when fees are varied for reporting. The trigger fee is held at one declared value per run and recorded. Choosing it is an open item (§6).
- **P&L fee sensitivity is reporting only.** Each arm's realized P&L is shown gross and fee-net at $0, and at **$1.00 per contract per side [proposal]**. The reported fee level never changes when, or whether, a trigger fires.

### A7. Metrics (all arms, same eligible rows)
- **Expectancy:** session-clustered, per opportunity, per cycle and per session, gross and fee-net.
- **Risk:** drawdown (A4) and the worst session.
- **Relative to A0:** giveback avoided, lost tail gains, winners cut short.
- **Re-entry:** recovered continuation (A5 re-entries whose re-entered leg ends net-positive).
- **Churn:** *executed trades* per session and round trips per chain, which are not phase transitions.
- **Largest-winner sensitivity:** each arm's delta with A0's top 1, 3 and 5 winners removed.

### A8. Reporting outcome (history)
Each arm is reported as **supported / unsupported / insufficient evidence** on the exposed window, stated as feasibility and comparison evidence. **Improvement thresholds [proposal]:** for example, fee-net ≥ +$10/session vs A0, a paired session permutation test (dependence caveat stated), drawdown no worse by more than 10%, and the sign surviving leave-one-session-out and top-winner removal. These are illustrative until confirmed; no arm is "selected" by this plan.

## Open items for confirmation (five)
1. **Layering:** A2–A5 on top of the fixed 35% stop, un-layered, or both.
2. **A2 fixed take-profit level:** +50%?
3. **A3 trailing rule:** arm at net +20%, exit at 75% of the peak bid?
4. **Fees:**
   - (a) the **trigger fee** value for the frozen net triggers (A3–A5);
   - (b) the reporting fee schedule, $1.00/contract/side?
5. **Comparison thresholds:**
   - the material-improvement criterion (E = +$10/session, test, drawdown tolerance);
   - whether any baseline-parity threshold applies at all.

---

## Part B: optional 60-future-session study (new proposal, not authorized)

This would be a **separate proposal for a later decision**, not part of D4's completion.

- **Window:** the next 60 NYSE sessions after a freeze, fixed in size with no adaptive stopping.
- **Method:** the Part A Stage 2 replay, frozen, run offline after each close over archived snapshots (read-only R2 GETs only).
- **What it would not need:** no runner change, broker, KV or writer.
- **Checkpoints:** every 10 sessions, data quality only, with no interim performance claims.
- **Evaluation:** at session 60, against criteria confirmed beforehand.

It starts only if separately approved. OA-233 is separately paused and is not touched by this proposal.
