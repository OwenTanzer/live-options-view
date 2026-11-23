// scripts/deploy-staged-payload.mjs — G3b staged-payload promotion gate.
// Local only. Default is --dry-run: print the plan's command and exit 0 only
// if every guard passes; nothing executes. --execute requires an authorization
// record for the exact revision and rechecks every guard immediately before
// executing, under a shared release lock, so an old candidate A cannot
// overwrite a newer authorized candidate B. The workflow that calls this is
// a local proposal until the C2 packet is approved; this script never
// deploys on its own, never merges, and never touches a collector.
//
// B1 — Authorization is EXTERNAL to the immutable candidate. The record
// lives in DEPLOY_RELEASES_DIR, which defaults to a sibling
// `.deploy-releases` directory OUTSIDE the candidate tree (never
// docs/releases inside it). Authorizing therefore never moves HEAD, so the
// HEAD == record == plan == manifest check is satisfiable: the record binds
// the revision AND its tree SHA (head_tree), and the gate re-verifies both
// at deploy time. A record inside the tree is a paradox (commit it and HEAD
// moves: old revision -> Revision mismatch, new HEAD -> No record), so the
// gate refuses any record found inside the tree. The applied-source asset
// config is a C2 precondition: it must exist at the repo root before any
// real cutover; tests use a fixture applied config, never a faked real one.
//
// B2 — This gate is the ONLY deploy mechanism. The old automatic
// deploy.yml push-deploy is removed in the local proposal; both workflows
// (deploy-staged.yml for assets, deploy.yml for the API) are
// workflow_dispatch-only, share the deploy-live-options-view concurrency
// group, and call only this script. The API lane is a separate explicit
// dispatch (--target api) with its own authorization record; it deploys
// only the API service (wrangler.api.toml) and has no producer effects.
// It must never deploy the combined public worker (wrangler.toml): the
// api target is bound to wrangler.api.toml by configPath, command and
// recorded hash, and a deploy against any other config is refused.
//
// C2 repair (2026-11-11) — deployment order and rollback. The asset
// service's API binding points at the live-options-api service, so the API
// service must exist before any asset authorize or deploy; the gate
// refuses an asset step until VALIDATED API-deployment or
// operator-readback evidence exists for the revision (repair 2: a
// filename alone never satisfies readiness — the evidence is parsed and
// checked for the intended service and deployment context). The combined
// public worker is REPLACED by the asset service (same name
// live-options-view); it is not kept alongside. Rollback is a separate
// lane (--target rollback) that deploys only wrangler.toml to restore the
// combined worker, and only under a rollback authorization with a recorded
// reason. Receipts are per-target so an asset receipt cannot overwrite the
// API receipt that proves the API service was deployed. Repair 2 removed
// the nonfunctional deploy-rollback.yml workflow from the claimed
// executable path (it could not transport its authorization artifact and
// its checkout followed the dispatch ref, not the restoration revision);
// the rollback path is the bounded local candidate-script plus
// DEPLOY_REPO_ROOT restoration procedure (see docs/SITE_RELEASES.md and
// the C2 packet §5), proved by the rollback test in
// tests/deploy_sequencing.test.mjs.
//
// Repair 2 (2026-11-11) — freshness is now enforced in the gate, not
// only documented. verifyCurrentSourceFreshness() re-reads the current
// source config from the config root recorded in the plan, validates it,
// and compares its hash to the authorized record; it runs at authorize
// time and again in recheckBeforeExecute(), inside the shared lock,
// immediately before the executor. A missing, invalid, or drifted source
// refuses promotion (the operator re-stages and re-authorizes).
//
// Repair 3 (2026-11-11) — the selected config root is BINDING, not just
// recorded in the plan. Repair 2's freshness check trusted
// plan.config_root at deploy time, so changing only plan.config_root
// after authorization redirected the check to an old valid copy while
// the originally authorized source was invalid (Brian's repair-2
// witness: stub executor ran). The authorization record now also binds
// config_root at authorize time, and recheckBeforeExecute() refuses any
// later redirection — the plan's root must equal the record's bound
// root, or the executor never runs.
//
// B3 — One shared release lock for the whole release target, held in the
// external releases dir — not one lock per stage. authorize, supersede and
// deploy all take the same lock, so supersession cannot interleave with
// execution. Cross-runner serialization is the workflow concurrency group;
// the shared lock is the on-machine authority. The final recheck
// (recheckBeforeExecute) re-reads the authorization record, the plan and
// HEAD immediately before the executor runs, inside the lock; a stale
// candidate A never executes after B supersedes it.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, verifyArtifact } from './build-assets.mjs';
import { transitionPlanState, validateAssetConfig, verifyStage } from './prepare-asset-release.mjs';

