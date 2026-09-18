# MOO-171 — Unsigned gamma concentration/activity vs. subsequent strike attraction

## Question

At comparable initial distances from QQQ spot, do strikes with higher unsigned
gamma-weighted open-interest concentration (C) or gamma-weighted interval
activity (A) show different subsequent movement toward/away from the strike,
beyond ordinary proximity and unweighted activity?

## Data

Five sessions (2026-09-10, 11, 14, 15, 16), tastytrade/DXLink intraday QQQ
0DTE snapshots archived at ~60s cadence, `intraday/{date}/snapshot_*.csv`.
Coverage audit: all 5 sessions reconcile exactly against collector logs
(595/598/598/598/597 snapshots); 400,124 option rows; zero nonfinite Greeks,
negative volumes, or negative OI; 99.65% of interval-volume observations
usable (`ok`) after excluding first-observations, re-entries, resets, and
gaps >90s per the issue's rules. Full detail in `out/audit_report.json`.

Outcome dataset: 2304 (anchor, strike) rows from 384 non-overlapping
5-minute anchors x up to 6 selected strikes (3 nearest strictly above spot,
3 strictly below), each requiring an anchor snapshot <=90s old and an outcome
snapshot 5 minutes later, <=90s late. 30 rows dropped for missing
baseline features (mostly the first ~30 minutes of each session, before a
full preceding-volatility window exists) -> 2274 modeled rows.

## Method

`toward[k,t] = (|S[t]-k| - |S[t+5m]-k|) / S[t]`, positive = closer to k at
the outcome snapshot. Regressed on log1p(C) / log1p(A) / log1p(gamma alone)
each in turn, plus a fixed baseline (|distance|, |distance|^2, side of spot,
prior 5-min return, preceding 30-min realized vol, minutes since open,
log unweighted OI, log unweighted activity). Standard errors are clustered
by (date, anchor) throughout -- the 6 strikes at one anchor share one future
price path and are never treated as independent observations.

## Findings — read the day-by-day results before the pooled ones

**The pooled coefficients are small and not stable across days.** Pooling
all 5 sessions, log(C), log(A), and log(gamma alone) each show a positive,
nominally significant association with `toward` (predictor-comparison table
below) -- but the R^2 gain over the baseline-only model (0.0423) is on
the order of 0.002-0.003 in every case: detectable, not large.

