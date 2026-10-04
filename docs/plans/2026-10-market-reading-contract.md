# Shared market readings and snapshot lineage v1

Issue #107, work item 3 and the bounded Crassus provenance part of item 4.
Authority: [inventory](2026-09-market-reading-inventory.md) and
[ownership decisions](2026-09-market-reading-ownership.md), merged as #108/#110.
This defines the future shared-reading envelope; this PR implements only the
snapshot-lineage extension below. No shared-reading publisher is added.

## Shared-reading record contract

`market_reading.v1` is one reading for one input snapshot and one explicitly
identified calculation. Fields below are required in that future envelope;
unknown values are null with a reason, never guessed. Times are ISO 8601 with
an explicit UTC offset. Consumers tolerate additive fields; a semantic change
to an existing field requires a new schema version.

| Field | Meaning |
|---|---|
| `schema_version` | `market_reading.v1` |
| `reading_id`, `symbol` | Named measurement (e.g. `reference_oi_skew`) and underlying |
| `observed_at` | Relevant source observation time, null when not available; never substitute ingestion/publication time |
| `input_observed_at` | Per-input time map (spot, volume, Greeks, quote, anchor as applicable), preserving asynchronous inputs and explicit nulls |
| `calculated_at` | Time the calculation was made; replay time for a retrospective calculation |
| `published_at` | Time this reading was successfully published, null when unpublished or unknown |
| `publication` | `{status, reason}`: `published`, `archived_unpublished`, or `unknown`; concerns the input board's publication to latest.json, not merely CSV existence |
| `source` | Producer/feed identifiers and source namespace (archive bucket/public origin), excluding credentials |
| `input` | `{snapshot_key, snapshot_key_status, snapshot_timestamp, payload_url, payload_sha256}`; explicit nulls for unavailable data |
| `calculation` | `{version, parameters}`; version identifies formula/conventions, parameters contain actual values used, including effective collector configuration |
| `interpretation` | `{kind, account_alias}`; kind `reference` or `account_configured`; account null for a reference reading |
| `values`, `units` | Matching named value/unit maps; unavailable numeric values null, not zero/NaN/infinity |
| `status` | `{code, reason}`; retain the reading's existing status vocabulary (e.g. `no_data`, `insufficient_history`, `warming_up`, `stale_anchor`, `ok`); undefined PCR explicitly `undefined` with reason `zero_call_oi` |
| `freshness` | `{status, evaluated_at, parameters}`; `live`, `stale`, or `unknown`, using that reading's existing freshness definition; no universal age threshold introduced |
| `coverage` | `{partial, reason, details}`; partial true/false/null, with existing sample counts, band/strike coverage, partial-session flag and start time as applicable |
| `caveats` | Named limitations, including `oi_zero_or_missing_indistinguishable` and `oi_prior_day_settled` for OI-derived values |

The input identity is scoped by source namespace plus the exact supplied key.
`valid` means structurally valid current collector key, not verified object
existence or content integrity. A future archive reader must report failed
retrieval/checksum verification separately; it must not fall back to nearest
time. `payload_sha256` means SHA-256 of the complete fetched JSON bytes, with
whitespace/ordering included. It is neither a canonicalized-JSON hash, a row
hash, nor a CSV checksum. A CSV checksum, if later measured, needs its own
explicitly named field. CSVs cannot reconstruct the full underlying_market
block or the original payload bytes. This PR does not solve that history gap.

### Preserve the accepted measurements

| Reading | Parameters/conventions and units retained |
|---|---|
| VWAP | Existing snapshot-weighted approximation; USD/share, volume in shares; preserve spot/vwap/volume timestamps and partial-session coverage |
| RVOL | 5-minute buckets, default 20 calendar-day lookback and minimum 5 samples; record effective settings and days actually used; dimensionless multiple |
| Reference momentum | 60-minute lookback, 10-minute maximum anchor overshoot; percent return (0.05 means 0.05%, not 5%); 0.05% neutral band is display-only |
| Trading momentum | Account-configured lookback/thresholds and runner history; bot-local, never replaced by collector reference momentum |
| PCR | Existing `compute_pcr` over board rows; put/call OI totals in contracts, nonzero-OI row count, dimensionless ratio; zero call OI undefined; trailing z-score/baseline remain bot-local |
| Max pain | Existing `_compute_max_pain`, all board candidate strikes, lower strike wins ties; USD strike and count of strikes with OI on both sides; account coverage/pin thresholds remain bot-local |
| Reference OI skew | Explicitly labeled **2% reference band**, OI totals in contracts, dimensionless imbalance, band strike count; independent of account session trackers; account bands/drift rules stay bot-local |
| Snapshot IV / Black-Scholes | Existing option-buy-only annotation reuses observed execution quote; no standing series until Greeks timestamps and provider valuation/time-to-expiry conventions are adequate |