// External to the candidate tree by default. DEPLOY_RELEASES_DIR (tests/CI)
// wins; otherwise a sibling directory next to the checkout. Never inside
// the tree: authorizing must not move HEAD (B1).
const releasesDir = () => process.env.DEPLOY_RELEASES_DIR || resolve(ROOT, '..', '.deploy-releases');
// Which checkout the gate verifies. Tests point it at an isolated clone.
const repoRoot = () => process.env.DEPLOY_REPO_ROOT || ROOT;
// The API lane deploys ONLY wrangler.api.toml (the separate API service).
// The combined public worker (wrangler.toml) is never an api target: the
// config is read and hashed here, and every later check re-reads it.
export const API_CONFIG_FILE = 'wrangler.api.toml';
export function apiConfig() {
  const path = resolve(repoRoot(), API_CONFIG_FILE);
  if (!existsSync(path)) throw new Error(`API config missing: ${API_CONFIG_FILE} must exist at the repo root (C2 precondition)`);
  return { path, sha256: createHash('sha256').update(readFileSync(path)).digest('hex') };
}
// The rollback lane deploys ONLY wrangler.toml (the combined public
// worker), and only as a restoration (target 'rollback'). It is the single
// lane that may deploy the combined config; the api lane can never deploy
// it, and the asset lane deploys only the staged asset config. A rollback
// record is a separate authorization bound to the combined config hash
// and the recorded combined code revision.
export const ROLLBACK_CONFIG_FILE = 'wrangler.toml';
export function rollbackConfig() {
  const path = resolve(repoRoot(), ROLLBACK_CONFIG_FILE);
  if (!existsSync(path)) throw new Error(`Rollback config missing: ${ROLLBACK_CONFIG_FILE} must exist at the repo root`);
  return { path, sha256: createHash('sha256').update(readFileSync(path)).digest('hex') };
}
const authorizationPath = revision => resolve(releasesDir(), `${revision}.authorization.json`);
// Receipts are per-target: an api receipt and an asset receipt for the same
// revision are different evidence and must not overwrite each other (the
// asset receipt used to overwrite the api receipt, which destroyed the
// API-readiness evidence the asset order check needs).
const receiptPath = (revision, target) => resolve(releasesDir(), `${revision}.${target}.deploy-receipt.json`);
const apiServiceVerifiedPath = revision => resolve(releasesDir(), `${revision}.api-service-verified.json`);
// Repair 2 (2026-11-11) — API readiness is evidence, not a filename.
// apiServiceReady() used to return true for any directory entry ending
// .api.deploy-receipt.json or .api-service-verified.json, without reading
// it — a junk file satisfied the ordering guard (Brian's witness:
// junk.api.deploy-receipt.json containing "not JSON, no provider or
// deployment" returned true). Now each candidate file is parsed and
// validated: an API deploy receipt must be a completed deployment of the
// intended service (target 'api', the API deploy command, a full revision,
// a valid execution time, and service live-options-api when named); an
// operator verification must name the intended service, the
// operator-readback kind, a verifier, and a valid verification time.
// Malformed, wrong-target, or wrong-service files are skipped, never
// counted. Cross-revision reuse is preserved: valid evidence for ANY
// revision proves the named service exists (the binding references the
// service name, not a code revision). If the service was deleted after
// being created, the evidence is stale — the operator re-records a fresh
// verification in that case.
function isValidIsoDate(value) {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}
function isApiDeployReceipt(data) {
  return data && typeof data === 'object'
    && data.target === 'api'
    && data.command === 'wrangler deploy --config wrangler.api.toml'
    && /^[a-f0-9]{40}$/.test(data.revision || '')
    && isValidIsoDate(data.executed_at)
    && (data.service === undefined || data.service === 'live-options-api');
}
function isApiServiceVerification(data) {
  return data && typeof data === 'object'
    && data.service === 'live-options-api'
    && data.verification_kind === 'operator-readback'
    && typeof data.verifier === 'string' && data.verifier.length > 0
    && /^[a-f0-9]{40}$/.test(data.revision || '')
    && isValidIsoDate(data.verified_at);
}
export function apiServiceReady(revision) {
  // Existence is a property of the NAMED service, not of the revision:
  // a validated API deploy receipt or operator verification for ANY
  // revision proves the service was created. A per-revision check would
  // wrongly refuse a new asset candidate while the API service from an
  // earlier candidate still exists (the binding references the service
  // name, not a code revision).
  if (!existsSync(releasesDir())) return false;
  for (const name of readdirSync(releasesDir())) {
    const path = resolve(releasesDir(), name);
    try {
      const data = JSON.parse(readFileSync(path, 'utf8'));
      if (name.endsWith('.api.deploy-receipt.json') && isApiDeployReceipt(data)) return true;
      if (name.endsWith('.api-service-verified.json') && isApiServiceVerification(data)) return true;
    } catch { /* malformed evidence never satisfies readiness */ }
  }
  return false;
}
export function assertApiServiceReady(revision) {
  if (!apiServiceReady(revision)) {
    throw new Error('API service not ready: deploy the API service first (--target api) or record an operator verification that live-options-api exists; an asset deploy now would break the API service binding');
  }
}
export function recordApiServiceVerified({ revision, verifier }) {
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  if (!verifier) throw new Error('API service verification requires --authorizer (the verifying operator)');
  const lock = acquireReleaseLock();
  try {
    const record = { revision, service: 'live-options-api', verified_at: new Date().toISOString(), verifier, verification_kind: 'operator-readback' };
    mkdirSync(releasesDir(), { recursive: true });
    writeFileSync(apiServiceVerifiedPath(revision), JSON.stringify(record, null, 2) + '\n');
    return record;
  } finally {
    lock.release();
  }
}
const STALE_LOCK_MS = 10 * 60 * 1000;

