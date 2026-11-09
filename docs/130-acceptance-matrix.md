# #130 offline acceptance matrix (A1/A2 end state)

Branch `nova/130-multi-session`, base master `96d59b48`. Inert, non-deploying.
Local head `6481fd2`; MSI single commit `432ae6e3`. Identical file contents,
verified by blob hash. No push, no PR, no deployment, no collector runs.

Chej engineering review (2026-11-09) confirms the v1 definitions below for
inert/offline use only. They are NOT Owen release approval. D1-D5, D7 and
bounded D8 remain owner-approved. Cheapest entry stays separately labeled as
retrospective hindsight. The 30-minute quote-side freshness limit is unchanged.

## Chej engineering-reviewed v1 definitions (offline/inert only)

- **D6 — rolling two/three-session windows (v1).** One window ending on every
  session; consecutive windows overlap and are labeled by their session span.
  `WindowPolicy.multi_session_mode` is `"rolling"` and `__post_init__`
  rejects anything else.
- **D9 — expiration-day exit allowed (v1).** A contract may exit on its
  expiration day, flagged `exit_on_expiration_day`. AM-settled roots (SPX,
  NDX, RUT, VIX) are excluded on their settlement day (no PM quotes).
- **Opening tolerance — 900 s (v1).** Open endpoint is the first valid
  observation within 15 minutes after the open, matching the owner-approved
  900 s close tolerance (D7).
- **Continuity algorithm (v1).** One OCC symbol end to end;
  `missing_session` / `expires_before_exit` / `am_settled_on_exit_day`
  exclusions; adjusted roots flagged. **Adjusted/unknown deliverables fail
  closed**: `adjusted_deliverable_unknown` excludes the contract (raw
  observations preserved in the row with the reason). `contract_size` is
  compared but does not establish the economic deliverable.

## Acceptance checkboxes → exact tests (all passing)

Suite: OA-203, **162 tests, OK** (local and MSI; 1 skipped on MSI).
Run: `~/workspace/.tools/182-venv/bin/python -m unittest
tests.test_oa203_windows tests.test_oa203_windows_outputs
tests.test_oa203_windows_replay tests.test_oa203_return_scanner
tests.test_oa203_active tests.test_oa203_active_budget`
from the worktree root. Chej independently ran windows+replay: 33 passed /
15 subtests (prior round).

### Box 1 — Weekly and overnight fixtures reproduce the approved policy
(D1/D2/D3/D5, separate opening comparison, last-trading-day holiday rule,
weekend gaps, exact contract paths, endpoint arithmetic)
- `test_weekly_windows_normal_week` — D1/D2/D3 weekly construction.
- `test_friday_and_monday_holidays` — last-trading-day holiday rule.
- `test_short_week_is_skipped_not_shortened` — a short week is skipped,
  never shortened.
- `test_overnight_includes_weekend_gap_by_policy` — weekend gaps included
  by policy, excludable by flag.
- `test_cheapest_entry_to_friday_close` — D1 cheapest qualifying sampled
  ask as weekly entry.
- `test_fixed_and_best_exit_variants_stay_separate` — opening comparison
  (`weekly_fixed`) and hindsight best-exit (`weekly_cheapest_best_exit`)
  remain distinctly labeled; cheapest entry is retrospective hindsight.
- `test_close_endpoint_tolerance_and_no_interpolation` — endpoint
  arithmetic; a crossed quote is skipped, never blended.
- `test_15_minute_close_latest_qualifying_and_quote_side_age` — D7
  900 s close tolerance; 30-min (1800 s) quote-side freshness.

### Box 2 — Calendar tests (Friday/Monday holidays, weekend overnight gaps,
early close, DST, unavailable calendar coverage)
- `test_friday_and_monday_holidays`
- `test_overnight_includes_weekend_gap_by_policy`
- `test_early_close_endpoint`
- `test_dst_boundary_uses_each_sessions_own_open`
- `test_short_week_is_skipped_not_shortened`
- `test_calendar_rejects_missing_session_and_wrong_bounds`
  (replay) — a missing session or wrong session bounds raises; an
  incomplete week does not claim to be a full week. Unavailable calendar
  coverage fails closed, never fabricates a week.

### Box 3 — Expiring-before-exit, differing roots/deliverables, universe
changes, missing session/endpoint and delayed first quote cannot fabricate
a continuous hold
- `test_expires_before_exit_and_missing_session` — expires-before-exit and
  missing-session exclusions; `pct` is None when excluded.
