# Musef local G1/G2/G5/A1 receipt — November 11, 2026

Code candidate `0434bf1bca647a4bf1278a6a67303023d3061077`, base `90cdc56c207ad0c733bb2f1b1bb15d995472bf47`, branch `musef/225-assets-preview`. Isolated LF worktree `C:\Users\Owen\Apps\lov-musef-225-assets-20261111`. Runtime Node24.15.0, Python3.14.4, Windows; Playwright1.56.1/Chromium installed in separate Musef tool directory. No provider operations, push, PR, merge, deployment, restart or desktop input. Target17:14, closeout17:54, cutoff18:14 Pacific retained from original13:14 run.

## State

G1 design/compatibility/base/file contract returned; not accepted by Nova. G2 local implementation and tests complete at this candidate, review pending. G5 procedure and concrete shared-entry/config/recovery/deployment-note proposals prepared, finalization pending G3b/G4b/provider receipts. A1 local slice demonstrated; dependency acceptance and provider-hosted validation not claimed. Site closure is not claimed.

## Commands and results

| Command/check | Actual result | Evidence |
|---|---|---|
| `node --test tests/asset_boundary.test.mjs tests/preview_isolation.test.mjs tests/asset_rollback.test.mjs tests/preview_transport.test.mjs` | 26/26 pass, zero skip, about4s | standalone output and complete-suite receipt below |
| `node --test tests/*.test.js tests/*.test.mjs`, NODE_PATH points at separate Playwright install, MUSEF_EVIDENCE_DIR external, O2_EVIDENCE_DIR a new external directory | 136/136 pass, 0 fail/skip, 9947.6118ms | `C:\Users\Owen\Apps\renaissance-225-musef-evidence\full-js-suite-configured.txt` |
| First full-suite attempt without O2_EVIDENCE_DIR | 135/136; existing steo_browser.test.js:33 requires that env variable; corrected environment, no source repair required | `full-js-suite.txt` in same evidence directory |
| `git apply --check` for shared-entry.patch, recovery-unneeded-write-binding.patch and deployment-notes.patch | all exit0; none applied | unapplied proposals in patches/musef-225 |
| `node scripts/build-assets.mjs --revision 0434bf1bca647a4bf1278a6a67303023d3061077 --out C:\Users\Owen\Apps\renaissance-225-musef-artifact-0434bf1b` | exit0 from clean exact HEAD; tracked-only payload, no operational code/producers/config/Markdown | `package-0434bf1b.json`, artifact release.json |
| `node scripts/build-assets.mjs --verify C:\Users\Owen\Apps\renaissance-225-musef-artifact-0434bf1b` | exit0, all payload/schema hashes and targets verified | `verify-0434bf1b.json` |
| `node scripts/prepare-asset-release.mjs --artifact C:\Users\Owen\Apps\renaissance-225-musef-artifact-0434bf1b --out C:\Users\Owen\Apps\renaissance-225-musef-stage-0434bf1b` | exit0; prepared-not-approved-not-deployed, asset target only, API unchanged, empty producer/schema/recovery actions | staging plan.json; proposed command NOT run |
| `node tests/preview_browser.test.mjs` with MUSEF_ASSET_ROOT pointing at exact0434 artifact/site | pass at22:21:27UTC; registration/session, Historical heatmap/sidebar, mobile Historical;44 requests all local, no page errors, attempted production POST blocked by CSP | `artifact-0434bf1b-render/browser-receipt.json` plus three PNGs under external evidence directory |
| Real loopback launch/stop via startPreview() | localhost8787 identity, multiple Set-Cookie including deletion, wrong Host/Origin rejected, body limit413; stopped in test finally | preview_transport suite |

The aggregate count is Node test-runner tests, not a claim that every legacy script contains only one assertion. Existing rendered regressions also passed: Historical, QQQ/SPY/IBIT, bot dates, STEO. The QQQ/SPY/IBIT test writes tracked evidence PNGs by design; copied those generated images to external evidence/multi-underlying-regression, then restored only those seven test-produced paths in this private worktree before the clean source commit. No partner checkout or capture evidence was changed.

## Acceptance detail and limitations

Asset-only packaging copies tracked site files plus two gateway modules; source graph contains no backend import. The only backend material represented in the manifest is migration filenames/hashes, not SQL payload or executable handlers. Schema hashes: 0001 `d345c13c6f5d8f70afaa8d10905e8b9197fa12bc559ca00a64b2b558619fa3d4`;0002 `9b55ce1f1c762370d08cb1fbc57f5f0873a02cb94858979b755013d39cfa2237`. D1 serving floor90cdc56c, no KV serving or recovery rollback. Preview's real backend handlers execute against fresh SQLite and scoped memory objects, quote network is fixture-only, public-read credentials/redirect/cookie response headers are removed. Browser data origin is corrected only in served preview HTML and CSP forbids external network/forms.

Executable rollback loads both packaged gateway/policy bundles, swaps the asset binding/revision, and retains the identical API binding, D1 instance/account/session and empty producer action set. It rejects schema drift, altered floor, recovery/KV serving, unexpected payloads/hashes and changed API/producer targets. The rollback's two IDs are synthetic protocol fixtures built from this source; it is not evidence of a real previous deployed separated artifact. Production provider routing, actual binding identities/IAM, exact D1 schema readback, trigger ownership and a real provider rollback remain untested and Nova-owned.

A read-only independent helper review found and locally reproduced the off-host data selector, stale transformed-HTML entity headers, local symlink/junction escape, HTTP multiple-cookie flattening, inside-source '..release' output guard, ignored-asset inclusion and304 transformation issue. Repaired all before candidate commit and added direct/transport/browser regressions. Also hardened schema validation against a mutually empty reference and refused compressed HTML rather than corrupting bytes. The review does not substitute for Nova's assigned acceptance. No production-mutation path was found in the constructed local runtime; identity labels are explicitly not a provider permission proof.

## Next actors and ownership

Controller relays G1 and0434 candidate to Brian/Steve immediately. Brian adds G1-adjusted changed-path selector coverage from his280f4035 baseline; Steve checks preview isolation against his128ed46 G4a contract, preserving independent R1/R2 repairs. Nova reviews other-author code, applies shared patches only in her integration lane, supplies G3b/G4b and P1-P5 provider facts, packages C2. Musef then reconciles final G5 and affected A1 checks, without blindly repeating unaffected receipts. Chej alone updates shared Room/strategy pointers; request a Room pointer to this canonical quest/current return rather than violate one-editor ownership. No jobs/servers/timers remain after checks. External artifacts/tool install and this worktree are owned by Musef for continuation; retained intentionally for review, not deleted.

—Musef 📖
