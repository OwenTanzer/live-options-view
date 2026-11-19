# Findings protocol (frozen before any outcome is computed)

Gamma Exposure and Order Flow, items G4, G5a–c, D1–D3. This file is committed before any feature-outcome join or outcome statistic is computed. The commit hash is the freeze. Anything not specified here and decided later is labelled exploratory in the report.

## Sample

- **Staged sample unsupported.** OA-178 and OA-179 name September 10, 11, 14, 15 and 16. G2 found no trade prints or preceding quotes for any of those dates, so the signed tests cannot run on them. That five-date pilot is reported as unresolved and is **not silently replaced**.
- **Strongest supported sample (labelled substitution).** Every Tradier session from `moo144/tradier/` with summary status `complete`: no partial reasons, zero gap and late-start seconds, 16 same-day contracts. As of freeze this is 36 sessions, Sept 21 – Nov 16, 2026. Partial sessions (Sept 24, Sept 28, Sept 30, Oct 29) are excluded and listed.
- **Discovery.** October 5–8 were inspected in OA-203/OA-225 and October 7–8 generated the downside hypothesis. They stay in the sample for G5 only as development days and are flagged. Every G5 statistic is also reported without Oct 5–8.
- **Untouched evaluation block (D3, and a G5 holdout view).** Sessions Nov 2 – Nov 16 (11 sessions). D-series models are fit only on sessions up to Oct 30.

## Inputs (all causal: received at or before the anchor time t)

- **Prints.** Option prints and their signing come from `market_data/tradier/recent.py`. This is the OA-180 combined rule, reconciled print for print in G1. A print enters at its collector receipt time.
- **Option snapshot inputs.** Per contract: the latest chain snapshot with filename time ≤ t and age ≤ 120 s supplies Gamma, Delta, settled OpenInterest and UnderlyingPrice. Otherwise the input is missing. Usability follows OA-171 (finite, non-negative).
- **Opening OI.** The settled OI in the latest snapshot at or before 09:30, applied once per exact contract (underlying, expiry, strike, type).
- **QQQ price S on a one-minute grid** 09:30 … 16:00:
  - the midpoint of the last QQQ quote from the archive (bid > 0, ask > bid) received at or before the minute;
  - missing if that quote is older than 60 s.
  - One-minute log returns r_m are computed only between two present minutes.
- **Previous regular close.** The UnderlyingPrice of the last chain snapshot at or before 16:00 on the previous weekday that has snapshots.
- **QQQ dollar volume.** Sum of price × size of accepted regular-session QQQ timesales received in the window.

## G4: exported features (per session, every minute 09:31 … 16:00)

**Per contract:**
- B, S, U: cumulative contracts through t;
- q_low = S−B−U, q_central = S−B, q_high = S−B+U (dealer contracts, passive-dealer assumption);
- OI, Gamma, Delta, spot, snapshot age.

**Aggregates over covered contracts** (w_c = 100 × Γ_c × S², dollars per unit return; the export also gives × 0.01 for per 1%):
- **Six flow cases.** G_within[p, k] = Σ p × q_k,c × w_c, for p ∈ {1, 0.5} and k ∈ {low, central, high}.
- **Unsigned OI concentration.** G_oi_unsigned = Σ OI_c × w_c.
- **Conditional total endpoints at p.** total_low = Σ (p × q_low,c − OI_c) × w_c and total_high = Σ (p × q_high,c + OI_c) × w_c. They carry the label "conditional on zero settlement-to-open change".
- **Zero-crossing flags.** For each flow case range and total range: low < 0 < high.
- **Activity.** Unsigned contracts in (t−30 min, t] and unsigned dollar-delta volume V = Σ size × 100 × |Δ_c| × S, using the snapshot at t.
- **Signed component flow.** Signed put flow and signed call flow (B−S in contracts) over (t−30 min, t].

A contract with a missing input contributes nothing to that aggregate. The covered-contract count is exported, and an aggregate with zero covered contracts is missing.

**Sign/unit check** (`tests/order_flow_exposure.test.js`): B 10, S 4, U 8 → −6 contracts, endpoints −14/+2, total −34/+22 at 20 OI; one contract at multiplier 100, gamma 0.02, spot 100 = $200 per 1%.

