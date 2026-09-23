# Short-squeeze scanner panel (OA-191, sequencing step 4)

## What this adds

A "Squeeze" tab: a thin, read-only display of the independently-maintained
[short-squeeze scanner](https://github.com/OwenTanzer/short-squeeze-scanner)'s
published pointers (its `squeeze_scanner/storage.py`'s `upload()`), per
OA-191's confirmed page spec. This page does not fetch Finviz/Tradier, does
not score anything, and does not know the scanner's scoring internals
beyond the field names its published JSON already has -- same
producer/display boundary this repo already draws for
`intraday/latest.json` and `macro/eia_steo.json` (see `shared.js`'s
`formatVwapRvol`/`formatMomentum`/`fmtSteoDelta`).

Source: `docs/shared.js`'s `formatSqueezeScanStatus`/
`describeSqueezeAcquisitionFailure` (pure, tested in
`tests/squeeze_scanner_display.test.js`) plus `docs/index.html`'s
`// ── short-squeeze scanner panel (OA-191)` section (fetch/render, tested
in `tests/squeeze_panel.test.js` the same section-extraction way
`tests/bots_panel.test.js` already tests the Automated tab).

## Revision history

- **2026-09-22**: initial version.
- **2026-09-23**: PR #105 review (round 1) found four issues, all fixed
  here:
  1. `latest.json` only advances on a successful run, so a newer failed/
     partial attempt was invisible. Now also fetches `latest-attempt.json`
     and surfaces `describeSqueezeAcquisitionFailure`'s warning banner
     alongside (not instead of) the last successful table.
  2. Freshness was judged against `published_at` (upload time) instead of
     acquisition time, so a delayed upload of an old run could read as
     live. `formatSqueezeScanStatus` now uses `finished_at`/`started_at`,
     and explicitly renders the scan's calendar date for a prior-day
     result rather than relying on raw age alone.
  3. The localStorage-based "first seen today" tracking (`trackFirstSeenToday`)
     was removed outright, not patched -- see "First-seen tracking" below.
  4. `tests/squeeze_panel.test.js` asserted fixed-date fixtures against
     the real, unfrozen `Date.now()`. `squeezeNow()` is now an injectable
     free-variable clock (matching this file's existing `nowFn`
     convention on `LiveQuotePoller`/`TickerStateStore`), overridden in
     tests instead of ever reading the system clock.

## Why `pointer.candidates` needs no re-filtering or re-sorting here

`storage.py`'s `upload()` already does that before publishing:
`sorted([r for r in rows if r["comparison_eligible"]], key=lambda r:
r["ranks"]["combined"])[:10]`. This page renders exactly that list, in that
order. A consequence worth naming explicitly: **every row in
`latest.json`'s `candidates` array is already options-`valid`** --
`comparison_eligible` is only ever true for that status (see
`archive.py`'s `score_records`). OA-191's page-spec line "missing values
shown explicitly as unavailable" for the Options score column is handled
in `renderSqueezePanel` for robustness (a null `scores.options` renders
"unavailable" rather than blank/NaN), but it won't actually occur against
today's storage layer -- documenting this so a future reader doesn't
assume it's dead code covering something that can't happen; it's covering
a contract the storage layer happens to currently guarantee, not one this
page enforces itself.

## First-seen tracking: removed, not fixed

OA-191's page spec calls for a "First seen today" / "New" badge. The
initial version of this panel approximated that client-side via
`localStorage`, keyed by America/New_York calendar day. PR #105's review
rejected that approach outright rather than asking for a patched version,
for two independent reasons:

1. It cannot establish a canonical, page-level "first seen" fact --
   different viewers' browsers, opening the page at different times,
   would each compute a different answer for the same ticker.
2. The implementation bug this caused in practice: the badge disappeared
   on the very next 60-second poll of an *unchanged* run, because
   "newness" was computed once and consumed immediately as mutated state,
   rather than being a stable property of the run itself.

Per that review, the producer side (`OwenTanzer/short-squeeze-scanner`) is
adding real first-seen metadata to its published schema in a separate PR,
with a documented contract. Until that lands, the "first seen" column
reads a `first_seen_at` field directly off each candidate row (name
provisional, pending that PR's actual contract) and renders `unavailable`
when it's absent -- which is every row today, since the field doesn't
exist yet. This is deliberately inert rather than deliberately
approximate: no guess is better than a wrong one here, and this column
requires zero changes on the display side once the producer ships the
real field -- it already reads it.

## Poll cadence

`SQUEEZE_POLL_MS = 60_000`, matching `fetchSteoCalibration`'s existing
60-second poll for the same reason: the underlying data updates far less
often than that (manual runs today; twice a day once OA-191 step 3 ships),
so this is "notice a new run reasonably soon," not a cadence matched to a
real SLA.

## What this deliberately does not do

- No historical/outcome tables on this page -- OA-191 keeps those in R2 for
  on-demand retrieval, not the website.
- No scan-triggering: opening this tab only ever reads the published
  pointers; it never causes a scan to run.
- No re-implementation of the scanner's scoring, normalization, ranking,
  or run-outcome classification -- if any of those need to change, they
  change in `OwenTanzer/short-squeeze-scanner`, and this page picks up the
  new published numbers on its next poll, unmodified.
