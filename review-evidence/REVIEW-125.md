# Issue 125 acceptance review

Reviewed November 9, 2026 UTC by Chej. Implementation PR #126 is merged. Production-code baseline: 762efafe88425c73dcecb9aefa9963ea80c29781; replay revision 65830835b0c45dd56601fe93e4d885a258eafcc2 adds only review evidence.

## Verification

94 scanner tests and 63 consumer tests pass. See scanner-tests.txt and mcp-consumer-tests.txt. Current connector also returned the real finalized November 6 qualified view (57,908 contracts; 15,508 eligible and 42,400 excluded). This historical replay is separate from live cadence acceptance under #131.

The current production build_contract_rows function replayed all archived sweeps for each of the eight originally reviewed sessions. No production collection or archive was changed. Per-session JSON includes source URLs via the base in replay125.py, SHA-256 digests, counts, rank changes, selected controls and both endpoint evidence. The existing doc describes the historical lack of access at implementation time; permitted public raw archives were accessible for this later review.

| Session | Sweeps | Contracts | Eligible | Excluded | Unknown | Eligible <=0 | New top 200 outside old shortlist | Legacy rows compared |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-09-28 | 76 | 53646 | 18570 | 35076 | 0 | 11768 | 85 | 370 |
| 2026-09-29 | 77 | 53536 | 17200 | 36336 | 0 | 11407 | 111 | 282 |
| 2026-09-30 | 77 | 55822 | 15876 | 39946 | 0 | 10864 | 127 | 292 |
| 2026-10-01 | 77 | 54208 | 14875 | 39333 | 0 | 9084 | 98 | 262 |
| 2026-10-02 | 77 | 55430 | 14410 | 41020 | 0 | 9789 | 129 | 277 |
| 2026-10-05 | 77 | 51070 | 17944 | 33126 | 0 | 12297 | 96 | 300 |
| 2026-10-06 | 77 | 51140 | 17161 | 33979 | 0 | 11554 | 116 | 267 |
| 2026-10-07 | 77 | 50700 | 15702 | 34998 | 0 | 10730 | 90 | 251 |

All replay comparisons match the archived midpoint first-to-max, ask-to-later-bid, all rank, clean rank and clean classification (numeric tolerance 1e-6). No damaged sweeps reported. This comparison concerns quote-derived fields; trade-bar backfill was not downloaded or claimed revalidated.

## Named controls

Return values below are ratios (multiply by 100 for percent). Clean is the legacy artifact predicate, not liquidity. Full endpoint evidence and reasons appear in each session JSON.

| Session | Contract | Legacy clean/rank | Qualified status/rank | Qualified return |
|---|---|---|---|---:|
| 2026-09-28 | MU260928P01045000 | True/8 | eligible/13 | 5.861702 |
| 2026-09-28 | MU260928P01040000 | True/12 | eligible/7 | 6.401575 |
| 2026-09-28 | MU260928P01050000 | True/13 | eligible/21 | 5.083032 |
| 2026-09-28 | MU260928P01055000 | True/15 | eligible/31 | 4.2875 |
| 2026-09-28 | GS261002P00600000 | False/None | excluded/None | None |
| 2026-09-29 | SOXL260930P00111000 | True/1 | excluded/None | None |
| 2026-09-29 | GS261002P00600000 | False/None | excluded/None | None |
| 2026-09-30 | GS261002P00600000 | False/None | excluded/None | None |
| 2026-10-01 | GS261002P00600000 | False/None | excluded/None | None |
| 2026-10-01 | SOXL261002P00106000 | True/17 | excluded/None | None |
| 2026-10-02 | SPCX261002C00157500 | True/1 | eligible/15 | 10.05 |
| 2026-10-02 | SPCX261002C00155000 | True/2 | eligible/8 | 12.055556 |
| 2026-10-02 | SPCX261002C00152500 | True/3 | eligible/39 | 5.315789 |
| 2026-10-02 | GS261002P00600000 | False/None | excluded/None | None |
| 2026-10-05 | GS261009C01160000 | False/None | excluded/None | None |
| 2026-10-05 | SPCX261009C00175000 | True/12 | eligible/46 | 3.538462 |
| 2026-10-05 | SPCX261009C00180000 | True/13 | eligible/42 | 3.958333 |
| 2026-10-05 | SPCX261009C00177500 | True/14 | eligible/43 | 3.828571 |
| 2026-10-05 | GS261009C00940000 | True/15218 | eligible/9102 | -0.044444 |
| 2026-10-06 | GS261009C00940000 | True/1333 | eligible/15520 | -0.201613 |
| 2026-10-06 | GS261009C01160000 | False/None | excluded/None | None |
| 2026-10-07 | MU261007C01055000 | True/1 | eligible/5 | 61.169811 |
| 2026-10-07 | MU261007C01060000 | True/2 | eligible/4 | 70.923077 |
| 2026-10-07 | MU261007C01070000 | True/3 | eligible/1 | 83.545455 |
| 2026-10-07 | MU261007C01065000 | True/4 | eligible/2 | 82.214286 |
| 2026-10-07 | MU261007C01075000 | True/5 | eligible/3 | 77.888889 |
| 2026-10-07 | MU261007C01050000 | True/6 | eligible/6 | 50.824324 |
| 2026-10-07 | MU261007C01080000 | True/7 | eligible/7 | 44.909091 |
| 2026-10-07 | MU261007C01085000 | True/8 | eligible/9 | 38.411765 |
| 2026-10-07 | MU261007C01045000 | True/9 | eligible/8 | 40.298077 |
| 2026-10-07 | MU261007C01040000 | True/10 | eligible/11 | 30.904762 |
| 2026-10-07 | MU261007C01090000 | True/11 | eligible/10 | 33.166667 |
| 2026-10-07 | MU261007C01095000 | True/12 | eligible/13 | 19.333333 |
| 2026-10-07 | MU261007C01035000 | True/13 | eligible/12 | 23.12037 |
| 2026-10-07 | GS261009C00940000 | False/None | excluded/None | None |
| 2026-10-07 | MU261007C01030000 | True/15 | eligible/14 | 17.672131 |
| 2026-10-07 | MU261007C01025000 | True/17 | eligible/16 | 13.576471 |
| 2026-10-07 | MU261007C01100000 | True/18 | eligible/17 | 12.9 |
| 2026-10-07 | GS261009C01160000 | False/None | excluded/None | None |

## Interpretation and boundaries

The separately versioned first-qualifying-ask metric uses entry-time evidence, strictly later qualified bid exits and full-universe ranks. It preserves eligible losses and does not redefine legacy exec fields. Unknown-input behavior is covered deterministically even where a historical session has zero unknown statuses.

The replay supports implementation acceptance, not threshold calibration or trading profitability. Defaults remain provisional. Maximum later bids are hindsight observations; samples and displayed sizes do not establish fills, slippage, queue position or capacity. September 24-25 are outside this eight-session coverage. No deployment, producer scan, polling change or historical overwrite occurred.
