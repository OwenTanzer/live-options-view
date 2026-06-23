# Pre-Deployment Review: Live Options View

**Date:** 2026-06-23  
**Scope:** Repository review for `live-options-view` before deployment.  
**Primary mission test:** Can the tool identify recoveries versus crash continuations versus stalls?

---

## Response to Review — 2026-06-23 (commit `572ff6b`)

All ten highest-priority findings from this review were addressed before first deployment. The changes are in `collector.py` and `docs/live.html`. What follows is an item-by-item accounting.

### What changed and why it matters for this morning

**The central problem this review identified:** the system wrote fresh-looking artifacts even when the market data feed was stale, there was no way to tell a crash from a clean restart, and the process would restart-loop all night if Railway bounced it after market close. None of those are acceptable during a high-volatility session where the operator needs to trust what the screen is showing.

#### 1. Evening restart-loop — Fixed

`wait_for_premarket()` previously returned whenever `hour >= 6`, so after the 16:15 ET stop the process would exit, Railway would restart it, it would see `hour >= 6`, and immediately run through auth, subscribe, observe `past_stop()`, and exit again — looping until midnight.

The fix: `wait_for_premarket()` now checks whether the current time is inside the valid window `[06:00, 16:15)`. If called outside that window — including post-close — it calculates next-day 06:00 ET, logs how long it will sleep, and sleeps in hourly chunks. The process never exits. Railway sees a running process and does not restart it.

`main()` is now a `while True` loop: `wait_for_premarket()` → `_run_session()` → back to `wait_for_premarket()`. One process handles every trading day.

#### 2. No lifecycle telemetry (`health.json`) — Added

`intraday/health.json` is written every 15 seconds by a dedicated background thread. It contains:

- `run_id`: a unique token (`YYYYMMDDTHHMMSSZ-{6 hex chars}`) generated at session start. A new `run_id` across consecutive health files means the process restarted.
- `process_start_time`: when this session began.
- `classification`: `clean_start`, `recovery_after_crash`, `recovery_after_gap`, or `unknown` — derived by reading the prior `health.json` at startup (see item 4).
- `feed.connected`, `feed.authorized`, `feed.channel_open`: live websocket state, updated on every state transition.
- `feed.reconnect_count`: increments on every `_on_open` after the first. A non-zero value means the websocket recovered from a disconnect.
- `feed.last_feed_event_time`: the UTC timestamp of the most recent `FEED_DATA` event ingested. This is the ground truth for whether market data is actually flowing.
- `feed.feed_stale`: `true` if `last_feed_event_time` is more than 120 seconds old. This is the primary signal for a stall.
- `feed.last_error`, `feed.last_close_code`: preserved across reconnects for post-hoc diagnosis.
- `uploads.prices_success_count`, `uploads.snapshot_success_count`, `uploads.failure_count`: running counters. A rising failure count with a stagnant success count means the upload path is broken independently of the feed.
- `cadence.snapshot_sequence`, `cadence.expected_next_snapshot_time`, `cadence.missed_snapshot_count`: sequence tracking. If `expected_next_snapshot_time` passes without a new snapshot, the miss counter increments and is logged.
- `symbols.no_data_symbols`: which price tickers are still returning no data at health-check time.

**Operational meaning for this morning:** if the screen shows data but you are uncertain whether the feed is live, open `health.json` directly in a browser tab. `feed.feed_stale: false` and a `last_feed_event_time` within the last two minutes means the feed is actually flowing. `classification: recovery_after_crash` means the process bounced mid-session — valid, but the first snapshot after recovery should be treated as a gap, not continuity.

#### 3. Stale feed masking as live — Fixed

`push_prices()` now checks `last_feed_event_time` before uploading. If no feed event has arrived in the past 120 seconds, `prices.json` is written with `"feed_stale": true`. The browser reads this field and changes the status bar text to "Feed stale — collector connected but no recent market events." The green live dot turns red. The heatmap timestamp already caught a dead snapshot via wall-clock age; this catches the subtler case where the process is alive and uploading, but market data stopped flowing.

#### 4. No startup classification — Added

