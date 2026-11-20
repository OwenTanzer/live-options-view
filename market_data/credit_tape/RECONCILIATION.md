# DTCC coverage reconciliation (OA-242)

The tape's only live source is the intraday slices (`Slice.json` index, then the slice
ZIPs). DTCC's daily **cumulative** file is used **only to reconcile** a completed day:
- it is never written to `events.jsonl` or `alerts_outbox.jsonl`;
- it never creates a panel row;
- it never fills the tape.

Tool: `python -m market_data.credit_tape.reconcile` (`reconcile.py`).

## What a report contains, per channel (`CFTC_CR`, `SEC_CR`) and completed UTC day

Three parts are kept apart, and anything not proven is reported as **unknown**.

### 1. Archive audit on one snapshot (first)

`check_archive` audits **one consistent snapshot**, and the tape verdict sees only the
rows that audit validated.

- **Snapshot order:**
  1. the done markers;
  2. the commit records;
  3. each log's byte size.
  Every log is read only up to that size. A commit counts as completed only if its
  done marker is in the snapshot. The marker is written after the commit's lines are
  appended, so all of its lines lie inside the snapshot. Lines appended later are
  never read.
- **Tape rows = audited rows.** These are the publication rows of completed commits
  that validated with zero problems: log line == commit line == original ZIP row.
  Each is identified by `(commit_key, commit_line)`. A logged publication is never
  accepted by its slice filename: any line outside the audited set is `unverified`.
  That covers a line without a commit, a line from a commit that is in flight or
  completed after the snapshot, and a line from a commit with problems.
- **In-flight work is excluded, not failed.** Reconciliation audits in in-flight mode,
  because the collector may be running. These are reported under
  `archive_audit.in_flight` and their rows are not counted:
  - commits without a done marker;
  - commits created after the listing;
  - an unterminated last line;
  - `.part` files.
  The restart check keeps the strict default.
- **Binding.** Each channel's report carries `bound_to`: the `archive_id`, the
  snapshot (bytes and sha256 per log), and a sha256 of the exact verified tape rows
  the verdict used.
- If the audit is not clean, or any logged row for the channel/day is unverified, the
  verdict is `unverified`.

### 2. Numbered-slice coverage (`slice_coverage`)

- The slice day is the UTC day; numbering restarts at `_1` just after 00:00Z.
- The report lists captured slice numbers, live vs replay, and the uncaptured
  numbers `1..max`.
- `--recover-gaps` fetches uncaptured numbers from DTCC's numbered intraday endpoint.
  These ZIPs are **report-only**:
  - stored as originals under `dtcc-reconcile/<day>/report-only-slices/`, with a
    `<CHANNEL>.REPORT_ONLY.json` marker;
  - never logged to the tape, and never counted as tape rows.
  - Restoring them to the tape is a separate objective.
- Their outcome is its own result, `report_only`, with `status` `ok`, `none` or
  `decode_failures`, and each failure listed. **A report-only decode failure cannot
  change the tape verdict.** The rows that did decode feed only the
  `with_report_only` view.
- **Unknowns stay unknown.**
  - Any fetch that does not return a readable ZIP (404, error, non-ZIP) is recorded
    with its status under `unknown_numbers`.
  - The tail probe past the last captured number lists what it observed (e.g.
    `#1151: unavailable_http_404`). Its `boundary` is always `"unknown"`: an
    unavailable response is not proof that the day ended there.

### 3. Parity against the saved cumulative file

The cumulative file is stored as an original under `dtcc-reconcile/<day>/cumulative/`,
and its sha256 is recorded. Parity is a **full-row multiset** per
`Dissemination Identifier`:
- each side is a multiset of complete rows (all columns);
- equal rows are paired one to one;
- **every unmatched occurrence is listed on its own side**, even when both sides have
  the same total count for that ID (`same_total_count: true`);
- for a single one-to-one mismatch, the differing fields are named;
- nothing is deduplicated, merged or overwritten.

Per-ID states are `matched`, `occurrence_mismatch`, `captured_only` and
`cumulative_only`; `repeated_reports` lists every ID seen more than once.

| View | Rows compared | Counts toward the verdict |
|---|---|---|
| `tape_parity` | verified tape rows vs saved cumulative | **yes**, the only one |
| `with_report_only` | verified tape rows + report-only slices vs saved cumulative | no; explains gaps |
| `unverified_view` | audited + unverified logged rows + report-only vs saved cumulative | no; shown only when some rows are unverified, and labelled |

