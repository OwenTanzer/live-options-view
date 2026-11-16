# S3c results: combined pricing model (sim-v2)

Protocol: `S3C_PROTOCOL.md` / `s3c_protocol.json`, committed 714cade2a61d6795309cd85cae51c57c966f7121 2026-11-15T20:39:57-08:00, before fitting.
Frozen model: `pricing.py` + `s3c_params.json`, f99b36128c4dccd2f9dcba98ac84033fc5db8a35 2026-11-15T20:47:24-08:00 (params sha256 2f680858a9d30fe0…).

## Status

**There is no held-out result yet.** Every existing intraday day informed S3b or S3c choices, so held-out is forward collector sessions from 2026-11-16 on. The first 10 gated sessions per symbol are needed (about 2026-11-30). Every number below is **development (in-sample)** and is not validation. Next-session expiry is unsupported (no intraday archive).

## Development comparison (identical checks, same days)

| Model | Symbol | cells pass / 16 | S2 quote validity | C1 convexity violations | P1 max parity $/share | E1 15:55 / 16:05 / 16:10 ($) |
|---|---|---|---|---|---|---|
| combined (sim-v2) | QQQ | 3 | pass (0 crossed/locked, 0 neg, 0 off-grid of 98700) | 0 | 2e-13 | 7.58 / 6.85 / 7.41 |
| combined (sim-v2) | SPY | 4 | pass (0 crossed/locked, 0 neg, 0 off-grid of 29400) | 0 | 2e-13 | 7.31 / 5.74 ✓ / 6.36 |
| elementary baseline | QQQ | 2 | pass (0 crossed/locked, 0 neg, 0 off-grid of 98700) | 0 | 1.9e-13 | 21.60 / 24.00 / 20.50 |
| elementary baseline | SPY | 2 | pass (0 crossed/locked, 0 neg, 0 off-grid of 29400) | 0 | 2e-13 | 22.40 / 24.50 / 22.75 |
| sim-v1 | QQQ | 0 | FAIL (20094 crossed/locked, 0 neg, 0 off-grid of 98700) | 21552 | 49 | 27.50 / 22.25 / 21.00 |
| sim-v1 | SPY | 0 | FAIL (5264 crossed/locked, 0 neg, 0 off-grid of 29400) | 7179 | 19 | 26.75 / 22.50 / 18.75 |

Medians across the open/midday/close cells (USD per contract; B1 and U1 are ratios; S1 is per share):

| Model | Symbol | A1 | A2 | A3 | A4 | Q1 | B1 | B2 | S1 | U1 | N1 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| combined (sim-v2) | QQQ | 10.06 | -0.19 | 5.16 | 2.90 | 1.53 | 0.994 | 5.05 | 0.005 | 1.018 | 5.50 |
| combined (sim-v2) | SPY | 8.67 | 0.41 | 4.67 | 2.62 | 1.03 | 1.067 | 4.97 | 0.005 | 0.986 | 5.00 |
| elementary baseline | QQQ | 17.04 | 11.61 | 11.17 | 5.72 | 1.91 | 1.102 | 11.52 | 0.005 | 1.025 | 11.00 |
| elementary baseline | SPY | 12.49 | 4.65 | 7.43 | 3.40 | 1.33 | 1.078 | 8.05 | 0.005 | 1.000 | 7.00 |
| sim-v1 | QQQ | 78.75 | -46.75 | 34.75 | 16.75 | - | 0.571 | 34.75 | 0.022 | 0.926 | 33.50 |
| sim-v1 | SPY | 71.88 | -25.12 | 32.50 | 14.50 | - | 0.660 | 32.50 | 0.029 | 0.902 | 35.00 |

## Combined model per cell (development)

