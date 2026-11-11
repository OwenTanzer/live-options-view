# G1: independent asset boundary (#225)

Design return, Musef, 2026-11-11 approximately 14:06 Pacific (initial heading said 14:08; Notion creation metadata records 22:06 UTC). Proposal ready for partner implementation; not acceptance, deployment or verified provider topology.

## Exact source and ownership

Base `90cdc56c207ad0c733bb2f1b1bb15d995472bf47`, branch `musef/225-assets-preview`, isolated LF worktree `C:\Users\Owen\Apps\lov-musef-225-assets-20261111`. Main checkout is an older quest branch and is not this base. Brian's G3a baseline is `280f4035` (48 tests reported); Steve's current G4a is `128ed46`, superseding `e1d50f4`. Neither is merged here. R1/R2 repairs stay independent.

## Choice

Keep the monorepo. Introduce a tiny asset gateway in `frontend/worker.mjs`, with no backend imports, storage bindings, cron or producer dependencies. Keep browser URLs and relative API calls unchanged. Forward reserved API/data paths through a service binding, retaining the original Request URL, Origin, Cookie and body and the response's Set-Cookie/status. Static release bundles only site assets plus this gateway, never backend code. Ordinary asset rollback swaps the asset artifact only; API code, D1 schema, cron and producers remain fixed.

One-time topology change (Nova applies only after C2): the existing public `live-options-view` name/origin becomes the asset gateway; operational code runs as a separately named `live-options-api` service binding target. Existing hostname must not redirect to an API hostname. No claim is made that Workers.dev permits a zero-gap transfer; Nova must verify version/route transitions and preserve the old combined Worker until the switch can be reversed. A provider route can instead retain a stable edge dispatcher if necessary, with the same two independent artifact contracts. No tested equivalent to asset separation is substituted here.

## Compatibility table

| Surface | Baseline | Proposed production | Isolated local preview |
|---|---|---|---|
| `/`, `.html`, JS/CSS, diagnostics | combined backend asset fallback | gateway ASSETS, same paths; HTML no-store | same assets; environment header/banner and identity endpoint |
| `/api/register`, login/logout/me | same-origin D1 session auth | original URL/Origin/Cookie forwarded to API binding; host-only cookie unchanged, no CORS change | same handlers with disposable SQLite D1 shim and in-memory operational store; no production binding |
| `/api/bots`, bot-metadata/reconcile, paper-trade/settle | combined Worker | all `/api` namespace reserved to API; never static fallback | local API only, unknown API remains rejected; no network fallback |
| `/api/live-quotes` | backend uses quote origin/key | API binding retains existing quote contract | deterministic quote fixture; no production quote credential |
| `/browser-data/*` | allowlisted public R2 reads, hard-coded public origin check | original public URL retained through API binding | PUBLIC_DATA binding with approved read-only fixture; Nova owns future live allowlist adapter, never API fallback |
| `/r2-proxy/*` | arbitrary read proxy at fixed R2 origin | delegated unchanged initially; Nova owns G4b disposition | explicitly denied; no generic proxy |
| cookies | host-only HttpOnly, SameSite, Secure on HTTPS | unchanged host/path/attributes; no URL rewrite | localhost host-only cookie, disposable session DB; foreign session tokens cannot authenticate against fresh local DB; public-read credentials stripped |
| settlement schedule | combined Worker's scheduled export | API service alone retains exact cron; gateway none | no cron/scheduled export |
| recovery/KV | separate deliberate recovery Worker | unchanged offline recovery; no ordinary gateway route to migration | no recovery entry/KV; serving rollback floor D1 only |

## Contract for Brian G3a/G3b

New asset inputs: `site/**`, `frontend/**`, `scripts/build-assets.mjs`, and the applied asset config/workflow. Root docs/tests alone test but do not promote. Prose inside `site/**` is excluded from the packaged artifact when `.md`; classify it explicitly rather than assume glob exclusion. `backend/**` is API-only; no asset release imports it. Shared gateway/config/workflow edits are reviewed mixed changes. Existing producer watch scopes and explicit promotion rules stay untouched. Workflow ordering/exact SHA, provider trigger ownership and manual bypass enforcement belong to Nova G3b; this design does not repair or replace them.

## Contract for Steve G4a/G4b

Preview frontend accepts only `ENVIRONMENT=preview`, an exact configured preview origin and `ISOLATION=disposable`; local construction wires API only to real backend handlers over disposable SQLite and memory stores. Preview bundle contains no production IDs, R2 write binding, KV, secret, cron, remote=true or URL-based production API fallback. `/browser-data/` has a distinct read-only binding and `/r2-proxy/` is denied. Tests must try production Origin/cookies, unknown routes, all mutation paths, missing/mislabelled isolation and binding mismatches. This is application/emulation isolation, not proof of provider IAM or an arbitrary injected binding's identity. Nova must verify actual preview target/resource IDs before hosted preview acceptance.

