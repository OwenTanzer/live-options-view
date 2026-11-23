# Independent site releases and rollback (#225)

Current operating procedure after the November 12, 2026 cutover. The public
`live-options-view` Worker serves staged assets and forwards API/data requests
to `live-options-api`. The API owns accounts, execution, D1, operational R2
access and the sole settlement cron. Railway producers remain separate.

Cutover revision: `f759582e2b327989e31749ddfeae7df9c6474890`.
[API execution](https://github.com/OwenTanzer/live-options-view/actions/runs/43211318484)
and [asset execution](https://github.com/OwenTanzer/live-options-view/actions/runs/43212278203)
succeeded. Nova's implementation quest is closed; final normal-operation
acceptance remains on the strategy's checks page for November 12, 16:45 Pacific.
These receipts establish the cutover, not the result of that later check.

## Choose the release lane

| Change | Route | What stays running |
|---|---|---|
| HTML, CSS, browser JavaScript and packaged images | `deploy-staged.yml` | API and Railway producers |
| Gateway/policy or asset-build/configuration changes | Affected boundary review, then `deploy-staged.yml` | API and producers, provided contracts remain compatible |
| Backend handlers or API configuration | Separately reviewed `deploy.yml` API release | Asset version and producers |
| Producer code or shared Python packaging | Affected Railway producer release | Unrelated services; a producer restart creates a real capture gap |
| Schema, shared contracts or mixed changes | Explicit compatibility and ordering plan | Determined by the affected services |
| Markdown/docs only | No Cloudflare promotion needed | All Cloudflare services; check Railway watch scopes if prose is inside producer directories |

Both Cloudflare workflows are **manual dispatch only**, share concurrency group
`deploy-live-options-view`, require the protected `production` environment,
and query successful Web CI for the exact candidate. Cloudflare native Builds
were disabled at cutover. A merge alone does not publish the website.

**Do not run bare `wrangler deploy`.** Root `wrangler.toml` is the legacy
combined-Worker restoration configuration, retained for the explicit rollback
procedure below. It would replace the asset gateway with operational code.
`wrangler.assets.toml` uses a staged `payload/` layout and must not be deployed
directly from the source checkout either. Use the workflow.

## Ordinary site release during market hours

1. Review the complete change. Run `python tests/test_changed_paths.py` to
   identify affected services; mixed/producer changes are not site-only.
2. Commit the candidate and obtain successful Web CI for its full 40-character
   SHA. Record the current asset revision/version for rollback. For the first
   continuity canary use the existing #225 observation, not another study.
3. Dispatch the asset workflow at the current reviewed workflow ref, supplying
   the candidate SHA and `execute=true`. For example, from this repository:

   ```sh
   gh workflow run deploy-staged.yml --ref master -f revision=FULL_CANDIDATE_SHA -f execute=true
   ```

4. Approve that run's `production` environment deployment. The workflow builds
   and stages on its runner, verifies the API service exists, binds the exact
   revision/config/checks in its authorization, then rechecks under the release
   lock before deployment. Dry-run is the default when `execute` is omitted;
   a dry-run does not publish anything. No API release, migration, write fence,
   secret copy or producer restart is part of an ordinary asset release.
5. Read back the successful run/provider version and verify `/__environment`
   and `X-Asset-Revision` at https://options.moopertonic.net against the candidate.
   Check the changed feature and expected data/auth behavior. Preserve the API
   version, sole API cron and producer identities; inspect unexpected changes
   rather than restarting a collector to manufacture freshness.

The artifact contains allowed site assets plus the two frontend gateway/policy
modules. It excludes backend and producer code, credentials and Markdown prose.
The gateway has ASSETS/API bindings, explicit empty crons and no direct D1/R2/KV
binding. HTML is served with `no-store`. Static requests still invoke the
gateway (`run_worker_first=true`); no free-invocation or latency claim is made.
Browser API calls retain their same-origin URLs. Public archive reads on the
custom domain still use the fixed public R2 origin; `/browser-data/` retains its
workers.dev origin restriction. A custom-domain 403 from that proxy alone is
not evidence of a capture failure. The legacy `/r2-proxy/` is GET-only and uses
the bounded public-key policy, not arbitrary object access.

## Local preview and artifact inspection

Use Node 24 (Node 22 requires `--experimental-sqlite`) in an isolated clean
checkout. These checks and local preview do not contact production writers:

```sh
node --test tests/asset_boundary.test.mjs tests/preview_isolation.test.mjs tests/preview_transport.test.mjs tests/asset_rollback.test.mjs tests/preview_storage_boundary.test.mjs tests/deploy_sequencing.test.mjs
node scripts/preview-local.mjs
```

Preview uses localhost:8787, disposable SQLite/memory stores and fixture quotes
and public data. Use disposable credentials. Ctrl+C stops it; state disappears.
Hosted preview would need separate disposable resources and provider approval;
local emulation is not a provider-permission test.

For inspection, choose new OUT and STAGE directories outside the source and
outside each other, replacing all placeholders:

```sh
node scripts/build-assets.mjs --revision FULL_CANDIDATE_SHA --out OUT
node scripts/build-assets.mjs --verify OUT
node scripts/prepare-asset-release.mjs --artifact OUT --out STAGE
node scripts/prepare-asset-release.mjs --verify-stage STAGE
```

The CLI requires clean exact-HEAD source. Staging selects the applied root asset
config, refuses invalid/drifted source and records payload/config hashes. Stage
verification alone proves retained integrity, not permission or current-source
freshness. The deployment gate binds `config_root` and revalidates current
source, authorization and checks immediately before execution. Never edit an
old plan/hash to make a changed candidate pass. Historical proposal templates
under `patches/` are not ordinary release configurations.

## Website-only rollback

Use an accepted previous **separated asset** artifact with the current API/schema
contract. Verify old and current manifests, then stage the old against current:

```sh
node scripts/build-assets.mjs --verify OLD_ARTIFACT
node scripts/build-assets.mjs --verify CURRENT_ARTIFACT
node scripts/prepare-asset-release.mjs --artifact OLD_ARTIFACT --current CURRENT_ARTIFACT --out NEW_ROLLBACK_STAGE
node scripts/prepare-asset-release.mjs --verify-stage NEW_ROLLBACK_STAGE
```

Promote the old asset revision through the same protected asset lane after
compatibility review; for the workflow, Web CI must exist for that revision and
its build tooling must remain compatible. A retained-stage local promotion must
use the reviewed gate and applicable release authority. Verify the restored
asset revision and unchanged API/cron/producers. No schema, account, archive or
collector rollback belongs in a website-only rollback.

The API serving floor is D1-reading revision
`90cdc56c207ad0c733bb2f1b1bb15d995472bf47` or a reviewed compatible descendant.
Migration 0001/0002 hashes are in release.json; schema mismatch refuses rollback.
Never serve from stale archived KV. The first accepted separated asset baseline
is the cutover revision above; pre-split checkouts are not asset-only rollback
candidates.

### Combined Worker restoration (local procedure)

The combined Worker (wrangler.toml) is the transition rollback target, not an ordinary asset rollback. There is no rollback workflow: deploy-rollback.yml was removed in repair 2 (2026-11-11) because it could not transport its authorization artifact and its checkout followed the dispatch ref, not the restoration revision. Restoration is a bounded local procedure through the candidate gate script, with DEPLOY_REPO_ROOT pointing at a checkout of the recorded combined build (6c1051a7b4fcbc115551562b2eb531c13a440e2e, wrangler.toml SHA256 213c2e56a9f75cd0cdbce4e967022d12581cd711701f091cc6289214648eea72):

```
# 1. From the candidate checkout, run the reviewed gate script. Point
#    DEPLOY_REPO_ROOT at a checkout of the recorded combined build.
git worktree add /tmp/lov-rollback-restore 6c1051a7b4fcbc115551562b2eb531c13a440e2e
DEPLOY_REPO_ROOT=/tmp/lov-rollback-restore \
  node scripts/deploy-staged-payload.mjs --authorize --target rollback --revision 6c1051a7b4fcbc115551562b2eb531c13a440e2e --authorizer <name> --rollback-reason <reason>
# 2. Dry-run, then execute only under the same authorization:
DEPLOY_REPO_ROOT=/tmp/lov-rollback-restore \
  node scripts/deploy-staged-payload.mjs --deploy --target rollback --revision 6c1051a7b4fcbc115551562b2eb531c13a440e2e
```

The gate verifies the restoration checkout is the recorded combined build (its wrangler.toml hash must match the recorded SHA256), re-reads wrangler.toml before execution, and refuses on drift. It deploys ONLY wrangler.toml; it never re-deploys the candidate's asset or API config. The authorization record lives in the shared .deploy-releases directory (default sibling .deploy-releases), not in any workflow artifact — there is no artifact transport to supply. Proved by tests/deploy_sequencing.test.mjs ('Rollback: local candidate-script + DEPLOY_REPO_ROOT restores the recorded combined build').


Combined restoration is an exceptional topology reversal, requiring the approved
fence and reverse cron handoff. The commands above authorize and dry-run only;
execution adds `--execute` under that specific restoration authority. Prevent
duplicate settlement ownership and restore writes only after verification.
Ordinary site release does not repeat this one-time cutover procedure.

#### Reverse cron handoff: settlement weekdays must be MON-FRI

This section documents the procedure only. It authorizes no restoration and no
provider action; both still need the specific restoration authority above.

- **Preserved historical artifact (do not edit).** The pinned combined build
  6c1051a's `wrangler.toml` (SHA256 `213c2e56…eea72`) declares
  `crons = ["*/15 12-23 * * 1-5"]`. It stays byte-identical, because the gate
  refuses any restoration checkout whose hash differs. Likewise leave
  `patches/musef-225/*.proposed` and every recorded hash as history.
- **Why the pinned value is wrong live.** Cloudflare interpreted the numeric
  day-of-week `1-5` as Sunday–Thursday. The live API trigger was corrected to
  `*/15 12-23 * * MON-FRI` on 2026-11-14, and `wrangler.api.toml` matches it.
  Deploying the pinned config therefore installs the wrong weekdays unless the
  trigger is corrected as part of the handoff.
- **Intended restored live schedule.** Exactly one settlement cron in the
  account, `*/15 12-23 * * MON-FRI` (same UTC window and frequency), owned by
  the restored combined Worker `live-options-view`. The API Worker
  `live-options-api` then holds no cron.

Ownership rule: **at most one** settlement cron owner at every point of the
transition, and **exactly one** at completion. The existing restoration/write
fence stays in place from before step 2 through the final readback in step 4.

During the reverse handoff:

1. Before executing, read back the triggers of both Workers and record them.
   Expected: `live-options-api` holds only `*/15 12-23 * * MON-FRI`, and
   `live-options-view` holds none.
2. With the fence in place, clear the API Worker's trigger, then read back and
   confirm that it is actually absent. Do not run the combined deploy until
   that readback shows no API cron. Record the time the API trigger was
   cleared: the no-owner interval starts here, and it lasts until step 4
   passes. A failed deploy or correction can make it long.
3. After the combined deploy, read back `live-options-view`'s triggers. They
   will show the pinned numeric `1-5`. Still fenced, correct them with a
   trigger-only change to exactly `*/15 12-23 * * MON-FRI`, under the same
   restoration authority. Do not change the restoration checkout to do this.
4. Final readback, still fenced: exactly one settlement cron across both
   Workers, on `live-options-view`, with the named-weekday expression
   `*/15 12-23 * * MON-FRI`, and none on `live-options-api`. Record it in the
   dated release record together with the actual missed interval (from the
   step 2 clear time to this readback) and the scheduled sweeps that fell
   inside it. Lift the fence only after this readback passes.

If any step fails (the API trigger does not clear, the deploy fails or is
partial, the correction fails, or a readback disagrees):

- Stay fenced. Do not lift the fence and do not leave a numeric day-of-week
  cron active, including after escalation.
- Reconcile the actual state first: read back which Worker versions are
  deployed and the triggers on both Workers, and record them.
- Recover a verified single-owner schedule under the specific restoration
  authority. That owner is either the restored `live-options-view` corrected to
  `*/15 12-23 * * MON-FRI`, or, if the restoration is abandoned, the API Worker
  with `*/15 12-23 * * MON-FRI`. Do not restore the API trigger while
  `live-options-view` still holds any cron; remove or confirm the absence of
  the combined Worker's trigger first.
- Confirm exactly one owner by readback, record the actual missed interval, and
  only then lift the fence.

Forward re-separation reverses this: the API Worker again becomes the only owner
of `*/15 12-23 * * MON-FRI` from `wrangler.api.toml`, and the asset Worker keeps
its explicit `crons = []`.

## Related operating records

- [Release boundaries](RELEASE_BOUNDARIES.md): changed-path matrix and dated provider baseline.
- [Asset design](ASSET_BOUNDARY.md): original design and local evidence, not current release instructions.
- [Producer packaging](../deployment/python/README.md) and component guides: independent producer release paths.
- Recovery uses `wrangler.recovery.toml` only in an explicitly approved recovery window; it is never an ordinary website release.

Keep command receipts, provider identities and observation results in dated
release/quest records. Update this procedure when release behavior changes.