function parseCheck(value, name) {
  const i = value.lastIndexOf(':');
  if (i <= 0 || i === value.length - 1) throw new Error(`${name} must be <run-id>:<status>`);
  return { runId: value.slice(0, i), status: value.slice(i + 1) };
}
function loadAuthorization(revision) {
  const path = authorizationPath(revision);
  if (!existsSync(path)) throw new Error(`No authorization record for revision ${revision}`);
  return JSON.parse(readFileSync(path, 'utf8'));
}
function saveAuthorization(record) {
  mkdirSync(releasesDir(), { recursive: true });
  writeFileSync(authorizationPath(record.revision), JSON.stringify(record, null, 2) + '\n');
  return record;
}
function headRevision() {
  return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoRoot(), encoding: 'utf8' }).trim();
}
function headTree() {
  return execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoRoot(), encoding: 'utf8' }).trim();
}
function assertChecksMatch(record, webCheck, apiCheck) {
  // A rollback record waives checks by decision (recorded with the reason);
  // a supplied check on a rollback record must still match.
  if (record.web_check_status !== 'waived-rollback') {
    if (webCheck) {
      const web = parseCheck(webCheck, '--web-check');
      if (web.runId !== record.web_check_run_id || web.status !== 'success') {
        throw new Error('Web check evidence does not match the authorization record');
      }
    } else if (record.web_check_status !== 'success') throw new Error('Web check did not succeed for this revision');
  }
  if (record.api_check_status !== 'waived-rollback') {
    if (apiCheck) {
      const api = parseCheck(apiCheck, '--api-check');
      if (api.runId !== record.api_check_run_id || api.status !== 'success') {
        throw new Error('API check evidence does not match the authorization record');
      }
    } else if (record.api_check_status !== 'success') throw new Error('API check did not succeed for this revision');
  }
}
function assertHeadMatches(record) {
  if (headRevision() !== record.revision) {
    throw new Error('Revision mismatch: HEAD, authorization, plan and manifest must be the same exact revision');
  }
  if (record.head_tree && headTree() !== record.head_tree) {
    throw new Error('Tree mismatch: HEAD tree does not match the authorization record');
  }
}

