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

### a. Pilot archive: read-only, never the live volume

- `reconcile.py` writes into the archive it reads:
  - the cumulative original under `dtcc-reconcile/2026-11-20/cumulative/`;
  - with `--recover-gaps`, report-only slices under `.../report-only-slices/`.
  Running it on the live pilot volume would change the pilot. That is excluded.
- `check_archive` also refuses an archive that is not inside a mounted volume.
- So the run needs a read-only input that Owen and Chej provide and approve. **How
  that input is produced is not part of this packet.** One option is described
  separately in [Separate proposal: disposable volume](#separate-proposal-disposable-volume-not-part-of-this-packet).
- Whatever the input, record: its capture time, the source `archive_id` from
  `archive.json`, and sha256 of `events.jsonl`, `alerts_outbox.jsonl`,
  `archive.json`, `dtcc-slices/polls.jsonl` and `dtcc-slices/checkpoint.json`.
- Reconciliation audits with `allow_in_flight=True`. On a frozen input nothing is in
  flight, so any `archive_audit.in_flight` entry is itself a finding (a commit cut
  mid-write at capture time).

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

### c. Command (run by Owen/Chej against the read-only input)

```sh
python -m market_data.credit_tape.reconcile \
  --archive <read-only-input>/credit-tape --volume <its-mount> --day 2026-11-20 \
  --cumulative-dir <saved-cumulative-dir> --out <outside-the-input>/report-2026-11-20.json
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

**Parity is completeness, not live capture.** The full UTC day stays the completeness
interval. But `complete` only says the saved tape rows equal the cumulative file. It
does **not** say capture was uninterrupted or live: backfill (replay of the rolling
index) and catch-up after the restart can make the totals agree even when some rows
never arrived live. Live-capture claims come only from the proof table in section 4.

## 4. Proof table: pre-launch, restart overlap and post-restart

Pilot timeline (from Chej): **launch L = 13:48:53Z**, **restart R = 13:52:14Z**,
2026-11-20.

### Per-row fields used (all already in `events.jsonl`, read-only)

- **Publication time `P`:** the slice's `dissemDTM` (`slice.dissemDTM`, copied to each
  row as `dissemination_ts`). DTCC slice rows have no per-row dissemination column, so
  every row in a slice shares that slice's `P`.
- **First seen `F`:** `first_seen`, when our collector received the slice bytes.
  `requested_at` is the request time.
- **Provenance:** `replay` / `backfill` (true or false), `attempts`, and the done
  marker `done/<CHANNEL>__<sliceId>.json` (`at`, and `recovered: true` when a commit
  was finished by recovery at startup).
- **Slice window:** `slice.startTs` and `slice.endTs`.
- **Collector activity:**
  - `dtcc-slices/polls.jsonl`: one line per index poll, with `t`. A `200` line also has
    `recv` and `etag`; a `304` line has only `t`.
  - Saved index copies under `dtcc-slices/index/<UTC day of fetch>/`, one per `200`
    response, each with `fetched_at`, `etag` and the full `CFTC_CR` / `SEC_CR` lists.
    Copies near midnight sit in the 11-19 or 11-21 folder, so all three folders are read.

### Listing time is bounded, not observed

The collector polls DTCC's index at intervals. We only see a slice in the index when
we poll, so **the first saved copy that lists a slice is our first observation, not
the time DTCC listed it.** No new listing can be observed during a poll gap.

For each slice, per channel:

- **First positive snapshot `O`:** the earliest saved index copy whose channel list
  contains the slice's `fileName`. `O` is its `fetched_at`.
- **Last complete negative snapshot `N`:** the latest saved index copy **before `O`**
  that is complete for that channel and does not contain the slice. "Complete" means:
  - the copy parsed;
  - the channel key is present and is a list (`listed_in_index`);
  - nothing between `N` and `O` is a failed poll whose response was not saved.
  If a failed or missing poll sits between the last negative copy and `O`, there is no
  complete negative snapshot next to `O`.
- **DTCC's listing time lies in `(N, O]`.** When there is no `N` (no copy before `O`,
  such as the first poll after launch or after the restart, or a gap), the lower bound
  is **unknown** and the listing time is reported as `unknown, ≤ O`.
- **Optional tightening, labelled `derived_304`:** a `304` poll at time `t` between `N`
  and `O` means the index was byte-identical to `N` at `t` (the request carried the
  ETag of the latest `200` copy, which is `N`). The negative bound then becomes the
  last such `t`. Because `polls.jsonl` does not record the ETag that was sent, this is
  derived from the order of `200` lines, and is always reported as derived, alongside
  the snapshot-only bound.

**Delays, named for what they are:**

| Quantity | Name in the report | What it is |
|---|---|---|
| `F − P` | **observation minus publication** | DTCC's delay, plus our polling interval, plus our fetch time. **Not** a source delay |
| `(N − P, O − P]` | **source listing delay, bounded** | The only statement about DTCC's own delay (publication to index listing). Reported as an interval. Unknown lower bound when there is no `N` |
| `O − N` | **listing uncertainty** | How wide the bound is (normally one poll interval; wide across the poll gap) |
| `F − O` | **our fetch delay** | First observation of the listing to receipt of the slice bytes. This one is ours |

`P` itself is DTCC's slice `dissemDTM`. Whether a slice can be listed before its
`dissemDTM` is not assumed either way; `P` and the bound are reported side by side.

### Important: what the `replay` flag does and does not mean

`replay` is computed as `P < started_at`. `started_at` is written to
`dtcc-slices/checkpoint.json` on the **first** start and **kept across restarts**. So
for this pilot, `replay` is relative to **L (13:48:53Z), not R**.

- A slice published while the collector was down, and fetched after the 13:52:14Z
  restart, has `replay = false`. It is labelled live, and it raised an alert, even
  though it was caught up late.
- So `replay = false` means "published after first launch". It does **not** mean
  "captured in real time". The timing columns below decide that.
- The archive has no per-boot start record; `started_at` is the only start time
  stored. The restart time R comes from Chej (the pilot logs), cross-checked against
  the gap in `polls.jsonl`.

### The table (per channel, `CFTC_CR` and `SEC_CR`)

Segments are assigned by **publication time `P`**. The parity basis stays the
filename day, so every row also stays in the full-day parity in section 3.

| Segment | Rule | Expected provenance | Timing reported | Counts as live capture? |
|---|---|---|---|---|
| **Pre-launch** | `P < L` | `replay = true` (backfill), or uncaptured (rotated off the index before launch) and therefore `cumulative_only` | observation minus publication `F − P` (large) | **No**, never |
| **Restart overlap** | `L ≤ P < R'`, where `R'` is the first index poll after R (from `polls.jsonl`) | `replay = false` in all cases. Split by `F`: `F < D` (captured by the first run), or `F ≥ R` (caught up after restart). `D` is the last poll before the gap. Also flag any done marker with `recovered: true` | `F − P`, listing bound `(N, O]`, `F − O` | Only rows with `F < D` **and** the live test below. Rows with `F ≥ R` are **post-restart catch-up**, not live. Rows the evidence cannot separate are **`overlap_uncertain`** |
| **Post-restart** | `P ≥ R'` | `replay = false` | `F − P`, `(N, O]`, `F − O` | **Yes**, if the live test passes. Otherwise "late" with its bounds |

- **Live test** (a reporting rule, not a gate): a row counts as live only when
  1. a complete negative snapshot `N` exists for its slice, **and**
  2. `O − N` is no wider than the normal poll interval (read from `polls.jsonl`), **and**
  3. `F − O` is within one poll interval plus fetch time.
  This says the slice was fetched within about one poll of first appearing. It says
  nothing finer about DTCC's own timing. Any row without an `N` is never counted live.
- **`overlap_uncertain`:** in the overlap segment, a row is uncertain when its listing
  bound `(N, O]` contains the stop or restart. For example, `N ≤ D < O` with `O ≥ R'`:
  DTCC may have listed it before the first run stopped (and the first run missed it),
  or during the downtime. The evidence cannot tell which, so the row is neither live
  nor a confirmed miss.
- Per segment, every quantity is reported as count, median, p95 and max. The bounds
  are reported as intervals, never collapsed to one number.

### Labels for rows that cannot be placed

These rows stay in the full-day parity but are listed separately in the proof table:

- **`dissemination_missing`:** the slice has no `dissemDTM`. The code then treats it as
  `replay = false`, so it would look live. It is never counted as live; it is listed
  with its slice and `F`.
- **`crosses_launch` / `crosses_restart`:** the slice window `[startTs, endTs]`
  contains L or R, or `P` is on one side and `startTs` on the other. It is reported
  with both times and not counted as live.
- **`crosses_utc_day`:** `P` or `startTs` falls on 11-19 or 11-21 while the file name
  says 11-20. Parity keeps the filename day; the proof table lists the row with its
  actual times.
- **`no_listing_seen`:** no saved index copy lists the slice, so `O` (and `N`) are unknown.
  `F − P` is still reported, as observation minus publication.
- **`listing_lower_bound_unknown`:** `O` exists but there is no complete negative
  snapshot `N` before it. Reported as `unknown, ≤ O`.

### How to tell capture after restart from later replay

| Evidence | Captured after restart (catch-up) | Later replay (backfill) |
|---|---|---|
| `P` vs launch L | `P ≥ L` | `P < L` |
| `replay` flag | `false` (because `started_at` = L) | `true` |
| Alert written | yes | no |
| `F` | at or shortly after R | any time after L, on the backfill thread |
| Listing bound `(N, O]` | `O` is the first poll after the restart (`O = R'`), and `N` is at or before `D`, so the bound spans the gap | `O` is the first poll after launch (or after the restart) with no `N`, so the lower bound is unknown |
| `F − O` | small (fetched as soon as it was observed after R) | can be long (the backfill thread is slower) |
| Done marker | may be `recovered: true` if its commit was interrupted at the stop | normal |

The decisive test is `P` against **L** together with `F` against **R**. The `replay`
flag alone cannot separate the two, for the reason above.

### What the proof table cannot show

- Whether the first run captured everything published in `[L, D]` before it stopped:
  only parity (section 3) shows that, and parity does not show *when*.
- The true stop time of the first run: only `D` (the last poll before the gap) is
  known from the archive.
- DTCC's exact listing time: only the interval `(N, O]`, or `unknown, ≤ O`.
- Real-time behaviour before L: nothing was running.

### Illustrative case: parity passes, live continuity not proven

**Synthetic numbers, for illustration only. Not real data and not a prediction.** One
channel. The first run polls every 20 s.

| Time (Z) | Event |
|---|---|
| 13:48:53 | L: first run starts; `started_at` = L |
| 13:49:58 | `D`: last index poll of the first run, `200`, saved copy does **not** list slice `#k` (this is `N`) |
| ~13:50:05 | first run stops (not recorded in the archive) |
| 13:50:30 | DTCC publishes slice `#k` (`P`, its `dissemDTM`) with 3 rows |
| 13:52:14 | R: restart |
| 13:52:20 | `R'`: first poll after the restart, `200`, saved copy lists `#k` (this is `O`) |
| 13:52:21 | `#k` fetched (`F`); `replay = false` because `P > started_at`, 3 alerts written, panel shows the rows as live |

At reconciliation:
- **Parity:** the cumulative file has those 3 rows, and the tape has them once each.
  `tape_parity` can be `complete`.
- **Proof table:** `#k` is in the restart overlap. `F ≥ R`, so it is
  **post-restart catch-up**. Its listing bound is `(13:49:58, 13:52:20]`, 142 s wide
  across the gap. Observation minus publication is 111 s, but that is not DTCC's delay.
  The source listing delay is only bounded, and here its lower bound is negative, so it
  says nothing.
- **Verdict:** completeness proven; **live continuity not proven** for `#k`. The `live`
  label on the panel and the alerts do not change that.

A variant: had `#k` been published at 13:49:50 (`P` before `D`) but listed only after
13:49:58, the same bound `(N ≤ D < O)` would make it `overlap_uncertain`. We cannot tell
whether the first run missed it or DTCC listed it during the downtime.

## 5. What the packet returns

1. The JSON report, plus the sha256 of the input logs and of both cumulative files.
2. One line per channel: verdict; matched / captured-only / cumulative-only /
   occurrence-mismatch counts; uncaptured and unknown slice numbers; unobserved
   originals.
3. Every unmatched occurrence, listed by ID.
4. The proof table per channel: rows and slices per segment, provenance split, timing
   statistics (observation minus publication, bounded source listing delay, listing
   uncertainty and our fetch delay), the labelled and `overlap_uncertain` rows, and `D`,
   `R'` and the poll interval used.
No real payloads go into the public repo; the reports stay with the pilot owners.

**Gap:** `reconcile.py` does not compute the proof table yet. It is derived from the
same read-only `events.jsonl`, `polls.jsonl` and index copies. Building it into the
tool is a separate, approved change, and nothing is changed here.

## Not covered

Alert delivery, UK lanes (paused), restoring report-only slices to the tape, and any
pilot configuration change.

## Separate proposal: disposable volume (not part of this packet)

For Owen and Chej to decide on separately; nothing here depends on it.

- After 00:00Z, take a point-in-time copy of the pilot's credit-tape archive onto a
  disposable volume, and attach it to a one-off job that runs the command in section
  2c. This satisfies the mount check, and the tool's writes land on the copy.
- Alternatively, a small code change (a `--work-dir` that keeps every write outside
  the archive and allows a plain directory input). Not made.