- `test_expiration_day_rules` — AM-settled exclusion on exit day; PM-settled
  weekly roots allowed; expiration-day exit disallowed by policy flag.
- `test_adjusted_deliverable_fails_closed` — **new this round.** An
  adjusted root (e.g. XYZ1) is EXCLUDED (`adjusted_deliverable_unknown`),
  not merely flagged; raw observations preserved in the row. A standard
  root with the same shape is unaffected (eligible).
- `test_missing_observation_time_cannot_form_an_endpoint`
- `test_delayed_first_quote_makes_fixed_entry_missing` — delayed first
  quote makes the fixed entry missing, never interpolated.
- `test_future_or_missing_quote_times_never_enter_any_basis`
- `test_stale_is_distinct_from_impossible_future_evidence`
- Universe changes are the `missing_session` mechanism: a contract whose
  chain was not sampled on a session (it left the universe that day) is
  excluded with `missing_session:<day>`, never fabricated from partial data
  (`test_expires_before_exit_and_missing_session`).

### Box 4 — No-look-ahead (adding later observations cannot alter entry-time
qualification; fixed endpoint and hindsight best-flip distinctly labeled)
- `test_no_look_ahead_on_entry`
- `test_fixed_and_best_exit_variants_stay_separate`
- `test_overnight_rejects_future_entry_and_exit_sides`
- `test_future_or_missing_quote_times_never_enter_any_basis`
- `test_close_endpoint_tolerance_and_no_interpolation` — a later crossed
  quote never changes the exit.

### Box 5 — Quality/unknown/negative-return cases preserve the qualified
policy; full-universe ranking and legacy-output regressions pass
- `test_qualified_entry_skips_wide_spread` — qualified entry skips a wide
  spread.
- `test_full_universe_keeps_negative_and_excluded` — ranking keeps the
  full universe, including zero/negative returns and excluded contracts;
  deterministic tie-break.
- `test_shortlist_is_cut_after_full_ranking` (windows outputs) — the
  shortlist is cut only after the full ranking.
- `test_every_path_written_including_non_winners` (windows outputs) —
  every path is written, including non-winners; losses and exclusions
  are retained.
- **Legacy daily regression — actual legacy daily outputs, not the new
  windows writer.** The windows v1 work must not change the legacy daily
  scanner's outputs. These tests exercise the real legacy daily code
  (`market_data/options_scanner/returns.py` `contract_metrics` /
  `qualified_metrics` and `outputs.py` `build_contract_rows` /
  `write_outputs`):
  - `tests/test_oa203_return_scanner.py` (94 tests) — legacy return
    calculations, ranking and the qualified policy, including
    `test_qualified_winner_outside_legacy_shortlist`, which calls the
    actual `write_outputs` and verifies the real legacy daily files
    (`leaderboard.csv`, `qualified_ask_bid_v1.csv`) contain the right
    rows.
  - `tests/test_oa203_active.py` (14 tests) — `build_contract_rows` on
    the legacy daily archive (early/late cutoff, full-day rows, ranking
    stability).
  - The windows writer tests (`test_layout_and_summary`, `test_states`,
    `test_deterministic_content` in `test_oa203_windows_outputs.py`)
    cover the NEW windows writer layout only; they are not the legacy
    daily regression.
- `test_stale_is_distinct_from_impossible_future_evidence` — unknown vs
  stale are distinct; neither fabricates a result.

### Box 6 — A documented replay produces inspectable rankings with exact
evidence, coverage and calculation versions
- **Full-universe replay, Oct 5–7 2026.** The top-50 run was a sample;
  MAX_UNDERLYINGS=50 is a per-run bound, not a rescope. The full universe
  is the union of all selected archived underlyings for Oct 5–7:
  **661 underlyings** (500 selected per day; the selection changes daily).
  Run as **14 bounded batches** of at most 50 underlyings
  (`run-full-universe-batches.py`, the committed cross-platform runner;
  `run-full-universe-batches.sh` is the same runner for bash), each a
  standard replay with `--cache-dir` reusing the source blobs (no
  repeated downloads). All contract rows were concatenated per window,
  **globally re-ranked before any shortlist** (`combine-full-universe.py`,
  same ranking as `windows.rank_window`: qualified pct descending,
  symbol ascending for ties), losses and exclusions retained in every row.
