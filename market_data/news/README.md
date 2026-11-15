# News archive (MediaFlow M2, M3, M4a)

A historical news collector for Options View. Each cycle reads the configured
feeds, turns their entries into normalized observations, versions every change
to a story, and commits the result to an append-only archive. `join.py` (M3)
reads that archive back as of any time and joins it to price records. This slice is inert:
by default it reads fixture feeds and writes to a local directory. It starts no
service, runs no model and writes nothing to the bucket.

```sh
python -m market_data.news sources                     # sources, streams, digests
python -m market_data.news collect-once --clock 2026-11-09T15:00:00Z   # fixtures -> ./news-archive-local
python -m market_data.news verify                      # committed / corrupt / uncommitted runs
python -m market_data.news replay                      # rebuild identity state, list object digests
python -m market_data.news query --as-of 2026-11-09T16:00:00Z --mode collector --stream oil
python -m market_data.news enrich                      # classify revisions lacking a current classification
python -m market_data.news rebuild-index               # rewrite the derived indexes from manifests
python -m market_data.news join --as-of 2026-11-09T10:30:00Z --mode archive --tolerance-s 120   # news x prices
```

Live feeds need `--network`. The bucket needs `--bucket`, plus
`NEWS_ALLOW_BUCKET_WRITES=1` and the `R2_*` variables. Neither is used by the
tests or by the default commands.

| File | Role |
|---|---|
| `config.py` | Sources (feed URLs copied verbatim from the pinned prototypes), the `oil` and `ai_buildout` streams, keyword and category rules, contract bounds, digests |
| `adapters/rss.py` | RSS 2.0, RSS 1.0 (RDF) and Atom parsing with the standard library; bounded fetching (20 s timeout, 2 retries with backoff, 4 fetch workers shared across cycles, 180 s cycle budget) |
| `normalize.py` | Canonical URLs, source times, identity, content-addressed revisions |
| `classify.py` | Versioned rules classification (`rules-v1`), saved as separate events tied to one revision |
| `archive.py` | Object stores, writer lease, spool, commit protocol, history verification, `collect_once`, `enrich_pending` |
| `reader.py` | As-of reads (`collector`, `archive`, `publication`) and the derived daily index and latest view |
| `prices.py` | Validated, read-only price records (underlying quotes, option quotes, bars), each with a price time and an availability time |
| `adapters/sidecar_prices.py` | Read-only: collector provenance sidecars (`intraday/YYYYMMDD/*.provenance.json`) → `underlying_quote` records. Last-trade selections only (provider time, collector receipt and archive upload are kept separately); midpoints and other bases are excluded and counted; session comes from the exchange calendar. CLI: `prices-from-sidecars` |
| `join.py` | As-of join of news revisions to one price series, with price and news coverage |

## Sources and streams

Only OilPrice.com, EIA Today in Energy, TechCrunch and Ars Technica are
enabled. The other 79 sources from both prototypes are configured but disabled;
`--enable-source <id>` enables one explicitly. Every entry from an enabled
source is archived. Stream membership and category are enrichment, so
changing the rules later can be re-applied to history instead of having
filtered it away at collection. A source shared by both prototypes, such as
EIA Today in Energy, is one source with both streams, and its items can be in
both streams.

## Records

Each new revision produces one observation in `observations.jsonl.gz`:

| Field | Purpose |
|---|---|
| `item_id` | Stable identity for this source and story |
| `identity_quality` | `url` (canonical URL), `guid`, or `weak` (normalized title) |
| `original_url`, `canonical_url`, `url_status`, `source_guid`, `outlet`, `source_id` | Attribution. `url_status` is `ok`, `missing` or `invalid` (a link that cannot be parsed, such as a non-numeric port, has no canonical URL) |
| `categories` | Source categories, deduplicated and sorted |
| `revision_id`, `previous_revision_id`, `payload_digest` | Revision chain. The payload is title, excerpt, raw source times, canonical URL and categories: everything kept from the source that the classifier reads, so a category-only change is a new, dated revision |
| `published_raw`, `published_at`, `updated_raw`, `updated_at`, `time_quality` | Source times. A missing or zone-less time is `null` with a reason (`missing`, `no_timezone`, `unknown_offset`, `unknown_zone`, `date_only`, `unparseable`) |
| `observed_at`, `first_seen_at` | When the collector received this revision, and when it first saw the story |
| `run_id`, `adapter_version`, `config_digest`, `source_streams` | Provenance |