Open interest (OI) zeros currently conflate missing feed observations with real
zeros. No coverage count resolves that ambiguity; preserve the caveat even when
the numerical calculation succeeds. OI is lagged, not proof of new intraday
positioning. Black-Scholes must preserve the asynchronous snapshot IV, spot and
execution quote limitation and dxFeed's near-close fixed 30-minute convention;
this contract creates no new quote selection or measurement policy.

An archived CSV can exist without publication (zero bid coverage or publication
failure). Archive presence alone cannot establish a strategy saw that board.
A fetched payload is evidence of publication of that particular key; otherwise
publication is unknown unless producer evidence establishes the outcome.
The collector's payload `timestamp` is a snapshot construction clock, **not**
a feed observation time or confirmed publication time. The archive key uses a
separate clock read. Never derive either timestamp from the other, or fabricate
`published_at` from a fetch time. `fetched_at` is only consumer retrieval time.

## Implemented optional audit extension

The 19 mandatory fields and `crassus_audit.v1` stay unchanged. New runner
records may additionally contain:

```json
{
  "market_snapshot_lineage": {
    "schema_version": "market_snapshot_lineage.v1",
    "snapshot_key": "intraday/20261002/snapshot_120000123456.csv",
    "status": "valid"
  }
}
```

`MarketSnapshot.snapshot_key` retains the supplied locator and `.lineage`
provides the extension. Existing `market_snapshot_timestamp` and
`market_snapshot_url_or_hash` retain their meanings. The latter remains
`{fetched URL}#sha256:{exact fetched JSON bytes hash}`. The source URL provides
namespace context; a bare key must not be joined across unrelated sources.

| Input condition | `snapshot_key` | Status / behavior |
|---|---|---|
| Current `intraday/YYYYMMDD/snapshot_HHMMSSffffff.csv`, valid date/time | Exact supplied string | `valid`; deterministic producer-declared archive locator, no readback claim |
| Historical minute/second key `intraday/YYYYMMDD/snapshot_HHMM.csv` or `snapshot_HHMMSS.csv` | Exact supplied string | `legacy_key`; potentially overwritten, no immutable-identity guarantee |
| Key absent (legacy payload or old direct constructor) | null | `absent`; rows remain usable |
| Explicit null, nonstring, empty, wrong namespace/format, impossible date/time, path traversal, URL/query/fragment | null | `invalid`; no coercion, trimming, network lookup, or strategy veto |
| No snapshot available | n/a | Extension null (or absent on startup with no observation) |
| Pre-extension ledger or pending intent | n/a | Missing/null extension means lineage unknown; no timestamp inference |

Invalid raw key values are not copied into the ledger. The existing payload
hash still identifies the exact fetched JSON. All original strategy-visible
row values and underlying readings are unchanged.

The runner carries this extension through normal trade/no-trade, dry-run,
strategy/quote/reconciliation errors, liquidation, mandatory flatten, and
closed-account records when a snapshot is available. Pending execution intents
persist it before submission and recovery uses the **original intent's**
lineage, never the latest board. The HTTP execution request is unchanged.
Startup records without a snapshot do not invent an identity. Existing error
boundaries, retries and decisions are unchanged; this adds no new error ledger
paths. Old ledgers are never edited, and unknown historical lineage stays
unknown. Timestamp-nearest historical matching remains approximate/unverified.

## Remaining #107 work

1. Durable per-snapshot VWAP/RVOL/underlying_market history; the key alone does
   not recover those readings after latest.json advances.
2. Collector PCR, max-pain and labeled 2% reference-skew publication/history,
   with collector/Crassus formula parity fixtures and this envelope.
3. Wire future shared histories to exact snapshot keys and prove no-trade and
   inactive-bot visibility, missing/stale/partial semantics, and replay coverage.
4. Separately review symbol parsing, freshness and quote-check plumbing (item 5)
   without changing strategy behavior.

#107 remains open. OI-skew multi-account behavior, #69 lifecycle, Phelps/Reddit,
policy changes, production configuration, orders, deployment and historical
rewrites are outside this PR. Black-Scholes remains buy-only.

## Verification and deployment review (2026-10-04)

Base: `fbbc3456da8025b6b1021f3d7282b2d6cae570e3` on freshly fetched master.
No open PR was returned by the repository-scoped open-PR search before work
or at the final overlap check. No AGENTS.md, CLAUDE.md, or .agents/skills files
exist in this base's tracked tree. Read Crassus README/RELIABILITY, deployment
contract, collector source, ledger/client/runner paths and merged #108/#110 docs.