**Lifecycle records** (CORR, MODI, TERM, EROR, REVI) are kept as their own records.
A record's `Original Dissemination Identifier` is either:
- `original_observed_in_interval` (on either side), or
- `original_not_observed`: **unknown**. It is listed, and is **not** treated as
  proof that the original is on an earlier day.

**Verdict** (`verdict.tape_parity`):
- `complete`: clean audit, every logged row of the snapshot verified, and zero
  unmatched occurrences on both sides. Report-only results play no part;
- `unverified`: the audit failed or rows lack a validated commit;
- `incomplete`: any unmatched occurrence;
- `unknown`: the cumulative file is unavailable.

The exit code is 0 only if every channel is `complete`. `verdict.unknowns` carries
the unknown slice numbers, `tail_boundary: "unknown"`, and the count of originals
not observed.

Run after the day closes, e.g. at 01:00Z for the previous day:

```sh
python -m market_data.credit_tape.reconcile --archive "$CREDIT_TAPE_ARCHIVE" --volume /data --day <YYYY-MM-DD> \
    --recover-gaps --fetch-cumulative --out /data/credit-tape/dtcc-reconcile/<YYYY-MM-DD>/report.json
```

## Worked example: 2026-11-19 (00:00Z–24:00Z), real data, local run

This is my local run, not an independent raw-data verification. Only aggregate
counts are in the repository.
- **Archive:** the original local capture archive. It was written by an earlier
  revision (595d2d4/0b80674), so it has **no archive identity and no commit records**.
- **Saved cumulative files:** fetched 2026-11-20 ~11:45Z. sha256 prefixes:
  CFTC `5bedfef78e973ac0`, SEC `19f7f15617465bd4`.
- **Capture mode:** this day's slices came through the slice route's **replay**
  (backfill of the rolling index), not live.

| | CFTC_CR | SEC_CR |
|---|---|---|
| Archive audit | **not clean**: no identity; no commit records | same |
| Tape rows logged / verified | 2,621 / **0** | 1,227 / **0** |
| **Verdict** (`tape_parity`) | **`unverified`** | **`unverified`** |
| Slices captured | 1,134 (#17–#1150), all replay | 584 (#29–#612), all replay |
| Uncaptured numbers | #1–#16 | #1–#28 |
| Report-only recovered / unknown | 16 / none | 28 / none |
| Tail probe | #1151: `unavailable_http_404`; boundary **unknown** | #613: `unavailable_http_404`; boundary **unknown** |
| `with_report_only` (verified rows + report-only) | 17 matched; 2,621 `cumulative_only` (the unverified rows are excluded) | 39 matched; 1,227 `cumulative_only` |
| `unverified_view` (all logged + report-only, **unverified**) | 2,638 occurrences matched; **0 unmatched on either side** | 1,266 matched; **0 / 0** |
| Lifecycle: original observed / **not observed (unknown)** | 192 / 181 | 134 / 327 |

So for 11-19:
- **Proven:** the saved cumulative file and the logged rows plus report-only slices
  are the same full-row multiset.
- **Not proven:** the tape itself, because its archive cannot pass the audit.
- **Next:** reach a `complete` verdict on a full UTC day captured by the current
  collector (identity plus commit records), live rather than replayed.

## Verified vs not verified

| Claim | Status |
|---|---|
| Slice day = UTC day; numbering restarts after 00:00Z | Real data (11-19 → 11-20 boundary, both channels) |
| Logged rows plus report-only slices for 11-19 equal the saved cumulative file as a full-row multiset | Real data, my local run, **unverified view** (archive predates the audit) |
| The intraday endpoint serves old slices, byte-identical to the S3 originals | Real data (CFTC #17, SEC #29) |
| The cumulative file is not in slice order | Real data (11-19) |
| Unmatched occurrences reported with equal totals; repeats; field differences; captured-only; cumulative-only; unknown fetches; unobserved originals; audit gating (tampered original, legacy archive) | **Synthetic fixtures only** (`ReconciliationTests`) |
| Verdict bound to audited rows of one snapshot: a filename-matching line without a commit stays unverified; a commit completed after the snapshot listing is excluded (same day → unverified, other day → no effect); report-only decode failure leaves a complete tape verdict complete | **Synthetic fixtures only**; the collector is driven mid-audit through a test hook |
| A `complete` verdict on real data | **Not yet.** Needs a full UTC day captured by the current collector |
| Day boundary / tail of a day | **Unknown** by design; an unavailable response is not proof |
| An unobserved original is on an earlier day | **Unknown**; not inferred |
| Whether a cumulative file is restated after publication | **Not verified.** The sha256 is stored per fetch, so a re-run can detect it |
| Fresh SEC evidence | Separate |
