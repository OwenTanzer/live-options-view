# OI skew meter: proposed bot-state read path (inactive proposal)

Status: **proposal only.** This authorizes no provider write, new live writer or release. It answers Chej's 2026-11-13 14:09 request for the minimal read path and the exact missing fields. The browser side (#233) already renders a `oi_skew_meter.v1` document if one is ever served, and otherwise shows "Bot signal: not published".

## What exists today (verified)

- **Private runner ledger** (`crassus_audit.v1`, `decisions-<run_id>.jsonl` on the runner volume, archived to the private R2 bucket by `crassus/archive.py`). Every OI Skew decision row carries:
  - `run_id`, `decision_id`, `timestamp_utc` (decision time) and `market_snapshot_timestamp` (observation time);
  - `market_snapshot_url_or_hash`, `strategy_version` (`1.0.0`);
  - `decision.metadata`: `session_phase`, `imbalance_ratio`, `session_start_imbalance`, `delta_from_session_start`, `band_strikes`, `near_money_call_oi`, `near_money_put_oi`.
- **Effective parameters**, per Chej's read-only check of the deployed source: band 0.02, |imbalance| ≥ 0.3, |session change| ≥ 0.1, ≥ 4 OI-bearing strikes, with no account override observed. That is a readback of the inspected state, not a guarantee.
- **Resets happen in practice.** In the exported ledger (Sep 3 – Nov 9), **6 of 46** UTC session dates show more than one `session_start_imbalance` value, because a runner restart started a new tracker mid-day (Sep 4 had nine run IDs). The anchor's identity is therefore `(run_id, UTC session date)`, not the date alone.

## Exact missing fields

| Field | Why the display needs it | Where it would come from |
|---|---|---|
| `anchor_observed_at` | Shows *when* the session-start reading was taken. Together with `run_id`, it identifies a restart reset. | `SessionImbalanceTracker._observations[0][0]`; not emitted today |
| `session_key` | The tracker's reset key (the UTC date). It can be derived as `market_snapshot_timestamp[:10]`, but emitting it removes ambiguity. | `SessionImbalanceTracker._session_key` |
| `params` (`near_money_pct`, `imbalance_threshold`, `min_change_from_session_start`, `min_band_strikes`) | The gates actually applied in this decision. Today they must be inferred from source plus config. | the effective values already computed in `_decide_core` |
| `code_revision` | Source revision of the running image. Rows carry only `strategy_version` `1.0.0`. | the deploy environment's commit SHA, recorded once per run |

All four are additive metadata. They change no decision, but adding them is a `crassus/**` change, so it is a runner deploy candidate.

## Minimal proposed read path

1. **Runner (crassus lane, needs approval):** add the four fields above to the OI Skew decision metadata and run record. No behaviour change.
2. **Projector (the new writer, needs approval):** after each OI Skew decision, build one `oi_skew_meter.v1` document from that ledger row and write it to a single public key, e.g. `crassus/oi-skew/v1/latest.json`. It would include:
   - `schema_version`, `strategy_id`, `strategy_version`, `code_revision`;
   - `run_id` (a hash is acceptable), `session_key`, `anchor_observed_at`, `session_start_imbalance`;
   - `observed_snapshot_timestamp`, `decision_at`, `published_at`;
   - `session_phase`, `imbalance_ratio`, `delta_from_session_start`, `band_strikes`, `params`;
   - `status` (`ok` | `unavailable` with a reason).

   It would exclude account username/alias identifiers, cash, positions, trades, decision/execution IDs, server responses and any credential.
3. **API lane (needs approval):** add that one key to `BROWSER_DATA_KEYS` in `backend/browser-data.js`.
4. **Asset lane:** the site fetches the key on the existing 60 s cycle only when enabled. `describeBotOiSkew` already classifies with the same gates as `_decide_core`, which is pinned by the parity fixture. Freshness comes from `observed_snapshot_timestamp`, and a missing or failed fetch shows unavailable, never a stale value as live.

## Alternatives considered

- **Derive the anchor in the browser from the first page load:** rejected. It is wrong after any restart and is explicitly ruled out.
- **Serve from the private archive bucket via the API:** batched ~1 MB uploads add minutes of lag, and the private bucket would gain a public reader. That is worse than one purpose-built key.

## Implemented (inactive, local only) — branch `claude/oi-skew-projection`

