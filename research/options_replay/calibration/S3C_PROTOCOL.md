# S3c protocol: combined pricing model (predeclared)

This file and `s3c_protocol.json` were committed before any S3c fitting. The S3b protocol,
thresholds, results and `attempts.jsonl` stay as they are, for comparison.

## What S3c builds

S3c builds one combined quote model, which becomes generator `sim-v2`. `sim-v1` stays regenerable:
existing seeds and records are untouched. The model has these components:

1. **Variance clock**: the S3b business-time weights through 16:00, plus a post-close
   component (see *Expiration*).
2. **Volatility state**: a level σ0, plus an intraday response to the underlying,
   σ_t = σ0 · (S_t / S_ref)^β.
3. **Skew/convexity**: σ(K) = σ_t · (1 + a·z + b·z²), with z = ln(K/S) / (σ_t √τ), on one surface
   per symbol.
4. **Call/put consistency**: calls and puts at a strike share σ(K). With r = 0 this makes model mids
   satisfy C − P = S − K exactly.
5. **Spreads**: a symbol-specific half-spread h = h0 + h1·mid per window. Bid and ask are mid ∓ h,
   rounded outward to $0.01, with bid ≥ 0 and ask ≥ $0.01.

Every parameter is fitted on development data only.

## Expiration and settlement convention (stated, not assumed away)

- Expiring SPY and QQQ options trade until **16:15 ET**, while Cboe's same-day index options stop at
  16:00. Automatic exercise is decided from the underlying's **16:00 close** (OCC
  exercise-by-exception at $0.01 in the money, physical delivery). Holders can still give contrary
  exercise instructions after the close. An OCC education page gives the exchange cutoff as
  4:30 PM CT (17:30 ET); brokers set earlier cutoffs.
- These facts come from secondary sources plus that OCC education page. The Cboe rulebook text was
  not retrieved; treat them as such.
- Model convention: *remaining trading time* runs to 16:15. Premium after 16:00 is valued as
  remaining variance from after-hours underlying moves until the exercise decision. This is a
  post-close variance term, fitted per symbol on development quotes from 16:00-16:14 (the
  collector records through ~16:14).
- A 15:55 premium is not the endpoint. Expiration behaviour is checked at 15:45, 15:55, 16:05 and
  16:10.

## Evaluation split: forward, because nothing untouched remains

Every existing intraday collector day has informed a model choice: the S3b development, held-out
and exploratory days for QQQ (2026-06-24..2026-11-13) and SPY (2026-10-08..2026-11-13). None of them
is held-out evidence in S3c.

| Role | Days |
|---|---|
| Development (fit + development-only results) | all existing QQQ pool days and SPY days up to 2026-11-13 |
| **Held-out** | QQQ and SPY collector sessions dated **2026-11-16 or later** that pass the Replay gates, evaluated once the **first 10 sessions per symbol** exist |
| Informed re-use (comparison only, labelled) | the S3b held-out days, reported alongside development, never as held-out |

**Coverage gap:** as of 2026-11-15 there are zero untouched intraday days. No held-out result is
possible before about 2026-11-30, when 10 new sessions per symbol exist. Until then every S3c number
is development or informed re-use and is labelled that way. **Next-session expiry stays
unsupported**: neither archive records it intraday.

## Cells

Symbol {QQQ, SPY} × moneyness {ATM, 1-OTM} × side {call, put} × window
{open 09:45-10:30, midday 11:30-13:30, close 14:30-15:45, **endpoint 15:50-16:10**}.

## Tolerances (all held-out; per supported cell × window unless stated)

S3b thresholds are carried over unchanged. They apply to the open, midday and close windows:

| Id | Pass if |
|---|---|
| A1 | median \|mid error\| ≤ $8.00 / contract (anchored σ0) |
| A2 | \|median mid error\| ≤ $4.00 |
| A3 | median \|30-min mid-to-mid hold profit error\| ≤ $4.00 |
| A4 | median \|quiet-spot 15-min extrinsic change error\| ≤ $3.00 |
| B1 | median model/recorded mid ratio in [0.85, 1.15] (simulation-equivalent σ0) |
| B2 | median \|30-min hold profit error\| ≤ $6.00 (simulation-equivalent) |

New S3c checks:

| Id | Check | Pass if | Rationale |
|---|---|---|---|
| S1 | half-spread | median \|model − recorded half-spread\| ≤ $0.01 (one tick) per contract-share | a tick is the quote resolution |
| S2 | quote validity | 0 crossed quotes, 0 negative bids, 100 % of prices on the $0.01 grid (all quotes generated over the cells) | hard invariant |
| C1 | strike convexity | 0 violations of call/put monotonicity or butterfly ≥ 0 in model mids (every evaluated snapshot, all strikes) | no-arbitrage invariant |
| P1 | call/put consistency | model \|C − P − (S − K)\| ≤ $0.005 per share at every strike | r = 0 parity, rounding only |
| U1 | underlying response | median ratio of model to recorded 5-min Δmid/ΔS (on moves ≥ 0.05 %) in [0.85, 1.15] | delta/gamma experience |
| Q1 | quiet-spot drift net of IV | median \|model Δextrinsic − (recorded Δextrinsic − IV-movement part)\| ≤ $3.00 | the theta part only, after removing the change in recorded ATM IV between the two times; the half-spread test alone does not identify pure theta |
| E1 | expiration path (endpoint window) | median \|mid error\| ≤ $6.00 at each of 15:55, 16:05, 16:10 | about one round-trip friction |
| N1 | net trade profit | median \|model − recorded net profit\| ≤ $6.00 for 30-min long holds filled at ask and exited at bid, $0.65/contract/side fees (simulation-equivalent σ0) | what the trainee sees |

