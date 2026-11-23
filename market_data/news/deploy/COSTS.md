# News pilot: runtime, operations, storage and cost envelope

Status: evidence for the schedule within Owen's **$5/month incremental** News budget (2026-11-14).
Revised after Chej's review of #247: the history read now lists raw runs only after the checkpoint
frontier (bounded listing). Nothing here is provisioned. The pilot config stays `active: false` and
the cadence stays `*/15`.

Each figure is labelled:
- **OBSERVED:** measured from the shipped code on local stores;
- **PROJECTED:** arithmetic on observed figures plus a stated assumption;
- **NEEDS PROVIDER ACCESS:** cannot be measured without the provisioned buckets or service.

## How it was measured

- `tests/news_cycle_cost_measure.py` runs `publish.pilot_cycle` with the shipped config, once per
  simulated 15-minute tick, against two counting local stores. Every store call is recorded as the R2
  request it maps to in `R2ObjectStore`:
  - put → PutObject (Class A);
  - list → ListObjectsV2 (Class A, one request per 1,000 keys returned);
  - get → GetObject (Class B);
  - `last_modified` → HeadObject (Class B);
  - delete → DeleteObject (free).
- `--expiry` simulates a verified store-clock bound in a temporary copy of the config. The shipped
  `clock_bound_verified` stays `false`.
- `tests/news_cost_projection.py` projects monthly figures from a measurement file. Its assumptions
  are in its docstring and below.
- `tests/test_news_checkpoint.py` `BoundedListing` measures the run listing on a 1,500-run history
  (6,000 raw objects, about 16 days at `*/15`).

## Observed

