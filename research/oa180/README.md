# QQQ option transaction-signing feasibility

Replay the evidence with Python 3 and requests installed. From this directory run `python3 fetch.py /path/to/cache` once, then `python3 audit.py /path/to/cache > reproduced.json` offline. Compare the result with `results.json`. Run without Python optimization so that integrity assertions execute. The unit of every signing table is option contracts traded. Stock prints are retained in integrity checks only. No share multiplier is applied.

The study uses the first full sessions accepted after the September 23 gate. September 24 missed the opening, September 28 had a 13.572-second reconnect and September 30 started 257.7 seconds late. Each selected session has its own 16 option contracts plus QQQ. The report preserves symbols, manifest keys and digests, compressed-byte and segment-count checks, aggregate event and per-symbol reconciliation, provider diagnostics, field population and example source lines. Archive keys are relative to the public origin in fetch.py.

## Methods

Process archived receipt order, using provider epoch milliseconds for market ordering. Use the last independently archived quote for each option, with embedded pre-open context only during initial warmup. Skip the quote if its collector receipt is at or after the trade provider time (conservative cross-clock check); do not search backward for a more convenient quote. Provider quote timestamps often have whole-second granularity, so the 100 ms sensitivity is especially restrictive. A quote must already have been received and both sides must have provider timestamps strictly before the trade. Measure age from the older side. Require positive uncrossed, unlocked quotes. Classify prices above or below midpoint as buys or sells. A midpoint print is unknown. The baseline maximum quote age is one second. Repeat with 100 ms and five seconds, then cap the one-second spread at ten cents. These are sensitivity settings for this feed.

The tick rule compares against the same option's previous eligible print. An uptick buys, a downtick sells, and a zero tick carries the prior sign when known. Equal or reversed provider times yield unknown for that comparison. The combined method uses the baseline quote result first and falls back to tick. Quote and tick use different evidence and can assign opposite signs. The archive retains normalized events. Its exchange flags cannot reconstruct multi-leg packages. Missing or reused sequences are excluded; marked cancellations, corrections and duplicates do not enter signed or unknown volume. No retroactive bust matching is assumed. The estimates describe the accepted print tape as archived.

The result file supplies a buy/sell/unknown table for every method and session, volume agreement on the common signed subset, and reproducible segment/line examples with the preceding tick and quote. In example records the previous tick stores provider milliseconds, price in ten-thousandths of a dollar and prior sign. Provider diagnostic counts cover the entire QQQ lane; the signing tables cover options only.

## Interpretation and handoff to OA-178

Use signed flows only as scenarios and carry unknown volume explicitly. No participant identity, open/close flag, starting inventory or observed initiating side is established here. A bounded validation step is to compare a stratified sample of 100 independently side-labeled option trades, split equally across quote/tick agreement and disagreement, reporting concordance within each group. Without independently labeled trades, accuracy remains unmeasured.

Sources: [Lee and Ready (1991)](https://doi.org/10.1111/j.1540-6261.1991.tb02683.x), [Savickas and Wilson (2009), option trade direction](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/on-inferring-the-direction-of-option-trades/FDA4541B57F78B2C8DCE129AFC25AAF0), [Tradier streaming documentation](https://docs.tradier.com/streaming), [original source assessment](https://app.notion.com/p/4193c519147b810db806dc4bb68ed79b).
| Session | Accepted contracts | Quote signed | Tick signed | Combined signed |
|---|---:|---:|---:|---:|
| 2026-09-25 | 4,783,589 | 36.20% | 61.42% | 75.79% |
| 2026-09-29 | 4,396,227 | 40.40% | 61.87% | 77.33% |
| 2026-10-01 | 4,392,596 | 38.22% | 63.94% | 77.76% |

| Method | Buy contracts | Sell contracts | Unknown contracts | Signed coverage |
|---|---:|---:|---:|---:|
| combined | 5,188,501 | 5,252,005 | 3,131,906 | 76.92% |
| quote_100ms | 566 | 250 | 13,571,596 | 0.01% |
| quote_1s | 2,558,605 | 2,627,865 | 8,385,942 | 38.21% |
| quote_1s_spread_le_10c | 2,553,676 | 2,623,439 | 8,395,297 | 38.14% |
| quote_5s | 5,232,402 | 5,389,346 | 2,950,664 | 78.26% |
| tick | 4,230,678 | 4,236,011 | 5,105,723 | 62.38% |

Both methods assigned a side to 3,212,653 contracts. They agreed on 2,731,873 (85.03%) and disagreed on 480,780.
