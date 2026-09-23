# Short-squeeze scanner panel (OA-191, sequencing step 4)

## What this adds

A "Squeeze" tab: a thin, read-only display of the independently-maintained
[short-squeeze scanner](https://github.com/OwenTanzer/short-squeeze-scanner)'s
published `latest.json` pointer (its `squeeze_scanner/storage.py`'s
`upload()`), per OA-191's confirmed page spec. This page does not fetch
Finviz/Tradier, does not score anything, and does not know the scanner's
scoring internals beyond the field names its published JSON already has --
same producer/display boundary this repo already draws for
`intraday/latest.json` and `macro/eia_steo.json` (see `shared.js`'s
`formatVwapRvol`/`formatMomentum`/`fmtSteoDelta`).

Source: `docs/shared.js`'s `formatSqueezeScanStatus`/`trackFirstSeenToday`
(pure, tested in `tests/squeeze_scanner_display.test.js`) plus
`docs/index.html`'s `// ── short-squeeze scanner panel (OA-191)` section
(fetch/render, tested in `tests/squeeze_panel.test.js` the same
section-extraction way `tests/bots_panel.test.js` already tests the
Automated tab).

## Why `pointer.candidates` needs no re-filtering or re-sorting here

`storage.py`'s `upload()` already does that before publishing:
`sorted([r for r in rows if r["comparison_eligible"]], key=lambda r:
r["ranks"]["combined"])[:10]`. This page renders exactly that list, in that
order. A consequence worth naming explicitly: **every row in
`latest.json`'s `candidates` array is already options-`valid`** --
`comparison_eligible` is only ever true for that status (see
`archive.py`'s `score_records`). OA-191's page-spec line "missing values
shown explicitly as unavailable" for the Options score column is handled in
`renderSqueezePanel` for robustness (a null `scores.options` renders
"unavailable" rather than blank/NaN), but it won't actually occur against
today's storage layer -- documenting this so a future reader doesn't assume
it's dead code covering something that can't happen; it's covering a
contract the storage layer happens to currently guarantee, not one this
page enforces itself.

## Known limitation: "First seen today" is per-viewer, not canonical

OA-191's spec: "First seen today: first appearance in that day's displayed
shortlist," updated at the noon refresh with newcomers marked "New."

`trackFirstSeenToday` (shared.js) implements this against `localStorage`,
keyed by an America/New_York calendar day. This means:

- Two browsers loading the page at different times can compute **different**
  "New" badges for the same ticker -- whichever run each browser happens to
  see first, on that browser, is what "not new" gets measured against.
- Clearing site data, using a different browser/device, or private browsing
  resets a viewer's memory, so previously-seen tickers reappear as "New."

This is a real limitation, not a permanent design choice. The correct fix
needs one of:

1. The scanner's archive publishing a `first_seen_at` field per ticker
   (computed server-side against its own run history), or
2. The archive exposing a small same-day run-history index this page could
   fetch and diff against instead of trusting client memory.

Neither exists yet -- OA-191 step 3 (scheduled collection) is unimplemented,
so there is at most one manual run "per day" in practice today, which makes
this limitation currently unobservable (nothing to diff against yet) but not
actually fixed. Revisit once scheduled 9am/noon runs exist and there's a
real same-day pair to compare.

## Poll cadence

`SQUEEZE_POLL_MS = 60_000`, matching `fetchSteoCalibration`'s existing
60-second poll for the same reason: the underlying data updates far less
often than that (manual runs today; twice a day once OA-191 step 3 ships),
so this is "notice a new run reasonably soon," not a cadence matched to a
real SLA.

## What this deliberately does not do

- No historical/outcome tables on this page -- OA-191 keeps those in R2 for
  on-demand retrieval, not the website.
- No scan-triggering: opening this tab only ever reads `latest.json`; it
  never causes a scan to run.
- No re-implementation of the scanner's scoring, normalization, or ranking
  -- if any of those need to change, they change in
  `OwenTanzer/short-squeeze-scanner`, and this page picks up the new
  published numbers on its next poll, unmodified.
