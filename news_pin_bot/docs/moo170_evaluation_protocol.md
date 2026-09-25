# news_pin_bot evaluation protocol (MOO-170)

This freezes the evaluation rules *before* any market trial, per MOO-170's
requirement that thresholds not be tuned on the same sample being
evaluated. It also proposes the bounded trial itself. Nothing here
activates the bot -- it stays exactly as scoped in MOO-170: an
implementation handoff, independent of paper execution.

## What is measured

`Storage.evaluation_report()` (surfaced compactly by `scripts/daily_review.py`)
is the single source of truth. It reports, for the full history in
`market_pin_bot.sqlite3`:

- **By scorer**: sample size, confirmed count, incomplete count -- grouped
  by the exact stored `scorer` string (e.g. `mistral-nemo:latest@v1` vs.
  `vader-fallback`). These are never pooled into one statistic.
- **By score stratum**: every scored headline that got an observation
  window, including "shadow" (non-posting) windows opened for headlines
  scoring below `IMPACT_POST_THRESHOLD` but at/above `SHADOW_SCORE_FLOOR` --
  not just the score>=5 headlines that were actually posted. This is the
  denominator finding 6 asks for.
- **Alert burden**: count of pins actually posted to Discord.
- **Unexplained-move coverage**: how many flagged moves have no matched
  headline (monitored-source coverage limit, not evidence of leaks) vs. how
  many were reconciled by a headline that arrived later.
- **Scoring latency**: ingest-to-scored latency distribution (backlog
  visibility for finding 1).

## Classification semantics (frozen)

A pin's `classification` is one of:

- `already_moving_before` -- price already moved >= `PIN_MOVE_THRESHOLD_PCT`
  in the `PIN_BASELINE_LOOKBACK_SECS` window *before* the headline's own
  ingest time.
- `subsequent_move` -- confirmed post-window move, without a qualifying
  pre-headline move.
- `no_qualifying_move` -- resolved observation, threshold not met.
- `insufficient_evidence` -- missing window price history or inadequate
  baseline volume coverage; never silently treated as "no move."

None of these are causal claims. "Associated with a headline" is an
observation; "no matching headline observed" describes monitored-source
coverage, not proof of absence.

## Control group (approximation, documented)

For each real (non-shadow) pin, a same-symbol, same-time-of-day window with
no qualifying headline nearby is sampled as an approximate no-news control,
via the same `PriceTracker`/`return_over_interval` machinery. This is **not**
a true randomized control -- it is a time-of-day-matched observational
comparison, explicitly reported as such in `evaluation_report()`'s move
frequency vs. baseline. Exclusions: a control window is discarded if it
overlaps another pin's window on the same symbol, or if it falls outside
market hours.

## Exclusion rules (frozen before the trial)

- Observations marked `insufficient_evidence` are excluded from hit-rate and
  control comparisons, but their *count* is always reported (never silently
  dropped from the sample-size line).
- Shadow observations are excluded from `accuracy_stats()` (the
  posting-worthy hit-rate) but included in `evaluation_report()`'s
  stratified denominator.
- No threshold (`IMPACT_POST_THRESHOLD`, `PIN_MOVE_THRESHOLD_PCT`,
  `ANOMALY_ZSCORE_THRESHOLD`, etc.) may be changed based on results observed
  during the trial window below -- a threshold change starts a new,
  separately-reported trial period.

## Proposed bounded market trial

Proposal only -- requires separate sign-off before running, and does not
authorize paper or live trading of any kind.

- **Watchlist**: the existing default (`SPY, QQQ, NVDA, AAPL, MSFT, TSLA`) --
  fixed for the duration of the trial, not adjusted mid-run.
- **Measurement windows**: `PIN_PRE_SECONDS=30` / `PIN_POST_SECONDS=300` for
  pins, `ANOMALY_RETURN_INTERVAL_SECS=60` for the anomaly scanner --
  defaults, unchanged during the trial.
- **Duration**: 10 consecutive US market sessions (2 trading weeks),
  covering multiple market regimes (avoid a single-week special-event bias).
- **Alert-burden target**: no more than ~15 Discord posts/day across the
  6-symbol watchlist combined (pins + unexplained moves) -- if exceeded,
  that's itself a finding (over-triggering), not a reason to retune mid-trial.
- **Evaluation criteria** (reported at trial end via `daily_review.py`,
  aggregated over the full window):
  1. Confirmed-pin hit rate by scorer, with sample size.
  2. Move frequency in confirmed pins vs. the time-of-day-matched control.
  3. Count and rate of `insufficient_evidence` observations (data-quality
     health, not silently absorbed into the denominator).
  4. Unexplained-move match rate (immediate + later-reconciled) vs. no-match
     count.
  5. Scoring latency distribution -- confirms the ingest/score decoupling
     holds up under real ingestion load.

Only after this trial reports its results does any activation or
paper-execution integration decision get made -- that decision is explicitly
out of scope for this document and for MOO-170.
