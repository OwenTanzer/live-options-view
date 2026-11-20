# DTCC coverage reconciliation (OA-242)

The tape's only live source is the intraday slices (`Slice.json` index, then the slice
ZIPs). DTCC's daily **cumulative** file is used **only to reconcile** a completed day:
- it is never written to `events.jsonl` or `alerts_outbox.jsonl`;
- it never creates a panel row;
- it never fills the tape.

Tool: `python -m market_data.credit_tape.reconcile` (`reconcile.py`).

## Plan, per channel (`CFTC_CR`, `SEC_CR`) and per completed UTC day

1. **Interval.** One DTCC slice day, which is the UTC calendar day. Slice numbering
   restarts at `_1` just after 00:00Z (observed: `CFTC_SLICE_CREDITS_2026_11_20_1`
   started 00:01:14Z; `SEC_..._2026_11_20_1` started 00:38:06Z). The tool refuses a
   day that is not yet complete in UTC.
2. **Captured set.** For that day, take every slice the tape committed (from the
   slice events in `events.jsonl`).
   - Re-hash each original ZIP against the `raw_sha256` recorded at capture.
   - Keep every publication row with its slice file, slice number, row index and
     origin (`live` / `replay`).
3. **Slice coverage.** List the slice numbers `1..max` that were not captured.
   - With `--recover-gaps`, fetch each missing number from DTCC's numbered intraday
     endpoint (`/ppd/api/report/intraday/{cftc,sec}/…_SLICE_CREDITS_<day>_<n>.zip`).
   - Then probe past the last captured number until the endpoint answers 404, to
     find the day's true last slice.
   - Recovered slices are intraday records, not the daily file. They are stored as
     originals under `dtcc-reconcile/<day>/gap-slices/` before decoding, and labelled
     `gap_recovery`, never `live`.
4. **Cumulative set.** Fetch `<AGENCY>_CUMULATIVE_CREDITS_<day>.zip`. Store the
   original under `dtcc-reconcile/<day>/cumulative/` before decoding, and record its
   sha256 in the report.
5. **Compare** as a multiset keyed by `Dissemination Identifier`, using full-row
   equality over all 110 columns. Nothing is deduplicated or overwritten. Every
   record lands in exactly one of:
   - `matched`: the same row(s) with the same multiplicity on both sides.
   - `matched_with_differences`: the same ID, but different field values. Both
     versions are kept in the report, with the differing fields named.
   - `repeat_count_mismatch`: the ID appears a different number of times on each
     side. All occurrences are kept.
   - `repeated_reports` (informational): every ID reported more than once, with the
     slices it came from.
   - `captured_only`: captured, but not in the day's cumulative file. **Unresolved.**
   - `missing_in_uncaptured_slice`: in the cumulative file and not captured, while
     some slice numbers are still uncaptured. **Unresolved.** The cumulative file is
     *not* in slice order (checked on 2026-11-19), so a record cannot be pinned to
     one slice by position. Recovering the gap slices resolves it.
   - `missing_unexplained`: in the cumulative file and in no captured or recovered
     slice. **Unresolved.**
6. **Corrections and lifecycle events.** CORR, MODI, TERM, EROR and REVI are
   ordinary records; they are compared like any other and never merged into their
   originals. Each record with an `Original Dissemination Identifier` is linked:
   - `original_in_interval`: the original is in the same day;
   - `original_before_interval`: the original is on an earlier day. It is listed
     explicitly, not resolvable within this interval, and left for that earlier
     day's archive.
7. **Result.** The report is complete only if every one of these is zero:
   `captured_only`, both `missing_*` categories, `matched_with_differences`,
   `repeat_count_mismatch`, still-missing slice numbers, and capture problems such as
   a missing original or a hash mismatch. Otherwise the tool exits 1, with every
   unresolved ID listed (up to 200 per category; full counts are always given).

Run after the day closes, e.g. at 01:00Z for the previous day:

```sh
python -m market_data.credit_tape.reconcile --archive "$CREDIT_TAPE_ARCHIVE" --day <YYYY-MM-DD> \
    --recover-gaps --fetch-cumulative --out /data/credit-tape/dtcc-reconcile/<YYYY-MM-DD>/report.json
```

## Worked example: 2026-11-19 (00:00Z–24:00Z)

**Verified against real data.** Locally, against the original local capture archive
(not in this repository; aggregate counts only here). Cumulative files were fetched
2026-11-20 ~11:45Z.

The captured slices for this day came through the slice-index route's **replay**:
capture started late on 11-19 and backfilled the rolling ~24 h index. They are
labelled `replay`, not `live`.

| | CFTC_CR | SEC_CR |
|---|---|---|
| Slices captured by the tape | 1,134 (#17–#1150) | 584 (#29–#612) |
| First / last captured slice | 07:07:53Z / 23:51:49Z | 06:57:23Z / 23:50:19Z |
| Uncaptured slice numbers | #1–#16 (rolled out of the index before capture began) | #1–#28 (same) |
| Recovered from the intraday endpoint | 16 of 16 | 28 of 28 |
| Tail probe (first absent number) | #1151 → 404 (the day's last slice is #1150) | #613 → 404 |
| Rows captured + recovered | 2,621 + 17 | 1,227 + 39 |
| Cumulative rows (distinct IDs) | 2,638 (2,638) | 1,266 (1,266) |
| **matched** (full 110-column row equality) | **2,638**, of which 17 via gap recovery | **1,266**, of which 39 via gap recovery |
| matched_with_differences / repeat_count_mismatch | 0 / 0 | 0 / 0 |
| captured_only / missing_unexplained | 0 / 0 | 0 / 0 |
| Action types (identical on both sides) | NEWT 2,265, MODI 144, CORR 109, TERM 77, EROR 43 | NEWT 805, MODI 285, TERM 77, CORR 64, EROR 34, REVI 1 |
| Lifecycle links: original in interval / before interval | 192 / 181 | 134 / 327 |
| Result | complete | complete |

- **Without `--recover-gaps`** the same day correctly reports 17 CFTC and 39 SEC
  records as `missing_in_uncaptured_slice`, alongside uncaptured slices #1–16 and
  #1–28, and exits 1.
- **Endpoint check.** For two overlapping slices (CFTC #17, SEC #29), the intraday
  endpoint returned bytes identical to the live S3 slice originals (same sha256).
  So gap recovery returns the same records as the live route.

## Verified vs not verified

| Claim | Status |
|---|---|
| Slice day = UTC day; numbering restarts after 00:00Z | Real data (11-19 → 11-20 boundary on both channels) |
| Captured + recovered slices equal the cumulative file, row for row, for 11-19 | Real data (above) |
| The intraday endpoint serves old slices, byte-identical to the S3 originals | Real data (2 slices) |
| The endpoint returns 404 past the last slice | Real data (both channels) |
| The cumulative file is not in slice order | Real data (11-19) |
| Repeated reports, count mismatches, field differences, captured-only, unexplained missing, cross-day corrections, tampered originals | **Synthetic fixtures only** (`ReconciliationTests`); 11-19 had no repeats or differences |
| A live (not replay) capture reconciles the same way | **Not yet verified.** 11-19 came through replay; first fully live day still to run |
| Whether a cumulative file is restated after first publication | **Not verified.** Store each fetch's sha256 and re-run to detect it |
| Fresh SEC evidence | Separate, not covered here |

Not decided here: whether gap-recovered records should also be published to the
panel. Today they live only in the reconciliation area.