**Primary signed feature:** G_within[1, central] in $bn per unit return (the paper's units). The 50% case is a fixed scaling. It changes coefficient size but not sign or significance, which is reported once.

## G5a: next-30-minute variance (OA-179 A; Dim–Eraker–Vilkov eq. 9 adapted)

- **Anchors:** t = 10:00, 10:30, …, 15:30 (12 per session).
- **Outcome:** y = log RV(t, t+30], where RV is the sum of r_m² over the 30 one-minute returns. The anchor is excluded if any return is missing. Zero RV is excluded and counted.
- **Controls X:**
  - lagged log RV(t−30, t];
  - lagged log return (t−30, t];
  - log ratio of QQQ dollar volume (t−30, t] to (t−60, t−30];
  - tod and tod², where tod = (minutes since 09:30)/390;
  - session intercepts.
- **Nested models on identical rows:**
  - M0 = X;
  - M1 = M0 + log(1 + unsigned contracts (t−30, t]);
  - M2 = M1 + G_oi_unsigned ($bn);
  - M3 = M2 + G_within[1, central] ($bn).
- **Primary result:** the M3 coefficient on G_within, with a session-cluster bootstrap (2,000 resamples of whole sessions; percentile 95% interval). Expected sign: negative.
- **Out-of-sample:** leave-one-session-out mean squared error for M3 vs M2, with the session intercept replaced by the pooled intercept for the held-out session. **Useful effect:** at least a 2% LOSO MSE reduction.
- **Also reported:**
  - per-session coefficient signs;
  - the same results without Oct 5–8;
  - Nov 2–16 evaluated with M2/M3 fit on sessions up to Oct 30;
  - sensitivity substituting G_within low/high and total_low/total_high.

## G5b: continuation / reversal (OA-179 B; paper §4.1.2)

- **Anchors:** 10:00 … 15:30 every 30 min.
- **Outcome:** M = sign(log S_t / previous close) × (S_{t+60}/S_t − 1), with t+60 capped at 16:00.
- **Model:** M = a + b × G_within[1, central] ($bn) + c × G_oi_unsigned ($bn) + tod terms + e. b uses the session-cluster bootstrap. Expected sign: b < 0.
- **Non-overlapping sensitivity:** hourly anchors 10:00 … 15:00 only.
- **Descriptive:** mean M by G_within quartile, with counts.
- **Useful effect:** |b| × (interquartile range of G_within) ≥ 0.02% of price, with the interval excluding zero.

## G5c: strike attraction / repulsion (OA-179 C)

- **Anchors:** every 5 min, 09:45 … 15:50.
- **Reference strike K\*:** among the 8 covered strikes with |S_t − K| ≤ 2.00, the strike with the largest |strike G_within[1, central]|. No eligible strike means no event.
- **Control strike:** the covered strike, other than K\*, whose |S_t − K| is closest to that of K\*, within 0.50. If none, the event is unusable and counted.
- **Outcome:** D_K = |S_{t+5} − K| − |S_t − K|. The event statistic is Δ = D_K\* − D_control (negative means relative approach).
- **Primary statistic:** mean Δ for events where K\*'s strike G_within > 0 (dealer long gamma; attraction expected, Δ < 0) and for G_within < 0 (repulsion expected, Δ > 0). The contrast (long − short) is expected to be negative. All of these are session-cluster bootstrapped, with sessions as units.
- **Unsigned baseline:** the same construction with K\* = the largest strike G_oi_unsigned within 2.00.
- **Useful effect:** a contrast of at least $0.02 in absolute value with the interval excluding zero.

## D1–D3: downside warning (OA-225)

**D1.** Describe October 7 and 8 minute by minute, as discovery case studies only:
- S, cumulative signed put and call flow, opening OI below spot, put flow at 745–750, and G_within;
- the largest 15-minute drop and its onset.

Mark whether flow changes preceded the onset by at least 5 minutes, accompanied it, or followed it. Also mark what the archive cannot resolve: universe limits, participant identity, and opening/closing status.

**D2 (frozen here):**
- **Anchors:** every 5 min, 10:00 … 15:40.
- **Label.** DOWN_t = 1 if log(S_{t+15}/S_t) ≤ −0.25% and log(S_{t+15}/S_t) < log(S_t/S_{t−15}). The second condition is acceleration, not an already-equal decline.
- **Baseline features:** returns over 5/15/30 min, log RV over 30 min, tod and tod², log(1 + unsigned contracts over 30 min).
- **Augmented model.** Baseline plus:
  - opening put-OI gamma share below spot (put G_oi at strikes < S_t ÷ total G_oi);
  - signed put flow and signed call flow over 30 min (contracts);
  - G_within[1, central] ($bn).
- **Fitting.** Standardized logistic regression with L2 penalty, C = 1.0, fit on sessions ≤ Oct 30.
- **Primary loss:** Brier score. Daily paired difference = augmented − baseline, averaged over evaluation sessions with equal session weights.
- **Useful effect:** Brier reduction of at least 5% of the baseline Brier.
- **Holdout and stopping rule:** Nov 2–16, evaluated once. If the holdout has fewer than 20 positive anchors, or the paired-difference interval spans both "useful benefit" and "no benefit", the verdict is unresolved. In that case the shortfall is reported against the OA-225 sample-size table, using the development event rate and the daily paired-score SD.

**D3.** Run once and report:
- Brier for both models, the paired difference with a session bootstrap interval, and the positive count;
- precision and recall at the threshold maximizing development F1;
- false alerts per session;
- required versus available sessions.

## Reporting

Each of volatility (G5a), continuation (G5b), strike attraction (G5c) and downside warning (D3) gets a separate verdict:
- **supported**: the predicted sign, the interval excludes zero, and the useful effect is met;
- **opposite sign**;
- **null within precision**: the interval excludes the useful effect;
- **unresolved**: the interval is too wide, or there are too few events.

Each verdict carries its baseline, effect size, uncertainty, sample and reproduction command. Scenario bounds are not confidence intervals. Regressions are associations, not proof of dealer hedging.