| Symbol | Cell | n | A1 | A2 | A3 | A4 | Q1 | B1 | B2 | S1 | U1 | N1 | missed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| QQQ | 1-OTM|call|close | 1448 | 9.63 | -1.69 | 4.74 | 2.05 | 1.50 | 0.967 | 4.68 | 0.005 | 1.038 | 5.00 | A1, A3 |
| QQQ | 1-OTM|call|endpoint | 448 | 4.39 | -2.57 | - | 3.05 | 2.96 | 0.711 | - | 0.005 | 0.997 | - | none |
| QQQ | 1-OTM|call|midday | 2265 | 10.92 | -0.37 | 4.33 | 2.74 | 1.10 | 0.993 | 4.37 | 0.005 | 1.036 | 4.00 | A1, A3 |
| QQQ | 1-OTM|call|open | 899 | 6.20 | 0.74 | 6.04 | 3.34 | 1.56 | 1.001 | 6.83 | 0.005 | 0.999 | 7.00 | N1, A3, A4, B2 |
| QQQ | 1-OTM|put|close | 1429 | 9.83 | -1.74 | 4.86 | 2.18 | 1.56 | 0.980 | 4.72 | 0.005 | 0.973 | 5.00 | A1, A3 |
| QQQ | 1-OTM|put|endpoint | 438 | 4.42 | -2.52 | - | 2.70 | 3.53 | 0.738 | - | 0.005 | 0.972 | - | none |
| QQQ | 1-OTM|put|midday | 2253 | 10.30 | -2.40 | 4.53 | 2.66 | 1.23 | 0.971 | 4.72 | 0.005 | 1.024 | 4.00 | A1, A3 |
| QQQ | 1-OTM|put|open | 894 | 6.10 | -1.79 | 5.84 | 4.16 | 1.62 | 0.981 | 6.88 | 0.005 | 1.051 | 7.00 | N1, A3, A4, B2 |
| QQQ | ATM|call|close | 1439 | 12.44 | 0.38 | 5.55 | 2.91 | 1.32 | 1.029 | 5.46 | 0.010 | 1.023 | 6.00 | N1, A1, A3 |
| QQQ | ATM|call|endpoint | 448 | 7.55 | -1.57 | - | 4.79 | 2.60 | 1.007 | - | 0.015 | 1.055 | - | S1 |
| QQQ | ATM|call|midday | 2260 | 11.77 | -0.02 | 4.65 | 3.40 | 1.13 | 0.999 | 4.82 | 0.005 | 1.013 | 5.00 | A1, A3, A4 |
| QQQ | ATM|call|open | 899 | 5.94 | 0.20 | 5.85 | 3.85 | 1.68 | 0.996 | 6.90 | 0.005 | 0.979 | 7.00 | N1, A3, A4, B2 |
| QQQ | ATM|put|close | 1439 | 12.34 | 0.32 | 5.47 | 2.49 | 1.63 | 1.019 | 5.27 | 0.010 | 0.986 | 6.00 | A1, A3 |
| QQQ | ATM|put|endpoint | 448 | 7.25 | -1.11 | - | 3.02 | 3.90 | 0.988 | - | 0.010 | 1.006 | - | none |
| QQQ | ATM|put|midday | 2260 | 11.38 | -0.66 | 4.61 | 2.88 | 1.12 | 0.994 | 4.83 | 0.005 | 1.010 | 5.00 | A1, A3 |
| QQQ | ATM|put|open | 899 | 6.03 | -0.00 | 5.85 | 4.59 | 1.76 | 0.992 | 7.15 | 0.005 | 1.042 | 7.00 | N1, A3, A4, B2 |
| SPY | 1-OTM|call|close | 432 | 10.41 | -0.52 | 4.12 | 1.63 | 1.03 | 1.155 | 4.39 | 0.005 | 0.871 | 4.00 | A1, A3, B1 |
| SPY | 1-OTM|call|endpoint | 135 | 4.75 | -1.93 | - | 2.92 | 2.14 | 0.724 | - | 0.005 | 0.900 | - | none |
| SPY | 1-OTM|call|midday | 674 | 8.07 | 1.30 | 3.34 | 2.08 | 0.99 | 1.118 | 3.68 | 0.005 | 0.957 | 4.00 | A1 |
| SPY | 1-OTM|call|open | 269 | 6.02 | 2.09 | 4.80 | 2.89 | 1.00 | 1.040 | 5.00 | 0.005 | 0.934 | 5.00 | A3 |
| SPY | 1-OTM|put|close | 432 | 9.22 | -0.19 | 4.55 | 2.05 | 1.03 | 1.178 | 4.94 | 0.005 | 0.950 | 5.00 | A1, A3, B1 |
| SPY | 1-OTM|put|endpoint | 132 | 4.15 | -1.44 | - | 3.76 | 3.03 | 0.917 | - | 0.005 | 1.045 | - | none |
| SPY | 1-OTM|put|midday | 673 | 8.49 | -0.92 | 3.89 | 2.30 | 0.93 | 1.069 | 4.14 | 0.005 | 1.018 | 4.00 | A1 |
| SPY | 1-OTM|put|open | 269 | 4.11 | -1.01 | 5.38 | 3.09 | 1.67 | 1.009 | 6.70 | 0.005 | 1.059 | 7.00 | N1, A3, A4, B2 |
| SPY | ATM|call|close | 432 | 12.45 | 0.63 | 5.25 | 2.26 | 1.10 | 1.114 | 5.49 | 0.005 | 0.979 | 6.00 | A1, A3 |
| SPY | ATM|call|endpoint | 135 | 6.85 | -1.93 | - | 3.98 | 2.54 | 0.990 | - | 0.005 | 1.078 | - | none |
| SPY | ATM|call|midday | 674 | 9.58 | 0.85 | 3.74 | 2.64 | 0.96 | 1.065 | 4.11 | 0.005 | 0.993 | 4.00 | A1 |
| SPY | ATM|call|open | 268 | 5.14 | 0.47 | 5.39 | 3.46 | 1.48 | 1.023 | 7.09 | 0.005 | 0.964 | 6.00 | N1, A3, A4, B2 |
| SPY | ATM|put|close | 432 | 12.04 | 0.34 | 5.39 | 2.59 | 1.13 | 1.097 | 5.72 | 0.005 | 1.043 | 6.00 | A1, A3 |
| SPY | ATM|put|endpoint | 135 | 6.59 | -0.49 | - | 3.98 | 2.96 | 0.998 | - | 0.005 | 1.005 | - | none |
| SPY | ATM|put|midday | 674 | 8.86 | -0.17 | 4.14 | 2.69 | 0.92 | 1.057 | 4.33 | 0.005 | 1.036 | 4.00 | A1, A3 |
| SPY | ATM|put|open | 269 | 4.59 | 0.50 | 5.25 | 3.20 | 1.53 | 1.016 | 6.89 | 0.005 | 1.099 | 6.00 | N1, A3, A4, B2 |