`archived_at` is the run's `committed_at` from `commit.json`; the reader attaches it.
An unchanged entry produces no record and is counted in the manifest instead.
An entry that fails to normalize produces no record either: it is counted as
`failed_entries` and listed (up to 20 per source, with GUID, link, title and
error) in the source row, the other entries and sources are still committed,
and the run status becomes `partial`.
Classifications in `classifications.jsonl.gz` carry `classification_id`,
`input_revision_id`, `classified_at`, `method`, `version`, `rules_digest`,
per-stream labels, `stream_memberships`, and `instrument_mapping: "unknown"`.

## Archive protocol

1. Take the writer lease (`news/v1/_writer/lease.json`, create-if-absent). An
   expired lease is only broken with `--break-stale-lease`.
2. Commit any spooled run left by an interrupted process, reusing its `run_id`.
3. Verify every committed run, then rebuild identity state from them. A corrupt
   committed run stops collection (`HistoryCorrupt`), because collecting on top
   of it would silently reset deduplication.
4. Fetch, normalize and classify. A classifier failure leaves revisions pending
   (`enrich` picks them up later) and never blocks archiving the observations.
5. Write the objects and the draft manifest to the local spool. The manifest
   (`news-run-manifest/v3`) records the run's `source_scope`: the requested
   streams and sources, the selected sources and the full configured roster.
   v2 manifests, which have no roster, are still read.
6. Upload each object and read it back against its SHA-256. Then write
   `manifest.json` and read it back; the manifest is the commit. Gzip output is
   deterministic.
7. Only then stamp `committed_at` and write it to `commit.json`
   (create-if-absent, bound to the manifest's SHA-256). A time written inside
   an object always precedes that object's durability, so `committed_at` is
   kept out of the manifest: it is an upper bound on when the run became
   durable, and an archive-as-of cutoff never includes a run before then. A
   committed run without `commit.json` (crash between the two writes) still
   counts for deduplication and collector-mode reads, but archive mode
   excludes it until the next writer records its availability at that later
   time (`verify` lists it under `availability_pending`).
8. Clear the spool, the draft last. On restart, a spooled run that is
   already committed in the store is recognized before any spooled object is
   read, so an interrupted cleanup is finished instead of failing every
   restart. A spooled run that is not committed and is missing an object
   fails visibly.
9. Rebuild `news/v1/index/YYYY-MM-DD.json` (runs, missed intervals, corrupt and
   uncommitted runs) and `news/v1/latest.json` (newest 200 items, archive mode).
   Both are derived; losing them loses nothing.

## News and prices (M3)

`join` produces one row per news revision, timed by when it became available
in the chosen mode, and matches it backward to a price that was itself
available by then.

| Mode | Row time (`event_at`) |
|---|---|
| `collector` | `observed_at` |
| `archive` | the later of `observed_at` and `archived_at` (the run's `committed_at`) |
| `publication` | `published_at`: retrospective and labeled hindsight |

With `--stream`, a revision is in the stream only once a classification
putting it there is known, so `event_at` is also no earlier than that FIRST
in-stream classification's `classified_at` (in archive mode, its own
availability). A later reclassification never moves the event or its price.

- **Event-known history.** In the availability modes a row shows only what was
  known at its `event_at`:
  - `revisions_known_at_event` counts the item's revisions known by then.
  - `classification_at_event` is the newest classification known by then, or
    `null`.
  - Revisions, classifications and a removed stream membership learned later
    but before the cutoff are listed separately under `later_context`.

- **Window.** One window, `[--start, cutoff]`, applies to the rows (by
  `event_at`), the price intervals and the news segments (clipped to the
  start). Rows before it are counted as `before_window`. A start after the
  cutoff is rejected. Without `--start`, the window opens at the first run or
  event. In publication mode, runs after the cutoff are hindsight: they are
  listed under `after_cutoff_hindsight`, never as segments.
- **Price records.** Each has `price_time` (the quote's `quote_ts`, or the bar's
  `bar_end`) and `available_at` (when the collector had it; a bar is never
  available before it closes). A NaN or infinite price is rejected.
- **Matching.** A row uses the newest price whose `available_at` is at or before
  `event_at`. Later ticks, late-arriving quotes and unclosed bars are never
  used. `--tolerance-s` is required. A price older than that is reported as
  `stale`, and no price at all is `missing`.
- **Series.** A series is one instrument and one kind: `underlying_quote`,
  `bar_1m` or `bar_1d`. Option quotes are never an underlying price.
- **Coverage.** `price_coverage` lists the missing and stale intervals in the
  window. `news_coverage` has one segment per collect run, scoped to the
  sources of the requested stream (all sources without one). The relevant
  sources come from the run's recorded roster (the manifest's
  `source_scope`), never from its source rows alone: a `--streams oil` run
  has no rows for TechCrunch or Ars, which are AI sources. Each segment lists
  which relevant sources were fetched, succeeded, failed, were not selected,
  and how many are disabled. A run recorded without a roster (manifest v2)
  has `roster_complete: false` and is never `covered`. Its state is one of:
  - `covered`: every relevant enabled source was fetched, none failed and no
    entry failed to normalize.
  - `partial`: some relevant sources were fetched, but others failed, had
    entry failures or were not selected. Their coverage is unknown.
  - `unknown`: no relevant source was fetched in that run.
  - `pending_availability`: the collector has the run but archive mode does
    not yet. That is unknown coverage, not a quiet interval.
  - Disabled sources are outside the collector's scope: they are counted,
    never claimed as covered. A segment is also flagged `missed_interval`
    (a gap of more than two intervals), and the tail since the last run is
    reported.

