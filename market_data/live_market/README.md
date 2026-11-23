# Market snapshot collector

The collector acquires tastytrade/DXLink market data and publishes the QQQ/SPY
snapshots consumed by the website and Market MCP (Model Context Protocol).
Run `python -m market_data.live_market` from the repository root;
[__main__.py](__main__.py) delegates to [runner.py](runner.py).
Railway service `live-options-view` uses that command, the
[shared Python image](../../deployment/python/README.md), no cron and restart policy
`NEVER`. Build/start/watch settings are service-managed.
Crassus is a separate deployable with its own dependencies and strategy state.

## Follow the implementation

| Responsibility | Source |
|---|---|
| Session startup, threads and shutdown | [runner.py](runner.py), [sessions.py](sessions.py) |
| Read-only OAuth and token refresh | [oauth.py](oauth.py) |
| Streaming subscription and event ingestion | [feed.py](feed.py), [chain.py](chain.py) |
| Snapshot construction and underlying-market readings | [market.py](market.py) |
| Pure VWAP, RVOL and momentum calculations | [market_signals.py](market_signals.py) |
| Price publication and yfinance fallback | [prices.py](prices.py) |
| Process-local live-quote service | [quotes.py](quotes.py) |
| Restart state, health and weekly archives | [state.py](state.py), [health.py](health.py), [weekly.py](weekly.py) |
| Optional EIA forecast and vintage comparison | [steo.py](steo.py), [crude_calibration.py](crude_calibration.py) |

## What the outputs mean

- `intraday/latest.json` and `intraday/spy/latest.json` hold separate underlying
  snapshots. A missing SPY reading must not substitute QQQ. See the
  [SPY contract](../../docs/specs/SPY_COLLECTION.md).
- Snapshot open interest is prior-day settled information. Volume and volume
  changes provide intraday activity; they do not measure newly opened positions.
- VWAP (volume-weighted average price) uses sampled underlying prices weighted by
  cumulative-volume changes. It is an approximation, not a trade-by-trade tape.
  RVOL (relative volume) compares cumulative session volume with historical
  readings in the same time bucket; insufficient history is an explicit state.
- Snapshot time, provider observation time and retrieval time are distinct.
  Partial-session and stale-data metadata affect how a reading should be used.
- Every archived `snapshot_*.csv` gets a durable sidecar
  `snapshot_*.provenance.json` (`snapshot_provenance.v1`) recording
  `constructed_at`, `archive_uploaded_at`, `latest_published_at`,
  `publication.status` (`published`, or `archived_unpublished` with a reason),
  the CSV object key/row/column counts, and the selected underlier's
  `price_basis`, `collector_received_at`, `provider_observed_at` (null when
  unknown), `freshness`, and contributing bid/ask/last times — including why a
  midpoint was rejected. The CSV keeps its existing columns; readers that only
  know the named values are unchanged, and a missing sidecar means unknown
  provenance. A midpoint is stamped with the *older* contributing side's
  receipt and only when both sides carry one, so a fresh ask cannot rejuvenate
  an older bid or an old last trade. Historical CSVs written before the
  sidecar have no recoverable per-price provenance and are not backfilled.
- `intraday/latest.json` and each provenance sidecar carry `market_readings`:
  six `market_reading.v1` **reference** readings (VWAP, RVOL, reference
  momentum, put/call open-interest ratio, max pain, and the fixed 2%
  near-money OI skew) computed from the same board and underlying-market
  values the strategies see. They carry units, effective calculation
  parameters, the exact snapshot identity, freshness and explicit
  missing/zero semantics, and exist without any bot. A
  `market_readings_digest` (canonical SHA-256) lets a Crassus decision record
  join the exact observation by key and digest instead of a mutable
  `latest.json`; replay is `research/market_readings_series.py` over the
  sidecars. Reference readings never feed a strategy decision; account
  thresholds, baselines, bands, trading momentum and the buy-only
  Black-Scholes annotation stay bot-local.
