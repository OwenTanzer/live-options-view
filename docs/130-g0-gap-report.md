# #130 G0 gap report — offline replay and acceptance state

Branch: `nova/130-multi-session`, base master `96d59b48` (post-#199 Clyde merge).
Date: 2026-11-09. Status: G0 baseline; repair in progress on an inert,
non-deploying branch. No merge, deployment, activation, or collector run.

## Baseline (before repair)

- OA-203 test suite: **153 tests, all passing** (windows, windows_outputs,
  windows_replay, return_scanner, active, active_budget).
- Implementation present (PR #134, merged 2026-10-12): pure window module
  (`market_data/options_scanner/windows.py`, 451 lines), return calculations
  (`returns.py`), offline replay (`windows_replay.py`), local-only writer
  (`windows_outputs.py`), opt-in SPY/QQQ second-chain collector
  (flag `OA203_SPY_QQQ_SECOND_EXPIRY`, default false). Nothing deployed.

## Gap 1 (BLOCKING, found by running the replay) — truncated final sweep

**Symptom:** the documented example replay
`--days 2026-10-05..09 --underlyings SPY QQQ PLTR` failed on the first day:
`RuntimeError: 2026-10-05: truncated or damaged sweep`.

**Root cause:** `validate_manifest` rejected ANY truncated sweep, but EVERY
real archive day has a truncated FINAL sweep. The scanner stops at market
close, so the last sweep is cut short (Oct 5 sweep 77: 15:59:38 → 16:00:00,
22 seconds, 50/500 chains, 5,580/51,070 rows). The archive's own assessment
still marks such days "complete" (`complete_sweeps: 76` of 77;
`sweeps_in_session: 77`). Verified on all five days Oct 5–9: each has exactly
one truncated sweep, always the final one. Oct 8 is additionally
`assessment: partial` (71 sweeps) and is correctly rejected on that ground.

**Repair (in progress):** `validate_manifest` now accepts a truncated FINAL
sweep only; a truncated non-final sweep is still rejected. `load_session`
excludes the truncated final sweep's rows from window evidence and records
the exclusion (`excluded_final_sweep`) in the session metadata and the replay
report. No coverage is invented: endpoints come from the last complete sweep,
and a contract whose close endpoint then falls outside the tolerance is
reported as `close_endpoint_missing`, not filled in.

**Tests:** `test_oa203_windows_replay` updated — a truncated non-final sweep
is still rejected; a truncated final sweep is accepted; its rows are excluded
and the exclusion is recorded. 7/7 replay tests pass.

**Real-data verification:** Oct 5/6/7 now load (66,272 / 64,600 / 64,600
rows kept for SPY/QQQ/PLTR). Oct 8 is `assessment: partial` (71 sweeps) and
is correctly refused by the existing complete-assessment check — a data
limitation, not a code gap. The Oct 5–9 weekly window therefore cannot be
replayed from complete data for that week; a full-week real-data validation
needs a week with all five days complete. Overnight windows (Mon→Tue,
Tue→Wed) are validated on Oct 5–7 instead.

**Oct 5–7 replay result (real data, SPY/QQQ/PLTR):** 5 windows over 2,280
contracts, exit 0. Overnight Oct 5→6: 111 eligible (mid/exec), 91 qualified.
Overnight Oct 6→7: 109 eligible, 89 qualified. sessions_2 Oct 5–6: 110
eligible, 95 qualified. sessions_2 Oct 6–7: 105 eligible, 90 qualified.
sessions_3 Oct 5–7: 105 eligible, 90 qualified. Weekly W41 correctly
skipped (`incomplete_calendar_week`, only 3 of 5 days supplied). Dominant
exclusion is `missing_session` (2,134/2,280) — most contracts appear on only
one day because the universe is reselected daily; this is the honest
same-contract-gap behavior, not a bug. Excluded final sweeps recorded per
day (Oct 5: 5,580 rows/50 chains; Oct 6: 16,682/150; Oct 7: 1,856/50).

## Remaining acceptance checkboxes (from #130)

All seven offline boxes are now covered. Each maps to exact existing/passing
tests in `docs/130-acceptance-matrix.md`. The matrix is the authoritative
mapping; this section is the summary.

- [x] Weekly and overnight fixtures reproduce the approved D1/D2/D3/D5
  policy, separate opening comparison, last-trading-day holiday rule,
  weekend gaps, exact contract paths and endpoint arithmetic. (Matrix Box 1:
  8 tests.)
- [x] Calendar tests cover Friday/Monday holidays, weekend overnight gaps,
  early close, DST and unavailable calendar coverage. (Matrix Box 2: 6 tests;
  unavailable coverage fails closed via
  `test_calendar_rejects_missing_session_and_wrong_bounds`.)
- [x] Expiring-before-exit, differing roots/deliverables, universe changes,
  missing session/endpoint and delayed first quote cases cannot fabricate
  a continuous hold. (Matrix Box 3: 8 tests, including the new
  `test_adjusted_deliverable_fails_closed`.)
- [x] No-look-ahead tests prove adding later observations cannot alter
  entry-time qualification; fixed endpoint and hindsight best-flip results
  remain distinctly labeled. (Matrix Box 4: 5 tests.)
- [x] Quality/unknown/negative-return cases preserve the existing qualified
  policy; full-universe ranking and legacy-output regressions pass. (Matrix
  Box 5: 6 tests.)
- [x] A documented replay produces inspectable rankings with exact evidence,
  coverage and calculation versions. (Matrix Box 6: full-universe replay
  Oct 5–7 — 14 batches covering the 661-underlying union, globally
  re-ranked, 71,710 unique contracts/window; combined output in
  `replay-130-full-combined/` with sha256 hashes; PSKY/SKYD 54-symbol
  conflict found: all 54 pairs differ in coverage evidence, so they are
  excluded fail-closed and both records preserved in `conflicts.csv`.
  Prior top-50 sample superseded.)
- [x] Monthly evidence requirements and current historical limitations are
  documented. (Matrix Box 7: monthly evidence = retained raw chronological
  paths / expiry / deliverable / coverage for possible monthly-horizon
  research, no validated monthly metric; precise Oct 5–9 receipts —
  Oct 5/6/7/9 complete, Oct 8 partial — so the inspected week Oct 5–9 is
  not a complete week; no other week checked; Oct 8 week not invented.)
- [x] D8 fixtures verify SPY/QQQ same-contract continuity across
  Friday-to-Monday expiry-selection changes, including Oct 16–19, 2026,
  within the approved two-chain limit. (Added
  `test_d8_friday_to_monday_same_contract_continuity`: the Oct 23 contract
  sampled as additional on Fri Oct 16 is the same contract sampled as
  nearest on Mon Oct 19; missing Monday sampling excludes, never fabricates.)
- [ ] Post-merge live D7 check (10- vs 15-minute close coverage) — HELD,
  deployment-dependent; prepared as an explicit held check, not a pass.

## Engineering-review gates (D6/D9/opening tolerance/continuity)

**Chej engineering review (2026-11-09) CONFIRMS these as v1 definitions for
inert/offline use only.** They are recorded in `WindowPolicy.decisions_status`
as `chej_engineering_reviewed_offline_only`. They are NOT Owen release
approval. D1-D5, D7 and bounded D8 remain owner-approved. Cheapest entry
stays separately labeled as retrospective hindsight. The 30-minute
quote-side freshness limit is unchanged.

- **D6 — rolling two/three-session windows (v1).** One window ending on every
  session; consecutive windows overlap and are labeled by their session span,
  so the overlap is explicit. Rationale: rolling maximizes coverage (every
  session gets a 2- and 3-session window); calendar-anchored blocks would
  discard most windows. The code already implements rolling and rejects
  anything else in `WindowPolicy.__post_init__`.
- **D9 — expiration-day exit allowed (v1).** A contract may exit on its
  expiration day (this is how weekly options are held), flagged
  `exit_on_expiration_day`. AM-settled roots (SPX, NDX, RUT, VIX) are excluded
  on their settlement day (no PM quotes). Rationale: excluding expiration-day
  exits would discard the most common weekly holding pattern.
- **Opening tolerance — 900 s (v1).** Open endpoint is the first valid
  observation within 15 minutes after the open, matching the owner-approved
  900 s close tolerance. Rationale: symmetric tolerances; a wider window only
  admits more valid opens, it does not cherry-pick.
- **Continuity algorithm (v1).** One OCC symbol end to end; a chain not
  sampled in a session is `missing_session`; a contract expiring before the
  exit is `expires_before_exit`; AM-settled roots on exit day are excluded;
  adjusted roots are flagged `adjusted_root_check_deliverable`. This preserves
  same-contract gaps and never stitches contracts.

**Escalation note:** none of these changes scope, cost, or metric meaning —
they confirm the already-implemented recommended defaults as the v1 policy.
Any change to them (e.g. switching D6 to anchored blocks) would be a material
metric-meaning change and is escalated to Chej, not decided here.

## Bounds preserved

- Same-contract gaps preserved (`missing_session`, never stitched).
- Two-chain SPY/QQQ bound preserved (D8: one extra expiry, SPY/QQQ only,
  flag default false; no activation).
- No archive coverage invented (truncated final sweep excluded, not filled).
- No merge, deployment, publication, activation, production config/account
  change, or collector run. Inert branch only.