The prices are the synthetic QQQ fixture in `tests/fixtures/news/prices/`.
Real price paths and hashes are pinned later. Sessions are labels on the
records; there is no exchange calendar, so a closed market shows as a stale
or missing interval.

## News views (M4a)

`views.py` projects the M3 join into sanitized, bounded JSON pages for a
News panel. It is local fixture work: nothing here publishes, writes KV or
R2, or touches the site.

```
python -m market_data.news build-views --as-of 2026-11-09T11:15:00Z --tolerance-s 120     --label "SYNTHETIC fixture data" --out ./views-out [--date 2026-11-09]
```

- **Layout.** `news/v1/views/index.json` and
  `news/v1/views/<mode>/<stream>/<YYYY-MM-DD>/page-NNNN.json`. Mode
  (collector, archive, publication), stream (all, oil, ai_buildout) and UTC
  date are path dimensions. A view's window is [date 00:00Z,
  min(as_of, end of date)], the M3 join window.
- **Public fields only.** Rows keep ids, the cleaned title and outlet,
  source id, publication/observed/archived/event times and event basis,
  revision counts, the classification with its `timing` (`at_event` only
  when known by the event; publication rows and any later classification
  are `retrospective` and shown as later context with `classified_at`),
  later context,
  the instrument mapping (`unmapped` in this slice) and the matched,
  stale or missing price. Excerpts, raw fetch errors and per-entry failure
  details are never projected; failures appear as ids, statuses and counts.
- **Links.** Checked by the builder (urllib.parse) and again by the panel
  (`new URL()`): http/https only, a host, no userinfo, no whitespace,
  control or backslash characters, at most 2048 characters. A rejected
  link is `canonical_url: null` with a `url_rejected` reason.
- **Limits** (`views.LIMITS`), measured on the final serialized bytes:
  a page holds at most 256 KB, 50 rows, 200 coverage segments, 200 price
  intervals, 50 hindsight runs and 100 source ids per segment; a view at
  most 20 pages; `index.json` at most 256 KB and 500 views. The mandatory
  envelope of each page and of the index is measured first with its
  counters at their widest; if it cannot fit, the build fails. Coverage
  may use half a page; rows fill the rest and move to the next page.
  Every row is packed under the same limits, so `pages_omitted` is the
  exact number of further pages the omitted rows need.
  At any limit the file says `complete: false`, lists `overflow_reasons`
  and gives omitted counts (`rows_omitted`, `segments_omitted`,
  `intervals_omitted`, `hindsight_omitted`, `source_ids_omitted`,
  `pages_omitted`; `views_omitted` in the index). Omitted coverage sets
  `coverage_complete: false`.