Critically, **with only 5 day-clusters this pooled significance is not
trustworthy on its own** (per the issue's explicit caution), and the
day-by-day breakdown confirms why: for log(C), 3 of 5 sessions (9/10, 9/11,
9/14) show a positive, individually significant coefficient, while the other
2 (9/15, 9/16) show no relationship or a negative point estimate with a wide
CI spanning zero. Leave-one-day-out refits move the pooled coefficient by
roughly 3x depending on which day is excluded (lowest when 9/14 -- one of
the strong-positive days -- is held out; highest when 9/16 -- the
negative-point-estimate day -- is held out). The same pattern holds for
log(A). **This is not a stable, session-independent relationship on this
evidence; it is at most a candidate worth watching across more sessions.**

**Gamma alone (unweighted by OI or activity) shows an equal or larger
coefficient than either C or A**, and the same day-by-day instability.
Since the baseline already controls for |distance| and |distance|^2, this
raises a real possibility that the OI/activity weighting in C and A is not
adding information beyond what gamma's own mechanical dependence on
moneyness and time-to-expiry already contributes -- exactly the concern the
issue's spec named in advance. This analysis does not resolve that question;
it flags it as the main reason not to read C or A as validated signals from
this pass alone.

## Explicit limitations (do not read past these)

- Unsigned measures only. C and A are gamma-weighted open-interest and
  activity concentration -- not signed dealer inventory, not a hedging-flow
  estimate, and not validated against any independent position/trade data.
- 5 trading days. Every inference above is over 5 day-clusters; nothing here
  supports a trading gate, a causal dealer-hedging claim, or a
  predictive-performance claim.
- No threshold search was performed; the 5-minute horizon, 90-second
  staleness cap, and 3-strikes-per-side selection are exactly the issue's
  specified values, not tuned choices.
- `toward` observes one endpoint-to-endpoint distance comparison per anchor,
  not intraminute touches/crossings; it does not establish sustained
  pinning.
- Reported OI can legitimately read 0 well into a session for a given 0DTE
  contract (no settled prior-day OI yet) -- this is a real data-quality
  constraint on C specifically, not an artifact of this analysis.


## Predictor comparison (pooled, clustered by anchor)

| predictor       |          coef |            se |             p |   r_squared |    n |
|:----------------|--------------:|--------------:|--------------:|------------:|-----:|
| (baseline only) | nan           | nan           | nan           |   0.0423034 | 2274 |
| log_C           |   0.00010334  |   3.5588e-05  |   0.00368659  |   0.044397  | 2274 |
| log_A           |   0.000102396 |   3.8636e-05  |   0.00804265  |   0.0441807 | 2274 |
| log_gamma_alone |   0.000659548 |   0.000179265 |   0.000233978 |   0.0455059 | 2274 |



## log(C)

### Day-by-day
|     date |   n |         coef |          se |           p |
|---------:|----:|-------------:|------------:|------------:|
| 20260910 | 456 |  0.00042811  | 0.000187946 | 0.0227368   |
| 20260911 | 456 |  0.000261662 | 0.000100222 | 0.00903231  |
| 20260914 | 450 |  0.00035296  | 9.79744e-05 | 0.000315077 |
| 20260915 | 456 | -4.78678e-06 | 7.32738e-05 | 0.947913    |
| 20260916 | 456 | -0.000139302 | 0.000351939 | 0.692244    |


### Leave-one-day-out
|   held_out |    n |        coef |          se |           p |
|-----------:|-----:|------------:|------------:|------------:|
|   20260910 | 1818 | 0.000102346 | 3.52916e-05 | 0.00373155  |
|   20260911 | 1818 | 0.000101109 | 4.12613e-05 | 0.0142672   |
|   20260914 | 1824 | 7.13646e-05 | 4.25567e-05 | 0.0935558   |
|   20260915 | 1818 | 0.000115041 | 4.04655e-05 | 0.00447001  |
|   20260916 | 1818 | 0.000215813 | 5.43796e-05 | 7.22831e-05 |


## log(A)

### Day-by-day
|     date |   n |         coef |          se |          p |
|---------:|----:|-------------:|------------:|-----------:|
| 20260910 | 456 |  0.00045417  | 0.000200377 | 0.0234156  |
| 20260911 | 456 |  0.000266773 | 9.75225e-05 | 0.00622848 |
| 20260914 | 450 |  0.000440202 | 0.000138592 | 0.00149194 |
| 20260915 | 456 | -8.46017e-06 | 7.89718e-05 | 0.914687   |
| 20260916 | 456 | -0.000157725 | 0.000354955 | 0.656788   |


### Leave-one-day-out
|   held_out |    n |        coef |          se |           p |
|-----------:|-----:|------------:|------------:|------------:|
|   20260910 | 1818 | 0.000101968 | 3.8233e-05  | 0.00765287  |
|   20260911 | 1818 | 9.63225e-05 | 4.54924e-05 | 0.0342318   |
|   20260914 | 1824 | 7.17037e-05 | 4.34944e-05 | 0.0992353   |
|   20260915 | 1818 | 0.000113398 | 4.43596e-05 | 0.0105781   |
|   20260916 | 1818 | 0.000233993 | 6.22134e-05 | 0.000169146 |


## log(gamma alone)

### Day-by-day
|     date |   n |         coef |          se |          p |
|---------:|----:|-------------:|------------:|-----------:|
| 20260910 | 456 |  0.00176426  | 0.000889053 | 0.0472087  |
| 20260911 | 456 |  0.000962678 | 0.000397947 | 0.0155584  |
| 20260914 | 450 |  0.00183789  | 0.000592811 | 0.00193325 |
| 20260915 | 456 |  0.000110674 | 0.000323853 | 0.732545   |
| 20260916 | 456 | -0.00015473  | 0.00175379  | 0.929697   |


### Leave-one-day-out
|   held_out |    n |        coef |          se |           p |
|-----------:|-----:|------------:|------------:|------------:|
|   20260910 | 1818 | 0.000708597 | 0.000179041 | 7.5661e-05  |
|   20260911 | 1818 | 0.000649826 | 0.000220785 | 0.00324783  |
|   20260914 | 1824 | 0.000519429 | 0.000207568 | 0.0123335   |
|   20260915 | 1818 | 0.00073458  | 0.000206464 | 0.000373829 |
|   20260916 | 1818 | 0.000907864 | 0.000211547 | 1.77425e-05 |
