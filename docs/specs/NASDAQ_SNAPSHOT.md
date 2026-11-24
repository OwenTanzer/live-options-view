# NASDAQ-100 snapshot contract

Producer: `market_data/nasdaq_collector/` (new in the NASDAQ-100 panel PR,
part 1: collector + Market MCP + docs + tests). Consumer: the read-only
Market MCP `nasdaq_snapshot` tool (and, in part 2, the site NASDAQ-100
panel — not this document's scope). This specification is repository
documentation, not a served asset.

## Artifacts

| Object key (R2 bucket `qqq-options-chain-data`) | Contents |
|---|---|
| `nasdaq/latest.json` | The snapshot (below), `schema_version: 1`, `dataset: "nasdaq_snapshot"` |
| `nasdaq/latest.provenance.json` | `snapshot_provenance.v1` sidecar (below) |

Publication is hourly and opt-in (`--publish`). No artifact is published
yet; the collector is not deployed or scheduled, and no R2 object exists
until a maintainer deploys it and the first `--publish` run succeeds. The
MCP tool reads whatever the publisher writes; it never publishes, starts a
collector, or deploys.

## Sources (public only, no account or auth)

- **Prices, day range:** Nasdaq official quote API,
  `https://api.nasdaq.com/api/quote/<SYMBOL>/info?assetclass=stocks`
  (NDX with `assetclass=index`), browser-style User-Agent header. One
  request per listing plus NDX per run. Each quote carries a last-trade
  timestamp (`primaryData.lastTradeTimestamp`, e.g. "Nov 23, 2026 6:38 PM
  ET"); day range comes from `keyStats.dayrange`. Market cap is not in
  the quote response — it comes from one `list-type/nasdaq100` request
  per run (same official source as the universe). `secondaryData` is the
  regular-session close; `primaryData` is the latest (after-hours) quote,
  which is what the per-stock price/percent report.
- **Candles:** Yahoo Finance chart API,
  `https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>`,
  real 1-minute bars (`interval=1m`, `range=5d`) and daily bars
  (`interval=1d`, `range=10d`).
- **News:** Google News RSS per ticker,
  `https://news.google.com/rss/search?q=<SYMBOL>+stock&hl=en-US&gl=US&ceid=US:en`,
  re-fetched fresh on every collector run (hourly, and on demand). Only
  headlines published within the last 12 hours are stored or published —
  older or undated headlines are excluded. The 12-hour publication window
  is enforced at fetch time, in storage (stored headlines are pruned), and
  at read time in `build_snapshot`, so nothing older is ever shown.

## Universe

The 101 listings (100 companies) are embedded in
`market_data/nasdaq_collector/symbols.py`, taken from Nasdaq's official
`list-type/nasdaq100` API on 2026-11-23 (`totalrecords: 101`, API date
"Nov 23, 2026"). Alphabet Inc. appears twice — GOOGL (Class A) and GOOG
(Class C) — hence 101 listings for 100 companies. No other constituent has
two listings. Re-verify against the same endpoint after each annual
reconstitution (December) or announced change; `symbols.validate()` fails
closed on a wrong count or a duplicate symbol.

### Dynamic membership

The embedded list is the last-known universe, not a fixed one. On every
collector run, `collector.resolve_universe()` re-fetches the live
constituent list from the same official `list-type/nasdaq100` endpoint
(the same single request also supplies the market caps), diffs it against
the embedded list (`symbols.diff_universe()`), and collects the live
members: a listing that entered the index gets full treatment (quote,
market cap, news, candles on demand) and a listing that left it gets no
live row. The diff is recorded in the snapshot's `index_changes` section
(`as_of` run date, `added`/`removed` with symbol and name, `source`,
`fallback`), and carried into the provenance sidecar, so a leaver's
identity is preserved even though it is no longer published as a live
row. The site panel shows the changes under the controls, and the
heatmap and the table both render the live rows, so both follow
membership changes on every refresh.

A failed, empty or garbled list fetch falls back to the embedded
universe (`membership_source: "embedded_fallback"`, `index_changes`
with `fallback: true` and no recorded changes) — a bad fetch never
empties the snapshot. After a recorded change, update the embedded list
in `symbols.py` (and `SOURCE_AS_OF`) so the next run diffs against the
new baseline; `python -m market_data.nasdaq_collector universe --live`
prints the live list and the diff for that. `collect --symbols` accepts
a ticker that is live but not yet in the embedded list (checked against
one live fetch); other unknown tickers are still rejected.

## Snapshot shape (`nasdaq/latest.json`)

```json
{
  "schema_version": 1,
  "dataset": "nasdaq_snapshot",
  "as_of": "2026-11-23T15:00:00+00:00",
  "generated_at": "2026-11-23T15:00:00+00:00",
  "universe": {"source": "Nasdaq official list-type/nasdaq100 API",
               "source_url": "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
               "as_of": "2026-11-23", "listings": 101, "companies": 100,
               "membership_source": "live"},
  "index_changes": {"as_of": "2026-11-23",
                    "added": [{"symbol": "NEW1", "name": "New Entrant Inc."}],
                    "removed": [{"symbol": "OLD1", "name": "Old Leaver Inc."}],
                    "source": "live", "fallback": false},
  "index": {"symbol": "NDX", "status": "available", "level": 27000.0,
            "change": 200.0, "percent_change": 0.8,
            "percent_change_basis": "regular-session move vs previous close",
            "previous_close": 26800.0, "day_high": 27100.0, "day_low": 26900.0,
            "market_status": {"status": "open", "reason": "in_session"},
            "price_basis": "nasdaq_last_sale", "provider_observed_at": null,
            "freshness": {"status": "fresh", "...": "..."},
            "breadth": {"advancers": 51, "decliners": 50, "unchanged": 0},
            "top_gainers": [{"symbol": "AAPL", "percent_change": 2.0}],
            "top_losers": [{"symbol": "MSFT", "percent_change": -0.5}]},
  "stocks": [{"symbol": "AAPL", "name": "Apple Inc. Common Stock",
              "price": 350.0, "change": 1.0, "percent_change": 2.0,
              "percent_change_basis": "Nasdaq latest reported move (after-hours move after the close)",
              "market_cap": 5000000000000.0, "rank": 1,
              "day_high": 351.0, "day_low": 349.0, "volume": 1000000,
              "change_1h_pct": 0.1, "change_7d_pct": 2.0,
              "news": [{"title": "...", "source": "Reuters",
                        "link": "https://...", "published_at": "2026-11-23T12:00:00+00:00"}],
              "status": "available", "price_basis": "nasdaq_last_sale",
              "provider_observed_at": null,
              "freshness": {"status": "fresh", "...": "..."}}],
  "coverage": {"listings": 101, "available": 101, "source_unavailable": 0,
               "with_news": 0, "with_candles": 0},
  "sources": {"prices": "Nasdaq official quote API (api.nasdaq.com)",
              "candles": "Yahoo Finance chart API (query1.finance.yahoo.com)",
              "news": "Google News RSS (news.google.com)",
              "index": "Nasdaq official quote API (NDX)"},
  "session": {"model": "US equity session, weekdays 09:30-16:00 America/New_York",
              "holiday_calendar": "NYSE holidays, 2026 only; other years not asserted",
              "status": {"status": "open", "reason": "in_session"}}
}
```

### The two percentage bases (do not conflate)

- Per-stock `percent_change`: **Nasdaq's latest reported move (after-hours
  move after the close)**. Labelled on every stock row as
  `percent_change_basis`.
- Index `percent_change`: **regular-session move vs previous close**.
  Labelled on the index block.

The two bases are different measurements. The MCP consumer validates that
they differ (`nasdaqSnapshot` fails closed if they are equal) and surfaces
both labels verbatim.

### Stale / source-unavailable state

- A stock whose quote fetch fails is `status: "source_unavailable"` with
  null `price`, `change`, `percent_change`, `market_cap`, `rank`,
  `day_high`, `day_low` and `volume`. A price is never substituted, carried
  forward, or invented.
- A listing that left the index has no live row at all; its symbol and
  name are preserved in `index_changes.removed` (and in the provenance
  sidecar's `index_changes`), never silently dropped.
- A failed NDX fetch leaves the index block `status:
  "source_unavailable"` with null level/change/percent; stock rows still
  stand on their own and breadth is still computed from the stocks.
- Nasdaq's quote endpoint publishes a per-quote last-trade timestamp
  (`primaryData.lastTradeTimestamp`), so `provider_observed_at` carries
  it when present and `freshness` is measured from it; when only a date
  is published (as for NDX) the timestamp is null and freshness falls
  back to collector receipt. That basis is stated on every row, never
  implied.
- News headlines are evidence only; they do not establish a cause for any
  price move.

## Session model

US equity sessions, weekdays 09:30–16:00 America/New_York.
`market_status` is `open` only inside those hours on a non-holiday
weekday; otherwise `closed` with a reason: `weekend`, `holiday_2026`,
`outside_hours`. The embedded holiday calendar covers **2026 only** (NYSE
holidays; Jul 3 is the observed Independence Day closure). For other years
holidays are not asserted and an in-hours weekday reports `open` with
reason `outside_hours_holiday_calendar_unverified`.

## Candles

The tracker exposes 1m/3m/5m/9m/15m/30m/1h/2h/4h/1d/1w intervals per
stock. The non-standard intervals (3m, 9m, 2h, 4h) are aggregated from
real 1-minute Yahoo bars on America/New_York time, bucketed from the
09:30 ET regular-session open (so a 9m bucket always covers 09:30–09:39,
09:39–09:48, …). Only regular-session bars are aggregated; pre/post-market
bars are dropped, never folded in. `change_1h_pct` is the last 1m close vs
the close ~60 minutes earlier (null when under an hour of bars exists);
`change_7d_pct` is the last daily close vs the close 5 trading sessions
back (null when fewer than two daily closes exist).

**Candle bars are NOT published in `nasdaq/latest.json`** — 101 symbols ×
11 intervals of bars would make the hourly snapshot megabytes. The site
panel (`site/js/nasdaq-panel.js`) fetches candles on demand, per selected
stock, from Yahoo Finance's public chart API (the same source the tracker
uses: 1m bars `range=5d` for intraday intervals, daily bars `range=1y`
for 1d, weekly bars `range=5y` for 1w), aggregates the non-standard
intervals client-side with the same session-day bucketing, and caches
per symbol+interval. A failed fetch is an explicit "Candles unavailable"
state in the panel, never a substituted chart. The collector keeps
fetching 1m/daily bars only to compute `change_1h_pct` / `change_7d_pct`.

## Panel views

The site panel (`site/js/nasdaq-panel.js`) renders the snapshot's live
rows in two views, switched by a Heatmap/Table toggle in the controls;
the Heatmap is the default view on open. Both views render from the same
filtered snapshot rows, so a search filters both and a refresh updates
both. Heatmap tiles are a squarified treemap sized by market cap with
square-root scaling and a minimum-size floor (0.5% of the total area per
tile, so the smallest listings stay readable and tappable), coloured
green/red by daily % change with intensity by magnitude (full at
+/-3%), each tile labelled ticker + % change, with a -3%/+3% gradient
legend. Tapping a tile opens the same stock detail view (candle chart +
news) as tapping a table row. The panel shows the snapshot's
`index_changes` under the controls.

## Provenance sidecar (`nasdaq/latest.provenance.json`)

`snapshot_provenance.v1`, mirroring the live-market collector's sidecar:

- `constructed_at`, `archive_uploaded_at` (null — no archive is written),
  `latest_published_at` (null until a `--publish` run succeeds).
- `publication.status`: `published`, or `not_published` with a reason
  (e.g. `"local run: --publish not given"`).
- `source` label, `as_of`, `universe`, `index` block and per-stock
  `price_basis`, `provider_observed_at`, `freshness`, `status`.
- `index_changes` as in the snapshot (added/removed listings, run date,
  source, fallback), so a leaver's identity survives in provenance even
  though it has no live row.
- `coverage` counts as in the snapshot.

The snapshot is uploaded first, then the sidecar; a sidecar failure is
raised, not swallowed. A missing sidecar means unknown provenance.

## R2 publication (unconfirmed against live config)

Opt-in only (`--publish`). Environment:

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `NASDAQ_R2_BUCKET` — bucket holding the `nasdaq/` prefix

These names are the collector's expectation, **not a verified live
deployment**: they have not been confirmed against live service config,
and no R2 artifact has been published. A maintainer must confirm the
bucket/endpoint and the Railway (or other) schedule before the first
publish.

## MCP contract (`nasdaq_snapshot` tool)

Read-only. Reads `nasdaq/latest.json` through the allowlisted source
(`market_mcp/source.js`), validated by `nasdaqSnapshot` in
`market_mcp/consumer.js`: `schema_version === 1`,
`dataset === "nasdaq_snapshot"`, `universe.listings` a positive integer
(the live listing count — not hard-coded 101, since membership is
dynamic), at most `universe.listings` stock rows, `index.symbol === "NDX"`, and per-stock vs index
`percent_change_basis` labelled differently. Same envelope/freshness shape
as `market_context`: `freshness` from `as_of` with
`stale_after_seconds: 3600` and basis `collector receipt; hourly cadence`;
`producer_times` carries `acquisition_start: as_of` and nulls elsewhere;
`coverage` reports listings/available/source-unavailable/with-news and
index breadth; `status` is `partial` when any stock or the index is
source-unavailable, `available`/`empty` otherwise. Filters: `symbols`
(1–50 tickers; outside-universe symbols are reported as
`not_in_nasdaq_100`, never substituted), `sort`
(`strip`/`percent_desc`/`percent_asc`), `limit`/`offset`. Row
`detail_reference` values resolve through `result_detail` against the
retained queried snapshot. `discover_sources` lists `nasdaq_snapshot`
appended after `cross_market_overview`, so earlier capability positions
are unchanged. `market_context`'s QQQ/SPY-only contract is unchanged.

## Tests

- `tests/test_nasdaq_collector.py` (pytest, offline): universe count,
  snapshot schema, provenance shape, candle aggregation incl. 9m bucketing
  from 1-minute bars, the two `%` labels, news parsing / the 12-hour
  publication window, session
  model, R2 key order with a fake client, and dynamic membership (live
  list parsing incl. empty/garbled rejection, the added/removed diff,
  the embedded fallback on list failure or an empty response,
  `build_snapshot` with a changed universe and `index_changes`, and an
  end-to-end `collect()` run with a fake session).
- `market_mcp/nasdaq_snapshot.test.js` (node:test, offline fixtures):
  discovery, query, filters/sort, retained detail, missing vs failed,
  hourly staleness, tools/list count 9, and a changed listing count
  (100 listings) validating while rows over the declared count fail.
- `tests/nasdaq_panel.test.js` (node:test, offline fixtures): intervals,
  sort/search, % labelling, stale/absent/failed states, candles,
  escaping, the heatmap (colour scale, minimum-share floor, squarified
  layout tiling, tile labels and legend, default view, toggle, tile tap
  -> detail), index-changes rendering, and site wiring.

## CI wiring (maintainer)

The GitHub App token cannot create or update workflow files (403,
`workflows` permission missing — same blocker as the Crypto Panel PR
#389). A maintainer with workflow access must add the following steps;
they are **not** in this PR.

1. In `.github/workflows/collector-ci.yml`, add a step that runs the new
   collector tests (same Python job as the existing collector tests):

   ```yaml
   - name: NASDAQ-100 collector tests
     run: python -m pytest tests/test_nasdaq_collector.py -q
   ```

   (If the job installs test dependencies, `pytest` must be among them;
   the collector itself needs only `requests`, already in
   `deployment/python/requirements.txt`.)

2. In `.github/workflows/market-mcp-ci.yml`, extend the MCP test command
   so the new test file runs with the existing suite (it is picked up by
   the existing `node --test market_mcp/*.test.js` glob; no new command
   is needed if that glob is what the workflow runs — verify and add an
   explicit `node --test market_mcp/nasdaq_snapshot.test.js` step only if
   the workflow enumerates test files).

3. Also update the one-number/one-line assertions that a ninth tool and
   an eighth discovery capability invalidate (these live outside this
   PR's file scope and currently fail on this branch — see the PR body):
   `market_mcp/http.test.js:10` and `market_mcp/production.test.js:115`
   (`tools.length` 8 → 9), `market_mcp/window.test.js:322`
   (`tools.length` 8 → 9), and the exact capability-list assertions in
   `market_mcp/overview.test.js:243`, `market_mcp/spy.test.js:37`,
   `market_mcp/window.test.js:307` (append `'nasdaq_snapshot'` after
   `'cross_market_overview'`). `market_mcp/smoke.js:20` asserts the same
   tool count (8 → 9) for the fixture smoke run.

