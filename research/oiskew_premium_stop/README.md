# OI Skew: 35% entry-premium stop (inactive) + offline replay

Skew + Direction **step 2**. The selected stop is a **full-position exit at the first eligible executable bid at or below 65% of the position's own entry basis, including each re-entry**. It is implemented in the existing, inert exit layer (`crassus/crassus/exits.py`, `CRASSUS_EXITS_ENABLED` unset) and replayed offline over OI Skew's actual paper round trips. Nothing is activated.

## Code (inactive)

The OI Skew policy is a per-account `exits` block. It changes nothing until an operator sets both the block and `CRASSUS_EXITS_ENABLED`:

```json
{"alias": "OI Skew", "strategy_id": "oi_skew_qqq", "params": {"exits": {"stop_loss_pct": -0.35}}}
```

`profit_target_pct` stays `null` (the approved default) and the re-entry cooldown stays at 15 minutes.

**What changed in `exits.py` (version 1.1.0 → 1.2.0):**

- **Exact boundary.** The thresholds are now decided in exact decimal arithmetic (`_threshold_reached`). With the previous binary-float comparison, a bid of exactly 65% of a cent basis missed a −35% stop in **16 of the 100** exact-cent cases up to $20.00. For example, basis 1.40 with bid 0.91 computes to −0.3499999999999999. The spec says "at or below", so this is a correctness fix. It also applies to the −50% default and to targets, and is equally inert.
- **Audit field.** Every exit row now records `exit_threshold_price` (for example 0.65 on a 1.00 basis), so overshoot past the level is auditable.

**Tests:** `scripts/verify_exits.py` has 60 tests, 9 of them new (`PremiumStop35Tests`):

- an exhaustive cent grid at the 65% level, including the float-trap case;
- one cent above the level holds;
- re-entry uses its own basis, both cheaper and dearer than the closed lot;
- the exit is the full position;
- overshoot is recorded;
- stale or missing quotes wait for the first executable bid;
- the OI Skew config is inert until activation;
- the default boundaries are unchanged.

Restoring the old float comparison fails the grid test.

## Boundaries and reasons

| Situation | Behaviour | Recorded reason/metadata |
|---|---|---|
| Executable bid ≤ 65% of the position's own basis | Sell the full held quantity | `exit_trigger=protective_stop`, `exit_cost_basis`, `exit_threshold_price`, `exit_liquidation_price`, `exit_pnl_pct` |
| Bid exactly at 65% | Fires (inclusive, exact) | as above |
| Bid one cent above | Holds | — |
| Triggered, but the quote is older than 15 s (Worker rule) | No exit; new entries blocked this cycle; retried next cycle | `exit_pending: stale_quote` |
| No quote, or no bid side | Same: wait for the first executable bid | `exit_pending: missing_liquidation_quote` |
| Re-entry after a flat position | `Book` resets the basis on flat, so the new lot's own fill is the basis | — |
| Adding to an open position | Average cost of the open lot (OI Skew holds one contract, so this does not arise today) | — |
| Fees | Not in the trigger. The trigger is premium-only; fees are reported separately. | — |

**Precedence, existing and unchanged:**

1. EOD flatten.
2. **Protective stop** (this).
3. Profit target (off).
4. Strategy signal sell.
5. 15-minute re-entry cooldown on the stopped contract.

The stop runs before the strategy. When the stop and an OI Skew signal sell trigger on the same cycle, the stop is the recorded exit, at the same full quantity and quote. The stop is evaluated only at runner cycles, so a dip below 65% between cycles that recovers by the next cycle does not fire.

## Offline replay (`simulate.py`, entries held fixed)

- **Trips:** 75 actual OI Skew round trips, Aug 12 – Nov 9 (`round_trips.csv` from the ledger export).
- **Quotes:** the held contract's observed Bid/Ask from the collector's per-minute chain snapshots: 6,054 snapshots, read-only R2 GETs.
- **Cycles:** the account's actual runner cycles from the decision ledger, or synthetic 300 s cycles before the ledger began on Aug 31.
- **Decision:** made by the production `exits.evaluate`.

Missing or absent snapshots are pending cycles (74, all on trip 75). The snapshot quote age is a **proxy**: ≤ 90 s against the live 15 s rule.

| Scenario | Stop fired | P&L change vs actual (actual total +$2,085) | Helped / hurt | Median overshoot (% of basis) |
|---|---:|---:|---:|---:|
| Fill at decision bid | 14 / 75 | **+$558** | 12 / 2 | 5.3% (max 22.2%) |
| 1-tick slippage | 14 | +$544 | 12 / 2 | 5.9% |
| 1-snapshot latency (~64 s) | 14 | +$478 | 9 / 5 | 12.3% |
| Latency + 1 tick | 14 | +$464 | 9 / 5 | 13.0% (max 25%) |

- **Tail-loss concentration:** three disaster trips (−98%, −92%, −76%) supply $516 of the +$558. Without them the stop nets **+$42**.
- **Winners cut short:** one. Trip 26 was stopped at −40% and later recovered to +13% by the EOD flatten (−$85).
- **Largest winners** (+946%, +506%, +338%, +298%, +286%): none was stopped.
- **Losers ≤ −35% that did not fire (11):** every one breached the level only at its actual exit cycle, so a stop would have produced the same outcome there.
- **Fees:** zero in paper. The stop replaces an exit rather than adding one, so fees are unchanged per trip.

**Reading:** with entries held fixed, the stop truncates the tail of large losses and costs little elsewhere. This is not yet a portfolio result. What the bot would do after a stop (re-entry after the cooldown, a different strike), and the comparison against the other exit families, belong to step 4 on untouched data.

## Reproduce

The inputs are private and stay out of git:

```sh
python research/oiskew_premium_stop/simulate.py \
  --trips crassus-ledger/chej-export/analysis/out/round_trips.csv \
  --quotes crassus-ledger/premium-stop/quote_paths.json \
  --ledger crassus-ledger/chej-export/oiskew_decisions_2026-08-31_to_09-02.jsonl \
  --ledger crassus-ledger/chej-export/oiskew_decisions_railway_2026-09-03_to_11-09.jsonl \
  --out /tmp/stop35
```

`quote_paths.json` comes from a read-only fetcher: GET/LIST on `intraday/YYYYMMDD/snapshot_*.csv`, entry − 3 min to exit + 3 min.
