# Options View

Options View connects a live QQQ/SPY options chain, historical QQQ heatmaps, a scheduled squeeze scanner and paper-trading accounts. The browser is one reader of a larger system: collectors publish market artifacts, separate Cloudflare Workers serve the website and account API, and a read-only Model Context Protocol (MCP) server makes selected artifacts queryable by an assistant.

Open [the website](https://options.moopertonic.net), then use the [system map](ARCHITECTURE.md) to locate a component or follow the [walkthrough](docs/WALKTHROUGH.md) from a displayed value to its source. The [documentation index](docs/README.md) separates current navigation, specifications, operations and historical design records.

Agents working in this repository must read [AGENTS.md](AGENTS.md) for release boundaries, documentation routes and the no-KV rule.

## The shortest route through the repository

| Question | Start here | What it owns |
|---|---|---|
| Where is the page? | [site/index.html](site/index.html), [site/js](site/js) | Page shell and browser features; the source directory is not a public URL prefix |
| Where do prices and chain snapshots come from? | [market_data/live_market/](market_data/live_market) → [collector guide](market_data/live_market/README.md) | tastytrade/DXLink acquisition, snapshot-derived readings and optional EIA forecasts |
| What serves data and accounts? | [Backend guide](backend/README.md), [release procedure](docs/SITE_RELEASES.md) | Asset gateway plus separate API for archive access, live quotes, accounts and settlement |
| What decides paper trades? | [crassus/README.md](crassus/README.md) | Strategy runner; consumes market snapshots and calls the Worker account API |
| What produces Big Banana rankings? | [market_data/options_scanner/__main__.py](market_data/options_scanner/__main__.py), [scanner contract](site/plans/2026-09-options-return-scanner.md) | Tradier chain sampling, stored sweeps and retrospective return products |
| What stores Tradier time-and-sales? | [launcher](market_data/tradier/launcher.py), [market_data/tradier/OPERATIONS.md](market_data/tradier/OPERATIONS.md) | Separate daily archive collector; it does not feed the displayed chain |
| How does an assistant read it? | [market_mcp/README.md](market_mcp/README.md) | Bounded read-only tools over producer artifacts, not an alternate ranking engine |
| Where is strategy backtesting? | [Synthetic days](research/synthetic_days/README.md) | Offline replay against the current Crassus strategies |

Each service owns its implementation and operating guide. The three market-data producers share [Python image packaging](deployment/python/README.md); their launch paths and schedules are documented with each component. [Site releases](docs/SITE_RELEASES.md) use staged `wrangler.assets.toml`; separate API releases use `wrangler.api.toml`. Root `wrangler.toml` is retained only for combined restoration.

## Folder boundaries

- [market_data/](market_data/README.md): live acquisition, daily archives and option scans.
- [site/](site), [backend/](backend/README.md), [crassus/](crassus/README.md) and [market_mcp/](market_mcp/README.md): distinct browser, account/API, strategy and assistant-reader components.
- [research/](research/README.md): offline backtesting and links to completed studies.
- [docs/](docs/README.md), [deployment/](deployment/python/README.md), [scripts/](scripts), [tests/](tests) and [.github/](.github): documentation, shared packaging, repository-wide tools, cross-component tests and automation.

A new feature belongs under its owning component. Create a top-level folder only
for a durable system boundary or a repository-wide responsibility.

## Where does a displayed value come from?

For Live VWAP, [market_data/live_market/market.py](market_data/live_market/market.py) accumulates snapshot-weighted price × volume changes with [market_data/live_market/market_signals.py](market_data/live_market/market_signals.py), publishes `underlying_market` in `intraday/latest.json` (QQQ) or `intraday/spy/latest.json`, and the browser's [fetch cycle](site/js/data-fetch-cycle.js) passes it to [formatVwapRvol](site/shared/format.js). It is an approximation, not trade-by-trade VWAP. The snapshot carries partial-session metadata; the browser labels insufficient RVOL history, stale input and session-closed states, but does not itself surface every partial-session flag. The MCP consumer preserves that additional caveat.

Do not confuse that snapshot path with live bid/ask: visible contracts and open positions are refreshed through `/api/live-quotes` from the collector's in-memory feed, without an R2 write. The browser uses a same-origin `/browser-data/` archive proxy only on the configured `live-options-view.otmooper12.workers.dev` origin; on other origins, including the custom domain above, its archive base is the fixed public R2 origin. [The map](ARCHITECTURE.md#browser-and-data-boundaries) records that distinction.

Historical reads a different product: `manifest.json.dates` and daily `raw/qqq_chain_YYYYMMDD.csv` archives, whose producer is outside this repository. A recent manifest `updated_at` does not make its date index current. The Historical tab now warns when the newest date is more than five weekdays old; it does not repair missing archives. See [producer and freshness](site/historical-archive.md).

## What runs without an open browser?

The Railway collector runs its session lifecycle, snapshot/price/health loops and optional EIA loop independently of browser traffic. Crassus has its own five-minute strategy loop. Cloudflare invokes the Worker's settlement sweep every fifteen minutes during `12–23` UTC on weekdays. Two other Railway services start daily Tradier capture and OA-203 scanning on service-managed UTC cron schedules. The browser only polls and renders their outputs; closing the page does not stop those producers.

[Architecture](ARCHITECTURE.md#runtime-map) gives entry commands, trigger ownership and the date of available runtime evidence. Configuration, a successful deployment and a fresh market observation are different claims. In particular, an external archive schedule is still unconfirmed; Tradier's daily time-and-sales has no consumer in this repo; and a policy or test for active rankings does not establish a live in-session result. Settlement-source and closed-account policy changes are outside this documentation work.

## Safe local orientation

Start with offline tests, not a production collector or a trading command. From the repository root, these checks use fixtures:

```sh
node --test tests/build_stamp.test.mjs
node tests/historical_panel.test.js
node --test market_mcp/*.test.js
node market_mcp/smoke.js --out fixture-smoke.json
```

The [Web CI](.github/workflows/web-ci.yml), [Collector suite](.github/workflows/collector-ci.yml) and [MCP consumer workflow](.github/workflows/market-mcp-ci.yml) are the executable test inventories. A local test pass is not a deployment receipt. `node scripts/preview-local.mjs` starts the disposable fixture preview; see [local preview](docs/SITE_RELEASES.md#local-preview-and-artifact-inspection). Do not run a credentialed collector, scanner or Crassus runner merely to read this guide.

Cloudflare releases require explicit dispatch and production approval for the exact checked revision; merging alone publishes nothing. Use the [site-release procedure](docs/SITE_RELEASES.md), not bare `wrangler deploy`. Railway settings remain service-specific; prose inside a producer watch scope may still rebuild that producer. Review affected services before merging.