- **Results (71,710 unique contracts per window; 71,818 raw rows, minus
  54 conflicting symbols excluded fail-closed, see below):**
  - overnight Oct 5→6: 17,488 eligible / 54,222 excluded.
  - overnight Oct 6→7: 18,208 eligible / 53,502 excluded.
  - sessions_2 Oct 5→6: 17,683 eligible / 54,027 excluded.
  - sessions_2 Oct 6→7: 17,880 eligible / 53,830 excluded.
  - sessions_3 Oct 5→7: 16,077 eligible / 55,633 excluded.
  - Weekly W41 correctly skipped (incomplete week). Dominant exclusions:
    `missing_session` (universe reselected daily) and
    `expires_before_exit`. Excluded final sweeps recorded per day
    (Oct 5: 5,580 rows/50 chains; Oct 6: 16,682/150; Oct 7: 1,856/50).
  - Global top ranks (qualified basis) differ from the top-50 sample, as
    expected: overnight Oct 5→6 rank 1 is `CEG261009C00280000` (pct 15.87);
    sessions_3 rank 1 is `VST261009C00160000` (pct 13.38). The full
    universe surfaces names the top-50 sample did not contain.
  - Policy: `oa203-windows-v1`,
    `D1-D5_D7_D8_approved; D6_D9_opening_tolerance_continuity_chej_engineering_reviewed_offline_only`.
  - Calendar: NYSE, pandas_market_calendars 5.6.0.
- **No duplicate / no missing assertion (fail closed).** Before any
  combination, `combine-full-universe.py` validates: the exact planned
  batch membership (batch-01..batch-14, no missing, no unexpected);
  the disjoint union of the planned 661 underlyings (no underlying in
  two batches, union exactly the planned set); and that every batch's
  `source_lock.json` has the same (key, sha256) set. A missing,
  unexpected or duplicate batch, a non-disjoint or incomplete union, or
  an inconsistent source lock FAILS the run (exit non-zero) instead of
  producing a partial result. It then asserts each window's rows are
  fully accounted for with **two separate accountings**: raw-row
  accounting (combined rows + removed duplicate rows + conflicting
  attributions == the sum of the 14 batch summaries, 71,818) and
  distinct-symbol accounting (kept symbols + conflicting symbols ==
  distinct symbols). Removed duplicate rows are counted as
  `len(group)-1` per pure-duplicate group (so 3+ identical copies
  remove 2+ rows), and `kept` already contains one entry per
  pure-duplicate group, so the duplicate-group count is NOT added to
  the distinct-symbol accounting (a defect in the earlier accounting
  double-counted pure-duplicate groups and raised AssertionError on
  byte-identical duplicates; fixed and covered by
  `test_identical_duplicate_dedups_and_metadata_or_flags_conflict`).
  **Data-quality finding: 54 OCC symbols are attributed to two
  underlyings in the source archive — PSKY and SKYD share 54 SKYD-rooted
  symbols** (the same contract appears under both the PSKY chain and the
  SKYD chain). This is the only duplicate pattern. **All 54 pairs differ
  beyond attribution in every window** (verified: 54/54 differ in
  `reasons` in each of the 5 windows; status and values agree, but the
  coverage evidence differs — e.g. `SKYD261009P00004000` overnight
  Oct 6→7: PSKY reason `missing_session:2026-10-07`, SKYD reason
  `missing_session:2026-10-06`). **Conflict semantics (fail closed):**
  a symbol whose attributions differ in evidence is EXCLUDED from the
  combined ranked output — it is not ranked, not eligible, and no
  canonical economic identity is asserted from root spelling; its paths
  are NOT stitched. Evidence for the comparison includes the identity
  fields (`option_type`, `strike`, `expiration`) and the quality fields
  (`flags`, `abs_change_per_contract`) as well as status, reasons,
  values and sessions, so a metadata-only or flags-only difference is
  a conflict, not a silent selection. Both provenance/coverage records
  are preserved in `conflicts.csv` (108 rows per window: 2 attributions
  × 54 symbols; 540 rows total), with the complete record including
  `abs_change_per_contract`. The 54 conflicting symbols are absent from
  all five combined CSVs (verified: 0 rows for the example symbol in
  each). Eligible counts are unchanged by the exclusion (all 54 were
  excluded in both attributions). Regression tests:
  `test_conflicting_duplicates_fail_closed_with_both_records`,
  `test_identical_duplicate_dedups_and_metadata_or_flags_conflict`
  (byte-identical dedup, 3+ copies, metadata-only and flags-only
  conflicts) and `test_missing_or_inconsistent_batch_fails`.