- Live bid/ask requests use the in-memory quote server through the Worker's
  `/api/live-quotes` route. That transport does not write an R2 object. Stored
  snapshots support other readings and recovery; they are not the live quote feed.
- Weekly archives produced here are separate from Historical's daily archive
  pipeline. Updating weekly manifest metadata does not advance its `dates` list.

Follow a displayed reading using the [walkthrough](../../docs/WALKTHROUGH.md).
The [OAuth operations reference](../../site/tastytrade-oauth.md) covers credential
provisioning; credentials do not belong in documentation or logs. Fixture checks
are inventoried in [Collector CI](../../.github/workflows/collector-ci.yml).
Historical design details remain linked from [documentation history](../../docs/HISTORY.md).

## One Clock immutable publication

`ONE_CLOCK_PUBLICATIONS=enabled` opts the existing snapshot writer into
[`one_clock.py`](one_clock.py) and [`publications.py`](publications.py). It writes
verified immutable evidence, an intent and a sequential commit before updating
the existing latest view. A failed commit leaves latest unchanged and records an
explicit provenance gap. Native publication does not add a feed, timer or thread.
The dedicated S3 transport has bounded requests/time and socket timeouts; latency
and failure counts are logged without payloads. Other collector transports keep
their existing configuration.

The compatible API and reviewed migrations must precede producer enablement.
The continuous price lane makes a watched merge/redeployment a real service
replacement even outside equity hours. Verify provider triggers, build manifest,
replacement timing and capture gap before claiming a completed rollout. See
[publication and handoff contract](../../docs/ONE_CLOCK_PUBLICATIONS.md) for
identity recovery, cross-language validation, source composition and rollback.

## Private Bitcoin monthly quote lane

[btc_options.py](btc_options.py) optionally shares the continuous price-lane feed.
It has no archive writer and is disabled unless `BTC_OPTIONS_ENABLED=1`. Read the
[product source and continuity gates](../../docs/BTC_PAPER_OPTIONS.md) before enabling;
fixture coverage does not establish account coverage or audience display rights.

Untimed BTC option sides require a post-baseline price or size change observed on
the existing connection; snapshots, unchanged repeats and reconnect replay do not
start an execution clock. [btc_quote_clock.py](btc_quote_clock.py) keeps this
process-observed clock separate from source and receipt timestamps, invalidates
it across connection epochs, and supplies counts-only raw time-field diagnostics
on the private BTC route. No added connection, subscription budget, public data
route or archive writer is involved. See the product guide for limits and the
collector/API/asset rollout order; old API/UI revisions cannot use this fallback.

## Minute capture and One Clock recovery

All enabled chains share the feed snapshot and capture time taken at the start
of a cycle. The next capture targets the next wall-clock minute. If an overrun
crosses a minute boundary, the next cycle captures the current feed immediately.
Health counts a snapshot after the CSV upload; latest-view uploads retain their
own success counter. An unobserved minute remains missing.

With One Clock enabled, acquisition stages the exact payload, sidecar and CSV
digest in R2 under `collector-pending/v1/<symbol>/`. A single background worker
per symbol recovers the oldest pending item, verifies the archived CSV, and
commits the existing immutable UUID before conditionally advancing latest. The
worker retires the pending copy only after the sidecar write. Ambiguous failures
retry from exact bytes with exponential delays up to 60 seconds. Each immutable
attempt has a 20-second cooperative budget, at most 40 requests, and bounded
socket timeouts. A permanently invalid item holds that instrument's publication
queue; capture continues. The pending key in logs identifies the instrument.
Only the active producer may own these writes. A rollback stops the new workers
and leaves any outstanding R2 items for later recovery.

Retain the observation timestamp for market chronology. An individual reading's
stamp is the publication attempt timestamp. Use the immutable entry's R2 upload
time for native availability and the successful latest write recorded in the
final sidecar for compatibility-view availability. A pending sidecar establishes
archive capture only.
