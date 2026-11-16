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

- Expiring SPY/QQQ options trade until 16:15 ET. Automatic exercise is decided from the underlying's 16:00 close (OCC exercise-by-exception, $0.01 in the money, physical delivery). Contrary instructions are possible after the close (exchange cutoff 17:30 ET per an OCC education page; brokers earlier). The Cboe rulebook text was not retrieved.
- The model's remaining variance after 16:00 is a post-close term decaying to 17:30. It was fitted on 16:05 development quotes: tail = 0.0265 (QQQ) and 0.0312 (SPY) of a day's variance.
- Recorded quotes keep updating until about 16:14. The generated sim-v2 session still ends at 16:00 (stated limitation).

## Carried forward (not resolved)

S3b misses A1 level, A3/B2 hold profit and A4 opening decay remain open. The development improvements above do not validate them. The held-out run on forward sessions decides.
