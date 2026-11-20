# Credit tape (OA-242) — prototype

Private, unfiltered tape of public credit derivatives reports. Local prototype only:
no Railway service, no R2 or KV writes, no change to the options collector.
Captured data never enters this repository (it is public); tests use synthetic fixtures.

## Components

| File | Role |
|---|---|
| `capture.py` | Runs both routes in one process (`--no-ticker` / `--no-slices`). Polls each ticker, persists every distinct original response (gzip, byte-identical) **before** decoding, appends one event per newly seen publication with first-seen time, raw reference, publisher and jurisdiction. Decode failures still produce an event pointing at the raw payload. Per-source checkpoint, poll log and `health.json`. |
| `serve_private.py` + `web/` | Recent-trades panel. GitHub identity-only sign-in (PKCE, `scope=""`), numeric-ID allowlist, opaque 8-hour sessions, server-side checks on `/api/trades`, `/api/raw`, `/api/export` and the panel assets. Design copied from directional-move-lab `scripts/serve_private.py@2bdebbe`; no shared session, cookie or OAuth app. `CREDIT_TAPE_DEV_LOCAL=1` serves without sign-in on 127.0.0.1 only. |
| `slices.py` | Slice-index route (selected U.S. route). `poll_index` fetches the public `Slice.json` with `If-None-Match` and only **enqueues** unseen `CFTC_CR`/`SEC_CR` slices into `dtcc-slices/pending.json`, which is kept separately from the index ETag (the ETag advances only after pending entries are durable). `drain_live` runs on the live loop every pass, whether the index answered 200 or 304; replay slices (disseminated before the route started) drain on a separate backfill thread. Each slice is published as a durable, idempotent commit keyed by `source/channel/sliceId`: the complete zip is written to `.part`, fsynced, hash-checked and renamed; a commit record (`commits/`) holds every event and alert line plus both logs' pre-append offsets; lines are appended tagged `commit_key`/`commit_line`; then a `done/` marker is written. On restart, any commit without a marker is re-applied from its offsets (torn tail trimmed, present lines skipped, missing lines appended), so a stop at any point gives no loss and no duplicate. A commit whose original ZIP is missing or hash-invalid is never marked complete: the slice stays pending and completes only when re-downloaded bytes match the committed sha256. If the replay worker fails, its error is recorded in health and the collector stops (the service exits 1). `rowCount` is compared (`rowcount_mismatch` alerts). Failed downloads stay pending with per-slice backoff (5 s doubling to 5 min) and do not block other slices. Slice IDs are only unique within a channel (CFTC and SEC can carry the same number), so pending and seen entries are keyed `CHANNEL:sliceId`; legacy bare-ID entries are migrated or dropped safely because done markers are already channel-scoped. Only `https://kgc0418-tdw-data-0.s3.amazonaws.com/{cftc,sec}/slices/...` is fetched. |
| `service.py` | Isolated service entrypoint (release handoff: [RELEASE.md](RELEASE.md)). One process: slice-only collector thread (ticker output off) plus the GitHub-gated panel on `PORT`, archive on `CREDIT_TAPE_ARCHIVE`. Fails closed on incomplete gate config or `CREDIT_TAPE_DEV_LOCAL`; exits non-zero if the collector or replay worker dies. Sign-in flow follows directional-move-lab `serve_private.py@2bdebbe` (pending-claim failure cleanup, one-time replay records) and accepts GitHub's documented `code`+`state` callback (`iss` optional, exact if present). `/healthz/collector` reports per-channel (`CFTC_CR`, `SEC_CR`) state without trade content. Image: `deployment/credit_tape/Dockerfile` (stdlib only). |
| `check_archive.py` | Read-only integrity check for restart verification: duplicates, torn lines, open commits, raw ZIP hashes, `.part` leftovers, per-channel publication counts, ticker publications; `--compare` against an earlier run. Fails on a missing/unmounted archive and on any ticker output. |
| `synthetic_demo.py` | Runnable local slice-only panel demo (no network, no ticker): a CFTC and an SEC fixture slice share one numeric `sliceId`; the process is stopped inside the SEC commit, restarted twice on an unchanged (304) index, then the real panel server (dev-local) is queried over HTTP. Passes only with two publication rows, one per channel, each `/api/raw` link byte-identical to its published ZIP, no duplicates. `python -m market_data.credit_tape.synthetic_demo --out <new empty dir> --serve` then open http://127.0.0.1:8790. |
| `synthetic_crash.py` | Crash-safety run (no network): stops publishing after the raw zip, after the commit record, mid-append (torn line) and after output before the done marker; each restart must leave one intact original, one publication event, one slice event and one alert. `python -m market_data.credit_tape.synthetic_crash` exits 0 only if every stop point passes. |
| `synthetic_resilience.py` | One synthetic run (no network): live polling continues while backfill is blocked mid-slice, and a failed live slice survives a restart and is recovered exactly once while the index keeps answering 304. `python -m market_data.credit_tape.synthetic_resilience` prints the timeline and exits 0 only if every claim holds. |
| `tests/test_credit_tape.py` | Raw-before-decode, no duplicate alert on a repeated payload, decode-failure event, backfill label, every data route 401 without a session, non-allowlisted identity refused, Host check, traversal rejection. |

