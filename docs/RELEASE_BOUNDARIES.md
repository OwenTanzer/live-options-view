# Release boundaries

Which changes can reach production, through which route, and who owns each
route. This is the C1 deliverable for
[#225](https://github.com/OwenTanzer/live-options-view/issues/225); the
executable half of it lives in [`tests/changed_paths.py`](../tests/changed_paths.py)
and [`tests/test_changed_paths.py`](../tests/test_changed_paths.py).

Repository facts below were read from the working tree at
`90cdc56c207ad0c733bb2f1b1bb15d995472bf47`. Provider facts come from the #225
readback of **2026-11-11T17:27Z** (09:27 Pacific) and the latest-deployment
metadata in the 2026-11-11 vulnerability report (09:34:47 Pacific receipt).
Lines marked *readback* were not re-inspected while writing this document and
are not verified here.

## Two kinds of lane

**Repository lanes** are workflow files. Reading the file tells you what runs.
The test suite parses them, so an edit that changes a filter fails the suite
until this document and the expectations are updated together.

| Lane | Workflow | Promotes? | Trigger paths |
|---|---|---|---|
| `cloudflare_deploy` | `deploy.yml` | yes, straight to production | `site/**`, `backend/**`, `wrangler.toml`, `scripts/stamp-build.mjs`, `tests/build_stamp.test.mjs`, the workflow itself |
| `web_ci` | `web-ci.yml` | no | `backend/**`, `site/**`, `tests/**`, `wrangler.toml`, both stamp/quote scripts, both workflows |
| `collector_ci` | `collector-ci.yml` | no | `market_data/**`, `deployment/python/**`, named producer tests and research modules, the workflow itself |
| `crassus_ci` | `crassus-ci.yml` | no | `crassus/**`, the workflow itself |
| `synthetic_ci` | `synthetic-days-ci.yml` | no | `research/synthetic_days/**`, the workflow itself |

**Provider lanes** are settings in Railway. No file in this repository can
prove them, so they are recorded as a dated baseline and every one of them is a
readback question, not an established fact.

| Service | Watch scope *(readback)* | GitHub trigger *(readback)* | checkSuites | cron | Entry point |
|---|---|---|---|---|---|
| `main-collector` | `market_data/live_market/**`, `market_data/overview/**`, `market_data/__init__.py`, `deployment/python/**` | present | true | none | `python -m market_data.live_market` |
| `crassus-runner` | `crassus/**` | present | true | none | `python -m crassus.runner --interval 300` |
| `tradier-collector` | `market_data/tradier/**`, `market_data/diagnostics.py`, `market_data/__init__.py`, `deployment/python/**` | absent | — | `15 12 * * 1-5` | `python -m market_data.tradier` |
| `banana-options-scanner` | empty list | absent | — | `45 12 * * 1-5` | `python -m market_data.options_scanner` |

An empty watch list is not a narrow scope. It selects nothing today and
everything the moment a trigger is reattached, which is why #225 treats Banana's
list as a dormant hazard rather than a safe default.

Two further services exist in the same project and are not part of this
matrix: the `moo144` probe is stopped with an `EXITED` instance and no cron, and
`r2-paper-trades-recovery` is running with an embedded command, no cron and
unresolved authority. Neither is a repository deployment until someone states
otherwise.

## Build, promotion and execution are three separate events

The producers share one image. `deployment/python/Dockerfile` copies all of
`market_data/`, so editing Banana's scanner changes the image digest of the main
collector and the Tradier collector too. That does **not** mean those services
deploy, and it is not a reason to restart them. It does mean a rebuild of one
service picks up every other service's unreleased code, so promotion decisions
must name the exact commit rather than "the image".

Three relations are therefore tracked separately, and the tests assert all
three:

- **watch** — the provider would make this commit a deployment candidate.
- **runtime** — the file is inside the entry point's transitive import closure.
- **image input** — the file is copied into the deployed image.

`deployment/python/**` and `market_data/__init__.py` are shared build inputs:
they affect all three Python producers regardless of what each one imports.
Everything else under `market_data/` moves all three digests while affecting
only the service whose closure contains it.

## Import closure

Computed from `__main__` entry points, with package initializers included
because importing a submodule runs them.

| Service | Files executed | Covered by its watch scope |
|---|---|---|
| `main-collector` | 26 | yes |
| `crassus-runner` | 42 | yes |
| `tradier-collector` | 17 | yes |
| `banana-options-scanner` | 29 | no scope exists yet |

Banana's closure reaches outside `market_data/options_scanner/`:
`market_data/__init__.py`, `market_data/diagnostics.py`,
`market_data/tradier/__init__.py` and `market_data/tradier/probe.py`. A
scanner-only watch is therefore provably incomplete, and the test asserts those
four files rather than trusting the argument. The candidate scope in #225
(`market_data/options_scanner/**`, `market_data/tradier/**`,
`market_data/diagnostics.py`, `market_data/__init__.py`,
`deployment/python/**`) covers the entire closure.

## Routes

**Automatic.** A push to `master` matching `deploy.yml`'s filter runs the
Cloudflare promotion. A push matching the main collector's or Crassus's watch
scope becomes a Railway deployment candidate gated on check suites. Both
provider gates are readback claims, not repository guarantees: `deploy.yml` has
no `concurrency` group and no `needs:` edge, so two pushes can promote out of
order and neither waits for `web_ci`. What the repository *does* guarantee is
that every path which can promote is also a path that runs `web_ci` — that
subset invariant is asserted by the suite and fails if `deploy.yml` grows a path
`web-ci.yml` does not select.

**Manual.** `wrangler deploy` from a workstation; Railway redeploy, restart and
rollback; Railway staged changes committed without redeploy; a manual rebuild of
a producer image; a manual cron trigger. None of these consult the watch matrix
or the changed-path tests. A scheduled service promoted this way still fails
closed outside its run window, which is correct capture safety and looks like a
red deployment.

**Configuration.** `wrangler.toml` (bindings, D1 database, cron trigger),
`wrangler.recovery.toml` (a deliberately separate recovery Worker bound to live
storage), `crassus/railway.toml` (Dockerfile builder and start command), the two
Dockerfiles and their ignore files, provider watch patterns and check-suite
settings, and environment variables. The first three are repository files; the
last two are provider state that no commit can change.

**Rollback.** The website rolls back by deploying a previous revision that
reads D1. It must never roll back to pre-cutover KV-serving code; the KV
namespaces still hold data but are not bound to the production Worker. A
producer rollback is a Railway rollback to a previous deployment, and it
necessarily creates an observation gap that recovery cannot reconstruct. A
successful restart is not evidence of continuous capture.

## Dispositions

**Prose inside runtime directories still restarts the runtime.**
`market_data/live_market/README.md` and `crassus/README.md` are inside their
services' watch scopes, so editing them promotes. Encoded as current behaviour,
not as intent. Narrowing it is a provider watch-pattern change, which belongs to
Nova's integration surface; a repository edit cannot fix it.

**The stale deployment contract is itself a deployment.** `site/DEPLOYMENTS.md`
is dated 2026-09-03 and describes entry points and settings that no longer
exist. It also sits inside `site/**`, so reconciling it triggers the very
promotion it documents. This document is the reconciliation, placed outside
every watch scope. Replacing the old file with a pointer should travel with
Nova's next site change rather than being its own release.

**Ordering.** The subset invariant holds; the dependency edge and the
`concurrency` group do not exist. A patch that adds either belongs in the
integration review, not in this record.

**Banana.** No trigger restoration is proposed here. The closure evidence
supports #225's candidate scope and refutes a scanner-only one.

## Release checklist

Before any site or backend change ships during market hours, confirm all of the
following on the exact candidate revision.

1. `python tests/test_changed_paths.py` passes, and the case for this change
   shows the lanes you expect and `auto=[]` for the producers.
2. The artifact classification is the one you want: an asset change shows
   `artifact=asset` and `preview_stale=True`; a backend change shows
   `artifact=api` and `preview_stale=False`; a commit showing
   `needs_integration_review` is a mixed change and goes to Nova rather than
   straight out.
3. `web_ci` is green for the commit being promoted, not for an earlier one.
4. No producer appears in `auto` or `manual` for this change. If one does, the
   change is a producer release; stop and schedule it deliberately.
5. The Worker version currently serving is recorded, so the rollback target is
   named rather than remembered.
6. Producer deployment IDs and instance identities are recorded before the
   canary, so "unchanged" is a comparison rather than an impression.

A change that ships the website alone cannot create a collector observation gap.
A change that promotes a producer during a live session does, and no checklist
item makes that gap go away.

## The asset/API split (tested against Musef's actual tree)

Musef's G1/G2 tree (`bff49eed` on `origin/musef/225-assets-preview`) puts a static
asset gateway in `frontend/` that forwards the reserved API paths to a
separately bound API service, with every browser URL and relative `/api` call
left alone. None of the topology is applied: production still runs the combined
Worker, and the provider facts it needs — service binding, name split, trigger
ownership, disposable resources, D1 readback — remain unverified under Nova's
hold. What the tree does settle is which artifact a change belongs to, and the
packager decides that, not a directory sweep.

`scripts/build-assets.mjs` copies `site/` through a thirteen-extension allowlist
(`.html .js .css .json .svg .png .jpg .jpeg .ico .woff .woff2 .txt .webp`) and
then exactly two gateway files, `frontend/worker.mjs` and
`frontend/preview-policy.mjs`. It also reads the two migrations under
`backend/migrations/` for the hashes it embeds in `release.json`. Extension
matching is case-sensitive here because it is case-sensitive there:
`site/index.HTML` is not packaged, so it is not payload here either. Those rules,
read out of the script itself, are what the classifier applies:

| Change | Payload | Tooling | Contract | Config | Acceptance | API | Shared |
|---|---|---|---|---|---|---|---|
| `site/**` with an allowed extension | yes | no | no | no | yes | no | no |
| Markdown under `site/` or `frontend/` | prose | no | no | no | no | no | no |
| `frontend/worker.mjs`, `frontend/preview-policy.mjs` | yes | no | no | no | yes | no | no |
| any other file under `frontend/` | no | no | no | no | no | no | no |
| `scripts/build-assets.mjs`, `prepare-asset-release.mjs`, `stamp-build.mjs` | no | yes | no | no | yes | no | no |
| `scripts/preview-local.mjs`, `tests/helpers/**`, `tests/preview_*.mjs` | no | no | no | no | yes | no | no |
| `backend/migrations/0001…`, `0002…` | no | no | yes | no | yes | yes | no |
| `backend/**` otherwise | no | no | no | no | yes | yes | no |
| `wrangler.assets.toml`, the fallback template under `patches/musef-225/` | no | no | no | yes | yes | no | yes |
| `wrangler.api.toml`, `wrangler.toml`, recovery, workflows | no | no | no | no | yes | no | yes |

Measured on that tree: 715 tracked files, 31 artifact entries, and no path
classified as payload that `release.json` does not list, or the reverse. Eighteen
Markdown files sit inside `site/` today, so the prose class is not hypothetical.
A migration change is both an API change and a rebuild trigger: it ships nothing
to the artifact, but the artifact's recorded schema hashes go stale, which is why
`needs_asset_rebuild` is separate from `payload`.

**Two kinds of staleness, not one.** `preview_stale` asks whether a built preview
would serve different bytes, so it turns on packaged content, the tooling that
assembles it, and the configuration that serves it. `acceptance_evidence_stale`
asks whether the preview and A1 evidence must be re-run, and it also turns on the
real handlers under `backend/`, the preview constructor in
`scripts/preview-local.mjs`, the shared helper the preview imports, and the
executed suites. A backend change is therefore an API change and an
acceptance-stale change while remaining no part of asset payload: the preview
composes those handlers at the revision it ran against, and a helper or suite edit
retires the evidence without shipping a byte.

**Configuration evidence has its own clock.** A staged plan records
`config_source` and `config_sha256` for whichever file was selected — the applied
root `wrangler.assets.toml`, or the labelled fallback template under
`patches/musef-225/` while none exists. Either candidate changing makes that
record a statement about the past even when the artifact is untouched, so
`config_evidence_stale` is tracked apart from `needs_asset_rebuild`. The stager
itself prefers the applied config, refuses an invalid present one rather than
falling back, and names the fallback in the plan; what it cannot know is whether
the file it named has changed since.

**Prose is classified, not assumed.** `site/DEPLOYMENTS.md` and the seventeen
plan and operation notes beside it are documents that live inside the asset
tree. The design asks for that to be explicit rather than a side effect of a
glob, so it is its own class: it cannot change the artifact's bytes and it is
not an API change. Today it still fires the combined workflow, which is the
coupling the split removes; after the split it rebuilds static bytes and cannot
reach the API.

**A commit spanning both artifacts is a reviewed mixed change.** `backend/` and
`site/` in one commit belongs to both artifacts and to neither workflow alone,
and so does any edit to the shared configuration that decides how either is
built or promoted. The classifier marks those rather than letting one lane carry
the other silently.

**Preview is stale on anything that determines its bytes.** A preview built from
a commit's inputs is out of date when packaged content, the tooling that
assembles it, or the configuration that serves it changes — and not when the
API, a test, a document or prose changes. The identity the design hands to Steve
is `ENVIRONMENT=preview` with `ISOLATION=disposable`, `/r2-proxy/` denied, and
the API reached through local handlers over disposable D1 and memory stores.
Those are runtime properties, not path selection: they belong to the asset and
preview suites, and this record only names them so the two do not drift apart.

**Proposed selectors live in a patch.**
`patches/brian-225/deploy-selectors.patch` adds `frontend/**`,
`scripts/build-assets.mjs` and `wrangler.assets.toml` to `deploy.yml`; to
`web-ci.yml` it adds those three plus `wrangler.api.toml`, the labelled fallback
template under `patches/musef-225/`, and `scripts/prepare-asset-release.mjs` with
`scripts/preview-local.mjs`. The split is deliberate: the packager assembles the
artifact and the applied asset config decides how it is served, so those two are
promotion candidates, while the stager, the preview constructor, the fallback
template and the API config are inputs to a release or to its evidence and are
tested without ever deploying. Those `web-ci.yml` lines match Musef's
`patches/musef-225/web-ci-only.patch`, so Nova can consolidate the overlapping
hunk without choosing between two versions of it.

**Coverage is exhaustive, not sampled.** Both guards enumerate the tracked files
a filter could select and require the test workflow to select each one. A sample
per pattern was the earlier design and it could be fooled: with `frontend/**`
sampled as `frontend/preview-policy.mjs`, a test filter listing only that file
passed while `frontend/worker.mjs` shipped untested. The same shape applied to
promotion, where a filter matching a synthetic `site/x` looked covered while
`site/index.html` was never selected. Both are now caught, and a file added later
under any input root fails the guard the moment it is committed, which is the
new-path control rather than a separate test.

Run against Musef's `bff49eed` tree without the patch, the guard names all six
inputs that exist there and that neither workflow selected; with the patch the
same suite passes. Both runs are in the return.

**The deployed asset config points at a staged layout.**
`patches/musef-225/wrangler.assets.toml.proposed` names
`main = "payload/frontend/worker.mjs"` and `directory = "payload/site"`, because
`prepare-asset-release.mjs` copies a verified artifact into `payload/` before
`wrangler deploy` runs. So the asset workflow cannot simply deploy the checkout:
it has to build the artifact, verify it, then deploy the staging directory. That
sequencing is Nova's G3b, and this patch deliberately does not attempt it — a
selector change that assumed a staging convention would break the first time the
staging layout moved.

**The gateway may not import the API.** A static gateway that reaches into
`backend/` drags operational code into the asset bundle whatever the path
filters say, so the suite scans `frontend/` for any import whose specifier names
`backend/` and fails on one. On the base revision there is no `frontend/` tree
yet, so the check is vacuous until the design's files arrive; against
`bff49eed` it is live and clean, with the one `backend/` import in the tree
belonging to the preview constructor rather than to the gateway.

**The browser must not address the API by absolute URL.** The design keeps every
browser URL and relative `/api` call unchanged, and Steve's preview contract
forbids a URL-based production fallback. Both hold only while the page says
`/api/...`: an absolute origin would let a preview call production, and would
keep working after the topology split precisely because it bypassed the
gateway. The site is clean today, every call relative, so the check has no
exceptions to carry.

## Open questions for the provider-side readback
Each of these is a readback, not a design decision. Until it is answered the
corresponding conclusion stays pending and the local work continues.

1. Do the four service watch lists match the table above exactly, including
   whether `market_data/__init__.py` is named for `tradier-collector`? The
   closure requires it; #225's prose says "package initializer" without saying
   which one.
2. Is the `main-collector` watch really `market_data/overview/**`, or only the
   files its entry point imports? The directory-wide form is what makes an
   overview README a producer release.
3. Are check suites still enforced for `main-collector` and `crassus-runner`?
   Earlier records claim two attempts to enable "Wait for CI" were discarded.
4. Which Railway services are built from `deployment/python/Dockerfile` today,
   and which carry their own builder? The image is shared; the builders may not
   be.
5. Is `ignoreWatchPatterns` still true on the Tradier and Banana deployment
   metadata, and is `buildOnly` still set on both?
6. Does any Cloudflare Builds trigger exist and is it active, or is
   `deploy.yml` the only promotion path? The read-only diagnostic workflow
   answers this without changing anything.
7. Which environments exist below production, and do their bindings differ from
   production storage? `wrangler.toml` declares none.
8. Is the `r2-paper-trades-recovery` utility still needed, and under whose
   authority?

## Not verified here

No Railway, Cloudflare or GitHub provider settings were inspected. No collector
was restarted or launched. No deployment, rollback, configuration apply or
canary was performed. The build stamp served by the live site was not compared
to `master`. Nothing in this document establishes that a trigger is attached,
that a schedule fired, or that capture is healthy.

—Brian 🦡 with Chej, 2026-11-11
