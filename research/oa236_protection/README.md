# OA-236: profit protection / re-entry (`oa236-protect-v1.1`, inactive)

This is Skew + Direction **step 3**. The policy lives in `crassus/crassus/profit_protection.py`, and its decision table is `crassus/scripts/verify_profit_protection.py` (91 checks in v1.1, run in crassus-ci). Nothing calls it: there is no runner call site, no activation switch and no provider access. The rules are declared in the module docstring; this file holds the offline evidence.

## Family

| | `protect_only` | `protect_reentry` |
|---|---|---|
| Arm | Net (after round-trip fees) ≥ **+20%** of the position's own entry basis, at an executable bid | same |
| Protect (whole contracts) | Armed and OA-229 `breakdown` while still net-profitable, **or** net ≤ **+5%** (price floor) | same |
| Intact moves | `continuation` / `pullback` / `recovery` / `no_trend` hold | same |
| Stale / unknown | No direction exit; the price floor still applies; no bid means pending | same |
| Re-entry | never | After a protection exit, only on a confirmed OA-229 `recovery` on a later bar. Each recovery event is used once. The original skew must still be valid (same sign, \|imbalance\| ≥ threshold; no new OI flip). At least 2 bars after the exit and more than 30 minutes before the close. At most 2 re-entries and fewer than 3 protection exits per session. Same contract count, new own basis. |
| Ends the chain | — | Any stop-loss (e.g. the 35% stop), EOD flatten or signal exit |

**Precedence:** EOD flatten > 35% stop > protection > strategy signal sell > re-entry. A position that isn't net-profitable belongs to the stop layer, never to this family.

## v1.1: the D3 state-machine handoff

v1.1 changes no v1 threshold or rule. It makes the integration explicit, and `verify_profit_protection.py` now has 91 checks (50 from v1, kept, plus 41 new).

