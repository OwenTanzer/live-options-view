# U.S. pilot reconciliation packet: 2026-11-20 UTC (OA-242), plan only

Plan only, written 2026-11-20 before the interval closes. **No** download, credentials,
deployment or pilot change. It reuses the existing parity tools: `reconcile.py` and
`check_archive.py` (see [RECONCILIATION.md](RECONCILIATION.md)). It runs once, after
**both** cumulative references for the day exist. Owen and Chej own the pilot, its
volume and the run.

## 1. Interval and publication-time basis

- **Interval:** `[2026-11-20T00:00:00Z, 2026-11-21T00:00:00Z)`, the whole UTC day.
  It closes at 2026-11-21 00:00Z (2026-11-20 4:00 PM PST). `reconcile.py` refuses any
  day that is not before today (UTC), so it cannot run before then.
- **Basis = DTCC publication day, as carried in DTCC's own file names. Not execution
  time, and not our first-seen time.**
  - Tape side: a row belongs to the interval when its slice is named
    `{CFTC,SEC}_SLICE_CREDITS_2026_11_20_<n>.zip`. Slice numbering restarts at `_1`
    just after 00:00Z (real data, the 11-19 → 11-20 boundary).
  - Reference side: `CFTC_CUMULATIVE_CREDITS_2026_11_20.zip` and
    `SEC_CUMULATIVE_CREDITS_2026_11_20.zip`.
  - On 11-19 the two bases agreed exactly. All logged plus report-only rows matched
    the cumulative file as a full-row multiset, both channels, 0 unmatched (real data,
    my local run, `unverified_view`). That is the evidence that slice day and
    cumulative day are the same basis. It is not proof for 11-20, and a disagreement
    on 11-20 would show up as `captured_only` / `cumulative_only`, not be hidden.
  - Execution timestamps, DTCC dissemination timestamps inside rows, and our
    first-seen times are reported but **do not decide membership**. A row executed on
    11-19 and published on 11-20 belongs to 11-20.

## 2. Inputs, all read-only

### a. Pilot archive: a frozen copy, not the live volume

- **Why a copy:** `reconcile.py` writes into the archive it reads:
  - the cumulative original under `dtcc-reconcile/2026-11-20/cumulative/`;
  - with `--recover-gaps`, report-only slices under `.../report-only-slices/`.
  Running it on the live pilot volume would change the pilot. That is excluded.
- **How (Owen/Chej, on the pilot host):** after 00:00Z, take a point-in-time copy of
  the credit-tape archive directory from the pilot volume (a volume snapshot or a
  file copy), and mount or place it where the run can see it. Record:
  - the copy time;
  - the source `archive_id` from `archive.json`;
  - sha256 of `events.jsonl`, `alerts_outbox.jsonl` and `archive.json` at copy time.
- **Mount check:** `check_archive` refuses an archive that is not inside a mounted
  volume (`--volume`, or `CREDIT_TAPE_VOLUME` / `RAILWAY_VOLUME_MOUNT_PATH`). The
  copy must therefore sit on a mounted volume, such as a snapshot volume attached to
  a one-off job.
  - **Gap:** if only a plain directory copy is possible, the current CLI cannot audit
    it, because there is no flag to skip the mount check or write elsewhere. That
    would need a small, separately approved change (a `--work-dir` that keeps every
    write outside the archive). It is not made here.
- **The audit mode is already right:** reconciliation audits with
  `allow_in_flight=True`. On a frozen copy nothing is in flight, so any
  `archive_audit.in_flight` entry is itself a finding (a commit cut mid-write at
  copy time).

### b. Cumulative references

- `CFTC_CUMULATIVE_CREDITS_2026_11_20.zip` and `SEC_CUMULATIVE_CREDITS_2026_11_20.zip`,
  from DTCC's public cumulative report path (the same PPD route the tool uses).
