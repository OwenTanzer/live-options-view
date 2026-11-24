# Candle-cache backend contract

Producer/reader: `market_data/candle_cache_backend/` (new in this
PR). Consumer: the site NASDAQ-100 panel (`site/js/nasdaq-panel.js`,
PR #391, unmerged) via `site/js/candle-cache-one-clock.js` (new in
this PR, not yet wired into the panel — see below). This
specification is repository documentation, not a served asset.

## What this is

A local cache reader/writer for the R2 candle layout published by
`market_data/single_stock_historical_backfill/` (PR #393, unmerged
at build time), synchronised with One Clock. It does three things
and nothing else:

1. **Reads and writes the cache** — `nasdaq/candles/<TICKER>/<interval>.json`
   plus one `snapshot_provenance.v1` sidecar per ticker — through a
   store abstraction (local directory store; R2 store interface with
   opt-in publish only).
2. **Stamps every cached bar with the One Clock observation
   envelope** — source time, observed (available) time, and a state
   chip (`recorded` / `reconstructed` / `synthetic`), never a fake
   zero.
3. **Selects the cache state as of a chosen time** — a Python
   as-of selector mirroring One Clock's, and a JS adapter that
   reuses the real One Clock core for the panel.

It fetches no market data, starts no collector, schedules nothing
and deploys nothing. It never corrects, drops or invents a bar.

## Base and stacking

Based on master (PRs #391 and #393 both unmerged when this was
built). This PR is self-contained on master: it does not import
either unmerged package. It stacks *conceptually* on #393 (it reads
and writes #393's layout) and on #391 (its JS module is the read
path the panel will use), but it merges independently:

- If #393 merges first, this backend reads the backfill's files
  unchanged — same keys, same interval-file shape, same sidecar
  schema.
- If #391 merges first, the panel wiring for
  `site/js/candle-cache-one-clock.js` can land as a follow-up (the
  panel is a classic script; the wiring is a module script or a
  dynamic import — see "Panel wiring (follow-up)").
- If neither has merged, the package still works standalone against
  its seeded sample (`seed` command, no network).

## Artifacts

| Object key (R2 bucket `qqq-options-chain-data`, expected — see below) | Contents |
|---|---|
| `nasdaq/candles/<TICKER>/<interval>.json` | One interval file per ticker per interval, `schema_version: 1`, `dataset: "single_stock_candles"` — the exact PR #393 shape, unchanged |
| `nasdaq/candles/<TICKER>/provenance.json` | `snapshot_provenance.v1` sidecar for the ticker, plus the additive `envelope` block (below) |

Eleven interval files per ticker (1m, 3m, 5m, 9m, 15m, 30m, 1h, 2h,
4h, 1d, 1w) when a backfill is complete. Three bases only (1m, 1h,
1d); the other eight are derived, never fetched. Publication is per
ticker as it completes, and opt-in (`publish` command). No artifact
is published yet; the backend is not deployed or scheduled, and no
R2 object exists until a maintainer deploys it and the first publish
succeeds. The panel reads whatever the cache holds; it never
publishes, starts a collector, or deploys.

## One Clock synchronisation (verified from the repo)

Verified by reading the repo's One Clock code on master, 2026-11-24
(no API invented):

| Piece | Exact names (master) |
|---|---|
| JS core | `site/shared/one-clock.js` exports `instant(value, name='time')`, `observation(input)`, `observationKey(row)`, `selectAsOf(inputs, selection)`, `readingAsOf(inputs, selection, { maxAgeMs })`, `createSourceRegistry()`, `createClock({ registry, initial, now })` (methods `snapshot`, `select`, `read`, `tick`, `subscribe`, `ownLiveSubscription`, `dispose`) |
| Python binding | `research/options_replay/one_clock.py` exports `instant`, `observation`, `identity`, `select_as_of`, `reading_as_of`, `TrainerSource` |
| Fixture contract | `tests/fixtures/one_clock_contract.json` (base row fields) |
| Envelope fields | `source`, `id`, `revision` (positive integer), `session`, `instrument`, `origin` ∈ {`recorded`, `reconstructed`, `synthetic`}, `evidence`, `eventAt` (epoch ms or offset-qualified ISO), `availableAt` (epoch ms or null — never substituted), `value` (null = retraction), optional `valueAt` (≤ `eventAt`), optional `details` |
| Selector rule | Both `eventAt` and `availableAt` at or before the cutoff, one explicit `origin` cohort, highest eligible `revision` per identity (`source`, `session`, `instrument`, `origin`, `id`); conflicting same-revision different content throws; rows sort by `eventAt`, `availableAt`, identity key (UTF-16 code-unit order) |
| Freshness | `readingAsOf` returns `state` ∈ {`fresh`, `stale`, `unavailable`}, `ageMs` = cutoff − (`valueAt` ?? `eventAt`), `publicationAgeMs` = cutoff − `availableAt`; `maxAgeMs` is explicit and required |

