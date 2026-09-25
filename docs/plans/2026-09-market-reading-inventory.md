# Market reading ownership inventory

Issue: #107 (work item 1)

## Purpose

Issue #107 observes that several derived market readings are only visible
through a particular strategy's own implementation, and asks for an
inventory before deciding, per reading, whether it needs a standing
collector/research time series independent of bot activity, shared context
on every relevant decision in the private Crassus ledger, bot-local
metadata, or a combination. This document is that inventory (work item 1)
and the ownership table required by acceptance criterion 1. It makes no
code changes and proposes no policy changes -- work items 2-5 are follow-up
work, tracked against this doc.

## Method

Traced each reading from its raw feed event or snapshot field through to
whatever eventually displays or decides on it, reading `collector.py`,
`market_signals.py`, `crassus/crassus/market.py`, and each strategy module
directly, plus the existing design docs (`docs/plans/2026-07-vwap-rvol.md`,
`docs/plans/2026-07-momentum-indicator.md`) for readings that already went
through a similar exercise. Current as of `origin/master` at commit
`2412771` (PR #100 merged, plus its post-merge follow-ups through
`2d5a8ec`).

## Inventory

### VWAP + RVOL

Computed and published together; treated as one entry since they share a
source, a timestamp, and a freshness contract.

- **Raw source**: DXLink `Summary`/`Trade` events -> collector's per-cycle
  spot price and `dayVolume` (`collector.py` `_resolve_underlying_spot`;
  `market_signals.py:76` `accumulate_vwap`, `:163` `compute_rvol`).
- **Formula/parameters**: VWAP is spot-price-at-snapshot x
  volume-delta-since-last-snapshot, accumulated intra-session
  (`docs/plans/2026-07-vwap-rvol.md`). RVOL is current session volume
  against the mean of the available historical readings for the same
  5-minute time-of-day bucket. The baseline window is **calendar** days,
  not trading sessions: at end-of-session `collector.py`
  `finalize_rvol_baseline` passes `lookback_days` (`RVOL_LOOKBACK_DAYS`,
  default 20) to `market_signals.py` `prune_baseline_samples`, which keeps
  only samples dated in `[today - timedelta(days=lookback_days), today)`.
  Weekends, holidays, and any session where that bucket had no reading
  (e.g. a collector outage or a mid-session crash before finalize) simply
  contribute no sample, so a full window typically holds ~14 trading
  sessions or fewer, and the mean is over however many samples exist.
  `compute_rvol` returns `insufficient_history` below 5 available samples
  (`RVOL_MIN_DAYS_REQUIRED`) -- a count of samples, not of trading days.
- **Timestamps**: `spot_ts`, `vwap_ts`, `session_volume_ts`, and
  `vwap_session_started_at` are all tracked independently
  (`crassus/crassus/market.py:107-118`); `accumulate_vwap` rejects any tick
  that isn't strictly newer than the last one folded in.
- **Freshness/missing-data semantics**: `rvol_status` in `no_data |
  insufficient_history | ok`; `freshness` in `live | stale`;
  `vwap_partial_session` flags a VWAP that doesn't cover the full session
  (`market.py:78-93`).
- **Producer**: `collector.py`'s `_compute_underlying_market`, server-side
  only.
- **Consumers**: published on the shared `underlying_market` block of the
  public `intraday/latest.json` payload; parsed into
  `crassus/crassus/market.py`'s `UnderlyingMarket`; evaluated by
  `crassus/crassus/vwap_rvol.py`'s `evaluate_gate` as an optional
  confirmation for `momentum_qqq` (`vwap_confirmation_required`/
  `rvol_floor`, both off by default).
- **Persistence**: VWAP running sums persisted per-day to R2 for restart
  recovery; RVOL baseline persisted cross-day to R2.
- **Independently observable without a bot running**: **Yes.**

### Reference momentum (collector, display/log)

- **Raw source**: same paired spot price/timestamp as VWAP, fed into a
  rolling in-memory window.
