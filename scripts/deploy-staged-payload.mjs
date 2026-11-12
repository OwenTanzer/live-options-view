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
// only the Worker (wrangler.toml) and has no producer effects.
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
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, verifyArtifact } from './build-assets.mjs';
import { transitionPlanState, verifyStage } from './prepare-asset-release.mjs';

// External to the candidate tree by default. DEPLOY_RELEASES_DIR (tests/CI)
// wins; otherwise a sibling directory next to the checkout. Never inside
// the tree: authorizing must not move HEAD (B1).
const releasesDir = () => process.env.DEPLOY_RELEASES_DIR || resolve(ROOT, '..', '.deploy-releases');
// Which checkout the gate verifies. Tests point it at an isolated clone.
const repoRoot = () => process.env.DEPLOY_REPO_ROOT || ROOT;
const authorizationPath = revision => resolve(releasesDir(), `${revision}.authorization.json`);
const receiptPath = revision => resolve(releasesDir(), `${revision}.deploy-receipt.json`);
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
  if (webCheck) {
    const web = parseCheck(webCheck, '--web-check');
    if (web.runId !== record.web_check_run_id || web.status !== 'success') {
      throw new Error('Web check evidence does not match the authorization record');
    }
  } else if (record.web_check_status !== 'success') throw new Error('Web check did not succeed for this revision');
  if (apiCheck) {
    const api = parseCheck(apiCheck, '--api-check');
    if (api.runId !== record.api_check_run_id || api.status !== 'success') {
      throw new Error('API check evidence does not match the authorization record');
    }
  } else if (record.api_check_status !== 'success') throw new Error('API check did not succeed for this revision');
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
// wrangler.toml only, no producer effects). The record is written to the
// EXTERNAL releases dir (B1); authorizing never touches the candidate tree.
// A newer authorization supersedes every older one, so at most one
// candidate is deployable at a time. All record mutations run under the
// shared release lock, so supersession cannot interleave with a deploy.
export function authorize({ target = 'asset', stage, revision, webCheck, apiCheck, authorizer }) {
  if (target !== 'asset' && target !== 'api') throw new Error("target must be 'asset' or 'api'");
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  const web = parseCheck(webCheck, '--web-check');
  const api = parseCheck(apiCheck, '--api-check');
  if (web.status !== 'success' || api.status !== 'success') {
    throw new Error('Authorization requires successful web and API checks for the exact revision');
  }
  if (!authorizer) throw new Error('Authorization requires --authorizer');
  let plan = null;
  if (target === 'asset') {
    if (!stage) throw new Error('Asset authorization requires --stage');
    plan = verifyStage(stage);
    if (plan.asset_revision !== revision) throw new Error('Stage revision does not match --revision');
    if (plan.config_state !== 'applied-source') {
      throw new Error('Proposal-only stages can never deploy; an applied root config is required');
    }
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
      config_source: plan ? plan.config_source : 'wrangler.toml',
      config_sha256: plan ? plan.config_sha256 : null,
      web_check_run_id: web.runId, web_check_status: web.status,
      api_check_run_id: api.runId, api_check_status: api.status,
      authorizer, authorized_at: new Date().toISOString(), superseded_by: null,
      authorization_kind: 'external-artifact',
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

// Final guard, called immediately before the executor runs, inside the
// shared lock (B3). Re-reads the authorization record, the plan (asset
// only) and HEAD from scratch, so a supersession that landed after the
// first checks is caught here and the executor never runs for a stale
// candidate.
export function recheckBeforeExecute({ stage, revision, record, webCheck = null, apiCheck = null }) {
  const fresh = loadAuthorization(revision);
  if (fresh.superseded_by) throw new Error(`Revision ${revision} is superseded by ${fresh.superseded_by}`);
  assertHeadMatches(fresh);
  assertChecksMatch(fresh, webCheck, apiCheck);
  if (fresh.target === 'asset') {
    const freshPlan = verifyStage(stage, ['authorized']);
    if (freshPlan.config_sha256 !== fresh.config_sha256 || freshPlan.config_state !== 'applied-source') {
      throw new Error('Stage config drifted while waiting for the lock');
    }
    return freshPlan;
  }
  return null;
}

// Default executor runs wrangler from explicit fields as an argv array —
// never by shell-parsing the PowerShell-literal proposed_command string,
// which is review material for the Windows operating procedure, not a
// shell command on this machine.
export function defaultExecutor(command, { configPath, revision, target }) {
  if (target === 'api') {
    execFileSync('wrangler', ['deploy', '--config', configPath], { stdio: 'inherit' });
  } else {
    execFileSync('wrangler', ['deploy', '--config', configPath, '--var', `ASSET_REVISION:${revision}`], { stdio: 'inherit' });
  }
  return command;
}

export function deploy({ target = 'asset', stage, revision, execute = false, webCheck = null, apiCheck = null, executor = defaultExecutor }) {
  if (target !== 'asset' && target !== 'api') throw new Error("target must be 'asset' or 'api'");
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
      plan = verifyStage(stage, ['authorized']);
      manifest = verifyArtifact(resolve(stage, 'payload'));
      if (plan.asset_revision !== revision || manifest.revision !== revision) {
        throw new Error('Revision mismatch: HEAD, authorization, plan and manifest must be the same exact revision');
      }
      if (plan.config_sha256 !== record.config_sha256 || plan.config_source !== record.config_source ||
          plan.config_state !== 'applied-source') {
        throw new Error('Config hash/source mismatch or proposal-only stage');
      }
    }
    assertChecksMatch(record, webCheck, apiCheck);
    // Recheck immediately before deploy, under the lock (B3).
    const freshPlan = recheckBeforeExecute({ stage, revision, record, webCheck, apiCheck });
    if (!execute) {
      return target === 'asset'
        ? { dryRun: true, command: freshPlan.proposed_command, revision }
        : { dryRun: true, command: 'wrangler deploy --config wrangler.toml', revision };
    }
    const configPath = target === 'asset' ? resolve(stage, 'wrangler.assets.toml') : resolve(repoRoot(), 'wrangler.toml');
    const command = target === 'asset' ? freshPlan.proposed_command : 'wrangler deploy --config wrangler.toml';
    executor(command, { configPath, revision, target });
    if (target === 'asset') transitionPlanState(stage, ['authorized'], 'deployed');
    const receipt = {
      revision, target, command, executed_at: new Date().toISOString(),
      web_check_run_id: record.web_check_run_id, api_check_run_id: record.api_check_run_id,
    };
    mkdirSync(releasesDir(), { recursive: true });
    writeFileSync(receiptPath(revision), JSON.stringify(receipt, null, 2) + '\n');
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
    if (has('--authorize', 'DEPLOY_AUTHORIZE')) {
      console.log(JSON.stringify(authorize({
        target,
        stage: get('--stage', 'DEPLOY_STAGE'),
        revision: get('--revision', 'DEPLOY_REVISION'),
        webCheck: get('--web-check', 'DEPLOY_WEB_CHECK'),
        apiCheck: get('--api-check', 'DEPLOY_API_CHECK'),
        authorizer: get('--authorizer', 'DEPLOY_AUTHORIZER'),
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
      })));
    } else throw new Error('Usage: --authorize|--deploy|--supersede [--target asset|api] --stage DIR --revision FULL_SHA [--execute] [--web-check id:success] [--api-check id:success] [--authorizer NAME] [--superseded-by REV]');
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
