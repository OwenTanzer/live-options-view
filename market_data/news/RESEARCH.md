# Research captures and annotations (P1, local and inactive)

Positioning and institutional-pressure records, built from the accepted design
(`POSITIONING_ANNOTATIONS_DESIGN_v2.md`). The question each record answers is: **who is exposed,
what pressure is actually reported, and what adjustment might follow?** Every answer stays labelled
with how and when we knew it.

**Status:** local and inactive. Nothing collects, fetches or reads a bucket:
- The CLI commands below take local stores only. They have no `--bucket` option.
- Prices stay unavailable, and the v1 view fixtures are unchanged.

## Publication (R1 + R2: implemented locally, inactive)

The research view is published **as one page of each news generation**, not under a key of its own:
- **R2: who writes it.** `pilot-cycle` reads the research ledger from the **private** store
  (`news/v1/research/`), builds `research_view` in **research mode at the cycle's build cutoff**, with
  the collector archive's observations as annotation targets, and projects it (`project`). Ledger
  runs that fail verification are counted in the entry (`unverified_runs`), never shown.
- **R1: where it goes.** The projection is the unqualified page `news/v1/views/research/view.json`
  of the build (in memory only). Qualification moves it to
  `news/v2/views/p/<gen>.<n>/research/view.json`, adds `gen`, and covers it in `pages_sha256`. The
  index's `research` entry names it. So it uses the same head compare-and-swap, commit nonce,
  page-cap reservation, quarantine of unanswered uploads, retirement and index-only withdrawal as
  the news view pages, with no research-specific publish path.
- **The index entry:** `{schema_version, mode: "research", cutoff: build_as_of, fixed: true, state,
  page, items, runs, unverified_runs}`, where `state` is:
  - `published`: names this generation's research page;
  - `none`: no verified research run, so no page;
  - `omitted`: the page would exceed the page byte cap (`reason: research_view_bytes`). The news
    views still publish.
  The publisher refuses a build whose entry and page disagree: wrong mode, a cutoff that is not the
  build cutoff, a page in another incarnation, an item count mismatch, a page with no entry, or an
  entry with no page.
- **Reader.** `site/js/news-research.js` has no key and no fetch of its own. `loadNewsPanel` passes
  `NewsResearch.follow(...)` to the panel as `onIndex`. The research view reads the page that the
  panel's one index read names, through the panel's fetcher (exact-key check, deadline, no-store,
  `?_=`), and only while that index's lease holds. A tombstone, a 404, a deadline, an incarnation
  mismatch or a panel refresh shows the matching state, never another generation's research page.
  When only the research page is stale (404, deadline, lease or generation mismatch) the news
  views can stay ready with no Reload of their own, so the research view offers its own **Reload
  news and research** button. That calls the panel's shared-index reload (`event.reload`): the
  panel mount ends, the one index is read again, and news views and research both follow that one
  read. Without a shared reload the wording asks for a browser reload instead.
- **Gateway.** `backend/browser-data.js` and `frontend/preview-policy.mjs` admit only
  `news/v2/views/p/<gen>.<n>/research/view.json` (a real UTC instant, n = 1..9999). The P1
  single mutable key `news/v2/research/view.json` is **retired** (404). The private ledger is never
  served.
- **FIXED VIEW (explicit limitation).** Each publication carries exactly one research view, in
  research mode at its build cutoff. `load_pilot_config` refuses any other `research.mode` or
  `research.cutoff`. The tab has no mode or cutoff picker. Archive, collector, publication-hindsight
  and earlier cutoffs exist only through `research-view` on local stores. Adding them, a picker,
  adapters or live research sources is a separate decision.
- **Label.** The page's `data_label` comes from the pilot config (`research.data_label`, currently
  "PREPARED research records ..."). It is one label for the whole ledger, not per bundle.
- **Capacity arithmetic.** The pilot maximum per generation is now 1,260 view pages + 1 research page =
  1,261. Five generations at that maximum come to 6,305 pages, which is over the unchanged 6,300 cap.
  So the fifth admission at the absolute maximum pauses (visible) instead of evicting. That is an
  **availability limit**, not a reason to raise the cap: the 6,300 cap stays unchanged (Chej,
  2026-11-13). Real pilot generations are far below the maximum (the local runs have 64 pages).

Code: `market_data/news/research.py` (ledger, view, projection), `views.py` (`research_page`, the
research key and its qualification), `publish.py` (`build_research`, `_check_research`, the pilot
step). Tests:
- `tests/test_news_research.py`
- `tests/test_news_research_publish.py` (R1/R2)
- `tests/news_research_view.test.js` (renderer and `follow`)
- `tests/news_research_browser.test.js` (harness)
- `tests/news_research_site_browser.test.js` (the integrated News tab, real pilot stores)
- scenarios in `tests/news_research_scenarios.py`

## Records

**research_capture.** A source a person read, or tried to read.
- It is an ordinary revision: `item_id` comes from the URL (or from the bundle key when there is no
  URL), with a `revision_id` / `previous_revision_id` chain and a payload digest. These are the same
  identity primitives that collected items use.
- It carries:
  - the publication time, as `published.at` with `kind`: `publication`, `edgar_acceptance`, `sent` or
    `unknown`. The precision is kept: date, minute or second;
  - an optional `updated` time;
  - our `access` (`accessed_at` with its basis and result, plus earlier attempts).
