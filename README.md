# live-options-view

> Written as part of the quest "Make the whole workspace readable" (Chej, Nov 7 2026). The full evidence lives in the [quest Log](https://app.notion.com/p/4123c519147b811dbd36da5d1bcd251b?pvs=204); this file is the front door, not the proof.

## What this is

A live QQQ (and SPY) 0DTE options workspace: a collector streams the options chain into cloud storage, a Cloudflare Worker serves it to a browser app at `options.moopertonic.net`, and a paper-trading bot, a Tradier archive collector, a return scanner, and a read-only MCP server hang off the same data. One repo, five deployable runtimes plus an undeployed sixth, sharing one R2 bucket (`qqq-options-chain-data`) and one Worker.

## The system in one picture

```
tastytrade DXLink ──> collector.py (Railway) ──> R2 qqq-options-chain-data
                                                              │
                         ┌───────────────────┬────────────────┼───────────────┐
                         │                   │                │               │
                 Worker /browser-data/  Crassus bot    OA-203 scanner  Tradier collector
                 + docs/index.html     (Railway)      (Railway)       (Railway, writes only)
                         │                   │                │
                   options.moopertonic.net  paper trades   R2 oa203/
                         │                   via Worker API
              Live tab · Historical tab · Squeeze tab · Bots dashboard
```

## Where did this number come from?

Four paths have been traced end to end (code + live fetches + local replay). All four traces are recorded in the [quest Log](https://app.notion.com/p/4123c519147b811dbd36da5d1bcd251b?pvs=204).

- **Live VWAP 658.61, RVOL 1.45×** (QQQ trace): `collector.py take_snapshot()` → `_compute_underlying_market()` → `market_signals.accumulate_vwap()` → R2 `intraday/latest.json` → Worker `/browser-data/intraday/latest.json` → `docs/index.html fetchChain('QQQ')` → `docs/shared.js formatVwapRvol()` → the `#vwap-rvol` header. The number is a spot-price × volume-delta approximation, not tick-accurate VWAP. On a Saturday evening the label reads "VWAP/RVOL: session closed" instead — the value is hidden once the reading is more than 4 hours old.
- **Historical heatmap for 2026-09-18** (historical trace): `manifest.json` (the date index) → Historical tab → Worker `/browser-data/raw/qqq_chain_20260918.csv` → `renderHistoryHeatmap()`, with `prices/qqq_intraday_20260918.csv` feeding the intraday/daily candle widgets. Spot $716.92, tier Monthly. The same file was verified against the desktop viewer: ATM 717 call OI 6,787 → bucket 4, and the repo's own bucketing code reproduces it exactly.
- **EIA STEO crude signal** (STEO signal trace): EIA STEO API → `collector.py eia_steo_loop()` (every 6 h, only if `EIA_API_KEY` is set) → `crude_calibration.parse_steo_rows()` + `compare_vintages()` + `classify_ovx_regime()` → R2 `macro/eia_steo.json` → Worker `/browser-data/macro/eia_steo.json` → `#steo-panel` (Brent/WTI/balance forecast + revision deltas + OVX regime). The signal math and the panel are fully wired and test-covered. **Update Nov 7, 2026 ~21:45 PT: the feed went live.** The first publication exists (`timestamp` 2026-11-08T05:45:25Z, release 2026-11) — it landed ~77 s after the redeploy, so the loop polls on startup, not only on its 6 h cadence. As predicted, the first write carries `prior_release: null` and `revisions: []` — the headline revision value appears only after a second, changed release (the Dec STEO, ~a month away). OVX is `no_data` (no OVX price in the strip at poll time). Replayed with the module's own June/July example: a −5.40 Mbbl/d balance revision for 2026-09. **Panel render CONFIRMED in the live browser (21:58–21:59 PT):** `#steo-panel` is visible on `options.moopertonic.net` as a full-width strip reading "EIA STEO 2026-11 — FORECAST 2026-11 — BRENT FCST $104.00 — WTI FCST $97.00 — WORLD BALANCE 0.47 Mbbl/d — OVX — NO_DATA", matching the publication exactly. The Worker route returns 403 on direct navigation, which is its origin gate working as designed — the in-page fetch is the path that matters, and it works. The published forecast values themselves carry an unexplained jump (2026-03 → 2026-10 Brent 103–117 vs 2026-01/02 history 66.6/70.89); that is in the EIA data as published, recorded in the quest Log.
- **Worker settlement sweep** (settlement sweep trace): Cloudflare cron `*/15 12-23 * * 1-5` → `worker.js scheduled()` → `settleAllBots()` → R2 `system/bot-membership-v1.json` (bot roster) → KV `USERS` `user:<name>` → `computeBookFromTrades()` → `fetchSpotMarkForDate()` → `settleAccount()` (shared with the authenticated `POST /api/settle`). Live: `/api/bots` returns 31 bots, index `ready: true`, every open bot flat. **But the sweep prices expired shorts from the same stale `raw/qqq_chain_*` archive as the Historical tab** — `raw/qqq_chain_20261106.csv` and `raw/qqq_chain_20261030.csv` both 404 — so no short expiring after 2026-09-18 can be priced (latent today, since no open bot holds a position). The dashboard's own `/api/settle` path is not affected: its spot marks come from the live snapshot. One fossil: closed bot `crassus_cheshire` still holds an expired long 4× QQQ 714 call (exp 2026-09-11) that the sweep will never settle, because it skips closed accounts.

The general rule: every number on the page comes from a named R2 key through a named Worker route through a named function in `docs/index.html` / `docs/shared.js`. The data-contract table in `ARCHITECTURE.md` lists each key, its writer, and its readers.

## What runs when nobody has the site open?

- **Collector** (Railway, `python collector.py`): continuous, session-gated 06:00–16:15 ET. Writes `intraday/latest.json` every 60 s, price strip every 10 s, health every 15 s, weekly snapshots hourly.
- **Crassus bot runner** (Railway, `python -m crassus.runner --interval 300`): a 5-minute observe → decide → validate → submit → reconcile loop, paper-trading through the Worker's API.
- **Worker settlement sweep** (Cloudflare cron, every 15 min 12:00–23:45 UTC Mon–Fri): settles/expires bot positions nobody is watching. It reads the bot roster from R2, each bot's record from KV, and the spot mark for each expiration from the settled daily chain archive — **which stops at 2026-09-18, so it currently cannot price any short expiring after that date** (longs still settle at 0). The dashboard's own settle path uses the live snapshot instead, so it is not affected.
- **Tradier collector** (Railway cron `15 12 * * 1-5` UTC): writes daily time-and-sale archives to `moo144/tradier/<date>/`. Nothing in this repo reads them back.
- **OA-203 scanner** (Railway, schedule managed in Railway — not in this repo): polls Tradier, writes `oa203/scanner/<date>/`.
- **EIA STEO loop** (Railway, every 6 h, only if `EIA_API_KEY` is set): polls the EIA STEO API and writes `macro/eia_steo.json` + the vintage log. **Live since Nov 7, 2026 ~21:45 PT** — the first publication (release 2026-11) landed ~77 s after the redeploy; the loop polls on startup, not only on its 6 h cadence. The panel shows the forecast; the revision deltas appear only after a second, changed release.

## What ships if I change this file?

| You change… | What deploys | How |
|---|---|---|
| `worker.js`, `docs/**`, `wrangler.toml` | Cloudflare Worker + static site | `.github/workflows/deploy.yml` on push to master (the only deploying workflow) |
| `collector.py`, `market_signals.py`, `crude_calibration.py` | Railway collector service | Railway watch paths; a "Wait for CI" gate was reported **disabled** in Sep 2026 with no later evidence of a fix — a merge may deploy without CI |
| `crassus/**` | Crassus runner | Railway watch paths |
| `scripts/oa203_*.py` | OA-203 scanner | Railway watch paths |
| `scripts/moo144_tradier_*.py`, `tradier_cron_launcher.py` | Tradier collector | Railway (no `railway.toml` for this service exists in the repo) |
| `market_mcp/**` | Nothing — not deployed | Image publish is a separate manual workflow (`.github/workflows/publish-market-mcp-pr133.yml`, pinned commit) |

## Run and test it locally

- Collector: `python collector.py` (needs tastytrade + R2 credentials — do not point it at production). Tests: `tests/stage2_verification.py` + the `.github/workflows/collector-ci.yml` suites.
- Worker + site: `npx wrangler dev` (or the `.github/workflows/web-ci.yml` Node suites; `node tests/historical_archive.test.js` exercises the Worker + Historical panel against mocked R2 and passes at HEAD).
- Historical trace replay: recorded in the quest Log (reproduces the 2026-09-18 buckets from the captured archive excerpts).
- Crassus: `python -m crassus.runner --once --dry-run` from `crassus/`.
- OA-203: `python scripts/oa203_return_scanner.py smoke --symbols SPY,QQQ --sweeps 2 --dir /tmp/oa203`.

## What this README does not claim (honest gaps)

- **The Historical archive is seven weeks stale.** The date index in `manifest.json` ends at 2026-09-18; the live view shows Nov 6. The tab does not warn about this. The writers of the daily chain/intraday archives, the `dates` index, and `derived/OIranges.csv` are not in this repo — the repo itself names `qqq-data-pipeline` as the producer (see `docs/index.html:2697`, `collector.py:111`). Why the index stops at Sep 18 is unknown.
- **The Tradier lane is write-only.** Daily archives accumulate in R2 and nothing reads them. Either wire a consumer or record it as intentionally archive-only.
- **The settlement sweep cannot price recent expirations.** It reads spot marks from the same stale `raw/qqq_chain_*` archive as the Historical tab; `raw/qqq_chain_20261106.csv` and `raw/qqq_chain_20261030.csv` both 404. An expired short after 2026-09-18 stays pending forever, silently. A closed bot (`crassus_cheshire`) still holds an expired long 4× QQQ 714 call from 2026-09-11 that the sweep will never touch. And the 12 daily-trading bots last traded Nov 4 — whether the Crassus runner is still alive is unconfirmed and needs a Railway readback.
- **The STEO panel is live but young.** The EIA STEO feed published for the first time Nov 7, 2026 ~21:45 PT (release 2026-11), and the panel renders on the live site with the published values (Brent $104.00, WTI $97.00, balance 0.47 Mbbl/d, OVX NO_DATA — confirmed in the live browser 21:58 PT). The first write carries no revisions — the headline value appears only after a second, changed release (the Dec STEO, ~a month away) — and OVX reads `no_data`. The Worker route returns 403 on direct navigation (its origin gate, by design). The published forecast values carry an unexplained jump (2026-03 → 2026-10 Brent 103–117 vs 2026-01/02 history 66.6/70.89) that is in the EIA data as published.
- **The deployment contract is stale.** `docs/DEPLOYMENTS.md` (Sep 3) does not list the MOO-169 or OA-203 services, and the OA-203 cron expression is nowhere in the repo. A live Railway readback is needed before any deploy claim can be verified.
- **The squeeze producer is external.** The Squeeze tab reads `squeeze-scanner/v1/scheduled/*` from the `OwenTanzer/short-squeeze-scanner` repo.
- **Crassus's paper-trading API was 404ing** at the time its README was written; whether the live Worker still does is unverified from the repo.
- Every "runs / is deployed" claim above is **what code configures**, unless a trace says otherwise. See the [quest Log](https://app.notion.com/p/4123c519147b811dbd36da5d1bcd251b?pvs=204) for the full gap list and the open verification items.

## Review-readiness checklist (Cycle 5, Nov 7 2026)

For Owen's Nov 8 review — what this draft can and cannot yet answer:

- [x] **Where did this number come from?** Four paths traced end to end with code + live fetches + local replay (live VWAP, historical heatmap, STEO signal, settlement sweep), plus the Tradier lane traced to its loss point.
- [x] **What runs when nobody has the site open?** Every runtime/job rowed with trigger, inputs, outputs, consumers, run/test (workspace map §2 in the [quest Log](https://app.notion.com/p/4123c519147b811dbd36da5d1bcd251b?pvs=204)); the scheduled jobs are named in the README above.
- [x] **What ships if I change this file?** Deploy table above, per area, with the one deploying workflow and the Railway watch-path caveat.
- [x] **Honest gaps:** every known break is listed above with its evidence, not smoothed over — stale archive, write-only Tradier lane, sweep spot marks, fossil position, runner quiet, stale deployment contract, external squeeze producer, unverified Crassus P0.
- [x] **Panel-render verification for the STEO panel** — the publication exists, and `#steo-panel` renders on the live site with the published values (confirmed in the live browser, Cycle 5, 21:58 PT).
- [ ] **Owen's verification items** (quest Log, §10): live Railway readback, R2 Tradier listing, Tradier decision, IBIT tests, archive producers, Crassus P0 probe, runner state Nov 5–6. None is answerable from the repo; the drafts record them rather than guessing.
- [ ] **Q1 tripwire work** (redesign queue focus): the manifest-freshness tripwire and the README record of the archive producer + schedule are still undone — issue #148 is filed, work not yet done.
