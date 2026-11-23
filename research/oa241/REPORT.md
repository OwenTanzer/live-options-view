# Gamma Exposure and Order Flow: display reconciliation, Order Flow tab, five-date inventory

Branch `claude/oa241-order-flow-gamma` off master `6f78e7f`. Covers G1, the display archive check (Sept 29 and Oct 1), G3a, G3-live, the G3b handoff material and G2. G3 on the five-date sample is answered as unresolved, with the missing input named. G4, G5, D1–D3 and the findings report are not started here.

## Reproduction

```sh
# Accepted OA-180 archive (public origin), then everything offline:
git show origin/research/oa180-three-session-signing:research/oa180/fetch.py > fetch.py
python fetch.py ARCHIVE                     # moo144/tradier/{2026-09-25,09-29,10-01}
python research/oa241/g1_reconcile.py ARCHIVE > research/oa241/g1_results.json
python research/oa241/build_replay.py ARCHIVE SNAPSHOTS > site/order-flow-replay.json
python research/oa241/live_offline.py ARCHIVE 2> research/oa241/live_offline_20260925.json
python research/oa241/g2_inventory.py SNAPSHOTS TRADIER_LISTING.json > research/oa241/g2_inventory.json
node --test tests/order_flow_exposure.test.js tests/order_flow_model.test.js
python -m unittest discover -s tests -p test_qqq_recent.py
```

`SNAPSHOTS` is a read-only copy of `intraday/YYYYMMDD/snapshot_*.csv` from the chain bucket. `TRADIER_LISTING.json` lists `moo144/tradier/{date}/` for the G2 dates. OA-180's `audit.py` reproduces its committed `results.json` exactly; only the manifest path separators differ on Windows.

## G1 and display archive check

Both paths read the same digest-verified segments in key order. The display path is `market_data/tradier/recent.py` exactly as OA-240 shipped it, fed every archived event, with its 2,048-row deque widened so every accepted row is kept. The audit is OA-180's combined method (1 s quote midpoint, else tick), applied per print and asserted equal to OA-180 `results.json` before any comparison. Prints are joined on (symbol, seq). Each cell is **prints / contracts**.

| Session | Window (provider time, ET) | Path | Inferred buy | Inferred sell | Unknown | Total |
|---|---|---|---:|---:|---:|---:|
| Sept 25 | 09:30:00–16:00:00 full session | display | 269,875 / 1,798,966 | 281,278 / 1,826,314 | 129,008 / 1,158,309 | 680,161 / 4,783,589 |
| | | audit | 269,875 / 1,798,966 | 281,278 / 1,826,314 | 129,008 / 1,158,309 | 680,161 / 4,783,589 |
| Sept 25 | 09:30:00.094–09:30:45.136 (OA-240 replay span) | display | 955 / 4,814 | 1,011 / 4,995 | 600 / 5,022 | 2,566 / 14,831 |
| | | audit | 955 / 4,814 | 1,011 / 4,995 | 600 / 5,022 | 2,566 / 14,831 |
| | | published file | | | | 2,565 / 14,830 |
| Sept 25 | 15:57:00.587–15:59:59.736 (2,048-row screen at close) | display | 683 / 6,688 | 829 / 9,633 | 536 / 9,235 | 2,048 / 25,556 |
| | | audit | 683 / 6,688 | 829 / 9,633 | 536 / 9,235 | 2,048 / 25,556 |
| Sept 29 | 09:30:00–16:00:00 full session | display = audit | 263,021 / 1,669,865 | 278,475 / 1,729,777 | 118,088 / 996,585 | 659,584 / 4,396,227 |
| Sept 29 | 15:56:36.299–15:59:59.436 (screen at close) | display | 754 / 11,146 | 838 / 7,419 | 440 / 5,684 | 2,032 / 24,249 |
| | | audit | 754 / 11,146 | 839 / 7,420 | 440 / 5,684 | 2,033 / 24,250 |
| Oct 1 | 09:30:00–16:00:00 full session | display = audit | 257,267 / 1,719,670 | 271,975 / 1,695,914 | 109,813 / 977,012 | 639,055 / 4,392,596 |
| Oct 1 | 15:58:01.265–15:59:59.898 (screen at close) | display = audit | 756 / 5,525 | 792 / 13,484 | 500 / 5,533 | 2,048 / 24,542 |