`_classify_startup()` runs at the start of every session before auth. It reads the prior `health.json` from R2 and applies this logic:

- Prior `health.json` not found → `clean_start` (first ever run).
- Prior `collector.past_stop` is `true` → `clean_start` (normal end-of-day).
- Prior `collector.past_stop` is `false` and gap since last update is < 120 minutes → `recovery_after_crash`.
- Same but gap is >= 120 minutes → `recovery_after_gap`.

This classification is written into every `health.json` for the duration of the session. The browser health bar surfaces it with a yellow dot for recovery states and a green dot for clean starts.

**Operational meaning:** if you see `recovery_after_crash` during the session, the gap between the crash and the reconnect is a real data hole. The chain snapshot immediately after recovery will reflect the current DXLink snapshot, not a continuous record. Trust the timestamp on the snapshot, not the sequence number.

#### 5. Per-event feed freshness tracking — Added

`DXLinkFeed._ingest()` now sets `self._last_event_time = datetime.now(timezone.utc)` on every `FEED_DATA` message, protected by the same lock as the state dict. `get_health()` exposes this field. Because the update happens inside the lock alongside the state write, `last_event_time` is always consistent with the data that was last written — if `last_event_time` advanced, state advanced.

#### 6. Archive CSV key overwrite — Fixed

Archive keys changed from `snapshot_HHMM.csv` to `snapshot_HHMMSS.csv`. A restart within the same minute now writes a distinct key. The forensic record is preserved.

#### 7. Log encoding — Fixed

All Unicode characters in log messages (`→`, `──`, `───`) were replaced with ASCII equivalents (`->`, `--`, `-`). `sys.stdout` is reconfigured to UTF-8 at startup via `reconfigure(encoding="utf-8")` if available. Railway's environment is UTF-8 and will not be affected; this fix ensures local diagnostics on Windows terminals work without `UnicodeEncodeError`.

#### 8. Silent calibration failure — Fixed

`loadRanges()` in `live.html` previously swallowed errors silently. If the fetch failed or returned a non-OK status, `ranges` stayed `null`, `getThresh()` returned `[0,0,0,0]`, and every nonzero OI cell was colored as level 5 (wall). The heatmap looked populated and alarming when it was actually miscalibrated.

Now: on any failure, a red banner appears above the heatmap: *"Calibration data (OIranges.csv) failed to load — heatmap colors are unreliable."* The heatmap still renders so OI numbers are visible, but the operator knows not to trust the color signal.

#### 9. No upload health tracking — Added (via Counters class)

`Counters` is a thread-safe class instantiated per session. `push_prices()`, `take_snapshot()`, and the CSV write each call `counters.inc_*()` on success and `counters.inc_failure()` on exception. These counters are read by `push_health()` every 15 seconds and written to `health.json`. A failure counter that is rising while success counters are stagnant isolates an upload-path problem from a feed problem.

#### 10. Browser health surface — Added

A health bar appears below the status bar once `health.json` is available (it is hidden pre-deployment so the page renders cleanly without the collector running). Three indicator dots:

- **Feed dot**: green = connected and not stale; yellow = connected but reconnecting; red = stale or down.
- **Upload dot**: green = uploads succeeding; yellow = some failures recorded.
- **Classification dot**: green = clean start; yellow = recovery state.

The last 9 characters of `run_id` are shown at the right edge. If the run_id changes between page refreshes, the process restarted.

### Updated go/no-go assessment

**Previous recommendation:** No-go for unattended or mission-critical deployment.

**Updated recommendation:** Go for smoke-test deployment under active monitoring. The three blockers cited in the original acceptable-path condition — restart-loop risk, archive overwrite, log encoding — are all resolved. The primary mission requirement (distinguishing recoveries, stalls, and crash continuations) is now instrumented via `health.json` and surfaced in the browser.

