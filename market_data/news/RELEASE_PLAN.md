# MediaFlow News: release and rollback plan (R4 draft)

**Status (2026-11-14): code landed and web release live; producer inactive.** Owen granted full
delivery authority for MediaFlow (Chej, 2026-11-13). Chej coordinates merge and release operations and
provider access; production runs go through the protected "production" environment. Done: the landing
(section 1) and the web release (section 5, steps 1–2). Not done: provisioning, provider checks, the
config flip, the first cycle and the 15-minute schedule. New paid services or extra spending stay
Owen's decision. Accepted engineering stays accepted; this plan adds no new finish line. It is drawn from `PUBLISHING.md` (operations, generations,
settlement), `RESEARCH.md` (research publication) and the accepted local evidence. No new tests
were run for it.

## 1. Candidate

| | |
| --- | --- |
| Repository | `OwenTanzer/live-options-view` |
| Landed | PR #243, reviewed head `455f117e13a8865bc5e86127592ada6edfda4b49` (15 commits on master `4629da6`), merged as `55db03606bf18869b581c931cea75c23a9a1d49c` |
| Accepted local history | `d2dd21c` on `feat/news-p1-wiring` (13 commits on the transplant base `6df6442`: `1d62d3c` G1 → G3 `24da6cb`, `7610760`, `38e9995`, `ccefb6f` → G6 `98c8d80`, `26b6739` → G7 `b02d664` → P1 `4915d51` → P1d `ba609b1` → R1/R2 `e54467b` → corrections `6836414`, `d2dd21c`); the landing is that chain rebased, plus the R4 plan commits |
| Size vs base | 89 files, +22,598 / −21. Shared (non-`market_data/news`, non-test) files: `backend/browser-data.js`, `frontend/preview-policy.mjs`, `site/index.html`, `site/js/boot.js`, `site/js/tab-switching.js`, `site/js/news-panel.js`, `site/js/news-research.js`. **No workflow file is changed.** |
| Acceptance | Each stage has bounded local/static acceptance (see the quest Log), and it stays accepted. The evidence is contributor-run: 319 Python tests, 62/64 Node files with 2 base-level nonzero results, and 18 rendered checks at `e54467b`, plus the focused rechecks at `6836414` and `d2dd21c`. The R3 cases and any all-suite rerun are **deferred, separately authorized work**, not landing gates. A1–A8 are not verified (section 3). |

### Landing (done: PR #243, merge `55db036`)

What the landing involved, kept for reference:

1. **Bring the candidate onto the current `master`.** The base `6df6442` predates later merged work
   (for example the E1 price-feed fix) and the pending H1 Skew branch. The shared files to reconcile
   are `site/index.html` and `backend/browser-data.js`.
   - Earlier test merges of H1 (`7d123de`) touched different hunks and merged cleanly. That was a
     throwaway merge, not an integration.
   - Shared-file and landing coordination (D7) is Chej's to reconcile. It is not automatically an
     owner decision.
2. **Recompute both News script stamps** after the landing merge, if either file changed.
   - The rule is `?v=` = the first 16 hex characters of the file's SHA-256 (pinned by
     `tests/news_research_view.test.js`).
   - At `d2dd21c`: `js/news-research.js?v=aac117405484746a` and `js/news-panel.js?v=97258fdf37938322`.
   - `news-research.js` must stay loaded before `news-panel.js`.
3. **Deferred, separately authorized checks**, not newly mandatory gates:
   - all-suite reruns on a landed revision;
   - the R3 cases: cross-process lease races, a late head write, a mounted reload into a tombstone,
     high growth and incarnation 9999, W5/T4/T5, and a corrupt research commit record.
   Whether and when to run them is decided separately.

## 2. Settled policy (carried forward, not open)

- **Page cap 6,300**, unchanged. The pilot maximum is 1,261 pages per generation (1,260 view pages
  + 1 research page). At that maximum the fifth admission pauses instead of evicting. That is an
  **availability limit**.
- **Quarantine is permanent and keeps the full reservation.** An incarnation with an unanswered
  page upload keeps its head entry and its whole page reservation; nothing in code releases it.
- **Withdrawal is confirmed by the index only.** A tombstone read back live confirms it. Pages
  that land after deletion may reappear, named by no index, and every later withdraw or cleanup
  deletes them. This has no time bound.