// One shared lock for the whole release target (B3). It lives in the
// external releases dir, so every stage and both targets contend for the
// same lock. A per-stage lock (<stage>.lock) allowed A and B to be held
// simultaneously — that is the hole this replaces.
export function releaseLockPath() {
  return resolve(releasesDir(), '.release.lock');
}
export function acquireReleaseLock() {
  const lockPath = releaseLockPath();
  const tryOnce = () => {
    // The releases dir may not exist yet (first authorize in a fresh dir);
    // without this the lock mkdir fails ENOENT (Brian slice 8 residual).
    mkdirSync(releasesDir(), { recursive: true });
    mkdirSync(lockPath);
    writeFileSync(resolve(lockPath, 'owner.json'), JSON.stringify({ pid: process.pid, startedAt: Date.now() }) + '\n');
    return { lockPath, release() { rmSync(lockPath, { recursive: true, force: true }); } };
  };
  try {
    return tryOnce();
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    let stale = false;
    try {
      const owner = JSON.parse(readFileSync(resolve(lockPath, 'owner.json'), 'utf8'));
      if (Date.now() - owner.startedAt > STALE_LOCK_MS) stale = true;
      else try { process.kill(owner.pid, 0); } catch { stale = true; }
    } catch { stale = true; }
    if (!stale) throw new Error('Deploy lock held for the release target');
    rmSync(lockPath, { recursive: true, force: true });
    return tryOnce();
  }
}