- **Formula/parameters**: `market_signals.py:284`
  `compute_time_series_momentum` -- a deliberate reimplementation of
  `crassus/crassus/momentum.py`'s `compute_momentum`, same status
  vocabulary, explicitly not a shared import across the collector/crassus
  deployment boundary (`docs/plans/2026-07-momentum-indicator.md`).
- **Timestamps/freshness**: same four-state vocabulary as Newton's own
  signal: `no_data | warming_up | stale_anchor | ok`
  (`market.py:95-104`).
- **Producer**: `collector.py`'s `_compute_underlying_market`.
- **Consumers**: published as the `momentum` object on the same shared
  `underlying_market` block, and durably logged as an NDJSON time series at
  `intraday/{date}/momentum_log.jsonl` -- built explicitly because there was
  previously no way to observe the momentum signal outside of any specific
  account's private decision ledger.
- **Persistence**: rolling public per-day log in R2, independent of any
  bot's private ledger.
- **Independently observable without a bot running**: **Yes.**
- **Flag**: this reading is explicitly *not* guaranteed to be bit-identical
  to any specific Newton account's live trading signal (`market.py:98-101`)
  -- a real duplication of the same underlying idea, kept apart on purpose.
  Worth deciding in work item 2 whether that's still the right call now
  that a canonical payload exists for it to import instead.

### Trading momentum (Newton's own signal)

- **Raw source**: `ctx.snapshot.underlying_price`, the same durable
  per-cycle snapshot every strategy reads.
- **Formula/parameters**: `compute_momentum` in
  `crassus/crassus/momentum.py`; `DEFAULT_LOOKBACK_MINUTES=60`,
  `DEFAULT_MAX_ANCHOR_OVERSHOOT_MINUTES=10`; per-account
  `bullish_threshold`/`bearish_threshold` (+-0.30% default).
- **Timestamps/freshness**: anchored to `snapshot.timestamp` (the source's
  own clock, not wall time); statuses `no_data | warming_up |
  stale_anchor | ok`; separately gated by
  `DEFAULT_MAX_SNAPSHOT_AGE_MINUTES=5.0` before a stale collector read ever
  reaches the tracker.
- **Producer**: a module-level singleton `_tracker =
  PriceHistoryTracker(...)` inside `strategies/momentum_qqq.py` -- one
  process-wide QQQ price history shared across every account running that
  strategy, but private to the strategy module.
- **Consumers**: only `momentum_qqq` (Newton) and its puts-only mirror.
- **Persistence**: none beyond that account's own private audit ledger
  record.
- **Independently observable without a bot running**: **No** -- this
  signal exists only in-memory inside the strategy process and is invisible
  unless an account running `momentum_qqq` is actually active.

### Snapshot IV / Black-Scholes comparison (PR #100)

- **Raw source**: DXLink `Greeks` event -> collector's `IV`/`Delta`/
  `Gamma`/`Theta`/`Vega` per snapshot row.
- **Formula/parameters**: `crassus/crassus/black_scholes.py` (pure math:
  `theoretical_price`, `greeks`, `implied_volatility`);
  `crassus/crassus/bs_edge.py`'s `evaluate_edge_gate` feeds the row's own
  `IV` back through `theoretical_price` at the current `underlying_price`
  and time-to-expiry and reports `edge_pct` against `max_edge_pct` (default
  0.15).
- **Timestamps/freshness**: no observation timestamp exists for Greeks at
  all -- the row's `underlying_price`/`IV` come from the ~60s durable board
  while the quote it's compared against is a separately fetched ~15s
  execution quote, with nothing to confirm simultaneity. `bs_gate_status`
  in `no_iv | expired | no_snapshot | no_snapshot_row | no_quote | error |
  ok`.