| Measure | Before (#247 head 832a6cb) | Now (bounded listing) |
| --- | --- | --- |
| Class A per cycle, steady (192 cycles, expiry on) | 170 | 170 |
| Class B per cycle, steady | 231, plus one content replay a day (428 at 192 runs: 4 GETs per raw run) | 233, no replay spike (max 42 private) |
| Listings of `news/v1/runs/` per cycle | 4, each over the whole raw history | 4, each after the frontier only |
| Run keys listed per cycle, 1,500-run history | > 6,000 per listing | ≤ 16 in total (`BoundedListing`) |
| ListObjectsV2 for runs per cycle, 1,500 runs | ≥ 2 × 6 + … (grows) | constant over 10 further cycles |
| Scheduled verification (every 24 h) | content replay: every object read again | full listing compared with the checkpoint; re-reads only changed runs (0 normally) |
| Public objects held / deleted per cycle | 316 / 63 (free) | unchanged |
| Private growth per cycle with no new items | ~58 KB, 6 objects | unchanged |

- **Expiry off** (the shipped `clock_bound_verified: false`): nothing expires. The 6,300-page cap is
  reached after about 100 cycles (~25 h at `*/15`), and then publication pauses (96-cycle run). This is
  unchanged.
- **Live feeds** (2026-11-14, a Saturday, local stores, reads only):
  - All 4 sources were OK, with 82 items on first sight.
  - The feeds' own rates add up to about 35 items/day.
- **Process:** one fixture cycle under the launcher on Linux took 0.45 s with about 29 MB peak.
- **Local wall time is not runtime.** Its growth over many cycles is the local store's directory walk
  (`rglob` over directories left empty by deletes), which R2 does not have.

## Projected

Assumptions (in `tests/news_cost_projection.py`):
- **Prices** (list prices checked 2026-11-14):
  - R2 Standard: Class A $4.50/M, Class B $0.36/M, storage $0.015/GB-month; deletes and egress free.
  - Railway: $0.000463 per vCPU-minute, $0.000231 per GB-RAM-minute.
- **Free tier not subtracted.** It is account-wide and shared with the collectors.
- **Failure headroom:** ×1.25 on every request.
- **Runtime:** about 97 s per cycle (assumed 0.15 s per serial R2 request, plus 10 s for feeds).
- **Compute:** 0.5 vCPU and 0.1 GB while running.
- **Archive growth:** 35 items/day at 4 KB each.
- **Full listing:** 2 full listings of `news/v1/runs/` a day (the verifying read and its save), one
  request per 1,000 raw objects. This is the only growing term.

| Month | `*/15` Class A | `*/15` total | `*/30` total |
| --- | --- | --- | --- |
| 1 | 612 k | **$4.24** | $2.12 |
| 2 | 613 k | $4.25 | $2.13 |
| 3 | 614 k | $4.26 | $2.13 |
| 6 | 617 k | $4.28 | $2.14 |
| 12 | 622 k | $4.31 | $2.16 |

Breakdown at `*/15`, month 1:
- R2 $3.06: Class A $2.76, Class B $0.30, storage < $0.01.
- Railway $1.18.

The full listing's share of Class A is 0.1% in month 1 and 1.6% in month 12. Storage is about
0.18 GB after a month and about 2 GB after a year.

**Reading.**
- With the bounded listing, `*/15` is projected within $5 every month for the first year, with about
  $0.70 of headroom. The headroom rests on the assumed request latency and failure factor (both need
  provider data) and on no free-tier allowance.
- **Correction to the #247 projection.** It omitted the daily content replay, 4 GETs per raw run per
  day. That would have added about 4 M Class B a month (about $1.40) by month 12, on top of the
  listing growth it did show. The bounded design removes both growing terms.
- What still grows:
  - the daily full listing, linear and small;
  - stored bytes;
  - the checkpoint body's bytes, read and written once per cycle as one object each. The request
    counts stay the same; the transfer is free, and its time is part of the runtime assumption.

## Integrity schedule (changed by the bounded listing)

- **Every cycle:** runs after the frontier are listed and verified. A run that is not settled is never
  passed by the frontier, so it is listed every cycle. The head's `frontier_stopped_at` names it.
- **Before any upload at or before the frontier** (a recovered spool with an older run_id, or a clock
  that went backwards): the writer first clears the frontier durably. Covered by tests for recovery, a
  crash after the upload, a crash before it, and a backwards clock.
- **Every 24 h (writers):** a full listing compared run by run with the checkpoint. A rewritten run is
  re-read and fails visibly; a missing run fails visibly (`HistoryCorrupt`).
- **Trade-off:** a change to a run at or before the frontier is found within 24 h, not on the next
  read. `checkpoint compare` finds it at once. The full content replay now runs only when the
  checkpoint is missing, corrupt or incompatible, or on `checkpoint rebuild`.

## Retention cleanup (included above)

- Snapshots are capped at 3: 68 free deletes per cycle in the private store.
- Retired public incarnations are deleted after the reader grace once expiry is enabled: 63 free
  deletes per cycle.
- Raw runs, commit records and receipts are kept (immutable history, unchanged).

## Needs provider access (Chej's provider work)

1. R2 request latency from the scheduler host: it decides the real runtime and the Railway figure.
2. Current account R2 usage against the free tier.
3. Railway cron metering for a one-shot service, and its SIGTERM-to-SIGKILL grace on stop. The
   launcher's interrupt grace is 90 s.
4. The store-clock bound (`clock_bound_verified`).

## November 15 deployment revision

The earlier $4.31 listing-only Railway estimate is historical. The Linux Box scheduler has no incremental host subscription. Every settled raw run is byte-verified daily across 96 quarter-hour slots. Missed slots are checked together on the next writer cycle. At 96 runs/day, month 12 averages roughly 33,100 retained runs, or 3.97 million additional monthly GETs. At the stated 1.25 retry factor and list prices that adds about $1.79 to monthly R2 costs. The launcher adds two conditional writes and one read per successful run, about $0.034/month with headroom. Using the earlier fixture operation mix gives about $4.96/month in month 12 at gross rates. Public reads, actual publication sizes, retries and feed growth vary.

The scheduler uses a private SQLite ledger (NEWS_BUDGET_DB). It conservatively reserves each outgoing datastore request before transmission, including retries. It limits monthly writer requests to $4.40 and total PUT bytes since baseline to 20 GB ($0.30/month). It credits no deletes or overwritten bytes automatically. The remaining $0.30 covers other traffic. Direct public readership is outside the writer's meter. The shared account free tier is excluded from these estimates. Inspect actual R2 usage and reconcile stored bytes with both bucket listings under a stopped scheduler before changing the baseline. A budget refusal requires investigation before restoring operation.
