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
   shows `lanes=['cloudflare_deploy', 'web_ci']` and `auto=[]`.
2. `web_ci` is green for the commit being promoted, not for an earlier one.
3. No producer appears in `auto` or `manual` for this change. If one does, the
   change is a producer release; stop and schedule it deliberately.
4. The Worker version currently serving is recorded, so the rollback target is
   named rather than remembered.
5. Producer deployment IDs and instance identities are recorded before the
   canary, so "unchanged" is a comparison rather than an impression.

A change that ships the website alone cannot create a collector observation gap.
A change that promotes a producer during a live session does, and no checklist
item makes that gap go away.

## Where a preview lane will attach

G3a's outstanding dependency is G1, Jayden's preview contract. When it lands, a
preview deployment is a repository lane rather than a promotion: it gets an
entry in `LANE_WORKFLOWS` if it has its own workflow file, and no entry in the
promoting set. If previews turn out to be provider-native (Cloudflare preview
settings rather than Actions), it cannot be a repository lane at all and belongs
in the provider table with the other readback questions. Either answer is a
small explicit change here rather than a rewrite, because the matcher already
separates "runs" from "promotes".

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