// Authorize one candidate. target 'asset' requires an applied-source stage;
// target 'api' is the separately explicit API lane (no asset stage, deploys
// wrangler.api.toml only, no producer effects). target 'rollback' restores
// the combined public worker only (deploys wrangler.toml, no stage, no
// producer effects) and requires a recorded rollback reason; its web/API
// checks are optional and, if omitted, are recorded as waived with the
// reason. The record is written to the EXTERNAL releases dir (B1);
// authorizing never touches the candidate tree.
// A newer authorization supersedes every older one, so at most one
// candidate is deployable at a time. All record mutations run under the
// shared release lock, so supersession cannot interleave with a deploy.
export function authorize({ target = 'asset', stage, revision, webCheck, apiCheck, authorizer, rollbackReason = null, dryRun = false }) {
  if (target !== 'asset' && target !== 'api' && target !== 'rollback') throw new Error("target must be 'asset', 'api' or 'rollback'");
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  if (!authorizer) throw new Error('Authorization requires --authorizer');
  // Web/API checks are required for asset and api targets. A rollback
  // restores a known-good combined build under an emergency decision, so
  // its checks are optional: if supplied they must be success; if omitted
  // they are recorded as waived with the rollback reason.
  let web = null, api = null;
  if (target === 'rollback') {
    if (webCheck) {
      web = parseCheck(webCheck, '--web-check');
      if (web.status !== 'success') throw new Error('Authorization requires successful web and API checks for the exact revision');
    }
    if (apiCheck) {
      api = parseCheck(apiCheck, '--api-check');
      if (api.status !== 'success') throw new Error('Authorization requires successful web and API checks for the exact revision');
    }
    if (!rollbackReason) throw new Error('Rollback authorization requires --rollback-reason');
  } else {
    web = parseCheck(webCheck, '--web-check');
    api = parseCheck(apiCheck, '--api-check');
    if (web.status !== 'success' || api.status !== 'success') {
      throw new Error('Authorization requires successful web and API checks for the exact revision');
    }
  }
  let plan = null;
  let apiCfg = null;
  let rbCfg = null;
  if (target === 'asset') {
    if (!stage) throw new Error('Asset authorization requires --stage');
    // Deployment order: the asset service's API binding points at the
    // live-options-api service. It must exist before the asset can be
    // authorized (and again before it deploys); otherwise the first asset
    // deploy breaks the binding. In dry-run mode (dryRun=true) the service
    // is expected to be absent — it is created by the real API deploy,
    // which a dry-run never performs — so the readiness check is skipped
    // here. The real run (dryRun=false, the default) re-asserts it, so a
    // dry-run authorization can never enable a real deploy on its own.
    if (!dryRun) assertApiServiceReady(revision);
    plan = verifyStage(stage);
    if (plan.asset_revision !== revision) throw new Error('Stage revision does not match --revision');
    if (plan.config_state !== 'applied-source') {
      throw new Error('Proposal-only stages can never deploy; an applied root config is required');
    }
    // Freshness (repair 2): the source config must be currently valid
    // and match the stage at authorization time, not just at staging
    // time — an authorization for a stale source must never be created.
    verifyCurrentSourceFreshness(stage, plan.config_sha256);
  } else if (target === 'api') {
    // The api lane is bound to wrangler.api.toml at authorize time; the
    // hash is re-checked at deploy so a changed config cannot deploy.
    apiCfg = apiConfig();
  } else {
    // The rollback lane restores the combined public worker only. It is
    // bound to wrangler.toml at authorize time; the hash is re-checked at
    // deploy so a changed config cannot restore.
    rbCfg = rollbackConfig();
  }
  const lock = acquireReleaseLock();
  try {
    if (existsSync(releasesDir())) {
      for (const name of readdirSync(releasesDir())) {
        if (!name.endsWith('.authorization.json')) continue;
        const older = JSON.parse(readFileSync(resolve(releasesDir(), name), 'utf8'));
        if (older.revision === revision || older.superseded_by) continue;
        older.superseded_by = revision;
        saveAuthorization(older);
        if (older.stage_path) {
          try {
            const olderPlan = JSON.parse(readFileSync(resolve(older.stage_path, 'plan.json'), 'utf8'));
            if (olderPlan.state === 'authorized') transitionPlanState(older.stage_path, ['authorized'], 'superseded');
          } catch { /* stage already gone; the record is the authority */ }
        }
      }
    }
    const record = {
      revision, head_tree: headTree(), target,
      stage_path: target === 'asset' ? resolve(stage) : null,
      config_source: plan ? plan.config_source : (target === 'api' ? API_CONFIG_FILE : ROLLBACK_CONFIG_FILE),
      config_sha256: plan ? plan.config_sha256 : (target === 'api' ? apiCfg.sha256 : rbCfg.sha256),
      // Repair 3: bind the selected config root for the asset target.
      // The freshness check re-reads the source from this root; binding
      // it here makes any later plan.config_root redirection detectable
      // at deploy time. api/rollback lanes read their config from
      // DEPLOY_REPO_ROOT and compare by hash; they have no stage root.
      config_root: plan ? plan.config_root : null,
      web_check_run_id: web ? web.runId : null, web_check_status: web ? web.status : 'waived-rollback',
      api_check_run_id: api ? api.runId : null, api_check_status: api ? api.status : 'waived-rollback',
      authorizer, authorized_at: new Date().toISOString(), superseded_by: null,
      authorization_kind: target === 'rollback' ? 'rollback-restoration' : 'external-artifact',
      ...(target === 'rollback' ? { rollback_reason: rollbackReason } : {}),
    };
    saveAuthorization(record);
    if (target === 'asset') transitionPlanState(stage, ['prepared-not-approved-not-deployed'], 'authorized');
    return record;
  } finally {
    lock.release();
  }
}
export function authorizeStage(args) {
  return authorize({ ...args, target: 'asset' });
}
export function authorizeApi(args) {
  return authorize({ ...args, target: 'api' });
}
export function authorizeRollback(args) {
  return authorize({ ...args, target: 'rollback' });
}

export function supersede({ revision, supersededBy }) {
  const lock = acquireReleaseLock();
  try {
    const record = loadAuthorization(revision);
    if (record.superseded_by) throw new Error(`Revision ${revision} is already superseded by ${record.superseded_by}`);
    record.superseded_by = supersededBy;
    saveAuthorization(record);
    if (record.stage_path) {
      try {
        const plan = JSON.parse(readFileSync(resolve(record.stage_path, 'plan.json'), 'utf8'));
        if (plan.state === 'authorized') transitionPlanState(record.stage_path, ['authorized'], 'superseded');
      } catch { /* stage already gone; the record is the authority */ }
    }
    return record;
  } finally {
    lock.release();
  }
}
export function supersedeStage(args) {
  return supersede(args);
}

