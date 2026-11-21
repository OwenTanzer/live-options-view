# Compact BTC chain fixture evidence

Brian, November 21, 2026. [375×667 screenshot](375x667.png), [measurement and source-hash receipt](receipt.json).

Captured by `tests/btc_compact_chain.test.mjs` using the production entry HTML and disposable preview gateway. All accounts, prices and holdings are synthetic. This is candidate UI evidence, not a production screenshot or independent acceptance.

Seven complete distinct strike rows fit at each of 320×667, 375×667 and 390×667, measured using `getBoundingClientRect()` with top >= 0 and bottom <= viewport height, at scroll offset zero. At 375px the first row begins at 269.1875px and the seventh ends at 640.1875px, including the preview-only banner. No horizontal page scrolling. Calls are left, puts right; the owned first strike supports both close actions. Dotted bids are disabled where no position is owned. Details remain available through the ellipsis.

Reproduce from repository root after installing the existing browser dependencies:

```powershell
$env:BTC_COMPACT_EVIDENCE='.compact-checks'
node tests/btc_compact_chain.test.mjs
```

Tests check reversed input sorting, separate monthly series, empty legs, both call/put buy and owned-close ticket symbols and focus, and stale denial. Existing mobile/API suites cover actual synthetic submissions, provenance, authentication and account races. Release/review and independent production screenshot remain with Owen/operator.
