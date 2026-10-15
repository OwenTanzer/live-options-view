# Options View market MCP (first slice of #118)

Production follow-up status and Linux image evidence: [ROLLOUT.md](ROLLOUT.md).

Seven read-only tools expose existing producer artifacts. Node 22+; no package
install, provider token, R2 credentials, scanner invocation or trading imports.
The module remains in this repository because it consumes the existing calendar,
schedule semantics and producer contracts. Its transports and tests are separate
from the Worker, collector and Crassus deployments.

## Run and inspect

From the repository root:

```sh
node --test market_mcp/*.test.js
node market_mcp/smoke.js --out fixture-smoke.json
node market_mcp/smoke.js --public --session 2026-10-02 --out public-smoke.json
```

The first smoke command generates **synthetic** artifacts in a temporary directory,
launches the actual stdio server and writes complete MCP responses. Fixture
timestamps stay unchanged, so readings become stale against the real current clock.
The public smoke is explicit and uses only anonymous reads of the fixed public R2
origin from `worker.js`. It neither configures nor reads credentials.

For a stdio MCP client, use command `node` and arguments
`["<absolute repository path>/market_mcp/server.js", "--public"]`.
Alternatively use `--fixtures <artifact-root>` with files laid out by their public
artifact keys. No fallback from fixtures to network occurs. The client flow is:

1. `initialize` with protocol `2025-11-25`; send `notifications/initialized`.
2. `tools/list`; then `discover_sources({"session":"2026-10-02"})`.
3. `market_context({"type":"call","limit":3})`.
4. `squeeze_results({"limit":3})`; inspect both success and latest attempt/status.
5. `return_rankings({"session":"2026-10-02","view":"clean","limit":3})`.
6. `result_detail({"reference":"<detail_reference from one returned row>","limit":1})`.
7. For a return path, follow `pagination.next_offset` with the **same** reference.

For the active Banana candidate, call `discover_sources` to inspect
`options_active`, then `active_rankings({"session":"current","view":"clean","limit":3})`.
The response names the selected exchange session and provisional cutoff.
Follow a returned `detail_reference` through `result_detail` for its pinned
quote path. Use `view:"all"` or `view:"qualified_ask_bid_v1"` to change the
producer ranking; use `sweep:<number>` to revisit one retained generation.
Each sweep pointer selects a digest-named immutable revision. Rebuilding the
same source and policy after local loss yields identical revision bytes.
`freshness.cutoff` reports the completed sweep age; each displayed row also
reports the age of its ranked quote endpoint. An old endpoint makes the active
view stale or partial even when the cutoff itself is recent. The 600-second
ranked-quote threshold is proposed and awaits Owen's decision. Active source rows
must match their manifest sweep interval and cannot use future or seconds-unit
bid, ask, trade or spot timestamps. Published zero timestamps are accepted only
beside a missing bid, ask or last price, and are returned as null evidence.
When the final daily summary exists, the active tool returns the final
producer product. A missing current-day snapshot never selects yesterday.
This tool is an undeployed review candidate: the producer publication gate,
remote retention policy and live client verification remain outstanding.

The smoke client includes standard `_meta` and verifies text/structured results
agree. Stdout contains only newline-delimited JSON-RPC server messages.

## Local Streamable HTTP

```sh
node market_mcp/http.js --public
```

This listens at `http://127.0.0.1:8765/mcp`. Use POST with
`Content-Type: application/json`, `Accept: application/json, text/event-stream`
and, after initialization, `MCP-Protocol-Version: 2025-11-25`. The stateless
transport returns JSON for requests and HTTP 202 for notifications; GET/DELETE
return 405. No SSE stream or MCP session identifier is required. Evidence
references are retained in the one process and expire after fifteen minutes.
The three HTTP integration tests exercise initialization, discovery, query and
detail over an actual loopback socket.