### What the selector is

`selectAsOf` is **generic over session/instrument/origin** — it is
not QQQ/SPY-only. The panel read path therefore reuses the real
core: `site/js/candle-cache-one-clock.js` imports `selectAsOf` /
`readingAsOf` from `../shared/one-clock.js` (the same relative
import pattern `backend/one-clock-publication-contract.js` uses) and
registers a `candle-cache` source adapter whose `read()` fetches one
interval file plus its provenance sidecar and returns observation
rows; the shared clock applies the selection.

The Python side is a **compatible mirror, not a reuse** — stated
explicitly, not claimed otherwise:

- The JS core has no Python import path a producer package can use
  (it is an ESM browser/Node module).
- The Python binding lives in `research/options_replay/` (the
  Trainer slice); a producer under `market_data/` importing it
  would invert the dependency (producer → research) and pull in
  Trainer-only machinery (`TrainerSource`).

`market_data/candle_cache_backend/selector.py` (`select_as_of`,
`reading_as_of`) copies the JS core's semantics field-for-field,
and its tests exercise the same cases as the One Clock contract
fixture (before/at/after publication, unknown availability,
duplicate delivery, correction, rewind, missing/partial data,
session/instrument isolation). If the core's semantics ever change,
the mirror must be re-verified against it — the spec records the
verified revision date (2026-11-24) for that check.

### How a cached bar carries the envelope

The on-disk interval files keep the exact PR #393 shape (plain OHLC
bars), so the PR #391 panel keeps reading them unchanged. The
envelope is carried in two places:

