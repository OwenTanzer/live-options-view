# Four opening OI associator paper bots

The experiment tests whether the shape of the opening QQQ 0DTE open-interest ladder anticipates the underlying price path. Each Crassus strategy serves one model and runs in its own paper account. At 09:35 Eastern it forecasts QQQ returns in basis points to 10:30, 12:30 and 15:30. A final forecast above 2 bp admits one long QQQ share, subject to account and executable quote checks. Otherwise the account holds cash. An open position is due for sale at 15:30. Simultaneous active accounts and equal starting balances are important for a forward comparison.

All architectures receive the same 14 log-transformed call/put quantities at the seven dollar strikes centered on opening spot, plus its fractional distance from the center strike. Missing required OI excludes the input. The scaler and all model parameters are fitted on training days only.

| Architecture | Learned association |
| --- | --- |
| Ridge regression | A regularized linear map from the opening ladder to three forward returns. |
| Random forest | Shallow trees learn nonlinear interactions between strike/side quantities. Each leaf stores a three-point mean path. |
| OI prototypes | K-means learns four opening-chain shapes without path labels. A new opening receives the average training price path of its nearest group. |
| Partial least squares | Two supervised latent components maximize covariance between the OI features and the price-path targets, then reconstruct the three forecasts. |

## Historical paper scoreboard

Training used 66 sessions through July 31, 2026. The frozen models saw the same 28 later sessions from August 3 through September 18. Of 112 candidate dates, 17 had bars without timezone offsets and one lacked valid opening OI. The models trade long/cash with one share on a $10,000 starting balance. The fill model uses the 09:35 and 15:30 bar opens, moved one basis point against the order on each side. The drawdown is calculated from session-ending balances. Trade-level observations and excluded dates are in [results](evidence/results.json) and [source hashes](evidence/sources.json).

| Bot or comparison | Round trips | Net P&L | Max drawdown | 15:30 forecast MAE (bp) |
| --- | ---: | ---: | ---: | ---: |
| Ridge | 16 | $14.32 | $13.75 | 52.48 |
| Forest | 15 | $14.97 | $17.51 | 48.96 |
| OI prototypes | 21 | $7.64 | $20.08 | 49.52 |
| Partial least squares | 14 | $18.80 | $11.12 | 52.42 |
| Always long, same entry/exit | 28 | $15.60 | $15.50 | — |
| Historical mean path | 0 | $0.00 | $0.00 | 47.06 |
| Opening price only | 2 | $13.77 | $0.00 | 45.07 |
| Cash | 0 | $0.00 | $0.00 | — |

The price-only diagnostic fits ridge to opening price, fractional strike offset, and first five-minute range and return. It was added during review and gets the same training and test sessions. All four OI bots had greater final-horizon forecast error. PLS earned $3.19 more than always long; excluding September 16 reverses that difference to a $3.45 shortfall. This cohort provides a reason to collect forward evidence, with no reliable OI advantage demonstrated.

Daily files contain settled OI keyed to trade date and expiry. Their exact historical publication times are absent. Bar opens also lack executable spreads, order latency and fill confirmation. The historical ledger assumes availability at the entry time. Forward Crassus uses the recorded as-of opening packet, authenticated session/account state, current bid/ask, and the existing paper order client. The five-minute entry grace means a prospective fill can occur later than its historical counterpart. Track skipped entries and unfilled exits as well as cash balances.

## Reproduce

From the repository root, install the research dependencies into a Python environment, then run:

```sh
python -m pip install -r research/oi_associator/requirements.txt
python research/oi_associator/download.py /tmp/qqq-oi-cache
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python research/oi_associator/train.py /tmp/qqq-oi-cache /tmp/qqq-oi-run
python -m unittest research.oi_associator.test_train
```

Use an empty cache for a fresh source inventory and a new output directory. The downloader reads the public manifest and dated CSVs, and the trainer records SHA-256 for each accepted pair. Compare those hashes with the checked-in evidence before comparing reruns. The runner ships with a frozen JSON bundle and uses only standard-library model inference. Research training and historical simulation can be rerun independently of account provisioning. A future refit should receive a new version, training cutoff and untouched test period.
