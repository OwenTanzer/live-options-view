# OA-203 qualified ask-to-later-bid view (v1)

Issue [#125](https://github.com/OwenTanzer/live-options-view/issues/125) adds a separate research ranking. `oa203-returns-v1`, raw sweeps, `exec_first_to_max`, midpoint ranks, and legacy `clean` are unchanged. `clean` screens selected artifacts, not contract liquidity. The new field prefix is `qualified_first_ask_to_later_bid` and the policy version is `oa203-qualified-ask-bid-v1`.

For each archived contract, sort samples by acquisition time. Choose the first sample satisfying the entry criteria below, then choose the highest bid among qualified exit samples with **strictly greater acquisition time**. Record `entry_delay_s` from the first archived sample; this is deliberately a different metric from `exec_first_to_max`. A zero or negative return remains eligible. No profit filter is used. The highest later bid is known only in hindsight.

## Provisional policy

Defaults are configurable through `OA203_QUAL_*` variables and written into `summary.json` as `qualified_policy`. They are research starting points, not calibrated thresholds.

| Criterion | Default | Unit / meaning |
|---|---:|---|
| `MIN_PREMIUM` | 0.10 | USD per option share, ask at selected entry |
| `MAX_REL_SPREAD` | 0.30 | `(ask - bid) / midpoint`, separately at entry and exit |
| `MAX_AGE_S` | 1800 | seconds from **each side's** quote timestamp to its sample |
| `MIN_ASK_SIZE` | 1 | displayed ask contracts at entry |
| `MIN_BID_SIZE` | 1 | displayed bid contracts at exit |
| `MIN_ENTRY_VOLUME` | 1 | cumulative contracts observed **at entry** |
| `MIN_SAMPLES` | 3 | archived samples of this contract |
| `MIN_COVERAGE` | 0.50 | samples divided by successful chain sweeps |
| `MIN_OI` | disabled | optional first-sample, lagged open interest; missing is unknown when enabled |

Both endpoint quotes must have finite positive bid and ask, with ask **greater than** bid. Crossed and locked endpoints fail. Both side timestamps must be known, no later than the sample time, and within the configured age. Missing quote, timestamp, size, volume, or enabled OI evidence stays `unknown` and cannot qualify; known failures are `excluded`. `qualified_first_ask_to_later_bid_reasons` records machine-readable codes. `summary.json` counts statuses and reasons across the full contract archive. The selected row includes both endpoint prices, side timestamps, sizes, absolute and relative spreads, entry observed volume, entry/exit acquisition times, and return inputs.

The scanner's prior-session OCC selection is **chain selection**, not contract liquidity. `day_volume` is the last cumulative sample and `median_spread_pct` is day-wide; both remain retrospective diagnostics and are never entry gates. OI is lagged and optional. Contract sample coverage and observation-time volume are separate criteria.

## Publication and consumer

`contracts.csv.gz` retains every contract and all status/reason columns. Legacy `leaderboard.csv` still contains the top 200 midpoint-all or midpoint-clean rows. New `qualified_ask_bid_v1.csv` contains the top 200 **qualified** ranks computed over the full archived universe. It is not derived from `leaderboard.csv`. Summary counts explain the unpublished remainder.

The market MCP accepts `view: "qualified_ask_bid_v1"` on `return_rankings` for an exact session. It reads only the bounded new shortlist, preserves producer rank, policy, status and reasons, and keeps source digests with detail references. Older sessions without the policy return `unavailable`; a declared but missing shortlist is `partial`. Detail reads remain limited to the selected contract and sweep page. All/clean requests continue using the legacy shortlist.

## Evidence and limits

The repository contains no raw Sep 29/Oct 1 SOXL or reviewed GS/MU/SPCX session archive. Issue #125 reports legacy-clean SOXL midpoint gains above 1,100% with roughly −77.3%/−72.7% ask-to-later-bid comparisons and 120%/133% median spreads; it identifies three GS contracts already excluded from legacy clean. Tests use explicitly synthetic quote paths shaped like these cases, preserve clean status and return signs, and add tighter-spread MU/SPCX-shaped controls. These are **not** offline replays of the eight reviewed sessions and do not establish actual included/excluded/unknown counts or rank changes for them. The linked public leaderboard files were inaccessible in this environment; denied raw archives were not fetched. Replay from permitted complete local archives is needed before threshold calibration.

Sampled quotes miss between-sample moves. Displayed size, spread and freshness do not establish fills, queue position, slippage, capacity, execution, or trading profitability.
