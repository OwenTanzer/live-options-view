# Short-squeeze scanner (rules/factor + options composite)

## Why this exists

A request to build a short-squeeze detection bot. Web research (see PR
description / commit history) turned up no credible pretrained model for
this -- every real implementation, institutional or hobbyist, is a
composite score over a handful of known mechanical inputs, not a
classifier. This adds that composite score as a new, self-contained module
(`squeeze_scanner/`) rather than folding it into `collector.py` or
`crassus/`, since it's a different universe (finviz's whole small/mid-cap
short-interest list, not the SPY/QQQ 0DTE names this repo otherwise
tracks) and a different cadence (on-demand scan, not a continuous poll
loop).

## What it does

1. `finviz_client.py` pulls finviz's short-interest screener (short float %,
   days to cover / short ratio, float size, relative volume, 5-day
   performance) via the `finvizfinance` package.
2. `tradier_options.py` pulls each candidate's near-dated options chain from
   Tradier (this repo's existing token, `TRADIER_TOKEN`) and computes a
   dealer-gamma-exposure estimate (standard "customers net long calls / net
   short puts" convention, same one public GEX trackers use) plus a call/put
   OI skew.
3. `scoring.py` turns both into two independent 0-1 scores (pure functions,
   unit-tested, no I/O) and blends them 60/40 factor/options into a single
   composite -- see that module's docstring for the exact weights and why a
   missing options signal falls back to the factor score alone instead of
   being zeroed.
4. `scan.py` is the CLI: finviz candidates -> per-candidate Tradier lookup ->
   ranked table, optionally written to CSV.

## Known gaps (deliberately deferred, not forgotten)

- **IV rank is a proxy, not real.** A true IV rank needs a persisted
  52-week IV history per underlying; this scanner doesn't have anywhere to
  store that yet. `scan.py` currently maps ATM IV linearly onto a generic
  20-150% band as a placeholder. Fixing this properly means either a small
  R2/SQLite time series (mirroring how `collector.py` already persists
  other series to R2) or accepting a third-party IV-rank source.
- **No sentiment/social layer.** The original ask also covered a WSB/Reddit
  sentiment scanner and a FinGPT-based news layer; those were scoped as
  separate follow-ups, not built here, since they're a different data
  source category (unstructured text) from the two numeric groups this PR
  covers.
- **Normalization ranges are not backtested.** `scoring.py`'s
  `_SHORT_FLOAT_RANGE`, `_DAYS_TO_COVER_RANGE`, etc. are reasonable priors,
  not fit to historical squeeze data. Once `scan.py` has run for a while
  and produced real ranked history, revisit these against actual outcomes.
- **No alerting/scheduling.** This is a script you run, not a monitor.
  Wiring it into a Railway cron (like `moo144-tradier-probe`) is a natural
  follow-up once the scoring itself has been eyeballed against a few live
  sessions.