- **Hashes (sha256, `replay-130-full-combined/`):**
  - `full_universe_summary.json`: `c70253f79aacd5e6e54ad63b3241506cc1370f8debce63d7574489939ba1fb6e`
  - `conflicts.csv`: `c5942a099b42d5400e9a5d82030bc9016dca65a83158c9bdc2c7d7d2824ba89d`
    (changed from round 4 because the receipt now includes
    `abs_change_per_contract`)
  - `overnight-2026-10-05_2026-10-06.csv`: `0500e7d439044dfd710bc119657fb7e3bb662db9a22ce1a351f16b1afe24fa36`
  - `overnight-2026-10-06_2026-10-07.csv`: `de08d1446bcb8eab870b31e08e2907f446cd46e4cbf8357d5fd5fa26c6f6ebdc`
  - `sessions2-2026-10-05_2026-10-06.csv`: `8b07d0f8aa0d027327f1159a649f1925c66ec8226ca99a16c24138df7aa95e96`
  - `sessions2-2026-10-06_2026-10-07.csv`: `08b5fc01b192210238e9b027062cd516e0425195700ea13e737bee9c97857996`
  - `sessions3-2026-10-05_2026-10-07.csv`: `4f92b5e11f422448bb92a3450a8e4cf2cf2153a175ab06629e51d1fbf7d5fd43`
  - The five combined CSV hashes are **unchanged from round 4** (the
    accounting and evidence-field fixes do not change the real ranking);
    only `conflicts.csv` and the summary changed (new receipt column and
    renamed summary fields).
- **Source lock:** each batch's `source_lock.json` records every fetched
  blob with its SHA-256 (237 source blobs per batch: 228 sweeps — 76 per
  day, the truncated final sweep per day is not fetched — plus
  summary/universe/manifest per day). The cache (`replay-cache/`,
  329MB) holds 240 files (the same 237 plus the 3 truncated final
  sweeps, which the replay skips); a cached blob whose hash disagrees
  with the lock is rejected, not trusted
  (`test_cache_reuses_blob_and_verifies_hash`).
- **Artifacts (directly accessible, not a private /tmp path).** The
  combined outputs, the conflict report and the summary are on the MSI
  worktree (`live-options-view-130/replay-130-full-combined/`, committed
  summary + hashes; the 40MB CSVs are on the MSI filesystem in the
  worktree). The batch runner, combine script and batch lists are
  committed in the worktree (`run-full-universe-batches.py`,
  `run-full-universe-batches.sh`, `combine-full-universe.py`,
  `replay-130-batches.json`, `replay-130-union.json`). The local copies
  are in the worktree (`replay-130-full/`, `replay-130-full-combined/`).
  Prior top-50 sample in `/tmp/replay-130-c` (5 windows over 15,536
  contracts) is superseded by the full-universe result.
- Replay tests: `test_extra_rows_require_successful_manifest_and_exact_count`,
  `test_sweep_bounds_inclusive_session_close_exclusive`,
  `test_row_after_matched_sweep_end_is_rejected`,
  `test_occ_identity_rejects_stitched_metadata`,
  `test_manifest_cutoff_requires_complete_contiguous_sweeps`,
  `test_truncated_final_sweep_rows_excluded`,
  `test_calendar_rejects_missing_session_and_wrong_bounds`,
  plus the `OutputSchema` tests (ranked CSV schema, per-basis
  summary, cache reuse + hash verification, conflicting duplicates
  fail-closed with both records preserved, identical dedup with 3+
  copies and metadata/flags-only conflicts, missing/inconsistent batch
  fails).
- Each replay report records `policy` (version `oa203-windows-v1` and
  `decisions_status`), `window` (id, kind, sessions), `sources`
  (R2 prefix, manifest sha), and `excluded_final_sweep` rows/chains.
- **MSI replay limitation (environment, not code):** The replay command
  is correct, but on the MSI the replay's HTTPS fetch fails with
  `SSL: CERTIFICATE_VERIFY_FAILED: certificate has expired` from the
  MSI's Python 3.14 `urllib`. The R2 certificate is valid: PowerShell
  `Invoke-WebRequest` on the same MSI returns HTTP 200, and the local
  Python replay succeeds. The MSI's Python CA bundle is stale/mismatched
  (certifi 2026.05.20 is installed; upgrading it did not resolve it).
  SSL verification is NOT bypassed. **Workaround used:** PowerShell
  verified HTTPS can download source blobs into a local cache without
  disabling TLS and without global CA changes — the MSI run therefore
  uses a pre-populated cache (`replay-cache/`, copied to the MSI) so the
  replay reads blobs from disk. The MSI test suite (159 tests) passes,
  validating the code on the MSI. Fixing the MSI's Python CA store is an
  environment task, not a #130 code change.