## Proposed files and shared patches

Musef owns new `frontend/worker.mjs`, `frontend/preview-policy.mjs`, `scripts/build-assets.mjs`, `scripts/prepare-asset-release.mjs`, `scripts/preview-local.mjs`, `tests/asset_boundary.test.mjs`, `tests/preview_isolation.test.mjs`, `tests/preview_transport.test.mjs`, `tests/preview_browser.test.mjs`, `tests/asset_rollback.test.mjs`, `docs/ASSET_BOUNDARY.md`, `docs/SITE_RELEASES.md` and fixture/receipt files under `tests/fixtures/preview/` and `docs/receipts/`. No existing partner file is edited. Reuse, do not edit, existing D1 test helpers. Shared `wrangler.toml`, new applied Wrangler configs, `backend/index.js`, `.github/workflows/*`, `site/DEPLOYMENTS.md` and recovery configuration are Nova's: return proposed integration material under `patches/musef-225/`, do not apply. No backend edit is currently needed for gateway delegation.

## Partner reconciliation after G1

Brian's returned4f4fa5b51f8f490b016dd47cea933388e430fe5e closes all four remaining findings. Applied/fallback configuration are config+shared inputs with separate config_evidence_stale; real-handler dependencies drive acceptance_evidence_stale without becoming payload. Tracked-file exhaustive coverage catches narrowed/exact-path omissions, and suffix matching now matches the case-sensitive packager. His slice6 reports77/77 and715 tracked files/31 manifest entries with zero payload differences. Independent review closes Musef's applied-config coupling finding at bff49eed. These are closed local contracts, not deployment approval. --verify-stage deliberately checks retained stage config integrity rather than current source selection; SITE_RELEASES.md separately requires immediate prepromotion approved-source/config reselection, hash/state comparison, fresh staging and serialized required-check enforcement. No runtime/stager implementation change is needed to make that distinction.

`patches/musef-225/web-ci-only.patch` proposes missing PR/push CI selection and explicit execution of new suites plus Brian's tests; provider application remains Nova-owned. It is an alternative/reconciled **web-ci** hunk, not additive to Brian's overlapping web-ci hunks; Nova retains any reviewed deploy-selector material separately. No deploy.yml edit or new promotion is included. Node22 experimental-SQLite flag is retained from existing CI; local evidence is Node24, not an invented hosted CI receipt. All integrated test files must exist before applying this proposal. Ordinary patch check needs an LF checkout; Brian's patch required whitespace normalization in the CRLF worktree but applied normally to an LF archive.

Steve returned `1312c5fa25029317c1aa9a35ef2ed49e6e55e043`, whose product boundary remains128ed46. Adopted his exact authored `tests/preview_storage_boundary.test.mjs`. Six checks pass on this base; one explicit todo awaits his shared backend module. His test checks that product module independently, so Musef added a stronger local-wrapper type/delegation assertion. The local harness automatically imports the product `assertOperationalKey` once Nova integrates that file, exposes `policySource`, and propagates its typed refusal; missing module is openly labelled fixture-only, while present-but-broken imports fail. No shared backend file was copied into this lane. A production label plus any surviving preview signal now fails403 before delegation. Public-read malformed queries now match backend400 (unknown key404); a direct backend/preview parity test pins these cases. The frontend allowlist is still a deliberate independent copy to keep the gateway free of operational imports, guarded by parity tests; a future shared data-only contract is Nova-reviewed work, not an invented current identity. Provider IAM/binding identity limitations remain P4.

## Named provider assumptions and gates

P1: service binding can forward the original public Request and preserve Set-Cookie (local tests will prove code semantics only). P2: Nova can split public name from API service without losing route, secrets, D1 or schedule; exact operations remain C2-held. P3: provider-native Builds/automatic deployments are not known; Nova must suppress combined promotion before switching ownership. P4: preview has new/disposable resources and no production write binding or secret; creation held. P5: production D1 has migrations 0001 and 0002 as repository comments state; provider readback pending. Provider readbacks and their remaining unknowns belong to Nova; access approval is resolved, and neither manual Wrangler labels nor local tests prove sole deployment ownership. G5 final and A1 full acceptance wait for partner receipts; local emulation may proceed now. No provider writes, production merge/deploy, collector restart or desktop action is authorized by this document.