This is a tested local transport, **not a deployed endpoint or verified Chej
connection**. `http.js` binds only loopback and rejects other Host/Origin
values. The production followup adds `production.js`, bounded HTTP and
authentication, a dedicated Node image recipe and a review-only deployment plan.
Its Linux image checks pass; live client/grant gates remain pending. See
[production followup and exact approval prerequisites](PRODUCTION.md) and
[readiness and deployment prerequisites](READINESS.md). The protocol follows the
[MCP stdio and Streamable HTTP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## Tools and evidence

| Tool | Existing products and supported scope |
| --- | --- |
| `discover_sources` | Stable IDs `qqq_snapshot`, `scheduled_squeeze`, `options_returns`, `spy_snapshot` (appended, so earlier positions are unchanged), plus an `underlyings` summary of chain coverage and availability; current parse/read availability, fields/units, filters, cadence, limits and unsupported capabilities. Optional explicit return session; no historical index. |
| `market_context` | Optional `underlying` (`QQQ` default, or `SPY`; #121). QQQ reads `intraday/latest.json` (dataset `qqq_snapshot`), SPY reads `intraday/spy/latest.json` (dataset `spy_snapshot`). Returns `requested_underlying` and `actual.underlying`, the underlying block and a chain page filtered by expiry/type/strike/OCC contract. The payload's symbol, `underlying_market.symbol` and every contract root must match the request or it fails as `symbol_mismatch`; an archive locator outside the requested underlying's layout fails as `incompatible_schema`. Unsupported symbols, a contract for the other underlying, or a missing SPY publication return explicit errors; QQQ is never substituted. Detail references keep the originating underlying and snapshot. |
| `squeeze_results` | Scheduled `latest.json`, `latest-attempt.json`, `latest-schedule.json`; uses the existing exported XNYS calendar and `formatSqueezeScheduleStatus`. `runs` compares last success and latest attempt (run ID, status, schedule/acquisition/publication times, eligible/excluded denominators, `attempt_relation`); `rows_basis` names the run the rows came from. `run=latest_attempt` reads that attempt's own manifest and `results.json` (digest-verified), pages every row (eligible by producer rank, then excluded) and reports `reconciliation` against its coverage counts. |
| `return_rankings` | One exact date's `summary.json` and bounded shortlist: legacy `leaderboard.csv` for all/clean, or `qualified_ask_bid_v1.csv` for `view: "qualified_ask_bid_v1"`. Preserves producer ranks, policy, reasons, underlying/type filters, and pagination. Older sessions report the qualified view as unavailable. |
| `active_rankings` | Current or explicit XNYS session's immutable, producer-built cutoff generation. All/clean/qualified views, bounded filters and explicit retained sweep selection. Provisional state, cutoff, cadence and newer attempts stay visible; a final daily result uses the final path. Requires producer publication, currently disabled. |
| `market_overview` | #132 first slice: the existing `intraday/prices.json` dashboard strip. Source identity distinguishes Yahoo JPY/USD spot from CME yen futures; unknown-origin carried yen is withheld. ETF proxies retain ETF identities. Selected-price basis and collector receipt time are labeled separately from unavailable provider observation time. Producer `chg_pct` is preserved where valid; unit change (`price - prev_close`) and 10Y bps after CBOE scale normalization are consumer-derived. The unresolved yen futures contract has no comparable change. Undated Yahoo fills and carried values are not live. Unsupported assets are named `gaps`, including under asset-class filters. Filters: `asset_class`, `symbols`, `sort`, `limit`, `offset`. |
| `result_detail` | Retained snapshot JSON row; exact squeeze run's manifest/results/normalized inputs; selected return contract from bounded sweep pages with producer calculation cells/flags. |

The qualified return view's provisional thresholds and evidence rules are in
[OA203_QUALIFIED_ASK_BID_V1.md](../OA203_QUALIFIED_ASK_BID_V1.md).
The qualified view and SPY collector/website are now on `master`; the collector
deployment succeeded. The draft SPY MCP work is based on `master` and has not
been merged or deployed. Fixture coverage here exercises both capabilities
together. A live SPY snapshot and client access to a deployed SPY MCP endpoint
have not been verified.

Every result carries dataset/envelope schema, requested filters, actual identity,
retrieval time, source locators/links and retrieved digests, supplied producer
times (missing ones are null), units, coverage/status/warnings/errors and
pagination. Availability and freshness are separate: a last successful squeeze
is available while its freshness is stale and a newer failed/partial attempt is
visible. Exchange closures and calendar expiry follow the existing display
contract; no inferred weekday scheduler is added.

Detail source entries carry `retained: true` for evidence saved by the originating
query and `retained: false` for evidence fetched for this detail request. Each
source retains its original retrieval_time; envelope retrieval_time is response
assembly time, not a new market observation or network fetch. Repeating detail
does not relabel or mutate originating-query evidence.

Snapshot detail retains the queried row/readings even after `latest.json` advances;
its full-JSON digest does not verify its archived CSV. Squeeze detail validates
the manifest digest against the queried pointer and the two **read** members
against that manifest; it does not claim full-archive verification. First-seen
metadata remains exactly producer supplied, including nulls and false values.

OA-203 ranks, flags and calculation inputs are preserved as producer CSV cells
(strings, with empty cells null). Ranking follows existing ordinal rank values;
there is no recalculation or re-scoring. Summary/leaderboard digests are checked
before/after rankings and compared again for detail. Detail also checks before/
after manifest digests. A detected change returns `source_changed`, not a mixed
path. This is observed-change detection, **not atomic revision/checksum assurance**:
the producer has no immutable session generation or per-sweep checksums in these
products. Session open/close are exchange boundaries, never acquisition times.

Returns remain sampled, midpoint/ask-entry-bid-exit/trade-bar bases remain
distinct, and no measure asserts achievable fills. Quote paths are paged by
manifest sweep rather than exporting all daily contracts. Missing/failed/empty
selected-chain pages and damaged/oversized members are explicit partial detail.
Raw trade-bar drilldown and the full `contracts.csv.gz` universe are deferred.

## Bounds and failures

| Bound | Value |
| --- | --- |
| Rows per page / offset | 1..50 / 0..10000 |
| Session | One real date, 2020..2100; no multi-date search/fallback |
| Artifact / expanded gzip | 4 MiB / 24 MiB |
| Total query bytes / source requests / deadline | 64 MiB / 12 / 15 seconds |
| Detail quote page | At most 6 sweeps; reduce limit if byte cap is reached |
| Serialized result | 256 KiB, before text/structured duplication |
| Input frame/body | 16 KiB |
| Retained references | 64 / 16 MiB serialized, fifteen-minute expiry, oldest evicted |
| Artifact parsing | Incremental JSONL; physical line/record/cell bounds in [PRODUCTION.md](PRODUCTION.md) |
| Stdio queued / HTTP active requests | 16 / 4 |
| HTTP body acquisition | 5 seconds |

Schemas disallow unknown filters. Validated symbols, dates and opaque references
cannot select account/admin/trade routes, private archives, URLs, bucket keys,
shells or arbitrary origins. Only hardcoded artifact families are read; redirects
are refused. Local artifacts use canonical containment checks to reject symlink
escapes. Retrieved strings are data; nothing evaluates or executes them.

Typed errors include `invalid_filter`, `invalid_reference`, `missing_artifact`,
`incompatible_schema`, `upstream_failure`, `upstream_timeout`, `rate_limited`,
`excessive_request`, `excessive_response`, `excessive_or_malformed_artifact` and
`source_changed`. A detail reference may expire or be evicted: repeat its bounded
originating query. No zero substitution or automatic alternate-session search.

## Producer boundaries

#107 owns shared-reading publication. Merged #116 preserves Crassus lineage;
it does not publish additional readings for this consumer. Shared PCR/max-pain/reference OI skew,
historical VWAP/RVOL, account trading momentum and buy-only Black-Scholes are
explicitly unsupported. OI lag/zero-versus-missing, approximate VWAP, missing
Greeks/quote observation times and historical/sampled coverage travel with results.

Source contracts: [inventory](../docs/plans/2026-09-market-reading-inventory.md),
[ownership](../docs/plans/2026-09-market-reading-ownership.md),
[squeeze display](../docs/plans/2026-09-scheduled-squeeze-display.md),
[squeeze archive](https://github.com/OwenTanzer/short-squeeze-scanner/blob/main/docs/archive-contract.md),
[OA-203 returns](../docs/plans/2026-09-options-return-scanner.md).