// Freshness (repair 2, blocker 2; root binding repair 3) — the retained
// stage's config is integrity-only: verifyStage checks the stage's own
// bytes, so an unchanged stage still verifies after its named source
// config changes or becomes invalid. Before promotion, re-read the
// CURRENT source config from the config root recorded in the plan,
// validate it, and compare its hash to the authorized record. If the
// source is missing, invalid, or drifted since authorization, promotion
// refuses — the operator must re-stage and re-authorize a new candidate.
// This is the current accepted source/config snapshot check the
// freshness contract (docs/SITE_RELEASES.md) promises; it runs inside
// the shared lock, immediately before the executor, so a source change
// that lands after the first checks is still caught. It does not
// re-stage: a refusal is the signal to re-stage fresh and authorize a
// new candidate.
//
// Repair 3: when expectedConfigRoot is given (the root bound in the
// authorization record), the plan's config_root must equal it. Without
// this, changing only plan.config_root after authorization redirects
// this check to an old valid copy while the originally authorized source
// is invalid. At authorize time there is no record yet, so no expected
// root is passed — the plan's own root is the selected root being bound.
export function verifyCurrentSourceFreshness(stage, expectedSha256, expectedConfigRoot = null) {
  const plan = JSON.parse(readFileSync(resolve(stage, 'plan.json'), 'utf8'));
  if (plan.config_state !== 'applied-source') {
    throw new Error('Freshness check requires applied-source config');
  }
  if (!plan.config_root || typeof plan.config_root !== 'string') {
    throw new Error('Stage plan lacks config_root; re-stage with the current gate before promotion');
  }
  if (expectedConfigRoot != null && plan.config_root !== expectedConfigRoot) {
    throw new Error('Config root redirected since authorization; re-stage and re-authorize before promotion');
  }
  const sourcePath = resolve(plan.config_root, plan.config_source);
  if (!existsSync(sourcePath)) {
    throw new Error('Current source config missing; re-stage and re-authorize before promotion');
  }
  const source = readFileSync(sourcePath, 'utf8');
  const manifest = verifyArtifact(resolve(stage, 'payload'));
  validateAssetConfig(source, manifest);
  const sha256 = createHash('sha256').update(source).digest('hex');
  if (sha256 !== expectedSha256 || sha256 !== plan.config_sha256) {
    throw new Error('Current source config drifted since authorization; re-stage and re-authorize before promotion');
  }
  return { sourcePath, sha256 };
}

// Final guard, called immediately before the executor runs, inside the
// shared lock (B3). Re-reads the authorization record, the plan (asset
// only) and HEAD from scratch, so a supersession that landed after the
// first checks is caught here and the executor never runs for a stale
// candidate.
export function recheckBeforeExecute({ stage, revision, record, webCheck = null, apiCheck = null, dryRun = false }) {
  const fresh = loadAuthorization(revision);
  if (fresh.superseded_by) throw new Error(`Revision ${revision} is superseded by ${fresh.superseded_by}`);
  assertHeadMatches(fresh);
  assertChecksMatch(fresh, webCheck, apiCheck);
  if (fresh.target === 'asset') {
    // Same dry-run treatment as authorize/deploy: in dry-run mode the
    // API service is expected to be absent; the real run re-asserts it.
    if (!dryRun) assertApiServiceReady(revision);
    const freshPlan = verifyStage(stage, ['authorized']);
    if (freshPlan.config_sha256 !== fresh.config_sha256 || freshPlan.config_state !== 'applied-source') {
      throw new Error('Stage config drifted while waiting for the lock');
    }
    // Freshness (repair 2) + root binding (repair 3): the CURRENT source
    // config must still be valid and match the authorized hash, read from
    // the config root BOUND in the authorization record. A record with no
    // bound root (pre-repair-3) cannot prove its root, and a plan whose
    // root differs from the bound root is a redirection — both refuse
    // here, inside the lock, before the executor runs.
    if (!fresh.config_root || typeof fresh.config_root !== 'string') {
      throw new Error('Authorization record lacks bound config_root; re-authorize before promotion');
    }
    verifyCurrentSourceFreshness(stage, fresh.config_sha256, fresh.config_root);
    return freshPlan;
  }
  if (fresh.target === 'rollback') {
    // The rollback lane re-reads wrangler.toml and compares its hash, so a
    // config changed after authorization cannot restore.
    const cfg = rollbackConfig();
    if (cfg.sha256 !== fresh.config_sha256 || fresh.config_source !== ROLLBACK_CONFIG_FILE) {
      throw new Error('Rollback config drifted while waiting for the lock');
    }
    return null;
  }
  // The api lane re-reads wrangler.api.toml and compares its hash, so a
  // config changed after authorization cannot deploy.
  const cfg = apiConfig();
  if (cfg.sha256 !== fresh.config_sha256 || fresh.config_source !== API_CONFIG_FILE) {
    throw new Error('API config drifted while waiting for the lock');
  }
  return null;
}

