# OA-236: profit protection / re-entry (`oa236-protect-v1`, inactive)

This is Skew + Direction **step 3**. The policy lives in `crassus/crassus/profit_protection.py`, and its decision table is `crassus/scripts/verify_profit_protection.py` (50 checks, run in crassus-ci). Nothing calls it: there is no runner call site, no activation switch and no provider access. The rules are declared in the module docstring; this file holds the offline evidence.

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