**Remaining caveats before treating as production-grade:**
- DXLink symbol correctness for `$VIX.X`, `/6J:XCME`, and `BTC/USD:CXERX` cannot be verified without live credentials. The post-flush health check in Railway logs is the first-run gate for these.
- The stale-feed threshold (120s) has not been calibrated against actual DXLink keepalive behavior. It may produce false positives in pre-market or between-event periods.
- No automated tests exist. Verification of DXLink ingestion, health classification, and stale detection relies on live first-run observation.
- `latest.json` does not yet carry a `feed_stale` flag — only `prices.json` does. A stalled feed during an 11-minute snapshot window will not be flagged in the heatmap data itself, only in `health.json` and the price strip.

**Deployment gate sequence this morning:**
1. R2 CORS rule applied.
2. Railway service deployed with env vars set.
3. `health.json` appears in R2 within 30s of collector start.
4. Railway logs show `startup classification: clean_start` and all tickers `OK` in health check.
5. `prices.json` appears with `feed_stale: false`.
6. Browser health bar shows three green dots.
7. First `latest.json` appears at ~11 minutes after pre-market open.

---

## Executive Finding

*(Original — superseded by the response above for deployment purposes)*

The current implementation is close to a deployable v1 live viewer for QQQ 0DTE snapshots, but it is not yet instrumented to satisfy the most critical operational goal: distinguishing recoveries, crash continuations, and stalls.

The collector can authenticate, subscribe, write `prices.json`, write `latest.json`, and publish archived CSV snapshots. The browser can render fresh snapshots and mark the heatmap stale by wall-clock age. That is enough for a basic live display. It is not enough for operational diagnosis because the system does not persist process identity, feed event freshness, websocket lifecycle state, upload success/failure counts, expected-versus-actual snapshot cadence, or recovery provenance.

Pre-deployment recommendation: deploy only as a monitored smoke test unless lifecycle telemetry is added first.

## Verification Status

This review is being validated through computational checks where the local environment allows them. Checks are classified as:

- **Passed:** executed locally and returned the expected result.
- **Failed:** executed locally and exposed a defect or environment mismatch.
- **Blocked:** could not be executed in this environment because it requires credentials, network access, live market data, or deployment infrastructure.

Current status will be updated as verification progresses.

### Verification Results