- **Availability:** for 11-19 both were present by about 11:45Z on 11-20 (when we
  fetched them). No publication time is documented. The run waits until **both**
  exist; one channel is never reconciled alone for this packet.
- **Fetch (at run time, by whoever Owen approves):** one GET per file. Save each
  original untouched with its sha256, then pass the folder with `--cumulative-dir`.
  Don't use `--fetch-cumulative`, so that the fetch stays outside the archive copy and
  is logged separately.
  - If a file is later re-fetched and the sha256 differs, that is a restatement. Report
    both and do not overwrite either.
- **Gap recovery is off by default.** `--recover-gaps` fetches numbered intraday slices
  live. Without approval, uncaptured slice numbers are reported as uncaptured, and
  their rows fall out as `cumulative_only`. That is correct for a tape-coverage verdict.

### c. Command (run by Owen/Chej against the copy)

```sh
python -m market_data.credit_tape.reconcile \
  --archive <copy>/credit-tape --volume <copy-mount> --day 2026-11-20 \
  --cumulative-dir <saved-cumulative-dir> --out <outside-the-copy>/report-2026-11-20.json
```

Exit code 0 only if both channels are `complete`.

## 3. Expected output per channel (`CFTC_CR`, `SEC_CR`)

Each channel produces the same three parts, kept separate.

| Part | Field | What it shows | Expected for 11-20 |
|---|---|---|---|
| Audit | `archive_audit.clean`, `archive_id`, `bound_to` (snapshot bytes and sha256 per log, sha256 of the verified rows) | Which archive state the verdict is bound to | Clean, if the copy is consistent. `archive_id` equals the pilot's |
| Slice coverage | `captured`, `captured_live` vs `captured_replay`, `uncaptured_numbers`, `unknown_numbers`, `tail.boundary` | Which numbered slices the pilot logged | Depends on when the pilot started. Slices before pilot start are either replay (if still on the rolling index) or uncaptured. `tail.boundary` is always `"unknown"` |
| Tape parity (the verdict) | Per ID: `matched`, `occurrence_mismatch`, `captured_only`, `cumulative_only`; `repeated_reports`; field diffs for one-to-one mismatches | Full-row multiset per `Dissemination Identifier`; every unmatched occurrence listed on its own side, even when totals agree | See the outcomes below |
| Lifecycle | `original_observed_in_interval` vs `original_not_observed` | CORR / MODI / TERM / EROR / REVI rows, each kept as its own record | Unobserved originals are **unknown**, never assumed to be on an earlier day |
| Report-only | `report_only.status` | Only with approved `--recover-gaps` | `none` by default; cannot change the verdict |

**Scale reference only (11-19, not a prediction):** CFTC 2,638 rows in about 1,150
slices; SEC 1,266 rows in about 612 slices.

**Outcome rules:**
- `complete`: clean audit, every logged 11-20 row verified, and zero unmatched
  occurrences on both sides. This is the first real `complete` we have not had yet.
- `incomplete` with only `cumulative_only` rows, all in uncaptured slice numbers:
  coverage gap before pilot start or during an outage. The report names the slice
  numbers.
- `incomplete` with `captured_only` rows, or any `occurrence_mismatch`: a real
  discrepancy. Each one is listed with its slice, `commit_key` and `commit_line`;
  investigated, never corrected in place.
- `unverified`: audit failure or unverified logged rows; the parity numbers are then
  explanation only.
- `unknown`: a cumulative file was missing at run time; rerun when it exists.

## 4. What the packet returns

1. The JSON report, plus the sha256 of the copy logs and of both cumulative files.
2. One line per channel: verdict; matched / captured-only / cumulative-only /
   occurrence-mismatch counts; uncaptured and unknown slice numbers; unobserved
   originals.
3. Every unmatched occurrence, listed by ID. No real payloads go into the public repo;
   the report stays with the pilot owners.

## Not covered

Fresh-arrival latency, alert delivery, UK lanes (paused), restoring report-only
slices to the tape, and any pilot configuration change.