- **Limitation (precise):** a single replay run caps at 50 underlyings
  (`MAX_UNDERLYINGS=50`) and 10 sessions. The full-universe result is
  produced by the 14 bounded batches plus the global re-rank, not by
  rescoping the bound. No coverage is invented: Oct 8 is
  `assessment: partial` and is correctly refused.

### Box 7 — Monthly evidence requirements and current historical limitations
documented
- **Monthly evidence means retained raw chronological paths / expiry /
  deliverable / coverage, for possible monthly-horizon research.** It is
  NOT rerunning the weekly checks once a month, and there is NO validated
  monthly metric. The monthly evidence is the retained record that makes
  a monthly-horizon question answerable later: the raw chronological
  observation paths per contract, each contract's expiry and deliverable
  (root / adjusted-deliverable status), and the per-day coverage
  (which sessions were complete, which sweeps were truncated/excluded,
  which contracts were sampled). The replay's source lock and report
  hashes are the retention receipts. No monthly return, win rate or
  ranking is claimed or validated by this work.
- **Current historical limitations (precise, restricted to the inspected
  date range Oct 5–9, 2026):**
  - Inspected receipts (each day's `summary.json` fetched from the R2
    origin, HTTP 200):
    - Oct 5: `assessment.status` `complete`, 76 complete sweeps / 77 in
      session, universe_coverage 1.0.
    - Oct 6: `complete`, 76/77, coverage 1.0.
    - Oct 7: `complete`, 76/77, coverage 1.0.
    - Oct 8: `partial`, 70 complete sweeps / 71 in session, reasons
      `['sweep_gaps:2']`, gaps_ms [699398, 842321], coverage 1.0.
    - Oct 9: `complete`, 76/77, coverage 1.0. (Oct 9's files exist and
      are complete; an earlier draft wrongly said Oct 9 was not checked.
      Corrected here.)
  - The inspected calendar week Oct 5–9 (Mon–Fri) is therefore NOT a
    complete week, because Oct 8 is partial. A real full-week replay for
    that week is not possible without inventing Oct 8's missing sweeps,
    so it is not done. **The incomplete Oct 8 week is not invented.**
  - No other week was checked. No claim is made about any other week's
    completeness, and no broad archive census was performed.
  - The weekly window is therefore validated only on deterministic
    fixtures, not on a real complete week. A real complete-week replay
    is a held check pending an archive week with all five sessions
    `assessment: complete`.
  - Deterministic full-week/holiday fixtures (Box 1/2 tests above) cover
    the weekly construction, holiday rule, weekend gaps, early close and
    DST.
  - The replay's `MAX_UNDERLYINGS=50` bound (Box 6) limits a single run
    to 50 underlyings; the full-universe result is produced by bounded
    batches (see Box 6) rather than by rescoping the bound.

### Box 8 — D8 fixtures (checked, prior round)
- `test_d8_friday_to_monday_same_contract_continuity` — the Oct 23
  contract sampled as additional on Fri Oct 16 is the same contract
  sampled as nearest on Mon Oct 19; missing Monday sampling excludes,
  never fabricates. Within the approved two-chain limit.

### Box 9 — Post-merge live D7 check — HELD, deployment-dependent
- Prepared as an explicit held check, not a pass. Requires a deployed
  collector run; no deployment is authorized on this branch.

## Stable window/run/contract contract (for #131)