Local Linux / Python **3.12.14**, dependencies installed from
`crassus/requirements.txt`. Each command below is
`python scripts/<suite>.py`, run from `crassus/`. All exited 0; the counts mix
unittest test cases and repository assertion checks, so are not summed.

| Suite | Exact result |
|---|---|
| `verify_snapshot_lineage` | 15 tests passed |
| `verify_invariants` | 83 checks passed, 0 failed |
| `verify_observability` | 14 tests passed |
| `verify_runner_flatten_attribution` | 26 checks passed, 0 failed |
| `verify_archive` | 17 tests passed |
| `verify_reliability` | 15 tests run: 14 passed, 1 skipped |
| `verify_black_scholes` | 80 checks passed, 0 failed |
| `verify_vwap_rvol` | 56 checks passed, 0 failed |
| `verify_momentum_qqq` | 67 checks passed, 0 failed |
| `verify_momentum_puts_only` | 42 checks passed, 0 failed |
| `verify_put_call_ratio` | 74 checks passed, 0 failed |
| `verify_max_pain` | 45 checks passed, 0 failed |
| `verify_oi_skew` | 57 checks passed, 0 failed |
| `verify_flatten` | 44 checks passed, 0 failed |
| `verify_canopus_down_day` | 32 checks passed, 0 failed |
| `verify_exchange_calendar` | 9 checks passed, 0 failed |
| `verify_looking_glass_straddle` | 33 checks passed, 0 failed |
| `verify_looking_glass_straddle_runner_integration` | 24/24 checks passed |
| `verify_phelps` | 104 checks passed, 0 failed |
| `verify_reddit_sentiment` | 47 checks passed, 0 failed |
| `verify_reddit_ingestion` | 73 checks passed, 0 failed |
| `verify_trump_whisperer` | 59 checks passed, 0 failed |
| `verify_trump_ingestion` | 23 checks passed, 0 failed |

The reliability skip is `test_detached_browser_is_reaped_after_worker_exit`:
this sandbox's `/proc` exposes a different PID namespace. Docker is unavailable
locally, so the existing Docker build/browser soak gate was not run locally.
CI's Python 3.11 and production-image checks remain release requirements;
local tests do not certify a running deployment. The new lineage suite is
added to Crassus CI without changing any deployment trigger.

An additional cross-revision check ran the **same committed fixture driver**
against an untouched base worktree and the candidate:

```sh
python scripts/fixtures/snapshot_lineage_behavior.py /path/to/base/crassus > base.json
python scripts/fixtures/snapshot_lineage_behavior.py /path/to/candidate/crassus > candidate.json
cmp base.json candidate.json
```

`cmp` exited **0**: 18 cases (buy, sell, no-trade, closed-market, dry-run,
flatten, each with absent/valid/malformed key metadata) have byte-identical
strategy decisions, account-state projections, strategy-visible rows/price,
and actual HTTP submission bodies through a fake session. Fixture account
observation times/request IDs are fixed; newly added audit fields are excluded
from the behavior projection by design. Both output files have SHA-256
`5cda9f3c714be3460275a867bfd80c68776be4f086613fec8c922dcd97f1188c`.
`git diff --check` also passed. No production requests/orders were used.

### Watch-scope impact

Read-only Railway inspection of `live-market-monitor` production and repository
workflow inspection on October 4 found:

| Service / workflow | Effect of this PR upon a future master merge |
|---|---|
| Crassus runner | Matches live `/crassus/**`; source master, root `crassus`, Wait for CI enabled. Crassus tests and Docker CI run on this PR. |
| QQQ collector | No match: live paths are `/collector.py`, `/market_signals.py`, `/crude_calibration.py`, `/requirements.txt`, `/Dockerfile`, `/railway.toml`; Wait for CI enabled. |
| Cloudflare Worker | **Would deploy on master merge**: `deploy.yml` includes `docs/**`, even for plan Markdown. Web CI also matches. No Worker runtime code changes. |
| Big Banana (`oa203-banana-scanner-AWsT`) | **Potential unrelated rebuild/deploy**: live source master, no `watchPatterns` reported, Wait for CI false. Treat merge as deployment-sensitive; this PR does not repair configuration. |
| MOO-169 collector | Watch paths only Tradier launcher/collector/probe, root requirements/Dockerfile; no match. |
| MOO-144 probe | Source branch `moo-144-probe-recovery`, not this branch/master. |
| R2 paper-trades recovery | Image-backed service, not this repository branch. |

The older deployment document's statement that Wait for CI is disabled on
Crassus/QQQ is stale relative to this read-only inspection. No production
configuration, deployment, account, live order, or historical data was changed.
This is a draft PR only; merging/releasing it is a separate action.