Run locally:

```sh
python -m market_data.credit_tape.capture --out <private archive dir> --interval 20
CREDIT_TAPE_ARCHIVE=<same dir> CREDIT_TAPE_DEV_LOCAL=1 python -m market_data.credit_tape.serve_private  # http://127.0.0.1:8790
```

## U.S. sources (DTCC public price dissemination)

All unauthenticated HTTPS GETs used by the public dashboard at `pddata.dtcc.com/ppd/`.

| Endpoint | Content | Use |
|---|---|---|
| `/ppd/api/ticker/CFTC/CREDITS` | JSON `{tradeList, errorList}`, 100 most recent CFTC Part 43 credit records | Live capture (20 s poll) |
| `/ppd/api/ticker/SEC/CREDITS` | Same for SEC Regulation SBSR security-based swaps (single names, narrow indices) | Live capture (20 s poll) |
| `https://kgc0418-tdw-data-0.s3.amazonaws.com/dashboard/Slice.json` | Public slice index (~3.6 MB, rolling ~24 h, all asset classes); keys `CFTC_CR`, `SEC_CR` list `fileName`, `sliceId`, `dissemDTM`, `startTs`, `endTs`, `rowCount`, `fullFilePath`. Supports ETag / `If-None-Match` | **Selected route**: enumerate and fetch every unseen slice |
| `/ppd/api/report/intraday/{cftc,sec}/{CFTC,SEC}_SLICE_CREDITS_YYYY_MM_DD_N.zip` | Numbered intraday slice CSVs | Gap recovery, reconciliation |
| `/ppd/api/report/cumulative/{cftc,sec}/{CFTC,SEC}_CUMULATIVE_CREDITS_YYYY_MM_DD.zip` | Full-day CSV (11-19: 2,638 CFTC, 1,266 SEC rows) | Daily reconciliation |

Observed: the ticker window is not strictly newest-first (older records rotate in), so
records disseminated before capture start are labelled `backfill`. Caps are published as
text (`5,000,000+` SEC; `50,000,000+`/`250,000,000+`/`400,000,000+` CFTC; one `4,557,000+`
seen) and kept verbatim. The `/ppd/api/slice/...` listing route used by the dashboard
returned 400 for every parameter form tried; the S3 index above replaces it. The per-change
credit entries of the index are kept verbatim (`dtcc-slices/index/`), not the full 3.6 MB file.

**Terms.** No terms or disclaimer were found on the PPD dashboard or in its app bundle.
dtcc.com/terms (updated 2026-03-11) says some material is governed by separate agreements.
DTCC's licensed automated product is OTC Direct Connect (DTCC Solutions, via AWS Data
Exchange, EULA). No written permission for automated collection, retention or internal
display was found; the data is regulator-mandated public dissemination. Open for a decision.

## UK sources (inventory, not connected)

Under FCA PS24/14 (in force 1 Dec 2025) five-year iTraxx Europe Main and Crossover are
Category 1 (real-time post-trade transparency for venues and OTC firms); single-name CDS
are excluded from Category 1, so single-name UK coverage may be thin or deferred. APAs and
venues must offer post-trade data free of charge 15 minutes after publication; paid
real-time feeds exist (e.g. Tradeweb UK APA data set £13,200/yr flat).

| Candidate | Type | Next step |
|---|---|---|
| Tradeweb Europe UK APA / UK MTF | APA + venue | Locate free 15-min page; check credit coverage and fields omitted vs paid |
| Bloomberg APA / Bloomberg Trading Facility | APA + venue | Locate free delayed page |
| MarketAxess (Trax) APA | APA | Confirm derivatives coverage |
| TP ICAP, BGC, GFI, Tradition UK venues | OTF/MTF | Check credit post-trade pages |

Not connected yet; no zero-activity inference from missing UK coverage.

## Proposed cloud plan (not activated; steps in [RELEASE.md](RELEASE.md))

One isolated Railway service (`credit-tape`) running capture + panel, its own volume for
the private raw archive (or a separate **private** R2 bucket, never `qqq-options-chain-data`
and never KV), its own GitHub OAuth app and env (`CREDIT_TAPE_*`). No dependency on, or
writes to, the options collector, Worker, D1 or existing buckets. Rollback = stop the
service; the archive volume is retained.