// Default executor runs wrangler from explicit fields as an argv array —
// never by shell-parsing the PowerShell-literal proposed_command string,
// which is review material for the Windows operating procedure, not a
// shell command on this machine.
export function defaultExecutor(command, { configPath, revision, target }) {
  if (target === 'api' || target === 'rollback') {
    execFileSync('wrangler', ['deploy', '--config', configPath], { stdio: 'inherit' });
  } else {
    execFileSync('wrangler', ['deploy', '--config', configPath, '--var', `ASSET_REVISION:${revision}`], { stdio: 'inherit' });
  }
  return command;
}

export function deploy({ target = 'asset', stage, revision, execute = false, webCheck = null, apiCheck = null, executor = defaultExecutor, dryRun = false }) {
  // Repair 8: contradictory modes. dryRun skips the readiness
  // assertion and execute invokes the executor; the combination would
  // execute a real deploy with no API evidence (a dry-run bypass).
  // Refuse before any state is read or the lock is taken, on every
  // target — the CLI, the workflows and direct callers all reach
  // deploy(), so this is the single choke point. The legitimate
  // combinations are execute=false + dryRun=true (dry-run: readiness
  // skipped, nothing executes) and execute=true + dryRun=false (real
  // deploy: readiness asserted). authorize() takes no execute flag and
  // recheckBeforeExecute() is only called from deploy() after this
  // guard, so neither can produce the combination; the workflows
  // cannot either (deploy.yml never sets DEPLOY_DRY_RUN, and
  // deploy-staged.yml derives it as !inputs.execute).
  if (execute && dryRun) throw new Error('Contradictory modes: execute=true with dryRun=true — a real deploy must assert readiness; a dry-run must not execute');
  if (target !== 'asset' && target !== 'api' && target !== 'rollback') throw new Error("target must be 'asset', 'api' or 'rollback'");
  if (target === 'asset' && !stage) throw new Error('Asset deploy requires --stage');
  // The shared release lock is taken FIRST, before any state is read, and
  // held through the executor and the receipt write. Authorization and
  // supersession take the same lock, so no state change can interleave.
  const lock = acquireReleaseLock();
  try {
    const record = loadAuthorization(revision);
    if (record.target !== target) throw new Error(`Authorization record for ${revision} is for target '${record.target}', not '${target}'`);
    if (record.superseded_by) throw new Error(`Revision ${revision} is superseded by ${record.superseded_by}`);
    assertHeadMatches(record);
    let plan = null;
    let manifest = null;
    if (target === 'asset') {
      // Deployment order, enforced at deploy time as well as authorize
      // time: the API service must exist before the asset deploy. In
      // dry-run mode the service is expected to be absent (see
      // authorize); the real run re-asserts it.
      if (!dryRun) assertApiServiceReady(revision);
      plan = verifyStage(stage, ['authorized']);
      manifest = verifyArtifact(resolve(stage, 'payload'));
      if (plan.asset_revision !== revision || manifest.revision !== revision) {
        throw new Error('Revision mismatch: HEAD, authorization, plan and manifest must be the same exact revision');
      }
      // Repair 3: the plan's config root must equal the root bound in the
      // authorization record. This refuses a redirection at the earliest
      // point (recheckBeforeExecute enforces the same binding again
      // immediately before the executor); a record with no bound root
      // also refuses, since it cannot prove its root.
      if (plan.config_root !== record.config_root) {
        throw new Error('Config root redirected since authorization; re-stage and re-authorize before promotion');
      }
      if (plan.config_sha256 !== record.config_sha256 || plan.config_source !== record.config_source ||
          plan.config_state !== 'applied-source') {
        throw new Error('Config hash/source mismatch or proposal-only stage');
      }
    }
    assertChecksMatch(record, webCheck, apiCheck);
    // Recheck immediately before deploy, under the lock (B3).
    const freshPlan = recheckBeforeExecute({ stage, revision, record, webCheck, apiCheck, dryRun });
    const dryCommand = target === 'asset' ? freshPlan.proposed_command
      : target === 'api' ? 'wrangler deploy --config wrangler.api.toml'
      : 'wrangler deploy --config wrangler.toml';
    if (!execute) {
      return { dryRun: true, command: dryCommand, revision };
    }
    const configPath = target === 'asset' ? resolve(stage, 'wrangler.assets.toml')
      : target === 'api' ? resolve(repoRoot(), API_CONFIG_FILE)
      : resolve(repoRoot(), ROLLBACK_CONFIG_FILE);
    const command = dryCommand;
    executor(command, { configPath, revision, target });
    if (target === 'asset') transitionPlanState(stage, ['authorized'], 'deployed');
    const receipt = {
      revision, target, command, executed_at: new Date().toISOString(),
      // The API receipt names the intended service explicitly, so
      // readiness validation can check it (repair 2, blocker 1).
      ...(target === 'api' ? { service: 'live-options-api' } : {}),
      web_check_run_id: record.web_check_run_id, api_check_run_id: record.api_check_run_id,
    };
    mkdirSync(releasesDir(), { recursive: true });
    writeFileSync(receiptPath(revision, target), JSON.stringify(receipt, null, 2) + '\n');
    return { deployed: true, ...receipt };
  } finally {
    lock.release();
  }
}
export function deployStaged(args) {
  return deploy({ ...args, target: 'asset' });
}
export function deployApi(args) {
  return deploy({ ...args, target: 'api' });
}
export function deployRollback(args) {
  return deploy({ ...args, target: 'rollback' });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // Flags win; env vars are the fallback (F1: workflows pass everything via
  // env, so no workflow input is ever interpolated into a run: step).
  const args = process.argv.slice(2);
  const get = (k, env) => {
    const i = args.indexOf(k);
    if (i >= 0 && args[i + 1] !== undefined) return args[i + 1];
    return process.env[env] || undefined;
  };
  const has = (k, env) => args.includes(k) || process.env[env] === 'true' || process.env[env] === '1';
  try {
    const target = get('--target', 'DEPLOY_TARGET') || 'asset';
    if (has('--record-api-service', 'DEPLOY_RECORD_API_SERVICE')) {
      console.log(JSON.stringify(recordApiServiceVerified({
        revision: get('--revision', 'DEPLOY_REVISION'),
        verifier: get('--authorizer', 'DEPLOY_AUTHORIZER'),
      })));
    } else if (has('--authorize', 'DEPLOY_AUTHORIZE')) {
      console.log(JSON.stringify(authorize({
        target,
        stage: get('--stage', 'DEPLOY_STAGE'),
        revision: get('--revision', 'DEPLOY_REVISION'),
        webCheck: get('--web-check', 'DEPLOY_WEB_CHECK'),
        apiCheck: get('--api-check', 'DEPLOY_API_CHECK'),
        authorizer: get('--authorizer', 'DEPLOY_AUTHORIZER'),
        rollbackReason: get('--rollback-reason', 'DEPLOY_ROLLBACK_REASON'),
        dryRun: has('--dry-run', 'DEPLOY_DRY_RUN'),
      })));
    } else if (has('--supersede', 'DEPLOY_SUPERSEDE')) {
      console.log(JSON.stringify(supersede({ revision: get('--revision', 'DEPLOY_REVISION'), supersededBy: get('--superseded-by', 'DEPLOY_SUPERSEDED_BY') })));
    } else if (has('--deploy', 'DEPLOY_DEPLOY')) {
      console.log(JSON.stringify(deploy({
        target,
        stage: get('--stage', 'DEPLOY_STAGE'),
        revision: get('--revision', 'DEPLOY_REVISION'),
        execute: has('--execute', 'DEPLOY_EXECUTE'),
        webCheck: get('--web-check', 'DEPLOY_WEB_CHECK') || null,
        apiCheck: get('--api-check', 'DEPLOY_API_CHECK') || null,
        dryRun: has('--dry-run', 'DEPLOY_DRY_RUN'),
      })));
    } else throw new Error('Usage: --authorize|--deploy|--supersede|--record-api-service [--target asset|api|rollback] --stage DIR --revision FULL_SHA [--execute] [--web-check id:success] [--api-check id:success] [--authorizer NAME] [--superseded-by REV] [--rollback-reason TEXT]');
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
