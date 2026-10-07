# Options View market MCP (first slice of #118)

Production follow-up status and Linux image evidence: [ROLLOUT.md](ROLLOUT.md).

Five read-only tools expose existing producer artifacts. Node 22+; no package
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
launches the actual stdio server and writes seven complete MCP responses. Fixture
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
| `market_context` | Optional `underlying` (`QQQ` default, or `SPY`; #121). QQQ reads `intraday/latest.json` (dataset `qqq_snapshot`), SPY reads `intraday/spy/latest.json` (dataset `spy_snapshot`). Returns `requested_underlying` and `actual.underlying`, the underlying block and a chain page filtered by expiry/type/strike/OCC contract. The payload's symbol, `underlying_market.symbol`, every contract root and the archive locator must match the request, or it fails as `symbol_mismatch`. Unsupported symbols, a contract for the other underlying, or a missing SPY publication return explicit errors; QQQ is never substituted. Detail references keep the originating underlying and snapshot. |
| `squeeze_results` | Scheduled `latest.json`, `latest-attempt.json`, `latest-schedule.json`; uses the existing exported XNYS calendar and `formatSqueezeScheduleStatus`. |
| `return_rankings` | One exact date's `summary.json` and `leaderboard.csv`, all/clean, underlying/type, pagination. The published top-200-all OR top-200-clean subset is the query universe. |
| `result_detail` | Retained snapshot JSON row; exact squeeze run's manifest/results/normalized inputs; selected return contract from bounded sweep pages with producer calculation cells/flags. |

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
