# Architecture

> Companion to `README.md`, same quest and commit (`429950c`, Nov 7 2026). Evidence: the [quest Log](https://app.notion.com/p/4123c519147b811dbd36da5d1bcd251b?pvs=204).

## Service map

| Runtime | Where it runs | Entry point | Trigger | Deploy path |
|---|---|---|---|---|
| Collector (QQQ/SPY 0DTE, prices, health, EIA, weekly) | Railway `live-options-view` | `collector.py` | Continuous, session 06:00–16:15 ET | `railway.toml` (Dockerfile build, `restartPolicyType=never`) |
| Live + Historical + Squeeze views | Cloudflare Worker + `docs/` | `worker.js`, `docs/index.html` | Browser poll (60 s / 10 s / 5 s) | `.github/workflows/deploy.yml` (only deploying workflow) |
| Worker paper-trading API + settlement | Same Worker | `worker.js` `/api/*`, `scheduled()` | HTTP; cron `*/15 12-23 * * 1-5` | `.github/workflows/deploy.yml` |
| Crassus bot runner | Railway `crassus-runner` | `python -m crassus.runner --interval 300` | 5-min loop | `crassus/railway.toml` |
| Tradier collector (MOO-169) | Railway `moo169-tradier-collector` | `tradier_cron_launcher.py` | Railway cron `15 12 * * 1-5` UTC | Railway dashboard/API only — no `railway.toml` in repo |
| OA-203 scanner | Railway `oa203-banana-scanner-AWsT` | `scripts/oa203_return_scanner.py run` | Railway cron (expression not in repo) | `railway.oa203.toml` |
| Market MCP server | Nowhere (undeployed) | `market_mcp/server.js` | MCP client request | Image publish only (`.github/workflows/publish-market-mcp-pr133.yml`, pinned commit) |
| MOO-171 audit, synthetic_days, news-pin-bot | Manual / local | `moo171_snapshot_analysis/run_audit.py`, `synthetic_days/day_generator.py`, `news_pin_bot/main.py` | On demand | No manifest in repo |

## Data contracts — R2 bucket `qqq-options-chain-data`

Public origin: `https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev`. The Worker exposes a subset through `/browser-data/` (explicit allowlist + `isPublicArchiveKey`, `worker.js:6–30`).

| Key | Writer | Readers | Freshness / notes |
|---|---|---|---|
| `intraday/latest.json` (+ `intraday/spy/latest.json`) | Collector (`take_snapshot`, 60 s) | Live tab, Crassus, MCP, MOO-171, `docs/diag.html` | Live during session; not overwritten when the feed has zero bids |
| `intraday/YYYYMMDD/snapshot_*.csv`, `first.csv`, `vwap_state.json`, `momentum_log.jsonl` | Collector | MOO-171, synthetic_days | Per-session archive |
| `intraday/prices.json` | Collector (`prices_loop`, 10 s) | Price strip, MCP | 10 s poll |
| `intraday/health.json` | Collector (`health_loop`, 15 s) | Health display, `docs/diag.html` | 15 s |
| `derived/OIranges.csv` | **Not in this repo** (presumably qqq-data-pipeline) | Live + Historical heatmaps (bucket calibration) | 268 rows; Regular n=97, Weekly n=19, Monthly n=6 |
| `manifest.json` | **Shared, two writers:** `dates` — external pipeline (not in repo); `weekly_expirations` + `updated_at` — this collector | Historical tab (date index), weekly manifest readers | `dates` ends 2026-09-18; `updated_at` 2026-11-06 |
| `raw/qqq_chain_YYYYMMDD.csv`, `raw/opex/qqq_chain_*.csv` | **Not in this repo** (repo names qqq-data-pipeline) | Historical tab, Worker settlement sweep (`fetchSpotMarkForDate`) | Settled D-1 OI; 176 dates 2025-12-29 → 2026-09-18 |
| `prices/qqq_intraday_*.csv` | **Not in this repo** (repo names qqq-data-pipeline) | Historical tab widgets (intraday + daily candles) | 5-min bars; 78 bars on 2026-09-18 |
| `raw/weekly/<expiry>/qqq_chain_<date>_<ts>.csv` | Collector (hourly, ~09:30–15:30) | Nothing in the browser; manifest lists them | 9 expiries, 2026-09-11 → 2026-11-06 |
| `macro/eia_steo.json` | Collector (EIA STEO loop, every 6 h, only if `EIA_API_KEY` set) | STEO panel, `crude_calibration.py` | Independent of market hours — **live since Nov 7, 2026 ~21:45 PT: first publication exists (release 2026-11, `prior_release: null`, `revisions: []`, OVX `no_data`); loop polls on startup, not only on its 6 h cadence. Panel render confirmed in the live browser 21:58 PT. First revision value needs a second, changed release (Dec STEO). Worker route 403s on direct navigation (origin gate, by design).** Vintage log `baselines/eia_steo_vintages.json` also exists (single entry, 2026-11); deliberately not browser-exposed |
| `moo144/tradier[/-ibit]/<date>/` | Tradier collector | **None in this repo** — write-only | Gzip NDJSON segments + summary/manifest |
| `oa203/scanner/<date>/` | OA-203 scanner | MCP `return_rankings`, human review | Active snapshots gated by `OA203_ACTIVE_SNAPSHOTS_ENABLED=1` |
| `squeeze-scanner/v1/scheduled/*` | External repo `OwenTanzer/short-squeeze-scanner` | Squeeze tab, MCP | 09:00 + 12:00 NY slots |
| `history/QQQ/5/`, `history/QQQ/options_0dte/` | Cited writers (`backfill_history.py`, `backfill_options_history.py`) **not in this repo** | synthetic_days | — |
| `system/bot-membership-v1.json`, `paper-trades/requests/*` | Worker (paper-trading API) | Bot dashboard, Crassus | Via `PAPER_TRADES` R2 binding |
| KV `USERS`, `SESSIONS` | Worker (`/api/register`, `/api/login`) | Paper-trading API | 30-day sessions |

## The four worked traces

- **Live value** (QQQ trace): QQQ VWAP 658.6141 from `intraday/latest.json` → `#vwap-rvol`. Timestamp-matched (Nov 6, 16:14 ET), replayed with the repo's own `docs/shared.js`.
- **Historical value** (historical trace): 2026-09-18 settled chain → Historical heatmap, spot $716.92, Monthly. Bucketing replayed through the repo's own `getThresh`/`oiBucket` (`docs/shared.js`) — identical to the desktop viewer's verified numbers.
- **STEO signal** (STEO signal trace): EIA STEO vintage-over-vintage revision + OVX regime — fully wired (collector loop → R2 → Worker allowlist → `#steo-panel`), test-covered, replay-verified — **and now live: first publication observed Nov 7 ~21:45 PT** (release 2026-11, `prior_release: null`, `revisions: []`, OVX `no_data`; vintage log exists, single entry) **and panel render confirmed in the live browser 21:58 PT** (strip reads Brent $104.00, WTI $97.00, balance 0.47 Mbbl/d, OVX NO_DATA). Replayed with the module's own June/July example: −5.40 Mbbl/d balance revision for 2026-09. The headline revision value still needs a second, changed release (Dec STEO). The Worker route 403s on direct navigation (origin gate, by design).
- **Worker settlement sweep** (settlement sweep trace): cron `scheduled()` → `settleAllBots` → R2 bot index → KV `USERS` → `fetchSpotMarkForDate` → `settleAccount` (shared with authenticated `/api/settle`). Live roster verified (31 bots, index ready, all open bots flat); replay-verified with the repo's own functions — **but the spot-mark source is the stale archive**: `raw/qqq_chain_20261106.csv` and `raw/qqq_chain_20261030.csv` both 404, so no short expiring after 2026-09-18 can be priced. The dashboard path is unaffected (marks from the live snapshot). One fossil position on a closed account; the 12 daily-trading bots last traded Nov 4, runner state unconfirmed.

## The two hardest reconciliations

1. **The deployment contract no longer describes the running system.** `docs/DEPLOYMENTS.md` (Sep 3) lists collector, Crassus, a temporary probe, and a recovery utility. HEAD has four Railway services plus the Worker and an undeployed MCP; MOO-169 and OA-203 have no contract entry; OA-203 has no cron expression anywhere in the repo; the "Wait for CI" gate was reported disabled with no later evidence of a fix. Reconciling this needs a live Railway readback, not more code reading.
2. **The front end reads data whose producers are not in this repo.** The Squeeze tab (external scanner repo), the Historical tab (qqq-data-pipeline archives, index seven weeks stale), and `synthetic_days` (untracked backfill scripts) all depend on producers outside this tree. The display validates these artifacts meticulously — allowlisted keys, schedule-aware freshness, "unconfirmed" rather than invented labels — but the repo cannot say who produces them, on what schedule, or who to page when they go stale. The staleness now has a second victim beyond the Historical tab: the Worker settlement sweep prices expired shorts from the same archive, so no short expiring after 2026-09-18 can be priced (traced in Cycle 4).

## Deployment paths (detail)

- **Worker:** `.github/workflows/deploy.yml` runs on push to master touching `docs/**`, `worker.js`, `wrangler.toml`, `scripts/stamp-build.mjs`; runs `tests/build_stamp.test.mjs`, stamps the build, deploys via `cloudflare/wrangler-action@v3`. It is the only workflow that deploys anything.
- **Railway services:** deploy via Railway watch paths + (intended) "Wait for CI" gate. There is **no CI workflow for Railway deploys**. Watch paths are documented in `docs/DEPLOYMENTS.md` for the collector; the Tradier service's config lives only in Railway + `TRADIER_DEPLOYMENT.md`; OA-203's cron lives only in Railway.
- **Market MCP:** `.github/workflows/publish-market-mcp-pr133.yml` publishes an image for a **pinned commit** (`1eec17c1…`), not HEAD, and deploys no service. The service is empty and the OAuth/client/redirect setup is unverified.