A cell × window with fewer than 30 held-out samples is "insufficient". A component (clock,
volatility response, skew/convexity, spreads, expiration) is called **acceptable** only when every
check it governs passes on held-out.

## Comparisons

Each run reports the elementary baseline (S3b), `sim-v1`, the S3b candidate and the combined
model, with quote errors and sample net-trade profits, so every miss sits beside its baselines.

## Correction budget

- At most **one** bounded correction per component: five in total. Each is a single named change,
  fitted on development only.
- At most **six** held-out evaluations in total: the declared model plus up to five corrections.
- Tolerances never move. Every development and held-out run is appended to `s3c_attempts.jsonl`.

## Carried misses from S3b (stay open until a held-out pass)

These remain open: level (A1), opening-period decay (A4 at the open) and hold profit (A3/B2). An
S3c development improvement on them is not a validation.

---

## Amendment 1 (2026-11-15, after the S3c return; before any correction is fitted)

Nothing above is edited. The frozen candidate (`f99b361`, `s3c_params.json`, generator `sim-v2`), the
tolerances, the correction budget and the forward held-out reservation (sessions from 2026-11-16) are
unchanged. Reserved forward sessions are not inspected for any choice below.

### A1.1 Expiration conventions, verified and kept distinct

| Convention | What it is | Source | Verification |
|---|---|---|---|
| **Last trading time** | Expiring equity/ETF options (incl. SPY, QQQ) trade until **16:15 ET**. Index options such as SPXW stop at 16:00. | SEC Release No. 34-105153 (SR-CBOE-2025-079, Cboe Exchange, 2026-04-06): "as is the case for ETFs that trade until 4:15 p.m., trading on the expiration day of equity options will continue until 4:15 p.m." Cboe Equity Options Extended Trading Hours FAQ: the 4:00-4:15 window for those ETF classes is Regular Trading Hours, not a separate session. | primary text read (SEC filing extracted locally) |
| **Exercise-by-exception reference** | OCC automatically exercises expiring equity/ETF options in the money by **≥ $0.01**, judged against the underlying's **last sale during regular trading hours** (the 16:00 close). Delivery is physical. | OCC Rule 805 (threshold lowered to $0.01: SR-OCC-2007-18, Federal Register 73 FR 4297, 2008-01-24). Reference price: OCC *Unscheduled Market Closings* guide describing Rule 805. | **secondary for the reference price**: OCC pages return HTTP 403 to this environment, so the guide's wording comes from search excerpts only |
| **Contrary-instruction deadline** | Holders have until **17:30 ET** on the expiration business day to decide to exercise or not. Firms may set earlier cut-offs and may not accept instructions after 17:30. Members may submit contrary exercise advice for customer accounts until 19:30 ET in specified cases. | FINRA Rule 2360(b)(23)(A)(ii)-(iv) (rule page read) | primary text read |

### A1.2 What these conventions do and do not establish

- Between 16:00 and 16:15 the option still trades, but its exercise outcome is judged against a reference price that is
  already fixed (the 16:00 close). Value after 16:00 can come only from (a) holders' option to override automatic
  exercise using after-hours moves before 17:30, and (b) trading frictions until 16:15.
- **None of these rules establishes that remaining variance decays linearly to 17:30.** The deadline is when the
  decision must be made, not a measured variance profile. The S3c post-close term (and its linear decay to 17:30) is
  therefore a **modelling assumption**, fitted to development quotes. It is labelled that way in `pricing.py`,
  `S3C_RESULTS.md` and the sim-v2 limitations. It is not presented as a settlement rule.

### A1.3 Development candidate `s3c-dev2` (separately versioned; at most one correction per component)

These are fitted on development days (≤ 2026-11-13) only and stored in `s3c_params_dev2.json`, with code in
`pricing_dev2.py`. `pricing.py` and `s3c_params.json` are untouched. Results are development (in-sample) before/after
tables, not validation. Each correction is named and bounded:

| Component | Open miss it targets | Correction (single change) |
|---|---|---|
| clock | A4 opening decay | **implied clock**: bucket weights from the development median decay of ATM straddle-implied remaining variance (09:45-15:55), instead of realized spot returns. Buckets before 09:45 keep the realized-return weight shape. |
| volatility response | A1 level, A3/B2/N1 hold profit | **realized-vol feedback**: σ_t × (k·RV₆₀(t)/σ0)^η, where RV₆₀ is the trailing 60-minute realized vol of the path (known to a generator) and η is fitted. |
| expiration | E1 endpoint | **fitted post-close horizon** (modelling assumption): the post-close variance decays linearly to a horizon H fitted with the tail on development 15:55/16:05/16:10 quotes, instead of the assumed 17:30. |
| spreads | S1 at the QQQ ATM endpoint, N1 | **moneyness-split spreads**: separate h0 + h1·mid for ATM (nearest strike) and non-ATM quotes per window. |
| skew/convexity | none | no correction; budget unused |

The S3c held-out tolerances still apply to whichever candidate is eventually evaluated. The frozen candidate remains
the declared comparison, and any held-out use of `s3c-dev2` counts against the six-evaluation budget.
