# S3b calibration protocol: the experience of waiting (predeclared)

This file and `protocol.json` were committed **before** any fitting or held-out evaluation.
The commit that adds them is the predeclaration. Results that come later never change them. If a
rule turns out to be wrong, the change is a new, dated section here, with the earlier result left
as it was.

## Question

When a trainee holds a 0DTE option and the underlying is quiet, does the Simulation premium
change the way recorded premiums do? The comparison covers time of day, moneyness, side and
symbol. Separately, are Simulation premium levels right? The S3a check measured 0.36-0.56× of
recorded ATM straddles; that is a concrete miss.

## Data (read-only R2, live collector snapshots)

- **QQQ** `intraday/{YYYYMMDD}/snapshot_*.csv`, days in the Replay pool (`qualified_days.json`, 97 days,
  2026-06-24..2026-11-13).
- **SPY** `intraday/spy/{YYYYMMDD}/snapshot_*.csv`, 27 days 2026-10-08..2026-11-13. These go through the
  same quality gates as Replay: parity consistency, snapshot age ≤ 150 s, and the feed not frozen.
- Grid: the latest usable snapshot at or before each 5-minute mark, 09:45-15:55 ET. A grid point
  whose latest snapshot is older than 150 s, or fails a gate, is dropped (not filled).

## Split (chronological, fixed now)

| Symbol | Development (fit) | Held-out (evaluate once) |
|---|---|---|
| QQQ | pool days 2026-06-24 .. 2026-09-30 | pool days 2026-10-01 .. 2026-11-13, **minus** exploratory days |
| SPY | 2026-10-08 .. 2026-10-23 | 2026-10-26 .. 2026-11-13 |

**Exploratory days** are the recorded days I already opened while building S1-S3a, including the
14-day S3a straddle comparison: 2026-06-29, 07-13, 07-28, 07-31, 08-07, 09-01, 09-08, 09-11, 09-30,
10-22, 11-02, 11-03, 11-05. They are never used as held-out evidence. Those inside the development
window may be used for fitting. Results on them are reported only as "exploratory".

## Cells

Symbol {QQQ, SPY} × moneyness {ATM = strike nearest spot, 1-OTM = next strike away from spot on that
side} × side {call, put} × expiry {0DTE, next-session} × window {open 09:45-10:30, midday
11:30-13:30, close 14:30-15:45}.

**Next-session expiry is unsupported.** Neither symbol's intraday archive records a non-0DTE
expiry; the public chain files carry one pre-open snapshot per day. Those cells are reported as
unsupported, never substituted.

## Quantities

- `mid = (bid + ask) / 2` of the recorded quote. `extrinsic = mid − intrinsic(spot)`.
- **Error** `e = (model_mid − recorded_mid) × 100`, in **dollars per contract**.
- **Quiet spot**: a 15-minute pair (t, t+15) with |ΔS| ≤ 0.02 % of S.
  - The recorded extrinsic change on such a pair is the *waiting experience*.
  - It is decomposed into a time part (model at fixed σ), a volatility part (change in ATM implied
    vol inverted from the recorded straddle) and spread noise (the recorded half-spread, which is
    the noise floor). The share of changes smaller than the half-spread is reported.
- **Fixed-spot / less-time control**: the model evaluated at S(t) with time advanced 15 minutes,
  compared with the recorded quiet-spot change.
- **Expiration convergence**: recorded and model extrinsic at 15:45 and 15:55.
- **Trade-profit effect**: for every grid entry, mid-to-mid profit of a 30-minute long hold
  (entry t, exit t+30) using model mids, minus the same using recorded mids, in dollars per contract.

## Models

- **Baseline (elementary)**: Black-Scholes, r = 0, no dividend. Constant σ, uniform calendar clock
  to 16:00, no skew.
- **Candidate**: the same, with *business time*. Remaining variance time is the sum of intraday
  variance weights per 5-minute bucket, fitted on development spot returns only. That is the
  only structural change; anything else is a declared correction (below).
- **Reference**: `sim-v1` (the S3a generator's pricing given the same spot), reported to show the
  starting miss.

Volatility input, two conditions:

- **A, anchored**: σ is the ATM implied vol inverted from the day's recorded 09:45 straddle,
  one number per day, held all day. This isolates the decay and moneyness shape from the level.
- **B, simulation-equivalent**: σ = k × the same day's realized volatility of the 5-minute grid spot
  path. A generator knows its own path; it does not know the day's quotes. k is one number per
  symbol, fitted on development days.

## Predeclared tolerance (held-out, per supported cell and window)

The rationale is the trainee's own friction. A recorded ATM 0DTE round trip costs about $3.30-$5.30
per contract: fees 2 × $0.65, plus crossing a $0.01-0.02 half-spread twice. A model error inside
one or two frictions does not change whether a typical trade is worth taking.

| Id | Condition | Metric | Pass if |
|---|---|---|---|
| A1 | A | median \|e\| (level) | ≤ $8.00 / contract |
| A2 | A | \|median e\| (bias) | ≤ $4.00 / contract |
| A3 | A | median \|30-min hold profit error\| | ≤ $4.00 / contract |
| A4 | A | median \|quiet-spot 15-min extrinsic-change error\| | ≤ $3.00 / contract |
| B1 | B | median(model_mid / recorded_mid) | within [0.85, 1.15] |
| B2 | B | median \|30-min hold profit error\| | ≤ $6.00 / contract |

B1 is relative. A generator cannot know a real day's implied level, only a level consistent with
its path, so the test is for systematic bias. The ±15 % band is about a quarter of the S3a miss
(44-64 % short). A cell passes when all six pass. A cell with fewer than 30 held-out samples in a
window is reported as "insufficient", not passed.

## Correction policy

- Each material miss gets at most **two** bounded corrections. A correction is one named change,
  fitted on development data only.
- After each correction the held-out set is re-evaluated once, with tolerances **unchanged**.
- Every attempt is reported, including the ones that still miss.

## Out of scope (carried to S3c)

Bid/ask spread width, call/put parity residuals and the static donor smile are combined-model
work. A decay fit does not resolve them, and these results do not claim to.