- It has **no collector known-at** (`observed_at` / `first_seen_at`).
- An unread page is kept with only its URL and access result.
- **No third-party text is stored.** `excerpt`, `body`, `text`, `quote`, `content`, `html`,
  `summary` and `snippet` are refused anywhere in a bundle, and titles are our own wording.

**annotation.** A versioned judgment on one exact revision, either a capture or a collected
observation:
- `annotated_at` is stamped by the import and **never supplied**;
- `method` is `manual`, `rules` or `model`, with a `version`;
- each annotation is committed in its own run.

Its body:
- `claims[]`: each claim has one subject (a party *or* a population) and one action, with a literal
  status: `reported`, `offered`, `executed`, `unverified` or `contradicted`. Each claim also has an
  `evidence_kind`, a source `chain` of hops with access evidence, an exposure, `quantities` (each
  with what it is **of** and `measured_at`, often "unknown") and other `dates` (a bid deadline is
  not a measurement time).
- `context[]`: stated figures that are not pressure quantities. They are never multiplied or
  added.
- An optional `hypothesis`: a statement, a horizon, a disconfirmation and the missing evidence. It
  never upgrades a claim.

A later annotation in the same `lineage` names its predecessor (`previous_annotation_id`).

**Ledger:**
- Runs live under `news/v1/research/runs/<date>/<run_id>/`:
  `captures.jsonl.gz`, `annotations.jsonl.gz`, `manifest.json`, and `commit.json`. `commit.json` is
  the archive's own commit record: written once, and stamped only after the manifest reads back.
- The ledger has its own lease (`news/v1/research/_writer/lease.json`), so it never blocks the
  collector.
- It is outside `news/v1/runs/`, so the collector archive and its checkpoint never read it.

## Visibility per cutoff c

| Mode | Sources shown | Annotations shown | Label |
| --- | --- | --- | --- |
| collector | collected revisions with `observed_at` ≤ c, only through an annotation visible at c; **never captures** | `annotated_at` ≤ c | — |
| archive | as collector, and `archived_at` ≤ c | `annotated_at` ≤ c **and** annotation commit ≤ c | — |
| publication | captures with publication time ≤ c, using everything known now (the latest revision) | every annotation on the shown revision; `made_after_cutoff` when made or committed after c | RETROSPECTIVE (hindsight by design) |
| research | collected revisions as in archive mode, or captures with `accessed_at` ≤ c **and** capture commit ≤ c | `annotated_at` ≤ c **and** annotation commit ≤ c | RESEARCH |

Rules that apply in every mode:
- A time with date or minute precision gates at its **latest** possible instant. A date with no
  zone runs to D+1 12:00Z, so nothing is shown earlier than it could have been known.
- An unknown publication time is never guessed. It is counted under `no_publication_time`.
- Annotations appear only on the exact revision shown. Lineages left on an earlier revision are
  listed, not carried over.
- A hypothesis is **contemporaneous** only if its annotation was made *and* committed before the
  earliest possible horizon start. Otherwise it is **retrospective**: never a prediction, and
  withheld in collector and archive modes.
- For EDGAR, the acceptance time (publication), our access and our capture commit are three separate
  gates. A filing deadline is never availability.

## Commands (local stores only)

```
python -m market_data.news research-import --store DIR --bundle FILE [--clock T] [--commit-clock T] [--defer-commit]
python -m market_data.news research-settle --store DIR [--clock T]     # commit runs that have none, stamped now
python -m market_data.news research-view   --store DIR --cutoff T --mode research|publication|archive|collector \
                                           [--observations-store ARCHIVE_DIR] --out VIEW.json
```

How each command behaves:
- **Import is idempotent.** An unchanged capture or annotation adds nothing. A changed capture is a
  new revision, and an annotation that now targets it is a new annotation.
- **Import refuses** a capture accessed after the import time, any supplied `annotated_at`, any
  text field, a claim with two actions or two subjects, a quantity without `of`, and non-http(s)
  URLs.
- **A corrupt run** (digest or count mismatch) blocks imports and views.
- **`research-view`** writes the sanitized projection that `news-research.js` renders. It contains
  allowed fields only, http(s) links only, bounded text, and every time with its kind and precision.

## Prepared records

`tests/fixtures/news/research/positioning_p1_prepared.json` holds records A–F of the accepted design
v2, with their corrections and uncertainty:
- **A:** Goldman net is net/equity.
- **B and C:** split into one action per claim. Arini's $800M is dropped, and the redemption is
  `unverified`.
- **D:** November 19 is a bid deadline. The unread Bloomberg Law update is a separate capture.
- **E:** the thesis-email assertions are recorded as relayed, `thesis_assertion` / `unverified`. The
  access time is an upper bound.
- **F:** the 13F FAQ. 13F holdings are long-only, as of quarter end.

The access times are the 2026-11-12 tool-call times recorded in the design note. Nothing was fetched
again.

They are research records, **not proof** that any market claim or thesis is true. The view says so.

Hypothesis timing follows the real commit. Imported on 2026-11-13, D stays contemporaneous (its
horizon starts 11-19). E becomes retrospective, because its horizon started 11-13; the design note's
example had assumed an 11-12 commit.