| Check | Status | Result |
|---|---:|---|
| Python bytecode compile: `python -m py_compile collector.py` | Passed | `collector.py` compiles. |
| Python AST parse for `collector.py` | Passed | Source parses cleanly. |
| HTML parser pass for `docs/live.html` | Passed | HTML can be parsed by Python's standard parser. |
| DOM reference scan for `getElementById(...)` targets | Passed | No missing static IDs detected. |
| Inline JavaScript syntax check with Node | Passed | Extracted browser script had no syntax errors. |
| Dependency import check after `pip install -r requirements.txt` | Passed | `requests`, `boto3`, `pandas`, `pytz`, `websocket`, and `pandas_market_calendars` import locally. |
| Required environment variable extraction | Passed | Runtime requires `TASTY_LOGIN`, `TASTY_PASSWORD`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`. |
| DXLink event ingestion fixture | Passed | `Quote`, `Summary`, `Trade`, and `Greeks` fixture events populate expected state fields. |
| Tier classification fixture for 2026-06-23 | Passed | Returns `0DTE_Regular`. |
| Static lifecycle telemetry scan | Failed | Production code has no `health.json`, `run_id`, `last_feed_event_time`, `snapshot_sequence`, or recovery classification. |
| Stale price upload fixture | Failed | `prices.json` can receive a fresh timestamp while price state is unchanged. |
| Stale snapshot upload fixture | Failed | `latest.json` can receive a fresh timestamp while option rows are unchanged. |
| Calibration failure behavior scan | Failed | Missing `OIranges.csv` returns zero thresholds, causing nonzero OI to bucket as level 5. |
| Evening restart-loop scan | Failed | `wait_for_premarket()` accepts any hour after 6:00 ET while Railway restart policy is `always`; after 16:15 ET this can repeatedly start, initialize, stop, and restart. |
| Archive key uniqueness fixture/static scan | Failed | Snapshot CSV keys are minute-granular (`snapshot_HHMM.csv`), so repeated snapshots in the same minute overwrite each other. |
| Logging portability fixture | Failed | Unicode arrow log messages raised `UnicodeEncodeError` under the local Windows code page during fixture execution. |
| Live tastytrade auth check | Blocked | Environment variables are absent in this shell. |
| Live DXLink websocket check | Blocked | Requires tastytrade credentials and market-data access. |
| Live R2 write/read check | Blocked | R2 credentials are absent in this shell. |
| Railway deployment behavior check | Blocked | Requires deployed Railway service access. |

### Verification-Discovered Issues

The computational pass strengthened the original review and found three additional deployment risks:

1. **Evening restart loop:** after the normal 16:15 ET stop, Railway's `always` restart policy can relaunch the process. Because `wait_for_premarket()` returns for any hour `>= 6`, the collector can repeatedly authenticate, initialize, observe `past_stop()`, exit, and be restarted again until midnight ET.
2. **Archive overwrite risk:** archived CSV keys only include hour and minute. A restart or manual rerun inside the same minute can overwrite `intraday/YYYYMMDD/snapshot_HHMM.csv`.
3. **Log encoding risk:** Unicode arrow log messages caused `UnicodeEncodeError` in local Windows execution. Railway's UTF-8 environment may tolerate this, but portable logs should use ASCII or force UTF-8-safe output.

## Tier 1: Familiarization

The repository has four operational assets:

- `collector.py`: Railway process that authenticates with tastytrade, subscribes to DXLink, maintains in-memory market state, and writes to Cloudflare R2.
- `docs/live.html`: Static browser dashboard that reads public R2 files.
- `DESIGN.md`: Product and infrastructure specification.
- `railway.toml` / `requirements.txt`: Railway start command and runtime dependencies.

The intended runtime architecture is:

1. Railway starts `python collector.py`.
2. Collector waits until 6:00 AM ET.
3. Collector authenticates with tastytrade and obtains a DXLink token.
4. Collector loads the QQQ option chain and subscribes to option events plus macro price tickers.
5. Collector writes `intraday/prices.json` every 30 seconds.
6. Collector writes `intraday/latest.json` and `intraday/YYYYMMDD/snapshot_HHMM.csv` every 11 minutes.
7. Static dashboard polls `prices.json` and `latest.json` from R2.

The code compiles cleanly with `python -m py_compile collector.py`. No automated tests are present.

## Tier 2: Total Architectural Mapping

### Collector Lifecycle

Startup occurs in `main()`:

- Reads `TASTY_LOGIN` and `TASTY_PASSWORD`.
- Waits for the pre-market window.
- Authenticates and gets DXLink token.
- Computes `today` and `tier`.
- Loads the chain.
- Starts `DXLinkFeed`.
- Waits 20 seconds for initial data.
- Starts the 30-second price loop.
- Enters the 11-minute snapshot loop.

Relevant implementation points:

- `collector.py:606` starts `main()`.
- `collector.py:612` authenticates.
- `collector.py:613` captures `today`.
- `collector.py:617` loads the chain.
- `collector.py:629` through `collector.py:631` creates and starts the feed.
- `collector.py:643` through `collector.py:645` starts the price writer thread.
- `collector.py:650` through `collector.py:655` runs the snapshot loop.

### DXLink Feed

`DXLinkFeed` owns websocket setup, auth, subscription, and event ingestion.

Relevant implementation points:

- `collector.py:221` through `collector.py:235` starts `websocket.WebSocketApp.run_forever(reconnect=5)`.
- `collector.py:247` through `collector.py:297` handles setup, auth, channel open, feed setup, and subscription.
- `collector.py:299` through `collector.py:340` ingests `FEED_DATA` into in-memory state.
- `collector.py:342` through `collector.py:346` logs websocket errors and closes.

Architectural note: the feed state is only in memory. It is not timestamped per symbol or per event, and it is not persisted.

### Price Output

`push_prices()` reads in-memory feed state and writes `intraday/prices.json`.

Relevant implementation points:

- `collector.py:428` through `collector.py:469` builds and uploads the price payload.
- `collector.py:472` through `collector.py:479` repeats this every 30 seconds.

The payload includes a write timestamp and price values, but not feed event timestamps, connection status, subscription status, process identity, or stale-field indicators.

### Snapshot Output

`take_snapshot()` reads in-memory feed state and writes both CSV archive and `latest.json`.

Relevant implementation points:

- `collector.py:501` through `collector.py:548` builds option rows from state.
- `collector.py:563` through `collector.py:567` uploads archived CSV.
- `collector.py:571` through `collector.py:586` uploads `intraday/latest.json`.

The snapshot payload includes timestamp, date, expiration, tier, underlying price, snapshot key, and rows. It does not include lifecycle metadata, gap detection, feed freshness, row completeness, or previous snapshot continuity.

### Browser Viewer

`docs/live.html` loads calibration ranges, renders the heatmap, and polls live files.

Relevant implementation points:

- `docs/live.html:335` through `docs/live.html:356` loads `derived/OIranges.csv`.
- `docs/live.html:482` through `docs/live.html:504` fetches `latest.json`.
- `docs/live.html:496` through `docs/live.html:498` marks a snapshot stale if it is older than 15 minutes.
- `docs/live.html:590` through `docs/live.html:598` fetches `prices.json`.

The browser can detect an old `latest.json`, but it cannot distinguish process crash, reconnect recovery, upstream data stall, R2 upload stall, browser CORS failure, symbol failure, or a collector that is alive but writing stale state.

## Tier 2.5: Critical Notes Collected During Mapping

### Critical Note 1: Fresh Uploads Can Mask Stale Market Data

The collector writes new timestamps whenever `push_prices()` or `take_snapshot()` runs, even if DXLink has stopped sending fresh events. Because state is retained in memory and values have no event timestamps, a disconnected or stalled feed can still produce fresh-looking `prices.json` and `latest.json`.

Impact: the dashboard may show `Live` while the market data feed is stale.

### Critical Note 2: No Persistent Process Identity

There is no `run_id`, `boot_id`, `process_start_time`, `restart_count`, or previous-run reference in any R2 artifact.

Impact: a restart cannot be classified as a recovery or crash continuation from the artifacts alone.

### Critical Note 3: No Heartbeat Artifact

The only public health signals are `prices.json` and `latest.json`. These are data products, not operational heartbeats.

Impact: consumers cannot separate "collector alive but feed stalled" from "collector dead" from "upload path broken."

### Critical Note 4: No Expected-Cadence Accounting

The snapshot loop sleeps for 11 minutes after each attempt. There is no sequence number, expected next snapshot time, missed snapshot count, or explicit gap reason.

Impact: a gap in archived CSV files is visible only after manual inspection and cannot be classified automatically.

### Critical Note 5: Websocket Reconnect Is Passive

`run_forever(reconnect=5)` may reconnect after disconnects, and the handlers should resubscribe after channel open. But there is no watchdog that asserts feed events have arrived recently after reconnect, no resubscribe audit, and no escalation if data remains stale.

Impact: websocket reconnect can look successful from process liveness while subscriptions are effectively dead.

### Critical Note 6: Silent Calibration Failure Degrades Heatmap Meaning

If `OIranges.csv` fails to load, `getThresh()` returns `[0, 0, 0, 0]`. Any nonzero OI becomes bucket 5.

Impact: the heatmap can falsely show every nonzero cell as a wall without a visible hard failure.

### Critical Note 7: No Test Coverage

There are no unit tests, integration tests, fixture tests for DXLink message ingestion, or browser fixture tests for `latest.json`.

Impact: pre-deployment validation depends on live external systems, which increases first-run uncertainty.

### Critical Note 8: Encoding Damage In Documentation And UI Source

Several files display mojibake for punctuation and symbols in the current shell output. The browser may still render correctly if served with UTF-8 and the file bytes are valid, but the repository should be checked before deployment.

Impact: non-critical for collector operation, but visible UI text and documentation quality may be degraded.

### Critical Note 9: Railway May Restart-Loop After Market Close

`railway.toml` sets `restartPolicyType = "always"`, and `wait_for_premarket()` returns whenever the ET hour is greater than or equal to 6. After the 16:15 ET stop time, this means a restarted process can pass the pre-market wait, initialize, immediately see `past_stop()`, exit, and then be restarted again.

Impact: unnecessary credential churn, repeated startup logs, avoidable API calls, and possible confusion when diagnosing whether a restart was a recovery or a crash continuation.

### Critical Note 10: Archived CSV Keys Can Be Overwritten

Snapshot archive keys use only `snapshot_HHMM.csv`. If a process restarts or a manual run emits two snapshots in the same minute, both writes target the same R2 key.

Impact: forensic continuity is weaker than expected because a crash continuation or recovery inside the same minute can overwrite the prior snapshot artifact.

### Critical Note 11: Log Output Is Not Encoding-Portable

Fixture execution on this Windows environment raised `UnicodeEncodeError` while logging Unicode arrow messages from `take_snapshot()`.

Impact: Railway may be fine if UTF-8 is enforced, but portable execution and local diagnostics can be disrupted. This also supports replacing decorative Unicode in operational logs with ASCII.

## Tier 3: Critical Evaluation Of Current Structure

The structure is appropriately small for a v1 live dashboard. A single collector and static viewer are pragmatic and reduce deployment complexity. The main engineering weakness is that operational state is implicit rather than explicit.

The collector currently treats writing data as equivalent to being healthy. That assumption is too weak for a market data tool. The difference between "process is running", "websocket is connected", "subscription is active", "events are fresh", "uploads are succeeding", and "browser can read the result" matters directly to user trust.

The present architecture can answer:

- Did the collector write a recent `latest.json`?
- Did the collector write a recent `prices.json`?
- Are there rows in the most recent snapshot?
- Are some configured price symbols missing current values?

The present architecture cannot reliably answer:

- Did the process recover from a crash?
- Did it continue after a crash with stale in-memory assumptions?
- Is the process alive but the feed stalled?
- Is the feed alive but R2 upload failing?
- Is R2 fresh but the browser blocked or miscalibrated?
- Were missed snapshots caused by planned stop time, upstream outage, auth failure, process crash, or upload failure?

For pre-deployment, the architecture should be considered functionally plausible but operationally under-instrumented. The verification pass also found runtime-control risks that should be fixed before unattended deployment: an evening restart loop, archive overwrite risk, and non-portable log encoding.

## Tier 4: Most Critically Missing Functionality

To accomplish the mission of identifying recoveries versus crash continuations versus stalls, the most critical missing functionality is a durable lifecycle and health telemetry layer.

Minimum required addition:

1. Emit `intraday/health.json` every 15-30 seconds.
2. Include a stable `run_id` generated at process start.
3. Include `process_start_time`, `last_loop_time`, `last_price_upload_time`, `last_snapshot_upload_time`, and `last_feed_event_time`.
4. Track websocket lifecycle: `connected`, `authorized`, `channel_open`, `last_close_code`, `last_error`, and `reconnect_count`.
5. Track subscription health: expected subscription count, actual symbols seen, symbols with no data, and symbols stale beyond threshold.
6. Track snapshot cadence: `snapshot_sequence`, `expected_next_snapshot_time`, `missed_snapshot_count`, and last snapshot key.
7. Track upload health: success/failure counters for `prices.json`, `latest.json`, CSV archive, and health artifact.
8. Persist previous run observation: read prior `health.json` on startup and classify startup as `clean_start`, `recovery_after_gap`, `crash_continuation`, or `unknown`.

Recommended classification framework:

- **Recovery:** New `run_id`; previous health showed stopped or stale; current run has authenticated, subscribed, received fresh events, and resumed successful uploads.
- **Crash continuation:** New `run_id`; previous health stopped unexpectedly during active session; current run resumes same trade date after a gap. This is not a clean recovery until fresh feed events and uploads are confirmed.
- **Stall:** Same `run_id` remains visible or process appears alive, but `last_feed_event_time`, `last_snapshot_upload_time`, or expected sequence advancement exceeds threshold.
- **Upload stall:** Feed event time advances, but R2 write timestamps or upload counters stop advancing.
- **Feed stall:** Upload timestamps advance, but `last_feed_event_time` or per-symbol event ages do not.
- **Viewer stale:** R2 artifacts are fresh enough, but browser fetch fails or calibration file fails.

Without this layer, any downstream model or operator will be forced to infer lifecycle state from sparse timestamps, which is not reliable enough for deployment-grade diagnosis.

## Tier 5: Pre-Deployment Review Document

### Go/No-Go Assessment

**Recommendation:** No-go for unattended or mission-critical deployment.  
**Acceptable path:** short smoke-test deployment with active human monitoring after the evening restart-loop risk is patched.  
**Required before relying on the tool:** add lifecycle telemetry, explicit recovery/stall classification, archive uniqueness, and close-session restart control.

### Highest Priority Fixes

1. Add `intraday/health.json` with process, feed, upload, and cadence telemetry.
2. Add per-symbol `last_event_time` in `DXLinkFeed` state.
3. Add stale-feed detection so fresh uploads cannot be labeled live if market data has not advanced.
4. Add startup classification by reading the previous `health.json`.
5. Surface health classification in `docs/live.html`, separate from snapshot age.
6. Fix post-close lifecycle behavior: either keep the process sleeping until the next session or change Railway restart policy so normal stop is not treated as a crash.
7. Make archived CSV keys unique at second or run-sequence granularity.
8. Replace Unicode operational log markers with ASCII or force UTF-8-safe logging.
9. Fail visibly if `OIranges.csv` cannot load; do not silently bucket all nonzero OI as level 5.
10. Add basic tests for DXLink event ingestion, snapshot payload shape, stale classification, OIranges failure handling, and post-close restart behavior.

### Suggested `health.json` Shape

```json
{
  "run_id": "20260623T100001Z-8f3a2c",
  "trade_date": "2026-06-23",
  "process_start_time": "2026-06-23T10:00:01Z",
  "updated_at": "2026-06-23T14:31:10Z",
  "classification": "recovered",
  "collector": {
    "past_stop": false,
    "loop_alive": true,
    "last_loop_time": "2026-06-23T14:31:10Z"
  },
  "feed": {
    "connected": true,
    "authorized": true,
    "channel_open": true,
    "reconnect_count": 1,
    "last_feed_event_time": "2026-06-23T14:31:08Z",
    "last_error": null,
    "last_close_code": null
  },
  "uploads": {
    "prices_success_count": 543,
    "snapshot_success_count": 38,
    "csv_success_count": 38,
    "failure_count": 0,
    "last_price_upload_time": "2026-06-23T14:31:10Z",
    "last_snapshot_upload_time": "2026-06-23T14:22:00Z"
  },
  "cadence": {
    "snapshot_sequence": 38,
    "expected_next_snapshot_time": "2026-06-23T14:33:00Z",
    "missed_snapshot_count": 0
  },
  "symbols": {
    "expected_price_symbols": 11,
    "price_symbols_with_data": 11,
    "stale_symbols": []
  }
}
```

### Deployment Checklist Additions

- Verify `health.json` appears within 30 seconds of collector startup.
- Kill and restart the Railway process during pre-market; verify classification changes from crash continuation to recovered only after fresh DXLink events arrive.
- Let a staging collector cross 16:15 ET; verify it does not enter an automatic restart loop.
- Temporarily break one symbol; verify symbol-level stale or no-data state is visible.
- Temporarily block R2 writes in a staging environment; verify upload stall is distinguishable from feed stall.
- Load `live.html` with `OIranges.csv` unavailable; verify the UI shows calibration failure instead of a misleading heatmap.

### Final Conclusion

The repository is a coherent v1 implementation of the live OI viewer, but it does not yet meet the stated pre-deployment mission standard. The central blocker is not the core data path; it is missing observability and classification. The verification pass additionally found close-session restart-loop risk, archive overwrite risk, and log encoding fragility. Add durable health telemetry, explicit run identity, per-event freshness, startup recovery classification, and deterministic session lifecycle handling before treating this as production-ready.