## Component status (development only; 'acceptable' requires a held-out pass)

| Component | Development evidence | Status |
|---|---|---|
| call/put consistency (P1) | exact parity by construction, max residual ~2e-13 | development pass, held-out pending |
| quote validity (S2) / convexity (C1) | 0 invalid quotes, 0 violations | development pass, held-out pending |
| spreads (S1) | within a tick except QQQ ATM endpoint (0.015) | development miss at QQQ ATM endpoint |
| underlying response (U1) | delta ratio 0.87-1.10 in every cell | development pass, held-out pending |
| theta net of IV movement (Q1) | $0.9-1.8 outside the endpoint window | development pass, held-out pending |
| volatility level (A1/A2/B1) | bias ~$0 and ratio in band, absolute error $9-12 midday/close | carried miss (A1) |
| hold profit (A3/B2/N1) | $4-7 per contract, mostly over $4 | carried miss |
| opening decay (A4 open) | $2.9-4.6 against $3 | carried miss |
| expiration path (E1) | QQQ $6.9-7.6, SPY $5.7-7.3 against $6 | development miss (SPY 16:05 passes) |

## Development revisions before the freeze (logged in `s3c_attempts.jsonl`)

- **D1 skew/convexity:** the unbounded quadratic smile in z made far out-of-the-money prices rise with distance late in the day (6.7k QQQ violations). Fix: smooth saturation g = 3·tanh(z/3) at the fitted range. After it there are 0 violations.
- **D2 volatility response:** time-of-day drift γ per 30-minute bucket, on top of the spot response. The midday/close level bias went from +$6-9 to about $0.
- **D3 evaluation:** N1 is not applicable in the endpoint window. A 30-minute hold opened after 15:50 cannot finish before the 16:15 trading close.

## Settlement convention used

These conventions are verified and cited in S3C_PROTOCOL.md Amendment 1, and kept distinct:
- **Last trading time:** 16:15 ET for expiring SPY/QQQ options (SEC Release No. 34-105153, SR-CBOE-2025-079, read).
- **Exercise-by-exception reference:** OCC Rule 805 at $0.01 in the money against the regular-hours last sale (the 16:00 close), physical delivery. The threshold comes from the 2008 filing; the reference price is secondary (OCC pages return 403 here).
- **Contrary-instruction deadline:** 17:30 ET exercise-decision cut-off (FINRA Rule 2360(b)(23)(A), read). Firms may set earlier cut-offs.
- **Modelling assumption, not a settlement rule:** the model's remaining variance after 16:00 is a post-close term decaying to 17:30. No rule establishes that decay. It was fitted on 16:05 development quotes: tail = 0.0265 (QQQ) and 0.0312 (SPY) of a day's variance.
- Recorded quotes keep updating until about 16:14. The generated sim-v2 session still ends at 16:00 (stated limitation).

## Carried forward (not resolved)

S3b misses A1 level, A3/B2 hold profit and A4 opening decay remain open. The development improvements above do not validate them. The held-out run on forward sessions decides.

## Development candidate s3c-dev2 (Amendment 1): before/after and attribution

This is development (in-sample) only, with one declared correction per component. `pricing.py`, `s3c_params.json` and generator sim-v2 are unchanged. dev2 lives in `pricing_dev2.py` + `s3c_params_dev2.json`. Medians over the cells of each window, USD per contract; E1 is ATM.

