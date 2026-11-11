// scripts/deploy-staged-payload.mjs — G3b staged-payload promotion gate.
// Local only. Default is --dry-run: print the plan's command and exit 0 only
// if every guard passes; nothing executes. --execute requires a checked-in
// authorization record for the exact revision and rechecks every guard
// immediately before executing, under a lock, so an old candidate A cannot
// overwrite a newer authorized candidate B. The workflow that calls this is
// a local proposal until the C2 packet is approved; this script never
// deploys on its own, never merges, and never touches a collector.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, verifyArtifact } from './build-assets.mjs';
import { transitionPlanState, verifyStage } from './prepare-asset-release.mjs';

const releasesDir = () => process.env.DEPLOY_RELEASES_DIR || resolve(ROOT, 'docs/releases');
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
  return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim();
}

export function authorizeStage({ stage, revision, webCheck, apiCheck, authorizer }) {
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  const web = parseCheck(webCheck, '--web-check');
  const api = parseCheck(apiCheck, '--api-check');
  if (web.status !== 'success' || api.status !== 'success') {
    throw new Error('Authorization requires successful web and API checks for the exact revision');
  }
  if (!authorizer) throw new Error('Authorization requires --authorizer');
  const plan = verifyStage(stage);
  if (plan.asset_revision !== revision) throw new Error('Stage revision does not match --revision');
  if (plan.config_state !== 'applied-source') {
    throw new Error('Proposal-only stages can never deploy; an applied root config is required');
  }
  // A newer authorization supersedes every older one, so at most one
  // candidate is deployable at a time. Mark the older records (and their
  // stages, where they still exist) before writing the new one.
  if (existsSync(releasesDir())) {
    for (const name of readdirSync(releasesDir())) {
      if (!name.endsWith('.authorization.json')) continue;
      const older = JSON.parse(readFileSync(resolve(releasesDir(), name), 'utf8'));
      if (older.revision === revision || older.superseded_by) continue;
      older.superseded_by = revision;
      saveAuthorization(older);
      try {
        const olderPlan = JSON.parse(readFileSync(resolve(older.stage_path, 'plan.json'), 'utf8'));
        if (olderPlan.state === 'authorized') transitionPlanState(older.stage_path, ['authorized'], 'superseded');
      } catch { /* stage already gone; the record is the authority */ }
    }
  }
  const record = {
    revision, stage_path: resolve(stage),
    config_source: plan.config_source, config_sha256: plan.config_sha256,
    web_check_run_id: web.runId, web_check_status: web.status,
    api_check_run_id: api.runId, api_check_status: api.status,
    authorizer, authorized_at: new Date().toISOString(), superseded_by: null,
  };
  saveAuthorization(record);
  transitionPlanState(stage, ['prepared-not-approved-not-deployed'], 'authorized');
  return record;
}

export function supersedeStage({ revision, supersededBy }) {
  const record = loadAuthorization(revision);
  if (record.superseded_by) throw new Error(`Revision ${revision} is already superseded by ${record.superseded_by}`);
  record.superseded_by = supersededBy;
  saveAuthorization(record);
  try {
    const plan = JSON.parse(readFileSync(resolve(record.stage_path, 'plan.json'), 'utf8'));
    if (plan.state === 'authorized') transitionPlanState(record.stage_path, ['authorized'], 'superseded');
  } catch { /* stage already gone; the record is the authority */ }
  return record;
}

export function acquireLock(stage) {
  const lockPath = resolve(stage) + '.lock';
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
    if (!stale) throw new Error(`Deploy lock held for ${stage}`);
    rmSync(lockPath, { recursive: true, force: true });
    return tryOnce();
  }
}

// Default executor runs wrangler from the plan's own fields (config path +
// revision) as an argv array — never by shell-parsing the PowerShell-literal
// proposed_command string, which is review material for the Windows
// operating procedure, not a shell command on this machine.
export function defaultExecutor(command, { configPath, revision }) {
  execFileSync('wrangler', ['deploy', '--config', configPath, '--var', `ASSET_REVISION:${revision}`], { stdio: 'inherit' });
  return command;
}

export function deployStaged({ stage, revision, execute = false, webCheck = null, apiCheck = null, executor = defaultExecutor }) {
  const record = loadAuthorization(revision);
  if (record.superseded_by) throw new Error(`Revision ${revision} is superseded by ${record.superseded_by}`);
  const plan = verifyStage(stage, ['authorized']);
  const manifest = verifyArtifact(resolve(stage, 'payload'));
  if (headRevision() !== revision || record.revision !== revision ||
      plan.asset_revision !== revision || manifest.revision !== revision) {
    throw new Error('Revision mismatch: HEAD, authorization, plan and manifest must be the same exact revision');
  }
  if (plan.config_sha256 !== record.config_sha256 || plan.config_source !== record.config_source ||
      plan.config_state !== 'applied-source') {
    throw new Error('Config hash/source mismatch or proposal-only stage');
  }
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

  const lock = acquireLock(stage);
  try {
    // Recheck immediately before deploy, under the lock: the stage, the
    // authorization and HEAD must still agree at this instant.
    const fresh = loadAuthorization(revision);
    if (fresh.superseded_by) throw new Error(`Revision ${revision} was superseded while waiting for the lock`);
    const freshPlan = verifyStage(stage, ['authorized']);
    if (headRevision() !== revision) throw new Error('HEAD moved while waiting for the lock');
    if (freshPlan.config_sha256 !== fresh.config_sha256 || freshPlan.config_state !== 'applied-source') {
      throw new Error('Stage config drifted while waiting for the lock');
    }
    if (!execute) return { dryRun: true, command: freshPlan.proposed_command, revision };
    const configPath = resolve(stage, 'wrangler.assets.toml');
    executor(freshPlan.proposed_command, { configPath, revision });
    transitionPlanState(stage, ['authorized'], 'deployed');
    const receipt = {
      revision, command: freshPlan.proposed_command, executed_at: new Date().toISOString(),
      web_check_run_id: fresh.web_check_run_id, api_check_run_id: fresh.api_check_run_id,
    };
    mkdirSync(releasesDir(), { recursive: true });
    writeFileSync(receiptPath(revision), JSON.stringify(receipt, null, 2) + '\n');
    return { deployed: true, ...receipt };
  } finally {
    lock.release();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2), get = k => args[args.indexOf(k) + 1];
  const has = k => args.includes(k);
  try {
    if (has('--authorize')) {
      console.log(JSON.stringify(authorizeStage({
        stage: get('--stage'), revision: get('--revision'),
        webCheck: get('--web-check'), apiCheck: get('--api-check'),
        authorizer: get('--authorizer'),
      })));
    } else if (has('--supersede')) {
      console.log(JSON.stringify(supersedeStage({ revision: get('--revision'), supersededBy: get('--superseded-by') })));
    } else if (has('--deploy')) {
      console.log(JSON.stringify(deployStaged({
        stage: get('--stage'), revision: get('--revision'), execute: has('--execute'),
        webCheck: has('--web-check') ? get('--web-check') : null,
        apiCheck: has('--api-check') ? get('--api-check') : null,
      })));
    } else throw new Error('Usage: --authorize|--deploy|--supersede --stage DIR --revision FULL_SHA [--execute] [--web-check id:success] [--api-check id:success] [--authorizer NAME] [--superseded-by REV]');
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