- **Observed, no items.** A coverage segment is `quiet` only when it is
  covered for the view's stream scope, spans a real gap between two
  observed runs (not before the first run, not a missed interval), and
  both span counts are zero before pagination, source filtering or
  truncation: `observations_in_span` (rows by `observed_at`, what the run
  newly received) and `events_in_span` (rows by `event_at`, which a delayed
  archive write or classification can put after the receipt). Corrupt or uncommitted runs, revisions not yet
  classified for the stream, and publication mode (hindsight) disable
  quiet spans for the whole view (`quiet_blocked_by`). Everywhere else an
  empty list is "No items on this page".
- **Panel.** `site/js/news-panel.js` (the only copy) renders pages with
  `textContent` only, a page-local source filter labelled "N of M rows on
  this page" (coverage stays stream-scoped), prev/next navigation and an
  INCOMPLETE banner (also for an index cut before its first entry). Only
  the latest view or page request renders; an older response or failure
  that arrives later is ignored. `tests/news_panel/harness.html` (tests
  only) loads the site file and copies the panel CSS block from
  `site/index.html`.

### Site wiring (M4b, local only)

- **News tab.** `site/index.html` adds a News tab button, a hidden
  `#news-panel`, panel CSS scoped under `#news-panel` (marked block) and one
  `js/news-panel.js?v=<first 16 hex of SHA-256>` script tag between
  `squeeze-panel.js` and `tab-switching.js`. `tab-switching.js` mounts the
  panel once, on first open (`loadNewsPanel`); `boot.js` wires the button.
- **Two bases.** The panel reads from `BROWSER_DATA`: the bounded
  `/browser-data` proxy on the production origin, the public R2 origin
  otherwise. Its fetcher applies the exact view-key check (the index, or a
  fixed mode / stream, a real calendar date and page 0001-0020) before every
  request, including `first_page` from the index, the picker and prev/next,
  so a failing key is shown as refused and never requested on either base.
  `backend/browser-data.js` applies the same rule (`isPublicNewsViewKey`).
- **States.** "No news views published yet" only for a 404 on `index.json`;
  any other status, a missing page, a network error, invalid JSON, a page
  that fails the structural check (schema, window, view, coverage, rows) or
  a page that cannot be rendered is a failure ("Could not load ..."), which
  also drops `data-page` so nothing claims the failed load succeeded. `#news-panel[data-state]` is `loading`,
  `ready`, `empty`, `absent` or `failed`.
- Nothing is published yet. The panel and proxy are deployed (2026-11-14, `55db036`), the producer
  is inactive, and the deployed tab shows "No news views published yet".
