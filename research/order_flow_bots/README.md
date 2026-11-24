# Eleven QQQ order flow bots

For prospective paper trading see [running the forward service](forward-service.md). Each rule has its own one-share QQQ ledger. The service reads live flow and chain projections, records decisions as received, and uses subsequent, fresh underlying bid/ask observations for paper fills. Turnover against verified opening OI (slot 3) remains deferred until clearing provenance is available. The commands below exercise the historical research implementation and its distinct execution assumptions.

Run the audited QQQ replay and the regression tests from the repository root with Node 22 or later.

```sh
node research/order_flow_bots/run.cjs site/order-flow-replay.json /tmp/qqq-flow-run
node --test research/order_flow_bots/*.test.cjs
```

Choose a new output directory for each run. Omit the directory to print the count summary alone. The input is the full same-day QQQ print archive, enriched with timestamped spot, gamma, and OI snapshots. The replay identifies exchange and snapshot times. It reconstructs historical availability. A live adapter must retain receipt times, deduplicate prints, and prove the entire decision window survived the 2,048-print display cap.

| Slot | Signal | Forecast horizon |
| --- | --- | --- |
| 1 | Breakout under robust negative flow gamma, or fade away from a previously selected positive anchor under robust positive flow gamma | 15 min |
| 2 | Signed near-money premium in a historical tail | 15 min |
| 3 | First cumulative volume crossing of verified opening OI with dominant classified side | 15 min |
| 4 | Rejection of a positive lower-bound gamma strike frozen before the touch | 5 min |
| 5 | Failed price breakout after signed pressure weakens or recovers | 10 min |
| 6 | Unusually large unsigned near-money premium | 15 min sampled range |
| 7 | Strong buying or selling at a price extreme followed by a reclaim | 10 min |
| 8 | Signed pressure across strikes survives removal of its largest contributor | 15 min |
| 9 | An approach to frozen opening-snapshot put OI accompanied by selling pressure | 15 min |
| 10 | Volume migrates between strikes while QQQ spot changes little | 5 min |
| 11 | Synchronous same-strike put/call print residuals agree across strikes | 5 min |

Slots 2 and 6 freeze the same ±$1.50 basket, reject windows above 30% unknown **by print count**, and compute percentiles on the same complete, disjoint trailing five-minute reference windows. The minimum of six reference windows is an exploratory warm-up. Slot 2 uses signed premium dollars and looks in either tail, while slot 6 uses unsigned premium dollars and fires on the upper tail for expansion. Both diagnostics retain the 10th and 90th percentile. Quiet unsigned windows remain available for comparison. Unknown quantity and premium are recoverable separately from the frames. A missing spot, gap, or missing required strike cannot create a zero-valued observation.

The original replay has inferred clearing dates. Slot 3 abstains by default. If independently established, pass a JSON sidecar as the optional argument immediately following the output directory. It must specify session, clearingDate, availableAt (epoch milliseconds no later than open), source (evidence reference), and manifestSha256. The caller must authenticate that evidence. Slot 9 uses the opening **observed** OI ranking and makes no clearing-date claim.

## Reading a run

- frames.jsonl contains decision-time inputs and separated signed, unsigned and unknown activity. decisions.jsonl includes every rule at every minute. Each fresh signal transition outside its prior horizon starts one episode.
- anchors.jsonl holds forward spot return, sampled range and sampled downside excursion for every distinct decision time and horizon. outcomes.jsonl supplies episode-specific direction-adjusted or range labels. Scoring begins at the first distinct spot observation strictly after the decision. Gaps, missing endpoints and horizons outside the session retain missing labels.
- paired.json matches equal counts of signed and unsigned episodes chronologically within half-hour buckets, without selecting on outcomes. Both arms use the same sampled range measure. Inference on directional value needs additional days, price controls, and a chronological holdout.
- oneClock.json contains eleven native Event Builder rules and reconstructed source rows. The test suite evaluates them with the repository's One Clock engine and checks episode counts against the decision ledger.
- paper.json models eleven isolated one-share QQQ accounts, starting with $10,000 each. Synthetic entries and exits use observed underlying snapshots and an adverse one basis point per side. Slot 6 waits for a breakout of the pre-signal sampled range and uses the following observation for its entry. Inspect skips and unresolved positions alongside closed trades. The simulator cannot establish actual fills or options P&L.
- summary.json records source digest, active minutes, distinct episodes and scored episodes. Related strategies can forecast the same QQQ move. Assess coincidence, matched controls and incremental performance on held-out sessions before interpreting them as portfolio diversification.