**States** (`STATES`): `held` → `armed` → `flat_protected` (after this family's own exit) or `ended` (after an external exit). `TRANSITIONS` lists every row as (from, condition, to, action). The checks fail if any evaluated transition is undeclared, or if any declared row is never reached.

**Inputs** (all causal at the observation's `now`):

| Input | Contract | Not fresh or unknown |
|---|---|---|
| Direction | D1 `direction_asof` for the held side: `direction_status`, `phase` (used only when `ok`), and the bar's `event_end` | D1 not `ok` → `stale`/`unknown`. **Event time**: `event_end` after `now`, missing, or older than `max_event_age_s = 360` → `stale`/`unknown`. D1's own age is receipt age, so a late-received old bar is caught here. Stale or unknown never triggers a direction exit; the price floor still applies. |
| Skew | The bot's reading, threshold and **observation time** | A reading older than `max_skew_age_s = 360` (one 300 s cycle + 60 s), from the future, or missing is not a valid skew: re-entry is blocked, and it can't count as a signal reset. |
| Quotes | Executable bid for the held contract; executable ask for `ask_symbol` | No bid while armed → `pending`. No ask → re-entry blocked. Never a guessed price. |

**Contract selection:** `reentry_contract = "same_symbol"` is the only declared value. A re-entry buys the exited option symbol at the same contract count, from an executable ask quoted for that symbol, and records the symbol. A different strike or an unknown symbol blocks. No new contract-selection logic is introduced.

**Entry and session limits** (v1, unchanged): no re-entry within 30 minutes of the close; at least 2 bars after the exit; at most 2 re-entries; fewer than 3 protection exits per session; each recovery event (D1 `row_id`) used once.

**An unchanged signal can't undo an exit** (`strategy_entry_allowed`). After a protection exit or a 35% stop-loss exit, the strategy's own same-side buy stays blocked for the session until a **fresh** skew reading has been invalid (below threshold or opposite sign) at an observed bar since that exit, and the new signal is itself fresh and valid. A stale reading is not a reset. The opposite side is a new OI flip and is allowed. After an EOD flatten the session is over; after a signal sell the signal has already changed. D2's 15-minute cooldown still applies first, unchanged. After protection, the only same-side way back in is the confirmed-recovery path. **Review choice:** applying this guard after a D2 stop as well (not only after protection) is new behaviour proposed here, and it is inactive like the rest.

**External exits** are explicit kinds (`stop_loss`, `eod_flatten`, `signal_exit`, `replay_end`). Each ends the chain; this family never re-enters after someone else's exit.

**Precedence (unchanged):** EOD flatten > 35% stop (D2) > this protection > strategy signal sell > re-entry. Net ≤ 0 is the stop's domain.

**Replay under v1.1:** `replay.py` now passes the v1.1 inputs: `now` = bar availability, `event_end`, the skew reading's own ledger time, and the trip's symbol as the re-entry contract. The output is identical to v1 apart from provenance fields: the same 21 exits, −$1,862 (−$1,885 at one snapshot of latency), and 11 opportunity counts. No exit or opportunity depended on a stale input. The caveats below are unchanged.

## Offline replay (`replay.py`): a zero-fee, protection-only counterfactual with entries fixed

**What this is, and is not.** The replay config sets `fee_per_contract_per_side = 0`. Each row is a counterfactual: "had this protection rule closed the position at that snapshot bid instead of the actual exit", with entries held fixed. It is not fee-net realized performance, and it is not a causal profit estimate.

**Inputs:**
- The 75 actual paper round trips (Aug 12 – Nov 9).
- Held-contract snapshot bids, as in step 2.
- QQQ 5-minute bars built from the collector snapshots' spot samples. These are ranged reads of each snapshot's first row, about 360 samples per session across 33 days. The `history/QQQ/5` series stops on Aug 5.
- The unchanged OA-229 challenger, labelling each completed bar for the held side.

The policy acts only on bars that complete strictly before the actual exit. The entry reference is the last spot sample at or before the fill.

| Scenario | Armed | Protection exits | P&L change vs actual (actual total +$2,085) | Helped / hurt | Giveback avoided | Lost tail gains |
|---|---:|---:|---:|---:|---:|---:|
| Fill at the decision bid | 27 | 21 (11 breakdown, 10 floor) | **−$1,862** | 14 / 7 | +$1,243 | −$3,105 |
| One snapshot later (~64 s) | 27 | 21 | −$1,885 | 14 / 7 | +$1,199 | −$3,084 |

**Largest-winner sensitivity:** all five of the largest winners were cut.

| Trip | Actual | Protection exit | Change |
|---|---|---|---|
| #58 | +506% | +74% | −$933 |
| #63 | +298% | +4.5% (floor) | −$918 |
| #31 | +946% | +493% | −$534 |
| #75 | +338% | +206% | −$389 |
| #53 | +286% | +136% | −$295 |

Those five cost **−$3,069**. The other 16 exits net +$1,207, mostly floor exits on fading trades.

**Re-entry:** the 11 "re-entry opportunities" are **counts**, not re-entry economics. Each is a confirmed recovery with the original skew still valid inside an actual holding window, and the replay stops a trip at its first opportunity. Pricing a re-entry needs quotes for the *new* contract after an exit, plus full portfolio state, so that is left to step 4.

**Reading:** in this zero-fee counterfactual with entries fixed, protection without re-entry is **not supported** on this history. For a strategy whose results are concentrated in a few large 0DTE trends, intraday giveback protection removes more of the right tail than the giveback it avoids. The thresholds were declared before the replay and have not been tuned to it. Step 4 compares this family, with and without re-entry, against unchanged exits, stop-only, fixed take-profit and direction-free trailing, all frozen before any untouched evaluation.

**Caveats:**
- CI at f919f2d: the 50 D3 checks pass, but crassus-ci then fails at the paper-harness handshake (fixed separately in #237) and skips the downstream steps, so that workflow is not all green.
- Bar highs and lows come from about five spot samples per bar.
- Snapshot bids up to 90 s old stand in for the live 15 s quote rule.
- Availability is assumed at bar close.
- The skew used for the re-entry check is the bot's last ledger reading at or before each bar.
- The raw inputs are private and stay out of git.

## Reproduce

```sh
python research/oa236_protection/replay.py \
  --trips crassus-ledger/chej-export/analysis/out/round_trips.csv \
  --quotes crassus-ledger/premium-stop/quote_paths.json \
  --underlying crassus-ledger/premium-stop/underlying_paths.json \
  --ledger crassus-ledger/chej-export/oiskew_decisions_2026-08-31_to_09-02.jsonl \
  --ledger crassus-ledger/chej-export/oiskew_decisions_railway_2026-09-03_to_11-09.jsonl \
  --dml path/to/directional-move-lab@500a36e --out /tmp/oa236
```