The on-demand lookup (#131) consumes the same contract the replay writes.
The contract is stable across the offline replay and the future lookup.
**Status and returns are basis-specific** (mid / exec / qualified), not a
single global verdict. A row can be eligible on `mid`/`exec` while
excluded on `qualified` (e.g. a wide spread), and the top-ranked contract
differs by basis. The earlier draft's claim that "a row is eligible only
if every basis passes" was wrong and is corrected here.

- **Window:** `window_id` (`<kind>-<entry_day>_<exit_day>`), `kind`
  (`weekly_cheapest`, `weekly_fixed`, `weekly_cheapest_best_exit`,
  `overnight`, `sessions_2`, `sessions_3`), `sessions` (ordered list of
  `{day, open_ms, close_ms, early_close}`), `policy` (version
  `oa203-windows-v1`, `decisions_status`, `close_tolerance_s=900`,
  `qualified_endpoint_policy.max_quote_age_s=1800`).
- **Run (`replay_summary.json`):** `replay`, `issue`, `read_only`,
  `calendar` (name/package/version), `policy`, `sessions` (per day:
  `sweeps`, `rows_kept`, `final`, `assessment`, `excluded_final_sweep`,
  `universe_selection_version`, `occ_sha256`), `underlyings` (count),
  `contracts` (count), `skipped_weeks`, `label`, `windows` (per window:
  `window_id`, `kind`, `sessions`, `evaluated`, and per-basis
  `mid`/`exec`/`qualified` each with `eligible`, `exclusion_reasons`,
  `top`).
- **Contract (per row, replay CSV `<window_id>.csv`):** the CSV carries
  the **qualified basis's** values. Columns:
  `rank, symbol, underlying, option_type, strike, expiration, pct, entry,
  exit, abs_change_per_contract, entry_session, exit_session, entry_ms,
  exit_ms, flags, status, reasons`. `rank` is the qualified rank
  (empty when the row is excluded on the qualified basis); `status` is
  the qualified status (`eligible`/`excluded`); `pct`/`entry`/`exit` are
  the qualified values. Every ranked row is written, eligible or
  excluded, so losses and exclusions are retained. No row is deleted.
- **Per-basis values** live in `replay_summary.json` (each basis's
  `eligible` count, `exclusion_reasons` and `top` list) and in
  `paths.csv.gz` (per-basis columns `mid_status`, `mid_pct`,
  `exec_status`, `exec_pct`, `qualified_status`, `qualified_pct`, …).
- **Real output sample** (top-50 replay, `overnight-2026-10-05_2026-10-06.csv`,
  rank 1, qualified basis):
  `rank=1, symbol=XLU261009C00040500, underlying=XLU, option_type=call,
  strike=40.5, expiration=2026-10-09, pct=2.2, entry=0.15, exit=0.48,
  abs_change_per_contract=33.0, entry_session=2026-10-05,
  exit_session=2026-10-06, entry_ms=1791229787876,
  exit_ms=1791293734798, flags=, status=eligible, reasons=`.
  The same window's per-basis eligible counts differ:
  mid=2,510, exec=2,510, qualified=2,019 — proving the bases are
  distinct. The mid top-ranked contract is `NBIS261009P00155000`
  (pct 33.33), the exec top is `XLU261009C00041000` (pct 0.83), and the
  qualified top is `XLU261009C00040500` (pct 2.2).
- **Schema assertions** (tests, `tests/test_oa203_windows_replay.py`,
  class `OutputSchema`):
  `test_ranked_csv_schema_is_qualified_basis_and_retains_all_rows`
  (columns match `RANKED_CSV_COLUMNS`; all rows retained; a wide-spread
  contract is excluded on qualified but eligible on mid/exec; qualified
  rank is 1 only for qualified-eligible rows),
  `test_summary_schema_is_per_basis` (per-basis rank keys
  `mid_rank`/`exec_rank`/`qualified_rank`), and
  `test_cache_reuses_blob_and_verifies_hash` (cached blob reuse and
  lock verification).
- The lookup (#131) reads the same `window_id`/`symbol`/`expiration`/
  `strike` identity and the same policy version; it does not re-derive
  windows. It must name the basis it consumes (`qualified` for the
  CSV; `mid`/`exec` from the summary or paths file).

## Legacy-output regression and no-look-ahead evidence

- **Legacy daily regression — actual legacy daily outputs.** The windows
  v1 work must not change the legacy daily scanner's outputs:
  `tests/test_oa203_return_scanner.py` (94 tests) covers the legacy
  return calculations and ranking, including
  `test_qualified_winner_outside_legacy_shortlist`, which calls the
  actual `write_outputs` and verifies the real legacy daily files
  (`leaderboard.csv`, `qualified_ask_bid_v1.csv`). `tests/test_oa203_active.py`
  (14 tests) covers `build_contract_rows` on the legacy daily archive.
  The windows writer tests (`test_layout_and_summary`,
  `test_every_path_written_including_non_winners`,
  `test_shortlist_is_cut_after_full_ranking`, `test_states`,
  `test_deterministic_content` in `test_oa203_windows_outputs.py`) cover
  the NEW windows writer layout only; they are not the legacy daily
  regression.
- No-look-ahead: `test_no_look_ahead_on_entry`,
  `test_fixed_and_best_exit_variants_stay_separate`,
  `test_overnight_rejects_future_entry_and_exit_sides`,
  `test_future_or_missing_quote_times_never_enter_any_basis`.

## Monthly limits and held D7 release protocol

- **Monthly limits:** the offline replay is bounded to 10 sessions and
  50 underlyings per run; the collector is not run; no production config
  or account changes; no deployment, publication or activation. Monthly
  evidence is the retained raw chronological paths / expiry / deliverable
  / coverage (Box 7), not a rerun of weekly checks; there is no validated
  monthly metric.
- **Held D7 release protocol:** the post-merge live D7 check (10- vs
  15-minute close coverage) is HELD. It requires a deployed collector run
  and is prepared as an explicit held check, not a pass. No merge to
  master/main, no deployment, no publication, no activation on this
  branch. When a release is authorized separately, the D7 check runs on
  the deployed collector and its result is recorded before any release
  claim.

## Evidence paths (this round)

- Worktree: `~/workspace/goals/make-live-options-view-readable/work-130`
  (local) and the MSI worktree `live-options-view-130`.
- Gap report: `docs/130-g0-gap-report.md`.
- This matrix: `docs/130-acceptance-matrix.md`.
- Full-universe combined output: `replay-130-full-combined/` (5 CSVs +
  `full_universe_summary.json`, 40MB) — in the worktree and on the MSI
  worktree filesystem. Per-batch outputs: `replay-130-full/batch-*/`
  (14 batches, each with 5 CSVs + `replay_summary.json` +
  `source_lock.json`). Source cache: `replay-cache/` (329MB, 237 blobs).
  All three are gitignored (data, not source); the runner, combine
  script and batch lists are committed (`run-full-universe-batches.py`,
  `run-full-universe-batches.sh`, `combine-full-universe.py`,
  `replay-130-batches.json`, `replay-130-union.json`).
- The `full_universe_summary.json` is committed in the MSI worktree so
  its hashes are inspectable without the 40MB CSVs.
- Source lock / report / output hashes and the exact MSI replay commands
  are recorded in the replay report and in the return message.

## Exact replay command (MSI, for independent replay)

Run from the MSI worktree
`C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130`:

```powershell
C:\Python314\python.exe -m unittest tests.test_oa203_windows tests.test_oa203_windows_outputs tests.test_oa203_windows_replay tests.test_oa203_return_scanner tests.test_oa203_active tests.test_oa203_active_budget
```

Full-universe batches (Oct 5–7, 661 underlyings in 14 batches of ≤50).
The MSI's Python cannot fetch from R2 (stale CA bundle; see Box 6), so
the source blobs are pre-populated into `replay-cache/` with PowerShell
(HTTPS, TLS verified, no CA changes) and the replay reads them from disk:

```powershell
# Populate the cache with PowerShell (TLS verified, no CA changes):
$days = '2026-10-05','2026-10-06','2026-10-07'
foreach ($d in $days) {
  foreach ($k in @("oa203/scanner/$d/summary.json", "oa203/scanner/$d/universe.json", "oa203/scanner/$d/sweeps/manifest.jsonl")) {
    $dest = "replay-cache\$k"; New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Invoke-WebRequest -Uri "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/$k" -OutFile $dest -UseBasicParsing
  }
  $man = Get-Content "replay-cache\oa203\scanner\$d\sweeps\manifest.jsonl" | ConvertFrom-Json
  foreach ($m in $man) {
    $k = "oa203/scanner/$d/sweeps/$($m.file)"; $dest = "replay-cache\$k"
    Invoke-WebRequest -Uri "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/$k" -OutFile $dest -UseBasicParsing
  }
}
# Then run the batches with the committed Python runner (cross-platform;
# the .sh is the same runner for bash only):
$env:PYTHONPATH = '.'
C:\Python314\python.exe run-full-universe-batches.py
# Then combine and globally re-rank:
C:\Python314\python.exe combine-full-universe.py
```

The replay fetches from the R2 origin
`https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev` with User-Agent
`oa203-windows-replay/1` when a blob is not cached. It is read-only: no
collector runs, no production config or account changes.
