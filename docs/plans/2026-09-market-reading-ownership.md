# Market reading ownership decisions

Issue: #107 (work item 2)

## Purpose

Work item 2 asks, for each reading in the inventory
(`docs/plans/2026-09-market-reading-inventory.md`), whether it needs:

- **(a)** a collector/research time series independent of bot activity,
- **(b)** shared context on every relevant decision in the private Crassus
  ledger,
- **(c)** bot-local metadata,

or a combination, with the reason and, where a reading is to be shared, a
canonical parameter choice. This document records those decisions. It
changes no code and no strategy behavior. The record contract (work item 3)
and the implementation PRs (work item 4) are follow-up work. This doc only
names what they need to cover.

Current as of `origin/master` at `b10679d` (PR #108 merged).

## How the decisions were made

1. **Measurement vs. interpretation.** Each reading is split into the
   market measurement (a ratio, a strike, a price average) and what a
   strategy does with it (a threshold, a trailing baseline, a z-score, an
   entry/exit rule). Only the measurement is a candidate for (a). The
   interpretation stays (c): issue #107 says not to assume a strategy's
   thresholds or rolling baselines are universal.
2. **(a) requires one definition independent of account parameters.**
   When the measurement depends on a per-account parameter (the OI-skew
   band, the momentum lookback), the shared series uses one named
   **reference** parameter value. Accounts keep their own configured value.
   The reference series never replaces an account's input. This is the
   pattern reference momentum already follows.
3. **(b) by reference, not by copying.** Every ledger record already
   carries `market_snapshot_timestamp` and `market_snapshot_url_or_hash`
   (`runner.py` `_run_account`, `audit.py` `MANDATORY_FIELDS`). If that
   identity points at an immutable archived input, every (a) reading is
   joinable to every decision, trade or no-trade, from any strategy,
   without the runner computing or copying strategy readings into other
   strategies' records. Values are copied onto a decision only when that
   decision actually used them, e.g. the Black-Scholes annotation and the
   VWAP/RVOL gate inputs. The annotation then carries the exact values and
   timestamps that were observed.
4. **No behavior change** (acceptance criterion 5). No decision below
   changes an input, threshold, veto, or sizing path of any strategy.
   Where a canonical choice differs from what an account uses, the account
   is untouched.

## Findings from verification

Checking the inventory against source for this work surfaced three things
the decisions depend on.

### F1 -- VWAP/RVOL are observable live, but not kept as a time series

The inventory marks VWAP and RVOL "independently observable: Yes", which is
true of the **current** reading. No per-snapshot history of them is
persisted:

- The `underlying_market` block exists only in `intraday/latest.json`,
  which is overwritten every snapshot (`collector.py` `take_snapshot`).
- `intraday/{date}/vwap_state.json` holds only the latest running sums for
  restart recovery (`_persist_vwap_state`), overwritten each cycle.
- The RVOL baseline file holds one end-of-session cumulative volume per
  5-minute bucket per day (`finalize_rvol_baseline`), not intraday
  readings.
- The archived snapshot CSVs (below) carry option rows and
  `UnderlyingPrice`, but not the underlying's `dayVolume`, so VWAP/RVOL
  cannot be reconstructed from them afterwards.

Reference momentum is the only `underlying_market` reading with a durable
history (`intraday/{date}/momentum_log.jsonl`).

### F2 -- the chain rows are archived per snapshot

`take_snapshot` writes the same `rows` list to both
`intraday/{date}/snapshot_{HHMMSSffffff}.csv` (durable, one object per
snapshot) and `intraday/latest.json` (the board Crassus reads). So the raw
inputs to PCR, max pain, and near-money OI skew (`Strike`, `Type`,
`OpenInterest`, `UnderlyingPrice`) already have a durable per-snapshot
history, and any of those readings can be recomputed for a day when no bot
ran. Caveats that matter for replay and for work item 3:

- The CSV is written **before** the `bid_count == 0` check, so the archive
  contains snapshots that were never published to `latest.json` and never
  seen by a strategy.
- The 0DTE row builder writes `data.get("oi", 0) or 0`, so an
  `OpenInterest` that never arrived and a real zero are indistinguishable,
  in both the CSV and `latest.json`.
- The ledger's `market_snapshot_url_or_hash` is
  `{latest.json URL}#sha256:{payload hash}` (`market.py`
  `MarketSnapshot.provenance`). The URL is mutable, and `MarketSnapshot`
  does not keep the payload's `snapshot_key`, so today there is no exact
  link from a ledger record to its archived CSV. The payload hash cannot
  validate a CSV either, because it hashes the full JSON payload, not the
  chain rows. Timestamps are not an exact join key: `take_snapshot` reads
  the clock twice (`ts_et = datetime.now(ET)`, then
  `ts_utc = datetime.now(timezone.utc)`). The CSV key is built from
  `ts_et`, while the payload `timestamp` (and so the ledger's
  `market_snapshot_timestamp`) is `ts_utc`. The two are microseconds apart,
  so converting the ledger timestamp to ET does not reproduce the CSV key.
  Historical matching by nearest timestamp is therefore approximate and
  unverified, and can be ambiguous when snapshots are close together (e.g.
  after a rapid restart). A deterministic link needs `snapshot_key`
  propagated into provenance ("(b) mechanism" below).

### F3 -- the OI-skew session tracker is not safe for more than one account

`strategies/oi_skew.py` keeps one module-level `SessionImbalanceTracker`
per runner process. The runner calls every account on the same snapshot in
one process (`runner.py`), and the imbalance recorded is computed with the
**calling account's** `near_money_pct`. Driving `_decide_core` three times
with one tracker and one snapshot (accounts at 2%, 2%, 4%):

- the first account records a reading and decides normally,
- the second, with identical parameters, gets "Snapshot timestamp ... is
  not newer than the last recorded reading" -- a stale/duplicate no-trade,
- the third, at 4%, gets the same no-trade, and its
  `session_start_imbalance` is the 2% account's value (0.4286) rather than
  its own (0.875).

So with two or more `oi_skew_qqq` accounts in one process, every account
after the first never trades, and a differing band reads another band's
anchor. `put_call_ratio_qqq` and `momentum_qqq` do not have this problem.
Their shared trackers store a parameter-free input (PCR, price), and a
repeat snapshot skips the append but still computes the signal. This is a
strategy-behavior bug, related to #69's module-global tracker lifecycle
but distinct from it. It is **out of scope** here and should be tracked
separately. It also rules out the shared tracker as the source of any
shared OI-skew reading.

## Decisions

| Reading | (a) research series | (b) shared decision context | (c) bot-local | Canonical parameters for the shared part |
|---|---|---|---|---|
| VWAP | Yes -- add per-snapshot history (F1) | By reference | Newton's gate verdict + thresholds | Current collector definition (snapshot-weighted approximation) |
| RVOL | Yes -- add per-snapshot history (F1) | By reference | `rvol_floor` gate | 5-min buckets, 20 **calendar**-day window, min 5 samples |
| Reference momentum | Yes -- as is | By reference | -- | 60 min lookback, 10 min max anchor overshoot; 0.05% neutral band is display-only |
| Trading momentum (Newton) | No | No | Yes -- as is | n/a (per-account) |
| Snapshot IV / Black-Scholes | Not now | Option buys only -- as is | Threshold (`bs_max_edge_pct`) | n/a until Greeks are timestamped |
| Put/call ratio | Yes -- raw ratio + OI totals | By reference | z-score, 24h baseline, `extreme_z_threshold` | `compute_pcr` over board rows |
| Max-pain strike | Yes -- strike + OI coverage | By reference | `pin_threshold_pct`, `min_strikes_with_oi` | `_compute_max_pain` over board rows |
| Near-money OI skew | Yes -- at a reference band | By reference | Account band, session-drift rule, thresholds | 2% band (current default), labeled reference |

"By reference" means (b) is met by joining the decision's snapshot
identity to the (a) series, per rule 3 above. It depends on the follow-up
in "(b) mechanism" below.

### VWAP + RVOL

- **(a)**: already the canonical market-wide definition (issue #37), but
  only live (F1). A per-snapshot durable log of the published
  `underlying_market` values, with their own timestamps and statuses, is
  needed before this reading can be analyzed for a day after the fact. No
  formula change: VWAP stays the snapshot-weighted approximation, and RVOL
  keeps the 5-minute buckets, 20-calendar-day window, and 5-sample minimum
  described in the inventory.
- **(b)**: by reference. Every strategy reads the same board, so a
  decision's snapshot identity determines the VWAP/RVOL it could have seen.
- **(c)**: `momentum_qqq`'s `vwap_confirmation_required`/`rvol_floor` and
  the gate's verdict stay strategy-owned. The gate inputs the strategy
  actually evaluated stay on its own decision metadata.

### Reference momentum vs. Newton's trading momentum

The inventory left open whether the duplication should be resolved by
having Newton consume the collector's reading. **Decision: keep them
separate.** Reasons:

- Newton's `lookback_minutes` and `max_anchor_overshoot_minutes` are
  per-account parameters (`momentum_qqq._decide`) evaluated over one shared
  price history. A single published reading cannot serve accounts with
  different lookbacks.
- The inputs differ. Newton records `snapshot.underlying_price` at
  `snapshot.timestamp`, once per new board (`_last_recorded_snapshot`
  dedup), and measures age against the runner's `now_et`. The collector
  records its resolved spot at `spot_ts` on every collector cycle and uses
  its own `ts_utc` as "now" (`_compute_and_log_momentum`). Switching Newton
  to the collector reading would change its trading input, which
  acceptance criterion 5 rules out without a separate strategy review.

So reference momentum is (a) as it is today, published and logged under an
explicit "reference" label with its parameters. Newton's trading momentum
is (c) only, and a Newton decision's own metadata is its record. Work item
3 must keep the two distinguishable in any shared record (issue #107 names
this explicitly).

### Snapshot IV / Black-Scholes comparison

- **(b), option buys only, as today.** A buy is the only decision where a
  strategy has already observed the quote being compared, and
  `annotate_buy_decision` must reuse that quote rather than fetch a second
  one. That is exactly the "annotation uses the same observed values"
  property work item 4 asks for.
- **Not (a) now.** A standing series would have to pick a quote with no
  proposed trade behind it (e.g. ATM mid each snapshot). With no
  observation timestamp on Greeks, and dxFeed's fixed 30-minute
  time-to-expiry near the close (`bs_edge.py` docstring), such a series
  would largely measure feed timing, not mispricing. Its raw inputs (row
  `IV`, `Bid`/`Ask`, `UnderlyingPrice`) are already archived per snapshot
  (F2), so a research replay can compute it if needed. Revisit once work
  item 3 has timestamped Greeks or the provider's valuation convention.
- **(c)**: `bs_max_edge_pct` is a threshold on a diagnostic that gates
  nothing today, and it stays account-configurable.

### Put/call ratio

- **(a)**: the measurement is the ratio itself: `compute_pcr`'s definition,
  sum(put `OpenInterest`) / sum(call `OpenInterest`) over the board's rows,
  published with both OI totals and a count of rows carrying non-zero OI.
  Zero call OI is an explicit "undefined" status, not a value, matching the
  strategy's treatment. Units: ratio (dimensionless), OI in contracts. The
  F2 zero-vs-missing caveat must travel with it.
- **(c)**: the 24h trailing z-score baseline (`DEFAULT_RETAIN_MINUTES`),
  `min_baseline_samples`, and `extreme_z_threshold` are this strategy's
  interpretation. They are not published as a shared reading, and all of
  them can be recomputed from the (a) series.
- **(b)**: by reference.

### Max-pain strike

- **(a)**: `_compute_max_pain` is a stateless function of one snapshot's
  rows: candidate strikes are every strike on the board, and ties go to
  the lower strike. Publish the strike together with
  `strikes_with_oi_both_sides` so a consumer can apply its own coverage
  rule. Units: strike in USD, coverage as a count of strikes.
- **(c)**: `pin_threshold_pct` and `min_strikes_with_oi`.
- **(b)**: by reference.

### Near-money OI skew

- **(a)**: near-money call OI, put OI, imbalance ratio, and band strike
  count at a **reference** band of 2% of `UnderlyingPrice` (the current
  `DEFAULT_NEAR_MONEY_PCT`), published under that label. Accounts
  configured with another band keep it. The shared series must be computed
  independently of the strategy's `SessionImbalanceTracker` (F3).
- **(c)**: the account's own band, the session-start drift rule
  (`min_change_from_session_start`), `imbalance_threshold`, and
  `min_band_strikes`. The session-start anchor at the reference band can
  be recomputed from the (a) series (the first reading of the session
  date).
- **(b)**: by reference.

### External sentiment

`reddit_sentiment` and `trump_whisperer` inputs were not part of the work
item 1 inventory, and no general-purpose measurement is intended from them
here. They remain (c) unless a later review nominates one.

## (b) mechanism

"By reference" needs a join key that survives `latest.json` being
overwritten. The smallest change that provides one is carrying the
payload's `snapshot_key` (the archived CSV key) into `MarketSnapshot` and
the ledger's snapshot provenance. This is an audit-only change: no
strategy sees a different input. Until then, there is no exact join:
decisions can only be matched to the archive by nearest timestamp, which is
approximate, unverified, and can be ambiguous (F2). Records written before
that change stay approximate.

## Implications for work items 3 and 4

Work item 3's record contract should cover, beyond the fields the issue
already lists:

- a `reference` vs. account-configured marker, and the parameter values
  used (reference momentum, reference OI band),
- the F2 caveats as explicit fields/statuses: OI zero-vs-missing, and
  archived-but-unpublished snapshots,
- snapshot identity (`snapshot_key`) as the join key for (b).

Work item 4 PRs this implies, each separately reviewable, after work item
3:

1. **Collector**: durable per-snapshot log of the `underlying_market`
   block (VWAP, RVOL, reference momentum) -- closes F1.
2. **Collector**: per-snapshot PCR, max-pain, and reference-band OI-skew
   readings. Because these are reimplemented on the collector side of the
   deployment boundary, like reference momentum, they need parity tests
   against the Crassus functions on shared fixture rows.
3. **Crassus**: `snapshot_key` in ledger provenance, per "(b) mechanism".

Separately from #107: F3 (OI-skew multi-account tracker) needs its own
issue and a strategy review, since fixing it changes when `oi_skew_qqq`
trades.

## Not decided here

The record contract itself (work item 3), any implementation (work item
4), and the shared-plumbing review (work item 5).