Built on `claude/oi-skew-meter` 7d123de (#233). One commit per release lane:

| Lane | Commit | What | Live effect today |
|---|---|---|---|
| Runner (crassus) | f32116c | `oi_skew_qqq` metadata adds `session_key`, `anchor_observed_at` and `params`; `decision_base` adds `code_revision` (`RAILWAY_GIT_COMMIT_SHA`, else null). `scripts/verify_oi_skew_provenance.py` (13 checks). | None until deployed. Decisions are unchanged (the parity fixture from the real strategy is still current). |
| Runner (crassus) | ba05729 | `crassus/oi_skew_projection.py`: a pure, sanitized `oi_skew_meter.v1` builder with a local-file CLI. No writer, no call site. `scripts/verify_oi_skew_projection.py` (36 checks). | None. |
| API + gateway | 462f6ed, then relocated | `OI_SKEW_PROJECTION_KEY` with `OI_SKEW_PROJECTION_PUBLIC = false` in `backend/browser-data.js` and `frontend/preview-policy.mjs`. While disabled the key returns 404; a test pins both sides. | None. |
| Asset (site) | 888a12c | The meter reads the projection's status and provenance. `OI_SKEW_PROJECTION_ENABLED = false`, so no request is made. A missing, 404, wrong or incomplete document shows unavailable. 184 checks, with 14/14 projection parity cases. | None. The page still shows "Bot signal: not published". |

**Local evidence.** On the real private export (Sep 3 – Nov 9) the builder returns `status: unavailable`, `missing_or_invalid:code_revision,session_key,anchor_observed_at,params`. Those rows predate the additive fields, so nothing is guessed. No alias, username, raw run_id, cash or decision ID appears in the output.

**MediaFlow coordination.** This branch changes only the two `oi-skew` `?v=` lines in `site/index.html` and the key-set lines of the two allowlists. Trial merges with `feat/news-g6` and `feat/news-g7` are conflict-free.

## Release handoff (each separately approved, in this order)

1. **Runner:** merge the crassus commits. This makes crassus-runner a deployment candidate: a Railway redeploy and restart, which means an observation gap and a fresh OI Skew anchor. Afterwards, verify that new ledger rows carry the four fields and that `code_revision` is non-null.
2. **Projector (the new writer):** not built. It needs approval of the destination object and credentials, and a call site after each OI Skew decision that runs `build_projection(...)`, then `assert_sanitized(...)`, then writes `crassus/oi-skew/v1/latest.json`.
3. **API:** flip `OI_SKEW_PROJECTION_PUBLIC` in `backend/browser-data.js` through the reviewed `deploy.yml` lane, only after (2) is writing.
4. **Asset:** flip the gateway flag and `OI_SKEW_PROJECTION_ENABLED` through `deploy-staged.yml`. Verify the live meter shows the bot state with its anchor, reset identity and revision, and shows unavailable when the object is stale or missing.

## H1 delivery runbook (2026-11-13, authorized scope; operator: Chej)

### Candidates
- **A. Writer + both indicators, flags off:** branch `claude/oi-skew-h1-delivery`. It contains #233 → #235 in dependency order, then the reconciled #237 harness (master 4629da6 merged in), then the fail-safe writer (`crassus/oi_skew_publisher.py`) and `scripts/check_published_projection.py`. Safe to merge before anything is configured: the writer is off without its settings, and both flags are false.
- **B. Flags:** branch `claude/oi-skew-h1-flags`, a single commit on top of A that flips:
  - API `OI_SKEW_PROJECTION_PUBLIC = true`;
  - gateway preview flag = true;
  - site `OI_SKEW_PROJECTION_ENABLED = true`.

### Read path (verified read-only, 2026-11-13)
- **Production custom domain.** The page on `https://options.moopertonic.net` reads data **directly from the public R2 bucket** (`https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev`); its own `/browser-data/*` returns 403 for every key, including `intraday/latest.json`. The real site's read path for the bot indicator is therefore the public bucket object. **Writing it makes it publicly readable at the r2.dev URL immediately**, whatever the API flag says.
- **workers.dev host.** On `https://live-options-view.otmooper12.workers.dev`, `/browser-data/*` is served by the API allowlist. There the projection key needs the API flag (B). It returns 404 today.

### Steps
1. **Merge #237, then A** (Chej). A touches `crassus/**`, which makes it a crassus-runner deploy candidate and causes a **restart**:
   - observation gap;
   - new `run_id`;
   - fresh OI Skew anchor at the first snapshot after the restart.

   These appear honestly in the projection (`run_identity`, `anchor_observed_at`). Recommended timing: after the close, with capture protected. Record the current runner deployment ID first, for rollback.
2. **Configure the writer.** Set the runner environment variables:
   - `CRASSUS_OI_SKEW_PUBLIC_ACCOUNT_ID`
   - `CRASSUS_OI_SKEW_PUBLIC_BUCKET`, set to the bucket served at `pub-4d5c…r2.dev`
   - `CRASSUS_OI_SKEW_PUBLIC_ACCESS_KEY_ID`
   - `CRASSUS_OI_SKEW_PUBLIC_SECRET_ACCESS_KEY`

   Use a credential scoped to that bucket. Any missing setting keeps publication off, logged as `oi skew public projection disabled: <reason>`, and the runner is unaffected.
3. **Verify the object** after the first OI Skew decision (runner cycles are every 5 min; outside market hours the decisions are still recorded, with no trades):
   `python crassus/scripts/check_published_projection.py https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/crassus/oi-skew/v1/latest.json`

   It should exit 0, with `problems: []`, schema v1 and allowlisted keys only. `status` is `ok` once the new provenance fields are present, which they will be on every post-deploy row.
4. **API:** run `deploy.yml` at B (Chej dispatches and approves). This opens the key on workers.dev only.
5. **Assets:** run `deploy-staged.yml` at B. The site fetches the projection on the existing 60 s cycle.
6. **End-to-end:** Playwright, desktop 1366 and mobile 390, against `https://options.moopertonic.net`. Check:
   - both indicators render;
   - the bot indicator shows the anchor, session/run identity, decision time and revision;
   - freshness behaves correctly (live during the session, then stale or closed);
   - no horizontal overflow and the ARIA output is correct.

   The missing, stale and incomplete states remain covered by fixtures (303 meter checks, 14 parity cases).

### Rollback (per lane; flags-off containment first)
| Lane | Containment | Previous known-good |
|---|---|---|
| Assets | Revert B (site and gateway flags false) and release | Asset revision `f759582e2b327989e31749ddfeae7df9c6474890` (current production, verified 2026-11-13) |
| API | `OI_SKEW_PROJECTION_PUBLIC = false` via `deploy.yml` | The API version recorded by Chej immediately before step 4 |
| Writer | Unset the `CRASSUS_OI_SKEW_PUBLIC_*` variables (publication off; an environment change redeploys the runner). Optionally delete the object; otherwise the page shows it aging to stale or closed | The runner deployment recorded before step 1 |

Any runner rollback is another restart, with the same consequences: observation gap, new `run_id`, fresh anchor. Trading behaviour is unchanged in every lane, because the writer is post-write and non-blocking.