| Symbol | Variant | open A1 / A3 / A4 / N1 | midday A1 / A3 / A4 / N1 | close A1 / A3 / A4 / N1 | E1 15:55 / 16:05 / 16:10 |
|---|---|---|---|---|---|
| QQQ | frozen | 6.06 / 5.85 / 4.01 / 7.00 | 11.15 / 4.57 / 2.81 / 4.50 | 11.08 / 5.16 / 2.33 / 5.50 | 7.58 / 6.85 / 7.41 |
| QQQ | clock only (implied clock, refit sequence) | 6.12 / 6.20 / 4.38 / 7.00 | 11.68 / 4.51 / 2.76 / 4.50 | 11.21 / 4.66 / 2.36 / 4.50 | 7.17 / 10.10 / 10.40 |
| QQQ | vol feedback only (eta on frozen) | 10.03 / 7.87 / 4.71 / 9.00 | 10.15 / 5.03 / 3.09 / 5.00 | 8.43 / 4.84 / 2.49 / 5.50 | 6.90 / 6.71 / 7.09 |
| QQQ | expiration only (fitted horizon + tail) | 6.10 / 5.83 / 3.98 / 7.00 | 11.12 / 4.55 / 2.81 / 4.50 | 10.91 / 5.18 / 2.26 / 5.50 | 7.90 / 5.86 / 6.43 |
| QQQ | spreads only (ATM/non-ATM) | 6.06 / 5.85 / 4.01 / 7.00 | 11.15 / 4.57 / 2.81 / 4.50 | 11.08 / 5.16 / 2.33 / 5.50 | 7.58 / 6.85 / 7.41 |
| QQQ | all four (s3c-dev2) | 10.09 / 8.43 / 4.87 / 9.00 | 10.37 / 4.94 / 3.07 / 5.00 | 8.71 / 4.50 / 2.61 / 4.50 | 8.07 / 9.15 / 7.76 |
| SPY | frozen | 4.87 / 5.32 / 3.14 / 6.00 | 8.67 / 3.81 / 2.47 / 4.00 | 11.23 / 4.90 / 2.16 / 5.50 | 7.31 / 5.74 / 6.36 |
| SPY | clock only (implied clock, refit sequence) | 5.61 / 5.51 / 3.14 / 7.00 | 8.77 / 3.94 / 2.27 / 4.00 | 10.54 / 4.63 / 2.64 / 5.00 | 8.48 / 11.48 / 12.15 |
| SPY | vol feedback only (eta on frozen) | 6.56 / 5.68 / 3.37 / 7.00 | 8.48 / 3.75 / 2.55 / 4.00 | 9.22 / 4.61 / 2.00 / 4.75 | 6.82 / 5.34 / 6.26 |
| SPY | expiration only (fitted horizon + tail) | 4.87 / 5.27 / 3.17 / 6.50 | 8.82 / 3.77 / 2.42 / 4.00 | 11.42 / 4.98 / 2.15 / 5.50 | 8.12 / 7.95 / 5.92 |
| SPY | spreads only (ATM/non-ATM) | 4.87 / 5.32 / 3.14 / 6.00 | 8.67 / 3.81 / 2.47 / 4.00 | 11.23 / 4.90 / 2.16 / 5.50 | 7.31 / 5.74 / 6.36 |
| SPY | all four (s3c-dev2) | 7.27 / 6.27 / 4.20 / 7.50 | 8.68 / 4.02 / 2.57 / 4.00 | 9.73 / 4.46 / 2.25 / 5.50 | 7.38 / 5.36 / 6.60 |

Attribution (development):
- **Volatility feedback (eta)** lowers close-window level error (QQQ A1 11.1 to 8.4, SPY 11.2 to 9.2). It **worsens the open** (QQQ A1 6.1 to 10.0, A3 5.9 to 7.9, N1 7 to 9), because the trailing 60-minute realized vol is undefined or noisy in the first hour.
- **Implied clock** barely changes the open and **worsens the endpoint** (QQQ E1 16:05 6.9 to 10.1; SPY 5.7 to 11.5).
- **Fitted post-close horizon** (a modelling assumption) improves the QQQ endpoint (E1 16:05 6.85 to 5.86, 16:10 7.41 to 6.43) and is mixed for SPY. The SPY horizon fit sits at the 18:30 edge of the search grid.
- **ATM/non-ATM spreads** fix the QQQ ATM endpoint spread (S1 0.015 to 0.010) and leave mids unchanged.
- **Combined dev2** is mixed: better at the close, worse at the open and at the QQQ endpoint. The correction budget (one per component) is now spent. The frozen candidate stays the declared held-out comparison, and dev2 is not promoted.

Remaining misses (development): A1 level ($8-11), A3/B2 hold profit (open $6-8.5, elsewhere $4-5), A4 opening decay ($3-5), E1 endpoint (QQQ $6-9), and with dev2 the SPY 1-OTM call underlying response at the close (U1 0.755).