**Per-print agreement.** Over the full sessions every print joins, with zero display-only and zero audit-only prints. Every joined print has the same side: 680,161, 659,584 and 639,055 prints. The display path's lack of a session-time filter changes nothing, because the collector already excludes out-of-session prints. Its bounded 16,384-identity dedupe never admitted a duplicate on these days.

**Differences, all from windowing, none from signing:**
1. **The published OA-240 file holds one print fewer than its provider-time span.** QQQ260925C00745000 seq 555 (09:30:45.022, 1 contract, unknown side) has provider time inside the span but was received at 09:30:45.241, after the file's last row by receipt. The file was cut in receipt order. Its 2,565 rows equal the display path's rows field for field.
2. **The Sept 29 screen at close differs by one print.** The 2,048 cap evicts by arrival, not provider time. QQQ260929C00738000 seq 161591 (1 contract, sell) has the same provider millisecond as the oldest retained row and arrived one position before the cap boundary. 16 retained rows have provider times earlier than the oldest-by-arrival row. Against a full-session archive, the screen is therefore a receipt-order window, and its provider-time span can include evicted prints.

## G3a: replay view (Sept 25)

`site/order-flow-replay.json` (schema `order-flow-replay/v2`, 8.9 MB, 2.4 MB gzip) carries all 680,161 display-path prints (build asserts the audit totals). It also carries the 16 exact contracts, 598 chain snapshots (OI, gamma and spot for those contracts only) and each contract's settled-OI observations with first-seen times. On Sept 25 the pre-refresh OI (for example 6,854 on the 740 call) is replaced at 06:30:50 ET (9,097). Replay uses the refreshed value from the open.

The page (`site/order-flow.html`) keeps the existing clock, real strike axis and print plot. At the plot's right-edge time, a strike-aligned panel shows:
- unsigned settled-OI gamma concentration;
- dealer flow S−B, with the unknowns-to-buyers and unknowns-to-sellers whiskers;
- hatching where inputs are missing.

A table gives observed B/S/U, concentration, flow, endpoints and the conditional total range per strike, for 100% (primary) or 50% (illustrative) participation. Tapping a strike or print shows, per contract:
- recorded prints and inferred sides;
- S−B with endpoints, and the opening position that would offset it;
- settled OI and its first-seen time, gamma, spot, and dollars per 1%;
- both participation cases, the total range with its continuity assumption, and the snapshot age.

Values use one snapshot's gamma and spot at or before the displayed time. A snapshot older than 2 minutes, or a missing field, shows as no coverage. The whole calculation lives in `site/shared/order-flow-exposure.js`, and both modes go through `site/shared/order-flow-model.js`. The tests cover the quest fixture: −6 contracts, endpoints −14/+2 with uncertain sign, totals −34/+22 and −27/+21, and $200 per net contract.

## G3-live

`recent.py` now keeps a per-session, per-contract B/S/U accumulator beside the 2,048-row plot deque and publishes it as `session` in `order-flow/qqq/v1/recent.json`.
- **Dedupe:** exact for the whole session, using a compact per-symbol sequence set. The bounded 16,384 horizon is gone.
- **Reconnects:** gap markers written by `capture.py` are now fed to the projection and published as gaps.
- **Restarts:** at startup, `run.py` calls `rebuild`, which replays the session's archived segments (R2, then unuploaded local spool) before capture. Overlaps deduplicate by identity. The interval from the last archived receipt to the first live event is published as a restart gap, and a failed rebuild is published as incomplete history.

The page reads the session counters and `intraday/latest.json` (OI, gamma, spot) through the same calculation. Its status line shows:
- stale feed or chain;
- coverage start and gaps;
- INCOMPLETE HISTORY;
- "latest N of M session prints".