- **Retention:** reader grace 45 min (+120 s epsilon, +120 s skew = 49 min to delete); 7 UTC dates
  per generation; 3 snapshots; the raw archive is immutable and kept.
- **No Workers KV** (#208). Prices stay unavailable at first activation (D4). The only supported
  real input is the collector's provenance sidecars, read-only, **last-trade selections only**
  (`python -m market_data.news prices-from-sidecars`, `adapters/sidecar_prices.py`). Midpoints are
  excluded because they have no provider time.
- **The research view is fixed:** one per publication, in research mode at the build cutoff (D1).

## 3. Prerequisites before any activation

### Provider checks (separately authorized)

| Item | What must be shown on the real provider |
| --- | --- |
| A1 | Two different bodies never share an ETag. |
| A2 | `put_object` honours `IfMatch` / `IfNoneMatch`, returns the new ETag, and a failed condition surfaces as 412 / `PreconditionFailed`. |
| A3 | The no-retry client (`total_max_attempts = 1`) sends exactly one request per call. |
| A4 | A `GetObject` body and its ETag describe the same version. |
| A5 | The private bucket has the same conditional-write behavior. |
| A6 | An unanswered PUT or delete may land later with no bound. This is the safe reading, so it needs no proof, but it should be confirmed as the behavior the design assumes. |
| A7 | Only this publisher writes `news/v2/views/**` and `news/v1/publish/**`. |
| A8 | The head and commit records are durable. |
| Clock bound | The store clock's error is within `clock_epsilon_s` (120 s). See the expiry consequence in section 5. |
| Public exposure | Confirm that the public R2 origin exposes the whole bucket it fronts. If it does, the raw archive and research ledger must live in a **separate private bucket**. |
| Index caching on direct R2 | The index is written with `Cache-Control: no-store`. Confirm the public origin/CDN honours it; the proxy forces no-store regardless. Generation pages are immutable, so their long cache is correct. |
| Prefix-scoped tokens | Whether public-bucket credentials can be limited to `news/v2/views/`. If not, the code-level key guard (`PublicViewStore`) is the only fence. |

### Genuine pre-live decisions

These are the decisions a pinned-research, unavailable-price pilot actually needs:

1. **Buckets and credential scope.** Which private and public buckets; whether credentials can be
   limited to `news/v2/views/` (D5).
2. **Host and run authority.** Which scheduler host runs the producer, and who may start, stop and
   resume it (D6).
3. **Provider and clock prerequisites.** Which of the section-3 checks must pass before the first
   cycle, including the store clock bound.
4. **Whether a no-research pilot is acceptable.** That is, a pilot whose research entry is `none`
   (see "Research in a live pilot" below).

Deferred, not blockers to that pilot:
- **D1:** extra research modes or cutoffs, or a picker. The pilot keeps the pinned view.
- **D4:** a real price input. The pilot keeps prices unavailable.
- **D2:** live research sources, the excerpt policy and access questions 1–5.

D7 (shared-file and landing coordination) is Chej's to reconcile. D3 is settled: see section 2.

### Infrastructure (Owen)

- A **private bucket** for `news/v1/runs/**`, `news/v1/index/**`, `news/v1/latest.json`, leases,
  `news/v1/publish/**` and `news/v1/research/**`, with its credentials.
- The **public bucket** the site already reads (behind `/browser-data/` and the public R2 origin),
  with credentials scoped as narrowly as the provider allows.
- A **scheduler service** (for example a new Railway cron service).
  - Cron `*/15 * * * *` UTC, one-shot, restart policy NEVER.
  - Watch paths must not overlap the collectors' (#225).

### Research in a live pilot

- **Why it would be empty.** `research-import` and `research-settle` write **local stores only**;
  they have no `--bucket` option. A **fresh, unseeded** private ledger therefore holds no research
  runs, and each publication from it would carry `research.state = "none"` ("No research view
  published").
- **What this does not say.** It is a statement about a fresh ledger under this code. It is not
  evidence about what any current remote bucket holds.
- **Two separate questions:**
  1. Is a **no-research pilot** acceptable? Then nothing more is needed.
  2. Should the pilot carry research? That needs a **separately authorized import path** into the
     private bucket, plus the D2 source and excerpt decisions.

## 4. Exact configuration at activation

`market_data/news/deploy/news-pilot.json` is the only configuration file. These are the changes, made
as one reviewed commit, with release coordinated by Chej:

| Field | Now | At activation |
| --- | --- | --- |
| `active` | `false` | `true`. This is the only field that unlocks `--live`. |
| `storage.private.bucket` / `storage.public.bucket` | `null` | The bucket names. These are **descriptive**: the code reads the buckets from the environment, not from these fields. |
| `generations.clock_bound_verified` | `false` | Stays `false` until the clock-bound check passes, then `true` in a separate reviewed change. |
| `research.*` | mode `research`, cutoff `build_as_of`, PREPARED label | Unchanged. The loader refuses any other mode or cutoff. Revisit the label if live research sources are added (D2). |
| everything else (schedule, sources, streams, views, prices `unavailable`, `storage.public.prefix` `news/v2/views/`, retention, cap 6,300) | as shipped | unchanged |

Environment of the scheduler service:
- `NEWS_ALLOW_BUCKET_WRITES=1`;
- `NEWS_PRIVATE_R2_ACCOUNT_ID`, `NEWS_PRIVATE_R2_ACCESS_KEY_ID`, `NEWS_PRIVATE_R2_SECRET_ACCESS_KEY`,
  `NEWS_PRIVATE_R2_BUCKET_NAME`;
- the same four with `NEWS_PUBLIC_R2_`;
- optionally `NEWS_GIT_REVISION`.

The command is the config's `schedule.command`:
`python -m market_data.news pilot-cycle --config market_data/news/deploy/news-pilot.json --live`.
`--live` is refused unless `active` is `true` **and** `NEWS_ALLOW_BUCKET_WRITES=1` is set.

## 5. Activation order

Covered by the delivery authority. Chej coordinates each release step, and each step is checked
before the next one starts.

1. **Land the code** (section 1). **Done:** PR #243 merged as `55db036`; the stamps were unchanged.
   The config is still inactive, so the producer cannot write.
2. **Release the web side: API Worker first, then assets** (two lanes, both `workflow_dispatch`
   behind the "production" environment; mapped from the wrangler configs):
   - **API lane, `deploy.yml`** ("Deploy API Worker"): `wrangler.api.toml` → Worker
     `live-options-api`, main `backend/index.js`, which imports `backend/browser-data.js` (the
     `/browser-data/` proxy and the News key rule).
   - **Asset lane, `deploy-staged.yml`** ("Deploy staged asset payload"): `wrangler.assets.toml` →
     Worker `live-options-view`, main `payload/frontend/worker.mjs` (imports `preview-policy.mjs`),
     assets `payload/site` (`index.html`, `boot.js`, `tab-switching.js`, `news-panel.js`,
     `news-research.js`).
   - API first: the asset lane checks that the API service exists, and the new site needs the proxy
     to admit the research page key (until then research shows `refresh-required` on the proxy).
   - **Done 2026-11-14** at `55db036`: API run 43561200028, assets run 43561527996.
   - **Observed empty states** (2026-11-14 ~07:20Z, desktop 1440 px and mobile 390 px): through the
     proxy, "No news views published yet" and "No research view published". Direct R2 serves the
     same 404s, but a browser page off the production origin cannot read r2.dev at all: the
     public origin sends no CORS headers, so the panel there shows "Could not load news views …
     Failed to fetch" (a visible failure, not a false empty state; the Live tab fails the same way).
     The production origin always uses the proxy.
   - With nothing published, the News tab shows "No news views published yet" and the research view
     shows "No research view published". Check both on the proxy and on direct R2.
   - The retired `news/v2/research/view.json` and every `news/v1/**` key must return 404.
3. **Provision** the buckets, credentials and scheduler (paused), and run the provider checks in
   section 3.
4. **Flip the config** (section 4) and set the environment. Keep the scheduler paused.
5. **First cycle**, either one scheduler run or one supervised run, then:
   - `publish-status` exits 0;
   - the index is `published`, with `gen` = the head's pinned incarnation;
   - the pages and the research entry (`none` from a fresh, unseeded ledger: see "Research in a live pilot") render on both bases;
   - receipts are written;
   - the public bucket has nothing under `news/v1/`.
6. **Unpause the 15-minute schedule.** Proposed: watch `publish-status` each hour on the first
   day, and a review by Chej after the first live day.
7. **Clock bound.** With `clock_bound_verified: false`, replaced generations are never deleted
   (`expiry_blocked_clock_bound_unverified`), so pages accumulate against the cap until the safe
   pause (`publication_paused`); nothing is evicted.
   - **Illustration only, not a deadline:** at 64 pages per generation and one generation every
     15 minutes, 6,300 pages last about 98 cycles, about 24.5 hours.
   - The real time depends on page growth per generation, reservations (including quarantined
     incarnations, which keep theirs) and whether the research entry is `none`.
   - The 6,300 cap and the safe pause stay unchanged. Whether the clock bound is verified before or
     during the pilot belongs to the provider/clock prerequisites decision above.

## 6. Stop, withdraw, restore and roll back

All commands take `--config`, `--live` and the environment above. `publish-status` lists the
`gen_id` values (`snapshots`) and the problems.

| Situation | Procedure | Result and limits |
| --- | --- | --- |
| Pause everything | `publish-stop --reason "..."` | Cycles no-op (exit 0). Views stay as last published and age visibly (`build_as_of`). |
| Resume | `publish-resume` | The next tick collects and publishes. |
| Roll back the views | `publish-stop`, then `publish-restore --gen <gen_id>` | Reuses the still-pinned incarnation (a new index, fresh nonce) or writes a fresh incarnation from the snapshot (counted against the cap; refused at the cap). The research page is part of the snapshot and is re-qualified with it. Holds until `publish-resume`. |
| Close an open index commit now | `publish-reseal` | Rewrites the live index with a fresh nonce. When that lands, every open commit is dead. |
| Take everything down | `publish-stop`, then `publish-withdraw`, repeated while it reports `withdrawal_unconfirmed` | A tombstone over `index.json`; every page deleted, the research page included; the panel shows "News views are not currently published" and research "Research records are not currently published". Index-only confirmation; cached copies and downloads cannot be recalled; snapshots and the raw archive are kept. |
| Take down research only | No dedicated command. Either withdraw everything, or roll back to a generation whose research entry is `none` or `omitted`. | A research-only takedown needs a code change. The ledger is immutable. |
| Abandoned lease | `PUBLISHING.md`, "Recovering from an abandoned lease": confirm the holder is gone, wait for `expires_at`, then run once with `--break-stale-lease`. | An unexpired lease is never taken over. |
| Code rollback | Redeploy the previous revisions through both lanes: `deploy.yml` (API Worker) and `deploy-staged.yml` (assets). Roll back assets first, then the API, so that no older Worker runs under the newer site. | Through the proxy, a mismatch degrades visibly. A site newer than its Worker shows research `refresh-required`, because the pre-R1 Worker refuses the research page. An older site with a newer Worker requests the retired key, gets 404 and shows "No research view published". Direct R2 does not depend on the Worker. The raw archive is append-only, so the producer can be rolled back independently, and views are rebuilt on the next cycle. |
| Producer misbehaving | `publish-stop` first, then investigate with `publish-status` and the receipts. | Never delete objects by hand. A deleted head, commit record or page breaks settlement. |

## 7. What to watch

`publish-status` exits 1 on any problem. During the pilot, read the `PUBLISHING.md` list. In
particular:
- `published_views_stale` (older than 2 × 15 min + 180 s);
- `publication_paused` (cap or withdrawal);
- `open_index_commits`;
- `page_uploads_unsettled` (quarantine; permanent by policy);
- `withdrawal_unconfirmed`;
- `expiry_blocked_clock_bound_unverified`;
- `unknown_public_generations`;
- `stale_writer_lease` / `stale_publish_lease`.

Readers see `build_as_of`, the coverage gaps and "prices unavailable" on every page.

## 8. Ownership

| Role | Who |
| --- | --- |
| Merge and release coordination across MediaFlow, Emergency Repairs and Skew; bucket mapping, scoped access, scheduler host | Chej |
| Production environment approvals; new paid services or extra spending | Owen |
| Code and corrections, the price adapter, verification evidence, `publish-status` checks, stop/restore/withdraw when asked | Jayden |
| Review before activation and after the first live day | Chej |
