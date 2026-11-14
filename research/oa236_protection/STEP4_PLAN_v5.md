# Step 4 (D4) packet, v5: frozen candidates, historical feasibility, decision criterion

This supersedes `STEP4_PLAN_v4.md`; v1–v4 are kept unchanged for the record. v5 replaces v4's five open items with **one frozen proposal**, adds a **measured** input inventory (`d4_inventory.py`, read-only, no outcomes), and ends in one finite next step.

**Nothing has been run and nothing is authorized by this document.** There is no new evaluation, no 60-session study, no threshold search, no activation and no merge. Every number in §2 is a coverage measurement, not an arm outcome.

## 1. Frozen candidates (proposal, frozen before any further evaluation)

All thresholds below already exist. None was searched. Every arm keeps the existing precedence (EOD flatten > 35% stop > the arm's own exit > strategy signal sell > re-entry) and its own per-entry basis.

| Arm | Definition | Source (code at) |
|---|---|---|
| **A0** unchanged | Strategy signal sell + EOD flatten | the actual record |
| **A1** 35% stop | A0 + full exit at the first eligible bid ≤ 65% of basis; existing 15-minute same-contract cooldown | `exits.py` 1.2.0, #234 @ 11309ea |
| **A1g** 35% stop + stronger guard | A1, plus `strategy_entry_allowed`: after a stop, same-side strategy entries are blocked for the session until a fresh skew reading has been invalid and then valid | `profit_protection.py` v1.1, #236 @ c9d573b. **A separate, proposed candidate.** A1 is not silently replaced. |
| **A2** fixed take-profit | A1 + exit at the first eligible bid ≥ +50% of basis (premium-only, inclusive, exact) | `exits.py` `profit_target_pct = 0.50`, the earlier placeholder |
| **A3** direction-free trailing | A1 + arm at net ≥ +20% (the same arm as A4), then exit at the first eligible bid ≤ 75% of the peak eligible bid since arming | new code, using the existing values; no search |
| **A4** protection, no re-entry | A1 + `oa236-protect-v1.1` `protect_only` | #236 @ c9d573b |
| **A5** protection + re-entry | A1 + `oa236-protect-v1.1` `protect_reentry`. Re-entry uses the **original valid OI signal, no flip**; contract = the same symbol | #236 @ c9d573b |

**Two distinctions stay visible in every table:**
- A5's recovery re-entry, which needs no flip, is not the same as A1g's guard on the strategy's own re-buys. The guard never blocks an A5 recovery re-entry.
- A1g is reported next to A1, never in place of it.

**Layering is fixed: on top of A1.** The 35% stop is the selected input, so A2–A5 are only evaluated layered on it. The un-layered D3 replay result (−$1,862, zero-fee) stays as earlier context and is not a candidate.

## 2. What the history actually contains (measured, `d4_inventory.py`)

| Input | Measured | Consequence |
|---|---|---|
| Actual round trips | 75, 2026-08-12 → 11-09, 33 sessions (max 7 per session), 49 put / 26 call. Exits: 48 signal, 13 EOD flatten, 14 unattributed (pre-ledger). 7 predate the settlement fix. | Identical-entry comparison is possible on these 75 trips only. |
| Held-contract quotes | 5,273 snapshot rows inside actual holds; bid and ask always present; snapshot gap median 63.6 s (p90 65.1, max 135.9); 3 trips have a gap > 90 s; 338 absent rows; spread median 1.18% of mid (p90 2.68%, max 18.2%). Paths cover only entry − 3 min → actual exit + 3 min. | Exits **earlier** than the actual exit, and same-symbol re-entries **inside** the actual hold, can be priced. Nothing after the actual exit can. The 15 s executable rule cannot be verified historically: snapshots carry no per-row quote time, and the cadence is about 64 s. |
| Decision ledger | 20,023 records, 2026-08-31 → 11-10, 72 days, cycle gap median 300 s (max 628 s); skew reading present in 94.6% of records. | Skew-dependent logic (A1g guard, A5 re-entry) is computable only from Aug 31: **61 trips**. The 14 earlier trips have no skew readings, so those arms are not evaluable on them (reported, not imputed). |
| Underlying 5-minute spot samples | 33 sessions (all trade days), 357–382 samples each. Zero-trade sessions are not assembled. | The D1 direction can be computed on trade days only. |
| Untouched sessions | **None in hand.** Every trip above was used in the D2/D3 replays and in the earlier loss and validation analyses, and the strategy was selected as the best of 31 bots. The local record ends 2026-11-09. | History cannot provide untouched evaluation. |

## 3. What history can and cannot support

**Supported: Tier 1, the identical-entry exit comparison, as exposed feasibility and comparison evidence only.**
- The actual entries and bases are held fixed, and each arm's exits are replayed over the held-contract quotes.
- Every arm can only add exits earlier than the actual exit, so no arm ever needs a quote after the actual exit. A pending exit falls back to the actual exit fill, so there are **no unresolved positions in Tier 1**.
- A5 re-entries are same-symbol, inside the original hold. A re-entered leg ends at its own exit, or at the actual exit time at that snapshot bid, which is flagged.
- A1g in Tier 1 is limited to **removing** later actual same-side entries in a session whose skew never freshly reset after the stop. This uses ledger skew readings, so it covers the 61 trips only.
- It can rank arms and show cost and tail sensitivity. **It cannot accept any arm.**

**Not supported by history:**
1. **Untouched evaluation.** There are no unexposed sessions.
2. **Full portfolio and re-entry-cycle evaluation (Tier 2):**
   - What the strategy would have bought after an earlier exit never happened, because the bot was still holding. Recovering it would need `_decide_core` replayed over full-session chain snapshots that aren't assembled. Anchor state is also not reconstructable on restart days (6 of 46 dates had mid-day anchor resets).
   - Because of this, Tier 1 **understates A1's churn**: post-stop cooldown re-buys are missing from A1 and A1g alike. That asymmetry is stated next to every A1g figure.
3. **Executable quotes and real fills.** Snapshot quotes are a proxy, about 64 s apart.
4. **Fees** at a real venue. The paper server charges none.

## 4. Costs, carried through triggers and outcomes (fixed)

**Triggers** are part of each arm's frozen definition:
- The A1 stop and the A2 target are premium-only, with no fee.
- A3, A4 and A5 arm and floor on **net** profit with a trigger fee of **$0 per contract per side**. That is the fee at the venue the bot actually trades on (the paper server models no fees), so trigger net = gross there.
- A different venue fee means a **different candidate**, re-declared before use. It is never varied inside a run.

**Outcomes** are computed per fill:
- exits at the bid and entries at the ask, so the spread is always paid;
- **primary** (latency included): the fill is at the next snapshot after the decision snapshot (~64 s);
- **sensitivity:** the decision snapshot (optimistic), and the next snapshot + 1 tick (pessimistic).

**Reporting fees** are shown at $0 and at **$1.00 per contract per side**, applied to every fill including re-entries. They never change a trigger. Reporting fee sensitivity is kept separate from trigger cost in every table.

**Cash and drawdown:**
- One contract per position and unlimited cash, labelled as such; this is not the account's portfolio.
- P&L is per round trip in time order, aggregated per session.
- Drawdown is the maximum peak-to-trough of cumulative session P&L.
- The same formula applies to every arm, gross and fee-net.

## 5. Minimum material improvement (frozen before any untouched evaluation)

A candidate X (A1g, A2, A3, A4 or A5) is **materially better** than A1 only if **all** of the following hold on **untouched** eligible sessions, using the primary costs and the $1.00/side reporting fee. A1 is judged against A0 by the same rule.

1. **Expectancy:** the mean fee-net session P&L difference X − A1 ≥ **+$10 per session**, and the one-sided paired session permutation test gives p ≤ 0.10 (dependence caveat stated).
2. **Largest winners:** the difference stays ≥ 0 after removing A0's top 1, top 3 and top 5 winners. X also keeps ≥ **80%** of A1's P&L on those top-5 trips (tail retention).
3. **Drawdown:** X's fee-net drawdown is no worse than A1's by more than **10%**.
4. **Churn:** executed round trips per session are ≤ **1.5×** A1's (this matters for A5 and A1g).
5. **Robustness:** the sign of (1) holds under leave-one-session-out and under both latency sensitivities.
6. **Completeness:** no material unresolved exposure. If there is any, the result is *insufficient evidence*.

Each arm's result is reported as **materially better / not better / insufficient evidence**. Tier 1 exposed-history numbers are reported against the same criteria but are labelled exposed, and **cannot** satisfy this test.

## 6. Finite shadow plan, because history is insufficient for acceptance (proposal)

- **Window:** the first **30** eligible NYSE sessions after the freeze. The size is fixed, with no adaptive stopping. This is smaller than, and separate from, the shelved 60-session full-cycle study.
- **Method:** Tier 1 only, meaning identical entries on the live paper bot's **actual** OI Skew trips. All six arms are computed offline after each close from archived collector snapshots (read-only R2 GETs) and the runner ledger.
- **What it needs:** no runner change, broker, KV write or writer. Checkpoints every 10 sessions report data quality only, with no performance figures.
- **Evaluation:** once, at session 30, against §5.
- **Insufficient** if fewer than **20** actual round trips occur in the window. In that case the output is a data-quality report and no arm verdict.
- **Dependencies (Chej):**
  1. A post-close ledger and fill export, or read access to them.
  2. Confirmation of whether OI Skew traded from 2026-11-10 onward. If it did, those sessions are still untouched and can be the start of the window. I have not looked at them.

## 7. The next executable packet (one approval)

On approval of §1, §4 and §5 as written:

1. Implement `research/oa236_protection/d4_tier1.py`, a single script for all arms on identical entries. It gets focused tests on synthetic fixtures (no-lookahead, precedence, the latency fill index, fees never in triggers, re-entry inside the hold, A1g removal using fresh skew only).
2. Run it once on the exposed history. Report Tier 1 as feasibility and comparison evidence, with the §3 limits printed on every table.
3. Freeze the code hash. The §6 shadow window starts only on a separate go.

Rerunning `d4_inventory.py` reproduces §2 from the same private inputs (SHA-256 values are in its output).