- **Producer**: as of the post-merge follow-ups, this is no longer
  strategy-local. `crassus/crassus/runner.py` calls
  `bs_edge.annotate_buy_decision` on **every** account's buy decision,
  regardless of `strategy_id`, right before it enters the ledger
  (`runner.py:488`) -- so this reading moved from Newton-only
  (`momentum_qqq.py`) to a shared point in the runner, exactly the
  direction issue #107 asks the rest of these readings to move in.
  `bs_edge_diagnostics_enabled` now defaults to **on** (opt-out via account
  params, not opt-in).
- **Consumers**: any strategy whose account reaches a `buy` decision now
  gets this annotation on that decision's metadata. It is still **buy-only**
  -- a `sell`/`no_trade` decision is never annotated, so there is still no
  standing reference series independent of a specific proposed purchase.
- **Persistence**: none beyond that decision's own private audit ledger
  record; no collector-level or public record exists.
- **Independently observable without a bot running**: **No** -- despite
  now being shared across strategies rather than private to one, it is
  still only ever computed as a side effect of an account actually
  proposing to buy, and only stored in that account's private ledger entry.
  This is the reading issue #107 names as the motivating example ("PR #100
  moves the Black-Scholes snapshot-IV comparison from Newton to the shared
  option-buy audit path. It still records only proposed buys.") -- true
  before and after the post-merge follow-ups; only the "which strategies"
  half of that sentence has since changed.

### Put/call ratio (PCR)

- **Raw source**: `OpenInterest` per row, already public on every snapshot
  -- the raw ingredient is independently observable; the derived reading is
  not.
- **Formula/parameters**: sum(put OI) / sum(call OI) across the full chain
  (`crassus/crassus/pcr.py`), scored as a z-score against a trailing 24h
  baseline (`DEFAULT_RETAIN_MINUTES=1440`,
  `DEFAULT_MIN_BASELINE_SAMPLES=10`), deliberately session-spanning rather
  than session-reset.
- **Timestamps/freshness**: keyed off snapshot timestamp; statuses
  `no_data | warming_up | ok`.
- **Producer**: entirely inside `strategies/put_call_ratio.py` -- a
  module-level singleton tracker, same private-to-strategy pattern as
  Newton's own momentum tracker.
- **Consumers**: only `put_call_ratio_qqq`. The collector publishes no PCR
  field on `underlying_market` at all, unlike VWAP/RVOL/reference-momentum.
- **Persistence**: none beyond that account's private audit ledger record.
- **Independently observable without a bot running**: **No.**

### Max-pain strike

- **Raw source**: `Strike`/`OpenInterest`/`Type` per row, same public
  snapshot rows.
- **Formula/parameters**: for each candidate strike K,
  sum(max(0, K-Strike)*OI) over calls + sum(max(0, Strike-K)*OI) over puts;
  the minimizing K wins (`strategies/max_pain.py`). Guarded by
  `pin_threshold_pct` (default 0.15%) and `min_strikes_with_oi` (default 5,
  both sides) before the strike is trusted.
- **Timestamps/freshness**: none needed -- a stateless, pure per-snapshot
  computation with no history or tracker, recomputed fresh every cycle.
- **Producer**: entirely inside `strategies/max_pain.py`, no collector
  involvement.
- **Consumers**: only `max_pain_qqq`.
- **Persistence**: none beyond that account's private audit ledger record.
- **Independently observable without a bot running**: **No** -- even
  though it needs no accumulated state, it is never computed or exposed
  unless that specific account's strategy runs a cycle; there is no
  standing reference series anywhere.

### Near-money open-interest skew

- **Raw source**: `Strike`/`OpenInterest`/`Type` per row, filtered to a
  `near_money_pct` band (default 2%) around `underlying_price`
  (`strategies/oi_skew.py`).
- **Formula/parameters**: sums near-money call OI vs. put OI into an
  imbalance ratio; requires the imbalance to both clear
  `imbalance_threshold` (default 0.3) **and** move at least
  `min_change_from_session_start` (default 0.1) from the session's earliest
  recorded reading -- a deliberate anti-false-positive design against QQQ's
  naturally skewed opening OI. Also gated by `DEFAULT_MIN_BAND_STRIKES=4`.