- **Dedicated News origin.** News keys are read only from the dedicated public News bucket's origin
  (`NEWS_R2_ORIGIN` in `backend/browser-data.js`, `NEWS_R2` in `site/index.html`), never from the
  shared market origin. Both are `null` until that bucket is provisioned. While they are `null`, the
  proxy answers every News key with 404 and makes no upstream request (the tab shows "No news views
  published yet"). Off the production origin, the panel requests nothing and shows "Could not load
  news views: no public News origin is configured".
- **Scheduled command.** The scheduled command is the bounded launcher
  (`python -m market_data.news.launch`, see `launch.py`). It gives the whole cycle process one
  deadline and a clean terminal failure path. The cost and sizing evidence is in `deploy/COSTS.md`.

Tests: `python -m unittest discover -s tests -p "test_news*.py" -v`,
`node tests/news_panel_view.test.js`, `node tests/browser_data.test.js`,
`node tests/tab_switching.test.js`, `node tests/boot.test.js`, and the
optional rendered checks `NODE_PATH=<playwright> node tests/news_panel_browser.test.js`
(`NEWS_PANEL_EVIDENCE=<dir>`) and `node tests/news_site_browser.test.js`
(the full site page on both bases at 1280 and 390 px;
`NEWS_SITE_EVIDENCE=<dir>` saves screenshots). Of these, web-ci runs only
`browser_data.test.js` and the `node --check site/js/*.js` parse gate.

## Changes from the prototypes

- **Edited stories.** A changed story at a known URL becomes a new revision
  linked to the previous one. The prototypes' seen-set skipped it.
- **Unknown publication times.** These stay `null`. The oil official-page
  adapter wrote the collection time instead. Zone-less feed dates are no
  longer read as UTC (feedparser and `email.utils` both do this silently).
- **Publication vs update times** are stored separately, not collapsed into one.
- **URL case.** Canonical URLs keep path and query case (the prototypes
  lowercased the whole URL), and an `http` versus `https` link is still the
  same story.
- **Distinct publishers.** Each publisher stays a distinct item. Revision
  history replaces the daily Git snapshots.

## Fetch workers

A thread cannot be killed, so a fetch still running when its cycle's budget
expires keeps its worker until it returns (the network fetcher's own 20 s
timeout and two retries bound that). Every cycle in a process shares one pool
of 4 workers, so successive budget-expired cycles can never hold more than 4
between them: later sources queue, and any that never start are recorded as
`budget_exceeded` ("never started"). The manifest records
`fetch_workers_busy_at_start`.

## History checkpoint

Full replay (`scan_history`, `verify`, `replay`, `--full-replay`) reads and
verifies every committed run. It stays the reference. The checkpoint
(`checkpoint.py`) is a disposable speed-up over it:

- `news/v1/checkpoint/history/{sha256}.json.gz` holds the verified contents
  of every *settled* run (manifest read back and `commit.json` recorded).
  `news/v1/checkpoint/head.json` points at it, with its size and digest.
  The body is written and read back before the head. The current and previous
  bodies are kept, so a reader holding the old head still finds its body.
- **Frontier (bounded listing).** The head records a frontier: the last run
  prefix, in key order, before which every run is settled and checkpointed.
  A read lists `news/v1/runs/` only **after** the frontier (ListObjectsV2
  StartAfter; the listing's size and ETag, locally size and mtime), takes the
  runs at or before it from the body, reads the head and the body, and fully
  reads only the tail:
  - runs not in the checkpoint: new, availability pending, uncommitted or
    corrupt. The frontier never passes such a run, so it is listed and read
    on every cycle until it settles (the head's `frontier_stopped_at` names it);
  - listed checkpointed runs whose listing changed (object added, removed or
    rewritten): re-verified.
  The run listing therefore costs one request per read however long the raw
  history grows (`tests/test_news_checkpoint.py`, `BoundedListing`: 1,500 runs).
- **Writes behind the frontier.** Before uploading a run whose prefix is at or
  before the frontier (a spool recovered under its original, older run_id, or a
  clock that went backwards), a writer clears the frontier durably. A crash
  after the upload cannot hide the run: the next read lists everything.
- **Missing, corrupt or incompatible** checkpoints (a different checkpoint,
  manifest, commit or object schema) make the read a full replay. The next
  writer saves a fresh checkpoint.
- **Scheduled full listing:** once its last full verification is over 24 h old
  (`FULL_VERIFY_S`), a writer lists all of `news/v1/runs/` and compares every
  run's listing with the checkpoint. A changed run is re-read and verified; a
  checkpointed run that is **missing** is reported as corrupt history, and the
  cycle fails (`HistoryCorrupt`). A full replay alone could not tell that the
  run had disappeared. This is listings only: it does not read every object
  again. So a change to a run at or before the frontier is found at most 24 h
  later (or at once by `checkpoint compare`), not on every read.
  `checkpoint rebuild` re-baselines from the runs (a full content replay) and
  refuses corrupt history.
- Only writers (`collect-once`, `enrich`, `checkpoint rebuild`) save it, under
  the lease, after the commit. A failed save never fails a committed cycle: it
  is reported in the cycle output's `history`, and the next cycle reads a
  longer tail. Readers (indexes, views, query, join) never write.
- The archive is never pruned or rewritten; the checkpoint lives outside
  `news/v1/runs/`.
- `checkpoint status | compare | rebuild`. `compare` checks checkpoint plus
  tail against full replay run by run (exit 1 if they differ).
- Cost: a history read is 2 object reads, plus the tail, instead of 4 per run.
  The body's bytes still grow with history, because it holds every record,
  roughly the size of the compressed archive. It is read once per history read
  and written once per cycle.

## Known gaps (M2)
- **Payload changes re-revision history.** Adding a field to the revision
  payload (as categories were in M2.1) makes the next sighting of every known
  item a new revision. Nothing has been deployed, so no archive is affected;
  a future payload change needs a versioned migration.
- **R2 store not run against R2.** `R2ObjectStore` relies on R2 supporting
  conditional `If-None-Match: *` puts for the lease. It is covered by the
  local-store tests only.
- **Deferred adapters.** Official HTML pages, Reddit, NewsAPI, and
  Google News outlet links (identity uses the Google redirect link) are not
  implemented yet.
- **Unchanged sightings are counted, not listed per item.** Disappearance from
  a feed is therefore not recorded, and is never treated as a retraction.
- **Release review items.**
  - Feed excerpts are stored as received.
  - The manifests and run objects live in the existing bucket, which has a
    public read domain. They hold feed text and source status, with no
    credentials.
  - Retention and size are unbounded by design.
- **Not wired into CI.** The tests are not yet in `.github/workflows/collector-ci.yml`,
  which is a shared file. Proposed step:
  `python -m unittest discover -s tests -p "test_news_*.py" -v`.
- **M3 is fixture-only.** The join reads local JSONL prices and a full archive
  scan; it has not been run on real price history and has no index.
- **M4a is fixture-only.** Views are built from local stores into a local
  directory. The panel is not in the site shell or proxy, and the views are
  not published anywhere.
- **Not implemented here:** legacy prototype import (tagged
  `legacy_unverified`) and model enrichment.

Tests: `python -m unittest discover -s tests -p "test_news_*.py" -v`.
All fixtures are synthetic (see `tests/fixtures/news/README.md`).

## M5: publisher and inactive pilot (local preparation)

`publish.py` publishes sanitized views from the private archive to a separate public store (cleanup, then pages, then index.json), with stop/restore/reseal/withdraw and `publish-status`. `pilot-cycle` runs one 15-minute pilot cycle (four sources, prices unavailable). The config `deploy/news-pilot.json` is inactive and `--live` is refused. Operating plan and open storage decisions: [PUBLISHING.md](PUBLISHING.md). Tests: `tests/test_news_publish.py`, `tests/news_publish_browser.test.js`.

## Publication generations and settlement (G3 + G6, never run)

Published views are immutable incarnations of a generation: `news/v2/views/p/<gen>.<n>/...` pages
behind the fixed `news/v2/views/index.json`. `views.qualify_generation` turns a build into that
form and `views.commit_index` adds a fresh nonce to every index write. `publish.py` keeps one
compare-and-swapped head record (`news/v1/publish/head.json`) and a commit record per index write,
writes the index only by compare-and-swap on the expected ETag, leaves an index write with an
unknown outcome open until a later landed write closes it, never reuses a page namespace once a
delete has started in it, and withdraws by tombstone (the index key is never deleted). A replaced
generation is never deleted before its reader grace on two clocks (refused entirely until the
store-clock bound is verified). Legacy `news/v1/views` keys are untouched. See `PUBLISHING.md`
("Generations and settlement") for the contract, the provider assumptions, the operator commands
and what activation still needs. Tests (authored, NOT RUN): `tests/test_news_generations.py`,
`tests/test_news_settlement.py`.

## P1: research captures and annotations (local, inactive)

`research.py` keeps a separate research ledger (`news/v1/research/`) of prepared research captures and versioned annotations (positioning / institutional pressure), with collector, archive, publication (hindsight) and research visibility. `research-import`, `research-settle` and `research-view` work on local stores only. The view module `site/js/news-research.js` is not loaded by the site. See [RESEARCH.md](RESEARCH.md).
