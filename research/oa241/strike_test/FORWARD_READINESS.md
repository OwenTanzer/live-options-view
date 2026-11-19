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
| 4 | **Spot age and basis** | A sidecar `snapshot_*.provenance.json` (`snapshot_provenance.v1`, market.py:345–370) records `constructed_at`, `archive_uploaded_at` and, under `underlying` (`underlying_price.v1`, `as_provenance`, market.py:195–207), `price_basis`, `collector_received_at`, `provider_observed_at` and `freshness`. It also carries prices: `underlying.price`, `underlying.contributing.last_price`, and the `market` and `market_readings` blocks (market.py:560–561). It was added in 4fe03b8 (#146, 2026-11-08). Older CSVs have no sidecar. A missing sidecar means unknown. The upload is non-fatal, so individual sidecars can be missing. | Verified in code |
| 5 | **Option quote age** | Bid and ask receipt times (`bid_ts`, `ask_ts`) exist in feed state (feed.py:684–694) but are **not written** to the CSV or the sidecar. Per-contract quote age is not recorded. | Verified: not recorded |
| 6 | **Gamma** | From DXLink `Greeks` events. The last value persists with **no timestamp** (feed.py:727–731). A missing value is written blank (market.py:494). Gamma age, and whether a value is stale, cannot be known from the archive. | Verified in code; gamma age not recorded |
| 7 | **OI** | From DXLink `Summary.openInterest`. The last value persists, with no OI timestamp (feed.py:696–699). It is written as `data.get("oi", 0) or 0` (market.py:485). **A missing OI is written as 0, so missing and true zero can't be told apart.** Which day's settlement the value reflects is not recorded. | Verified in code: not distinguishable |
| 8 | **Strike window** | ±`STRIKE_WINDOW = 33` strikes around the rounded spot (config.py:44, market.py:460), which gives the 67 strikes seen. | Verified in code |
| 9 | **Deployed revision** | Whether production runs a revision that includes 4fe03b8, and from which date. Sidecar presence on a forward session would show it. | Unverified |
| 10 | **OI arrival time** | OA-239 H1 found OI refreshes around 06:00–06:35 ET. Jun 23 arrived late, and on Jul 20 values went from 0 to populated after 12:00. That is from observation, not code. | Unverified for any future session |

## Rules this forces (proposed)

- **Spot:** use an anchor's snapshot only if its sidecar exists with `underlying.price_basis = bid_ask_midpoint` and `constructed_at − underlying.collector_received_at` between 0 and 5 s, at t − 15, t and t + 5. The remembered, unstamped and last-trade bases are unusable.
- **Snapshot age:** each snapshot's filename time is ≤ 120 s before its anchor time, as in the draft design.
- **Variable horizon:** with s₀ and s₅ the filename times of the snapshots used at t and t + 5, require **225 s ≤ s₅ − s₀ ≤ 375 s**. This is a **variable horizon**, not a fixed five minutes, and the outcome is not rescaled. Sensitivity: 265–335 s. For the prior move: 825 s ≤ s₀ − s₋₁₅ ≤ 975 s.
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

## Smallest separate check: metadata readiness (partial, for approval)

**Purpose.** Show, for one certified session, whether production actually captured spot provenance good enough to apply the timing rules. It establishes nothing else.

### Exact inputs (QQQ, one certified session D, nothing else)
1. **The key listing** of `intraday/D/`, keeping only:
   - CSVs matching `^intraday/D/snapshot_[0-9]{12}\.csv$`;
   - sidecars with the same stem and `.provenance.json`.
   **CSV bodies are not read.**
2. **From each sidecar, only these fields** (allowlist):
   - `schema_version`, `snapshot_key`, `symbol`, `constructed_at`, `archive_uploaded_at`, `csv.object_key`, `publication.status`;
   - `underlying.schema_version`, `underlying.price_basis`, `underlying.selection_rule`, `underlying.collector_received_at`, `underlying.provider_observed_at`, `underlying.unavailable_reason`, `underlying.freshness`.

### Price handling
- Each sidecar is parsed in memory.
- A new record is built **only** from the allowlisted paths, using explicit lookups.
- The parsed object and raw body are then discarded. They are never cached, logged, printed or written.
- **Dropped unread:** `underlying.price`, `underlying.contributing` (which holds `last_price`), `market`, `market_readings`, `market_readings_digest`, and any unlisted field.
- **Output is counts and binned age histograms only.** There is no per-snapshot row and no value field.
- **Test before approval:** on a synthetic sidecar, the output must contain no `price`, `last_price`, `market` or `readings` key and no price value.

### Per-snapshot checks (each failure counted by its first reason)
1. **Pairing:** the sidecar key equals the CSV key with `.csv` → `.provenance.json`, and that CSV is in the listing.
2. **Binding:**
   - `snapshot_key` = `csv.object_key` = the CSV key;
   - `symbol = "QQQ"`;
   - `schema_version = snapshot_provenance.v1`;
   - `underlying.schema_version = underlying_price.v1`.
3. **Clock binding:** the ET filename time and `constructed_at` (converted to ET) are on date D and within 1 s. They are two separate `now()` calls (market.py:395–396).
4. **Ages:**
   - Spot age = `constructed_at − underlying.collector_received_at`.
   - Upload lag = `archive_uploaded_at − constructed_at`.
   - Missing or unparseable → **reject**. Negative → **reject**.
   - `provider_observed_at` is only counted (present / absent / unparseable), not gated.
5. **Spot rule:** `price_basis = bid_ask_midpoint` and spot age ≤ 5 s. Other bases are counted by value.

A snapshot that passes 1–5 is **metadata-usable**.

### Per-anchor checks (74 anchors, 09:45–15:50 ET)
- At t, t + 5 and t − 15, take the latest CSV by filename time at or before that time. It must be ≤ 120 s old and metadata-usable.
- Variable horizon: 225–375 s.
- Prior interval: 825–975 s.
- An anchor that passes is labelled **metadata-pass (partial)**, never "eligible".

**Not evaluated under this scope**, and listed as such in the output:
- strike proximity and the OI/gamma completeness of in-window strikes;
- the put-call parity gate;
- the frozen-feed check;
- stream or reconnect state.

**Still unverified:** OI settlement (items 7 and 10) and gamma age (item 6).

### Decision this check can support
- **PASS:** ≥ 95% of the session's CSVs have a paired, bound, parseable sidecar, **and** ≥ 40 of the 74 anchors are metadata-pass (partial).
  - PASS supports one statement only: *production captured spot provenance on D adequate for the timing rules.*
  - It is grounds to ask Owen to approve a separate, full input check (CSV columns for strike proximity, OI, gamma, parity and frozen feed).
  - **It is not** a go to a frozen run, an eligibility count or a feasibility claim.
- **FAIL:** reports the blocking reason by count, for example:
  - missing sidecars, which points to the deployed revision (item 9) or upload failures;
  - binding failures;
  - the basis mix;
  - negative or unparseable ages;
  - stale spot;
  - horizon failures.
- **Either way,** the $0.02 dispersion gate in `OI_DESIGN.md` is untouched, and the test stays **no-go** at $0.02.