- **Timestamps/freshness**: a `SessionImbalanceTracker` keys strictly-newer
  `snapshot.timestamp`s and resets on calendar-date rollover -- same dedup
  discipline as Newton's tracker.
- **Producer**: entirely inside `strategies/oi_skew.py` -- a module-level
  singleton, private to the strategy, one per process.
- **Consumers**: only `oi_skew_qqq`.
- **Persistence**: none beyond that account's private audit ledger record.
- **Independently observable without a bot running**: **No** -- same
  failure mode as PCR: the session-start anchor and current imbalance only
  exist while that strategy's process/account is running.

## Persistence model, generally

Every strategy decision (trade or no-trade) an active account reaches is
written to a durable, append-only, but private JSONL ledger with 19
mandatory fields per `crassus_golden_goose_guidance.v1`
(`crassus/crassus/audit.py`), incrementally backed up to R2
(`crassus/crassus/archive.py`). What lands in that ledger, beyond the
mandatory fields, is whatever metadata the deciding strategy itself
attaches: `runner.py` does not attach PCR, max-pain, OI-skew, or trading
momentum to other strategies' decisions. The only shared annotation the
runner adds is `bs_edge.py`'s `annotate_buy_decision`, and only on option
buys. So the per-strategy readings recorded there are durable **bot-local
metadata** -- option (c) from issue #107 -- not option (b), "shared
context on every relevant decision in the private Crassus ledger", which
no reading currently has (the buy-only Black-Scholes annotation is the
closest, and it covers only one decision type). None of those
strategy-private readings has option (a), a collector/research time
series independent of bot activity, either; only VWAP, RVOL, and
reference momentum do. Which readings need (b) is left to work item 2.

## Ownership table (acceptance criterion 1)

| Reading | Producer | Independently observable | Persistence |
|---|---|---|---|
| VWAP | `collector.py` (`_compute_underlying_market`) | Yes | R2 running-sum state |
| RVOL | `collector.py` (same) | Yes | R2 cross-day baseline |
| Reference momentum | `collector.py` via `market_signals.py` | Yes | Public `momentum_log.jsonl` in R2 |
| Trading momentum (Newton) | in-process singleton, `strategies/momentum_qqq.py` | No | Private audit ledger only |
| Snapshot IV / Black-Scholes comparison | `crassus/runner.py` + `bs_edge.py`, shared across strategies, buy-only | No | Private audit ledger only |
| Put/call ratio | in-process singleton, `strategies/put_call_ratio.py` | No | Private audit ledger only |
| Max-pain strike | stateless per-cycle, `strategies/max_pain.py` | No | Private audit ledger only |
| Near-money OI skew | in-process singleton, `strategies/oi_skew.py` | No | Private audit ledger only |

Five of the eight rows (everything except VWAP, RVOL, and reference
momentum) fail independent observability today. The raw ingredients
(`OpenInterest`, chain rows, Greeks) are already public on every snapshot;
it is specifically the *derived* aggregate -- the ratio, the z-score, the
tracked session baseline, and the reading's own freshness status -- that is
locked inside whichever strategy computes it.

## Conflicting/duplicated definitions found

- **Reference momentum vs. Newton's trading momentum** are two independent
  reimplementations of the same time-series-momentum math, deliberately
  kept apart across the collector/crassus deployment boundary, and the code
  documents that they are not guaranteed to be bit-identical. Whether that
  duplication should be resolved (e.g. Newton importing the canonical
  reading rather than recomputing it) is a work-item-2 decision, not made
  here.
- No other reading in this inventory has a second implementation elsewhere
  in the codebase.

## Not done here (work items 2-5)

This document is the inventory only. It does not decide per-reading
ownership (work item 2), define the versioned shared-record contract (work
item 3), implement any shared logging/publishing change (work item 4), or
review the duplicated-plumbing candidates named in work item 5. Those
remain open follow-up work against issue #107, informed by this inventory.