**Offline exercise with archived events** (`live_offline.py`, Sept 25):
- Process A takes the first 65 segments with an injected reconnect, then crashes.
- Process B rebuilds those 65 from a stand-in R2, then receives live events starting 2 segments before the crash (deliberate overlap).

All 8 checks pass:
- B's session equals the OA-180 audit per side (1,798,966 / 1,826,314 / 1,158,309).
- There is no double counting, and the display stays at 2,048 rows while the session counts 680,161 prints.
- The reconnect gap is published.
- The rebuild covers 65 segments, and the restart gap stays open until the first live receipt.

`tests/order_flow_model.test.js` shows that the live model fed that payload produces per-strike values identical to replay at the last print. The page was also exercised in a browser in replay mode and in live mode, with that payload substituted for the two fetches.

## G3b: handoff for Owen

**Tests run locally:**

| Suite | Result |
|---|---|
| `tests/order_flow_exposure.test.js` + `tests/order_flow_model.test.js` | 11 pass |
| `test_qqq_recent.py` | 6 pass |
| `test_moo144_tradier_collector.py` | 92 pass, unchanged from master |
| `browser_data`, `build_stamp`, `asset_boundary` | pass |

The new tests are added to `web-ci.yml` and `collector-ci.yml`.

**Deployment steps:**
1. Merge.
2. Deploy site assets through the existing asset workflow. The new `site/shared/order-flow-*.js` and the v2 replay file are tracked, so the build picks them up.
3. Redeploy the Tradier collector so `recent.py`, `capture.py` and `run.py` take effect. Until then, the live tab shows the old feed's rows with "no session history published", and every strike shows no coverage.

Rollback means reverting the commit. The old `recent.json` shape is a subset of the new one.

**Coverage limits:**
- **Contracts:** only the 16 contracts selected at 09:29 (8 strikes) are covered. OI concentration elsewhere in the chain is not shown.
- **Snapshots:** gamma and spot come from ~1-minute chain snapshots, not trade-time revaluation.
- **OI:** the settled OI clearing date is not in the source. The page labels it as the inferred prior weekday, with no holidays modelled. Live has no first-seen time.
- **Restart time:** a mid-session collector restart re-reads that day's archive before capture, which is roughly 60 MB late in the day, so stream start is delayed by that read. The delay is published as the restart gap.
- **Restart gap:** prints neither archived nor spooled before a crash are lost, and the gap remains visible.
- **Replay file size:** the replay file is 2.4 MB gzip.
- **Cross-origin reads:** R2 CORS allows only the production origin, so live mode cannot be exercised from previews or localhost against real data.

## G2: five-date inventory

| Date | Trade prints / preceding quotes | Settled OI at 09:30 | Spot + gamma (RTH snapshots) | Captured strikes | Gaps > 2 min | Strongest estimate |
|---|---|---|---|---|---|---|
| Sept 10 | none archived | 150/150 0DTE contracts; last change 08:53 | 378 snapshots, gamma 100% usable | 72 (674–745) | none | unsigned |
| Sept 11 | none archived | 144/144; 08:57 | 378, 100% | 70 (681–750) | none | unsigned |
| Sept 14 | none archived | 140/140; 08:37 | 378, 100% | 77 (670–746) | none | unsigned |
| Sept 15 | none archived | 140/140; 08:49 | 378, 100% | 72 (671–742) | none | unsigned |
| Sept 16 | none archived | 142/142; 09:04 | 378, 100% | 79 (667–745) | none | unsigned |

**"Unsigned"** means settled-OI gamma concentration (OA-171 C) plus unsigned minute volume activity (OA-171 A) by strike and time. The Tradier trade archive has no prefix for any of these dates: it starts with a single day on Sept 3, then runs from Sept 21. The snapshots carry minute bid/ask and cumulative volume, but no trades, so no print can be signed.

**G3 on the five-date sample: unresolved.** No date qualifies for the OA-178 signed reconstruction. The missing input on all five dates is trade-level prints with preceding quotes. The signed reconstruction is supported on Sept 25, Sept 29 and Oct 1, which this deliverable already exercises.