1. **The provenance sidecar's `envelope` block** (additive, schema
   stays `snapshot_provenance.v1`): per file, `origin`, `revision`,
   `available_at` and `evidence` (the file's R2 key). A missing
   sidecar means unknown provenance.
2. **As a read-time projection**: `cache.stamped_bars` /
   `cache.read_as_of` return each bar with `eventAt` (the bar's own
   start = source time), `availableAt` (observed time = the file's
   `generated_at`, or the envelope's `available_at` when present),
   `origin`, `revision`, `evidence` and `session`, without mutating
   the stored file. When no sidecar exists the reader falls back to
   `origin="recorded"`, `revision=1`, `available_at` = file
   `generated_at`, labelled in `details` as cache-derived so a
   derived availability is never mistaken for a proven publication
   time.

For the seeded sample, `available_at` is the original fetch time
(`2026-11-24T00:12:32Z`, proven by the PR #393 smoke run's own
provenance) — the earliest proven availability, not the seed-write
time. For bars this backend writes itself, `available_at` is the
write's `generated_at`: the write is when these bytes first became
available in this cache's view.

### State chips and gaps

- `recorded` — a bar Tiingo returned. `reconstructed` — a bar
  rebuilt from another source (e.g. a derived interval re-aggregated
  after a gap repair), never a silently filled price.
  `synthetic` — seed/demo bars that are not market observations at
  all. A missing bar is a gap, never a `0` close.
- A missing bar inside otherwise-present coverage is labelled
  `"reaction unmeasurable · price gap"` (One Clock's wording). A
  file whose first/last bars do not span the requested window is
  `"coverage partial"`. No eligible row at all is `"unavailable"`.
- A stale cache is reported, never silently refreshed:
  `read_as_of` returns `reading.state == "stale"` when the latest
  eligible bar is older than the explicit `max_age_ms`, and still
  returns the bars, labelled, so the caller can show them as stale.

No change is made to One Clock's own QQQ/SPY/IBIT fields: this
backend only ever produces `source == "candle-cache"` rows.

## The seeded sample (small, honest)

`market_data/candle_cache_backend/sample/aapl-seed.json` holds AAPL
only, three bases:

| Interval | Bars | Range (UTC) |
|---|---|---|
| 1m | 390 (full 2026-11-23 session) | 2026-11-23T14:30:00Z → 2026-11-23T20:59:00Z |
| 1h | 300 (newest) | 2026-09-15T14:00:00Z → 2026-11-23T20:00:00Z |
| 1d | 300 (newest) | 2025-09-16T00:00:00Z → 2026-11-23T00:00:00Z |

The bars are **live Tiingo bars, not synthetic**: a subsample of
the live fetch performed 2026-11-24T00:12:32Z by the PR #393
backfill smoke run (AAPL 1m: 390 bars, 1 request; 1h: 15,478 bars,
2 requests/2 pages; 1d: 6,764 bars, 1 request), re-cut here to
390 + 300 + 300 bars so the sample stays small. This build's own
fresh fetch was **not** possible: `api.tiingo.com` returned HTTP 429
on all three requests (the shared token's 50/hour budget was
exhausted by the day's verification calls; re-probed later the same
day, still 429). No bar was invented, corrected or filled. The
seed's `generated_at` is the build time; its `first_fetched_at` is
the original fetch time. `seed` derives the other eight intervals
locally (1,594 bars total for the ticker).

## Why the raw backfill data is NOT in the repo

The full backfill is ~4.8M bars, ~0.24–0.48 GB — far too large for
a PR batch — and the Tiingo free Starter tier is personal/internal
use only (redistribution needs a commercial licence), so the raw
bars do not belong in a public repository at all. What lives in
the repo is the code that reads/writes the layout, the contract,
and the small seeded sample above. The full backfill lives outside
the repo: the **Candles Backend zip in Notion**,
https://app.notion.com/p/4223c519147b8107b407dc77ce343d89
(and, once published, in R2 under `nasdaq/candles/`).

## R2 publication (opt-in, unconfirmed)

Environment only, never hardcoded (same names as PR #393):

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `TIINGO_R2_BUCKET` — bucket holding the `nasdaq/` prefix,
  expected to be the same `qqq-options-chain-data` bucket that holds
  `nasdaq/latest.json` (unconfirmed)

The sidecar is uploaded last; a sidecar failure raises, so a ticker
is never reported published without its provenance. The concrete
transport is `boto3` (lazy import). Without the environment — or
without `boto3` — publish fails closed with a plain error instead
of publishing partially.

## Resample-from-base consistency

Derived intervals are aggregated by `resample.py` (copied from PR
#393 so this package is self-contained on master) and never fetched:
3m/5m/9m/15m/30m from 1m (bucketed from the 09:30 ET session open),
2h/4h from 1h (consecutive hourly bars within one ET session day),
1w from 1d (ISO weeks, volume summed). The reader trusts only the
three bases: `read_bases` reads them and `derive_all` rebuilds the
rest. `verify_tree` checks every ticker's files against its sidecar
(bar counts, first/last bars, sort order, envelope block present
with `origin`/`revision`/`available_at`).

## Panel wiring (follow-up, not in this PR)

`site/js/candle-cache-one-clock.js` is not wired into the panel yet:
`site/js/nasdaq-panel.js` does not exist on master (it arrives in
PR #391), and the panel is a classic script while this module is
ESM. After #391 merges, the wiring is a module script (or a dynamic
import) that creates the clock, registers the adapter with
`createSourceRegistry`/`createClock`, and lets the panel read
`clock.read('candle-cache')` — sharing the panel's existing cutoff
instead of starting a second polling loop. The R2-first panel patch
against #391 (in the PR #393 work record) is the place that wiring
belongs; this PR does not touch it.

## CI wiring (maintainer)

Workflow files cannot go through the GitHub App (403 — the
`workflows` permission is missing; Meta would have to request it
before Owen could approve it). A maintainer with workflow access
must add these steps; the exact steps are:

- `collector-ci.yml`: `python -m unittest tests.test_candle_cache_backend -v`
  (or the repo's pytest step, alongside the backfill test).
- `web-ci.yml`: `node --test tests/candle_cache_one_clock.test.mjs`.

## Verification (local, 2026-11-24)

- Python unit tests: **38 pass, 0 fail**
  (`python -m unittest tests.test_candle_cache_backend`).
- JS tests: **8 pass, 0 fail**
  (`node --test tests/candle_cache_one_clock.test.mjs`) — these
  import the real `site/shared/one-clock.js` core.
- `py_compile` clean on all package and test files;
  `node --check` clean on the JS module and its test.
- `python tests/test_changed_paths.py`: run in the overlay (see PR
  body for the count).
- CLI smoke (seeded tree, no network): `seed` wrote AAPL (1,594
  bars); `verify` ok with 0 problems across all 11 intervals;
  `as-of --cutoff 2026-11-24T01:00:00Z` returned 390 1m bars,
  `fresh`, no gaps; the same query at `2026-11-23T21:00:00Z`
  correctly returned `unavailable` — the bars were not available
  in this cache until `2026-11-24T00:12:32Z`.
- Live Tiingo smoke: attempted, **not** completed — HTTP 429 on all
  three requests (see the seeded-sample section). The seed re-uses
  the PR #393 smoke run's live fetch instead.
- NOT verified: no R2 artifact published, no staged site preview
  read back. No producer restarted or redeployed. No live service
  ran on repo HEAD alone.

## Open questions

- Where does the full backfill data live if not in the repo? The
  Candles Backend zip in Notion (link above) is the current home;
  is that the durable home, or should R2 be the only home once the
  first publish succeeds?
- Who owns the Tiingo token in the repo environment? (Shared with
  PR #393 — the same token's hourly budget is shared with every
  other user of it, and this build hit its 429 twice in one day.)
- Is One Clock's as-of selector the right read path for the panel,
  i.e. should the panel read through `clock.read('candle-cache')`
  as proposed above, or keep its own R2-first fetch with the
  One Clock labels applied afterwards?
- R2 env vars (`TIINGO_R2_BUCKET`, shared `R2_ENDPOINT_URL`/key
  pair) remain unconfirmed against live service config.
