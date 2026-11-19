# OI-gamma strike test: forward-readiness checklist (paper only)

**Sources:** existing code and docs only, read at master `fcd7794`. No archive read, outcome, session reservation, collection change or PR #270 change.

**Status labels:**
- **Verified in code:** the master source establishes it. Whether production runs this revision is item 9.
- **Unverified:** the source can't establish it.
- **Proposed:** a rule for this test, not a property of the data.

## Writer facts

The writer is `market_data/live_market/market.py` `take_snapshot`, with `feed.py` and `runner.py`.

| # | Item | Finding | Status |
|---|---|---|---|
| 1 | **Snapshot filename time** | `datetime.now(ET)`, read at the *start* of `take_snapshot` (market.py:395). It is formatted `%H%M%S%f` into the key (market.py:512–513). This is collector construction time, not provider time or upload time. | Verified in code |
| 2 | **Cadence** | The loop takes a snapshot and then sleeps `SNAPSHOT_SECS = 60` (config.py:46, runner.py:322). The interval is 60 s plus the work time, with no fixed schedule. That is consistent with the ~61–68 s seen in listings. | Verified in code |
| 3 | **Spot in the CSV** | `UnderlyingPrice` = `_select_underlying_quote` (market.py:254). It is chosen in this order:<ul><li>a two-sided bid/ask midpoint, stamped with the *older* side's receipt;</li><li>a midpoint with no receipt ("unstamped");</li><li>the last trade;</li><li>a remembered, carried-forward spot.</li></ul>**The CSV does not say which one was used.** | Verified in code |
| 4 | **Spot age and basis** | A sidecar `snapshot_*.provenance.json` (`snapshot_provenance.v1`, market.py:345–370) records `constructed_at`, `archive_uploaded_at`, the spot `basis`, the collector `receipt_ts`, `provider_observed_at` and `freshness`. It was added in 4fe03b8 (#146, 2026-11-08). Older CSVs have no sidecar. A missing sidecar means unknown. The upload is non-fatal, so individual sidecars can be missing. | Verified in code |
| 5 | **Option quote age** | Bid and ask receipt times (`bid_ts`, `ask_ts`) exist in feed state (feed.py:684–694) but are **not written** to the CSV or the sidecar. Per-contract quote age is not recorded. | Verified: not recorded |
| 6 | **Gamma** | From DXLink `Greeks` events. The last value persists with **no timestamp** (feed.py:727–731). A missing value is written blank (market.py:494). Gamma age, and whether a value is stale, cannot be known from the archive. | Verified in code; gamma age not recorded |
| 7 | **OI** | From DXLink `Summary.openInterest`. The last value persists, with no OI timestamp (feed.py:696–699). It is written as `data.get("oi", 0) or 0` (market.py:485). **A missing OI is written as 0, so missing and true zero can't be told apart.** Which day's settlement the value reflects is not recorded. | Verified in code: not distinguishable |
| 8 | **Strike window** | ±`STRIKE_WINDOW = 33` strikes around the rounded spot (config.py:44, market.py:460), which gives the 67 strikes seen. | Verified in code |
| 9 | **Deployed revision** | Whether production runs a revision that includes 4fe03b8, and from which date. Sidecar presence on a forward session would show it. | Unverified |
| 10 | **OI arrival time** | OA-239 H1 found OI refreshes around 06:00–06:35 ET. Jun 23 arrived late, and on Jul 20 values went from 0 to populated after 12:00. That is from observation, not code. | Unverified for any future session |

## Rules this forces (proposed)

- **Spot:** use an anchor's snapshot only if its sidecar exists with `basis = bid_ask_midpoint` and `constructed_at − receipt_ts ≤ 5 s`, at t − 15, t and t + 5. The remembered, unstamped and last-trade bases are unusable.
- **Snapshot age:** each snapshot's filename time is ≤ 120 s before its anchor time, as in the draft design.
- **Elapsed interval:** with s₀ and s₅ the filename times of the snapshots used at t and t + 5, require **|(s₅ − s₀) − 300 s| ≤ 75 s**, which is about one cadence step. The outcome is not rescaled. Sensitivity: ±35 s. The same rule applies to t − 15 against t: |(s₀ − s₋₁₅) − 900 s| ≤ 75 s.
- **OI:** because the CSV writes missing as 0, a 0 is **missing** in the primary. A strike is eligible only if both contracts have OI > 0 in the latest snapshot ≤ 09:30. A session where any in-window contract's OI changes during regular hours is flagged. True zero cannot be recovered.
- **Gamma:** blank or ≤ 0 is missing. Staleness can't be checked (item 6). That limitation is reported, not solved.
- **Option mids** are not used for selection or outcome, so the missing quote age (item 5) affects only the parity gate, which inherits that limitation.

## Certification and overlap

- **Certifier:** Chej dates and certifies the freeze. This is the OA-236 step-4 plan v6 rule: "an untouched window can only consist of sessions after a freeze that Chej dates and certifies". **Owen approves any run.**
- **Overlap:**
  - S3c (OA-237, frozen `f99b361`) holds forward sessions from Nov 16 until 10 qualifying sessions exist for each symbol.
  - D4 has no certified freeze or length yet (v6: "untouched data: none is in hand").
  - Non-overlap therefore needs either a start date after both windows close, or Chej's ruling that one forward session may serve more than one frozen test. **Unverified** until Chej sets the D4 and S3c windows.
- **Freeze contents:** this checklist, `OI_DESIGN.md` and the code hashes, sent before the first certified session.

## Smallest separate check to approve before any run

**An input-readiness check** on the first certified session only. It is read-only and inputs-only: no spot change, distance change or other outcome is computed.

**Inputs:**
- the session's snapshot keys and sidecars, 09:28–15:56 ET;
- in each CSV, only the Strike, Type, Expiration, OpenInterest and Gamma columns.

**It reports counts:**
- sidecar present / parsed / missing;
- spot basis mix;
- the distribution of `constructed_at − receipt_ts`;
- snapshot-interval distribution and gaps over 120 s;
- per anchor, whether the t − 15 / t / t + 5 elapsed-interval rules pass;
- in-window contracts with OI = 0 at 09:30;
- blank or non-positive gamma;
- eligible anchors by failure reason.

**Go to a frozen run** only if the sidecars are present on ≥ 95% of snapshots and ≥ 40 of the 74 anchors pass every input rule. Otherwise the inputs are not ready, and the blocking item is named.

This is not a feasibility claim. The dispersion gate in `OI_DESIGN.md` still applies, and at the $0.02 threshold the test stays **no-go** until an accepted dispersion estimate exists.
