// tests/deploy_sequencing.test.mjs — G3b: staged-payload promotion gate.
// All stages live in temp dirs; authorization records live in a temp
// releases dir (DEPLOY_RELEASES_DIR). No provider, no wrangler, no real
// deploy: execution uses a stub executor. The gate under test is the
// ordering and recheck logic, not the wrangler invocation.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { buildAssets } from '../scripts/build-assets.mjs';
import { prepareRelease, verifyStage } from '../scripts/prepare-asset-release.mjs';
import { authorizeStage, authorizeApi, authorizeRollback, deployStaged, deployApi, deployRollback, recordApiServiceVerified, apiServiceReady, verifyCurrentSourceFreshness, acquireReleaseLock, releaseLockPath, recheckBeforeExecute } from '../scripts/deploy-staged-payload.mjs';

// F3: fileURLToPath, not URL.pathname — the pathname form breaks on
// Windows ("/C:/..." is not a usable path there).
const ROOT = resolve(fileURLToPath(new URL('../', import.meta.url)));
const HEAD = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim();
const OLD = 'bff49eedddbc816df5633bb5f492867caa8036ce';
// Recorded combined build (merge PR #223) and its wrangler.toml hash —
// the restoration revision for the local rollback procedure (repair 2).
const ROLLBACK_REV = '6c1051a7b4fcbc115551562b2eb531c13a440e2e';
const ROLLBACK_CONFIG_SHA = '213c2e56a9f75cd0cdbce4e967022d12581cd711701f091cc6289214648eea72';
const PROPOSED = readFileSync(join(ROOT, 'patches/musef-225/wrangler.assets.toml.proposed'), 'utf8');
const WEB = 'web-100:success';
const API = 'api-200:success';

const releases = mkdtempSync(join(tmpdir(), 'deploy-seq-releases-'));
process.env.DEPLOY_RELEASES_DIR = releases;
test.after(() => {
  rmSync(releases, { recursive: true, force: true });
  delete process.env.DEPLOY_RELEASES_DIR;
});

// Build a stage for `revision`. applied=true stages from an applied root
// config (a temp configRoot carrying wrangler.assets.toml); applied=false
// stages from the proposal-only template in an isolated temp configRoot.
// The real applied-source config now exists at the repo root
// (wrangler.assets.toml, C2 decision packet); the temp fixture is retained
// so the proposal-only path stays testable in isolation.
function makeStage(revision, applied = true) {
  const base = mkdtempSync(join(tmpdir(), 'deploy-seq-'));
  const artifact = join(base, 'artifact');
  buildAssets({ out: artifact, revision });
  const configRoot = join(base, 'config');
  mkdirSync(configRoot, { recursive: true });
  if (applied) writeFileSync(join(configRoot, 'wrangler.assets.toml'), PROPOSED);
  else {
    mkdirSync(join(configRoot, 'patches', 'musef-225'), { recursive: true });
    writeFileSync(join(configRoot, 'patches', 'musef-225', 'wrangler.assets.toml.proposed'), PROPOSED);
  }
  const stage = join(base, 'stage');
  prepareRelease({ artifact, out: stage, configRoot });
  return { base, stage };
}
function cleanup({ base }) {
  rmSync(base, { recursive: true, force: true });
}
function planState(stage) {
  return JSON.parse(readFileSync(join(stage, 'plan.json'), 'utf8')).state;
}
const stubExecutor = () => {
  const calls = [];
  return {
    calls,
    fn: (command, opts) => { calls.push({ command, ...(opts || {}) }); },
  };
};
// Deployment order (C2 repair): the asset service's API binding points at
// the live-options-api service, so the API service must exist before any
// asset authorize or deploy. Establish API readiness once for this file's
// shared releases dir (api authorize + stub deploy writes the receipt), so
// the asset tests below exercise gate ordering rather than the setup.
// Tests that use a separate releases dir establish their own readiness.
authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
deployApi({ revision: HEAD, execute: true, executor: stubExecutor().fn });
// A clean clone of the candidate checkout (an isolated copy of the exact
// committed tree). Used by the B1 tests so the authorization mechanism is
// exercised against a real checkout, not the working tree.
function cleanClone() {
  const wt = mkdtempSync(join(tmpdir(), 'deploy-b1-clone-'));
  rmSync(wt, { recursive: true, force: true });
  execFileSync('git', ['clone', '-q', ROOT, wt]);
  return wt;
}
// A checkout at an arbitrary revision (the recorded combined build).
// Used by the rollback-restoration test: the gate script comes from the
// candidate checkout, while DEPLOY_REPO_ROOT points at this restoration
// checkout — the two checkouts are different revisions by design.
function cloneAt(revision) {
  const wt = cleanClone();
  execFileSync('git', ['checkout', '-q', '--detach', revision], { cwd: wt });
  return wt;
}

test('deploy refuses failed web check for exact revision', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, webCheck: 'web-101:failure', apiCheck: API }),
      /Web check evidence does not match/);
  } finally { cleanup(s); }
});

test('deploy refuses failed API check for exact revision', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: 'api-201:failure' }),
      /API check evidence does not match/);
  } finally { cleanup(s); }
});

test('deploy refuses verify-stage drift (tampered payload/config/command)', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    writeFileSync(join(s.stage, 'payload', 'site', 'index.html'), '<html>tampered</html>');
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /contract mismatch|Hash mismatch|inventory/i);
    assert.equal(planState(s.stage), 'authorized');
  } finally { cleanup(s); }
});

test('deploy refuses revision mismatch (HEAD is not the authorized revision)', () => {
  const s = makeStage(OLD);
  try {
    authorizeStage({ stage: s.stage, revision: OLD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    // HEAD is the current commit, not OLD, so the recheck must refuse even
    // though the authorization record itself is well-formed.
    assert.throws(() => deployStaged({ stage: s.stage, revision: OLD }), /Revision mismatch/);
  } finally { cleanup(s); }
});

test('deploy refuses config hash/source mismatch', () => {
  const s = makeStage(HEAD);
  try {
    const record = authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    record.config_sha256 = '0'.repeat(64);
    writeFileSync(join(releases, `${HEAD}.authorization.json`), JSON.stringify(record, null, 2) + '\n');
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /Config hash\/source mismatch/);
  } finally { cleanup(s); }
});

test('deploy refuses proposal-only stage', () => {
  const s = makeStage(HEAD, false);
  try {
    assert.equal(planState(s.stage), 'prepared-not-approved-not-deployed');
    assert.throws(() => authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' }),
      /Proposal-only stages can never deploy/);
  } finally { cleanup(s); }
});

test('deploy refuses superseded candidate A after B authorized, and succeeds for B in order', () => {
  const a = makeStage(OLD);
  const b = makeStage(HEAD);
  try {
    authorizeStage({ stage: a.stage, revision: OLD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.equal(planState(a.stage), 'authorized');
    authorizeStage({ stage: b.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.equal(planState(a.stage), 'superseded');
    assert.equal(planState(b.stage), 'authorized');
    assert.throws(() => deployStaged({ stage: a.stage, revision: OLD }), /superseded/);
    const exec = stubExecutor();
    const result = deployStaged({ stage: b.stage, revision: HEAD, execute: true, executor: exec.fn });
    assert.equal(result.deployed, true);
    assert.equal(exec.calls.length, 1);
    assert.equal(planState(b.stage), 'deployed');
    assert.ok(existsSync(join(releases, `${HEAD}.asset.deploy-receipt.json`)));
  } finally { cleanup(a); cleanup(b); }
});

test('concurrent deploy attempts serialize (second refuses, never interleaves)', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    // B3: ONE shared release lock, not one per stage.
    const lock = acquireReleaseLock();
    try {
      assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /Deploy lock held/);
    } finally { lock.release(); }
    const dry = deployStaged({ stage: s.stage, revision: HEAD });
    assert.equal(dry.dryRun, true);
    assert.equal(planState(s.stage), 'authorized');
  } finally { cleanup(s); }
});

test('stale lock is cleared so a later deploy can proceed', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const lockPath = releaseLockPath();
    mkdirSync(lockPath);
    writeFileSync(join(lockPath, 'owner.json'), JSON.stringify({ pid: 999999, startedAt: Date.now() - 20 * 60 * 1000 }) + '\n');
    const dry = deployStaged({ stage: s.stage, revision: HEAD });
    assert.equal(dry.dryRun, true);
    assert.equal(existsSync(lockPath), false);
  } finally { cleanup(s); }
});

test('failed deploy leaves plan state and lock clean', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const failing = () => { throw new Error('wrangler unavailable in test'); };
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, executor: failing }),
      /wrangler unavailable in test/);
    assert.equal(planState(s.stage), 'authorized');
    assert.equal(existsSync(releaseLockPath()), false);
  } finally { cleanup(s); }
});

test('dry-run prints the command and deploys nothing', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const exec = stubExecutor();
    // The A/B test above wrote a HEAD asset receipt in the shared temp
    // releases dir; clear it so this test can prove a dry-run writes none.
    rmSync(join(releases, `${HEAD}.asset.deploy-receipt.json`), { force: true });
    const result = deployStaged({ stage: s.stage, revision: HEAD, executor: exec.fn });
    assert.equal(result.dryRun, true);
    assert.match(result.command, /wrangler deploy --config/);
    assert.equal(exec.calls.length, 0);
    assert.equal(planState(s.stage), 'authorized');
    assert.equal(existsSync(join(releases, `${HEAD}.asset.deploy-receipt.json`)), false);
  } finally { cleanup(s); }
});

// B1 — Authorization lives outside the candidate tree. A clean clone of
// the candidate can be authorized and dry-run verified without the record
// ever entering the tree, and HEAD never moves. This is the fix for the
// paradox: with the record in-tree, committing it moves HEAD and the gate
// can never be satisfied (see the next test).
test('B1: clean checkout authorizes with an external record; HEAD never moves', () => {
  const wt = cleanClone();
  const ext = mkdtempSync(join(tmpdir(), 'deploy-b1-rel-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  const prevRoot = process.env.DEPLOY_REPO_ROOT;
  process.env.DEPLOY_RELEASES_DIR = ext;
  process.env.DEPLOY_REPO_ROOT = wt;
  const s = makeStage(HEAD);
  try {
    assert.equal(execFileSync('git', ['status', '--porcelain'], { cwd: wt, encoding: 'utf8' }).trim(), '');
    // Deployment order: this fresh releases dir has no API readiness, so
    // establish it (api authorize + stub deploy) before authorizing the asset.
    authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    deployApi({ revision: HEAD, execute: true, executor: stubExecutor().fn });
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    // The record is outside the checkout: the tree is still clean and HEAD
    // is the same commit it was before authorizing.
    assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: wt, encoding: 'utf8' }).trim(), HEAD);
    assert.equal(execFileSync('git', ['status', '--porcelain'], { cwd: wt, encoding: 'utf8' }).trim(), '');
    assert.ok(!existsSync(join(wt, 'docs', 'releases', `${HEAD}.authorization.json`)),
      'the authorization record must not be in the candidate tree');
    assert.ok(existsSync(join(ext, `${HEAD}.authorization.json`)));
    const dry = deployStaged({ stage: s.stage, revision: HEAD });
    assert.equal(dry.dryRun, true);
    assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: wt, encoding: 'utf8' }).trim(), HEAD);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    if (prevRoot === undefined) delete process.env.DEPLOY_REPO_ROOT;
    else process.env.DEPLOY_REPO_ROOT = prevRoot;
    rmSync(ext, { recursive: true, force: true });
    rmSync(wt, { recursive: true, force: true });
  }
});

// B1 — The paradox, reproduced on the old in-tree behavior: the record is
// written inside the checkout, committed, and HEAD moves. The old revision
// then fails with Revision mismatch and the new HEAD has No record —
// endlessly. (The in-tree default is gone; this test forces the releases
// dir into the clone to prove the paradox exists if it ever returns.)
test('B1: in-tree authorization record is a paradox (old behavior)', () => {
  const wt = cleanClone();
  const inTree = join(wt, 'docs', 'releases');
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  const prevRoot = process.env.DEPLOY_REPO_ROOT;
  process.env.DEPLOY_RELEASES_DIR = inTree;
  process.env.DEPLOY_REPO_ROOT = wt;
  const s = makeStage(HEAD);
  try {
    // Deployment order: this in-tree releases dir has no API readiness, so
    // establish it (api authorize + stub deploy) before authorizing the asset.
    authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    deployApi({ revision: HEAD, execute: true, executor: stubExecutor().fn });
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', '-C', wt, 'add', 'docs/releases']);
    execFileSync('git', ['-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', wt, 'commit', '-q', '-m', 'authorize']);
    const newHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: wt, encoding: 'utf8' }).trim();
    assert.notEqual(newHead, HEAD);
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /Revision mismatch/);
    assert.throws(() => deployStaged({ stage: s.stage, revision: newHead }), /No authorization record/);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    if (prevRoot === undefined) delete process.env.DEPLOY_REPO_ROOT;
    else process.env.DEPLOY_REPO_ROOT = prevRoot;
    rmSync(wt, { recursive: true, force: true, maxRetries: 4 });
  }
});

// B2 — One authoritative release lane. Neither workflow auto-deploys on
// push, neither deploys except through the gate script, and both share one
// concurrency group. The old deploy.yml (push trigger + wrangler-action)
// fails every one of these assertions — that is the bypass this removes.
test('B2: one authoritative release lane, no automatic deploy bypass', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  const deploy = read('deploy.yml');
  const staged = read('deploy-staged.yml');
  assert.ok(!/^\s*push:/m.test(deploy), 'deploy.yml must not auto-deploy on push');
  assert.ok(!/^\s*push:/m.test(staged), 'deploy-staged.yml must not auto-deploy on push');
  assert.ok(!deploy.includes('wrangler-action'), 'deploy.yml must not deploy outside the gate');
  assert.ok(!staged.includes('wrangler-action'), 'deploy-staged.yml must not deploy outside the gate');
  assert.match(deploy, /group: deploy-live-options-view/, 'deploy.yml must share the release concurrency group');
  assert.match(staged, /group: deploy-live-options-view/, 'deploy-staged.yml must share the release concurrency group');
  assert.ok(deploy.includes('scripts/deploy-staged-payload.mjs'), 'deploy.yml must call the gate');
  assert.ok(staged.includes('scripts/deploy-staged-payload.mjs'), 'deploy-staged.yml must call the gate');
  // Regression witness: the old automatic deploy.yml fails all of these.
  const oldDeploy = [
    'name: Deploy Cloudflare Worker', 'on:', '  push:', '    branches: [master]',
    '    paths:', '      - "site/**"', '      - "backend/**"',
    'jobs:', '  deploy:', '    steps:',
    '      - uses: cloudflare/wrangler-action@v3',
  ].join('\n');
  assert.ok(/^\s*push:/m.test(oldDeploy), 'witness: old deploy.yml had a push trigger');
  assert.ok(oldDeploy.includes('wrangler-action'), 'witness: old deploy.yml deployed via wrangler-action');
  assert.ok(!/group: deploy-live-options-view/.test(oldDeploy), 'witness: old deploy.yml had no concurrency group');
});

// B1 — The releases dir may not exist yet (first authorize in a fresh
// dir). The lock mkdir used to fail ENOENT there (Brian slice 8 residual);
// authorize must create the dir.
test('B1: authorize creates the releases dir when absent', () => {
  const fresh = join(mkdtempSync(join(tmpdir(), 'deploy-rel-missing-')), 'releases');
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  try {
    assert.ok(!existsSync(fresh));
    const record = authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.equal(record.target, 'api');
    assert.ok(existsSync(join(fresh, `${HEAD}.authorization.json`)));
  } finally {
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// B2 — The API lane is separately explicit and gated: its own authorization
// record, its own dispatch, wrangler.api.toml only (never the combined
// wrangler.toml), no producer effects.
test('B2: API lane is separately explicit and gated (target api)', () => {
  const record = authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
  assert.equal(record.target, 'api');
  assert.equal(record.stage_path, null);
  assert.equal(record.config_source, 'wrangler.api.toml');
  const dry = deployApi({ revision: HEAD });
  assert.equal(dry.dryRun, true);
  assert.equal(dry.command, 'wrangler deploy --config wrangler.api.toml');
  const exec = stubExecutor();
  const result = deployApi({ revision: HEAD, execute: true, executor: exec.fn });
  assert.equal(result.deployed, true);
  assert.equal(exec.calls.length, 1);
  assert.equal(exec.calls[0].target, 'api');
  assert.ok(exec.calls[0].configPath.endsWith('wrangler.api.toml'));
  assert.equal(exec.calls[0].configPath, resolve(ROOT, 'wrangler.api.toml'),
    'the api lane must target wrangler.api.toml, never the combined wrangler.toml');
  assert.ok(existsSync(join(releases, `${HEAD}.api.deploy-receipt.json`)));
});

// B2 — The api lane refuses to authorize without wrangler.api.toml, and
// refuses to deploy if the config drifted after authorization. The temp
// repo root stands in for a checkout lacking the applied API config.
test('B2: API lane refuses without wrangler.api.toml and on config drift', () => {
  const wt = cleanClone();
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  const prevRoot = process.env.DEPLOY_REPO_ROOT;
  process.env.DEPLOY_RELEASES_DIR = mkdtempSync(join(tmpdir(), 'deploy-api-rel-'));
  process.env.DEPLOY_REPO_ROOT = wt;
  try {
    rmSync(join(wt, 'wrangler.api.toml'));
    assert.throws(() => authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' }),
      /API config missing/);
    execFileSync('git', ['-C', wt, 'checkout', '--', 'wrangler.api.toml']);
    authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    writeFileSync(join(wt, 'wrangler.api.toml'), readFileSync(join(wt, 'wrangler.api.toml'), 'utf8') + '# drift\n');
    assert.throws(() => deployApi({ revision: HEAD }), /API config drifted/);
  } finally {
    const rel = process.env.DEPLOY_RELEASES_DIR;
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    if (prevRoot === undefined) delete process.env.DEPLOY_REPO_ROOT;
    else process.env.DEPLOY_REPO_ROOT = prevRoot;
    rmSync(rel, { recursive: true, force: true });
    rmSync(wt, { recursive: true, force: true });
  }
});

// C2 repair — Deployment order: the asset service's API binding points at
// the live-options-api service, so the API service must exist before any
// asset authorize or deploy. A fresh releases dir has no API evidence, so
// the asset authorize must refuse there.
test('Order: asset authorize refuses without API readiness (fresh releases dir)', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-order-fresh-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    assert.throws(() => authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' }),
      /API service not ready/);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// C2 repair — The API deploy enables the asset authorize; removing the API
// readiness evidence afterwards makes the asset deploy refuse, and an
// operator-recorded verification restores it.
test('Order: API deploy enables asset authorize; deploy refuses if readiness is removed', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-order-flow-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const apiResult = deployApi({ revision: HEAD, execute: true, executor: stubExecutor().fn });
    assert.equal(apiResult.deployed, true);
    assert.ok(existsSync(join(fresh, `${HEAD}.api.deploy-receipt.json`)));
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    rmSync(join(fresh, `${HEAD}.api.deploy-receipt.json`), { force: true });
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /API service not ready/);
    recordApiServiceVerified({ revision: HEAD, verifier: 'test' });
    const dry = deployStaged({ stage: s.stage, revision: HEAD });
    assert.equal(dry.dryRun, true);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// C2 repair — An operator who read back the live service list can record
// that verification; it satisfies readiness without an API deploy receipt.
test('Order: operator-recorded API-service verification satisfies readiness', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-order-verify-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    assert.throws(() => recordApiServiceVerified({ revision: HEAD, verifier: '' }), /requires --authorizer/);
    const rec = recordApiServiceVerified({ revision: HEAD, verifier: 'test' });
    assert.equal(rec.service, 'live-options-api');
    assert.equal(rec.verification_kind, 'operator-readback');
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const dry = deployStaged({ stage: s.stage, revision: HEAD });
    assert.equal(dry.dryRun, true);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// Repair 2, blocker 1 — Readiness is evidence, not a filename. A junk
// file named *.api.deploy-receipt.json must not satisfy the ordering
// guard (Brian's witness), nor must a well-formed receipt for the wrong
// target, the wrong command, or the wrong service, or an operator
// verification with no verifier.
test('Readiness: malformed/wrong-target/wrong-service evidence does not satisfy readiness', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-readiness-neg-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  try {
    // Brian's witness, verbatim shape: junk JSON in a receipt-named file.
    writeFileSync(join(fresh, 'junk.api.deploy-receipt.json'), 'not JSON, no provider or deployment');
    assert.equal(apiServiceReady(HEAD), false);
    // Well-formed JSON, wrong target (an asset receipt wearing an api name).
    writeFileSync(join(fresh, 'asset.api.deploy-receipt.json'), JSON.stringify({
      revision: HEAD, target: 'asset', command: 'wrangler deploy --config wrangler.assets.toml',
      executed_at: new Date().toISOString(),
    }));
    assert.equal(apiServiceReady(HEAD), false);
    // Well-formed JSON, right target, wrong command (not the API deploy).
    writeFileSync(join(fresh, 'wrongcmd.api.deploy-receipt.json'), JSON.stringify({
      revision: HEAD, target: 'api', command: 'wrangler deploy --config wrangler.toml',
      executed_at: new Date().toISOString(),
    }));
    assert.equal(apiServiceReady(HEAD), false);
    // Operator verification for the wrong service.
    writeFileSync(join(fresh, 'wrong.api-service-verified.json'), JSON.stringify({
      revision: HEAD, service: 'live-options-view', verified_at: new Date().toISOString(),
      verifier: 'test', verification_kind: 'operator-readback',
    }));
    assert.equal(apiServiceReady(HEAD), false);
    // Operator verification with no verifier.
    writeFileSync(join(fresh, 'noverifier.api-service-verified.json'), JSON.stringify({
      revision: HEAD, service: 'live-options-api', verified_at: new Date().toISOString(),
      verifier: '', verification_kind: 'operator-readback',
    }));
    assert.equal(apiServiceReady(HEAD), false);
    // A genuinely valid operator verification satisfies readiness.
    recordApiServiceVerified({ revision: HEAD, verifier: 'test' });
    assert.equal(apiServiceReady(HEAD), true);
  } finally {
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// Repair 2, blocker 1 — Cross-revision reuse is preserved: valid API
// evidence for ANY revision proves the named service exists, so a new
// asset candidate is not refused while the API service from an earlier
// candidate still exists.
test('Readiness: valid API evidence for another revision satisfies readiness for this revision', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-readiness-xrev-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    // A completed API deploy receipt for an EARLIER revision (written by
    // that candidate's gate run) proves the named service exists.
    writeFileSync(join(fresh, `${OLD}.api.deploy-receipt.json`), JSON.stringify({
      revision: OLD, target: 'api', service: 'live-options-api',
      command: 'wrangler deploy --config wrangler.api.toml',
      executed_at: new Date().toISOString(),
      web_check_run_id: 'web-100', api_check_run_id: 'api-200',
    }) + '\n');
    assert.equal(apiServiceReady(HEAD), true);
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const dry = deployStaged({ stage: s.stage, revision: HEAD });
    assert.equal(dry.dryRun, true);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// Repair 2, blocker 2 — Freshness is enforced in the promotion path.
// Brian's witness: stage and authorize from a valid applied configRoot,
// then replace that configRoot's wrangler.assets.toml with invalid text;
// deployStaged(execute:true) with a stub executor must NOT call the
// executor and must refuse.
test('Freshness: invalid current source config after authorize stops deploy before execution', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    writeFileSync(join(s.base, 'config', 'wrangler.assets.toml'), 'invalid current config');
    const exec = stubExecutor();
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, executor: exec.fn }),
      /Unsupported asset config/);
    assert.equal(exec.calls.length, 0, 'executor must never run for an invalid current source');
  } finally { cleanup(s); }
});

test('Freshness: drifted (valid but changed) current source config after authorize stops deploy', () => {
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    writeFileSync(join(s.base, 'config', 'wrangler.assets.toml'), PROPOSED.replace('2026-06-23', '2026-06-24'));
    const exec = stubExecutor();
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, executor: exec.fn }), /drifted/);
    assert.equal(exec.calls.length, 0, 'executor must never run for a drifted current source');
  } finally { cleanup(s); }
});

test('Freshness: authorize refuses when the current source config is already invalid', () => {
  const s = makeStage(HEAD);
  try {
    writeFileSync(join(s.base, 'config', 'wrangler.assets.toml'), 'invalid current config');
    assert.throws(() => authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' }),
      /Unsupported asset config/);
  } finally { cleanup(s); }
});

// Repair 3 — the selected config root is bound in the authorization
// record; any later redirection refuses. Brian's repair-2 witness:
// stage and authorize from a valid applied configRoot, invalidate the
// ORIGINAL source, then change ONLY plan.config_root to an old valid
// copy holding the same config — the stub executor must NOT run.
test('Freshness: redirected config_root after authorize stops deploy before execution', () => {
  const s = makeStage(HEAD);
  const oldRoot = join(s.base, 'old-config');
  mkdirSync(oldRoot, { recursive: true });
  writeFileSync(join(oldRoot, 'wrangler.assets.toml'), PROPOSED);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    writeFileSync(join(s.base, 'config', 'wrangler.assets.toml'), 'invalid current config');
    const planPath = join(s.stage, 'plan.json');
    const plan = JSON.parse(readFileSync(planPath, 'utf8'));
    plan.config_root = oldRoot;
    writeFileSync(planPath, JSON.stringify(plan, null, 2) + '\n');
    const exec = stubExecutor();
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, executor: exec.fn }),
      /redirect/i);
    assert.equal(exec.calls.length, 0, 'executor must never run for a redirected config root');
  } finally { cleanup(s); }
});

// Repair 3 — the authorization record binds the selected config root,
// and the unchanged normal path still deploys.
test('Freshness: authorization binds config_root and the unchanged path still deploys', () => {
  const s = makeStage(HEAD);
  try {
    const record = authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.equal(record.config_root, join(s.base, 'config'));
    const exec = stubExecutor();
    const result = deployStaged({ stage: s.stage, revision: HEAD, execute: true, executor: exec.fn });
    assert.equal(result.deployed, true);
    assert.equal(exec.calls.length, 1);
  } finally { cleanup(s); }
});

// Repair 3 — fail-closed for legacy records: an authorization record
// with no bound config_root cannot prove its root, so deploy refuses.
test('Freshness: deploy refuses an authorization record with no bound config_root', () => {
  const s = makeStage(HEAD);
  try {
    const record = authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    delete record.config_root;
    writeFileSync(join(releases, `${HEAD}.authorization.json`), JSON.stringify(record, null, 2) + '\n');
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /redirect|bound config_root/i);
  } finally { cleanup(s); }
});

// C2 repair — Receipts are per-target: the asset receipt must not
// overwrite the API receipt that proves the API service was deployed.
test('Order: per-target receipts do not overwrite each other', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-order-receipts-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    deployApi({ revision: HEAD, execute: true, executor: stubExecutor().fn });
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const exec = stubExecutor();
    const result = deployStaged({ stage: s.stage, revision: HEAD, execute: true, executor: exec.fn });
    assert.equal(result.deployed, true);
    const apiReceipt = JSON.parse(readFileSync(join(fresh, `${HEAD}.api.deploy-receipt.json`), 'utf8'));
    const assetReceipt = JSON.parse(readFileSync(join(fresh, `${HEAD}.asset.deploy-receipt.json`), 'utf8'));
    assert.equal(apiReceipt.target, 'api');
    assert.equal(assetReceipt.target, 'asset');
    assert.equal(apiReceipt.revision, HEAD);
    assert.equal(assetReceipt.revision, HEAD);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// C2 repair — Rollback is a separate lane that deploys only wrangler.toml
// (the combined public worker). It requires a recorded reason; its
// web/API checks are optional and, if omitted, are recorded as waived.
test('Rollback: authorize refuses without reason; dry-run deploys wrangler.toml only', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-rollback-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  try {
    assert.throws(() => authorizeRollback({ revision: HEAD, authorizer: 'test' }), /Rollback authorization requires --rollback-reason/);
    const record = authorizeRollback({ revision: HEAD, authorizer: 'test', rollbackReason: 'asset cutover failed verification' });
    assert.equal(record.target, 'rollback');
    assert.equal(record.config_source, 'wrangler.toml');
    assert.equal(record.authorization_kind, 'rollback-restoration');
    assert.equal(record.web_check_status, 'waived-rollback');
    assert.equal(record.rollback_reason, 'asset cutover failed verification');
    const dry = deployRollback({ revision: HEAD });
    assert.equal(dry.dryRun, true);
    assert.equal(dry.command, 'wrangler deploy --config wrangler.toml');
    const exec = stubExecutor();
    const result = deployRollback({ revision: HEAD, execute: true, executor: exec.fn });
    assert.equal(result.deployed, true);
    assert.equal(exec.calls.length, 1);
    assert.equal(exec.calls[0].target, 'rollback');
    assert.ok(exec.calls[0].configPath.endsWith('wrangler.toml'));
    assert.ok(!exec.calls[0].configPath.endsWith('wrangler.api.toml'));
    assert.ok(existsSync(join(fresh, `${HEAD}.rollback.deploy-receipt.json`)));
  } finally {
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// C2 repair — The rollback lane re-reads wrangler.toml and refuses on drift.
test('Rollback: deploy refuses if wrangler.toml drifted after authorization', () => {
  const wt = cleanClone();
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  const prevRoot = process.env.DEPLOY_REPO_ROOT;
  process.env.DEPLOY_RELEASES_DIR = mkdtempSync(join(tmpdir(), 'deploy-rollback-rel-'));
  process.env.DEPLOY_REPO_ROOT = wt;
  try {
    authorizeRollback({ revision: HEAD, authorizer: 'test', rollbackReason: 'drift test' });
    writeFileSync(join(wt, 'wrangler.toml'), readFileSync(join(wt, 'wrangler.toml'), 'utf8') + '# drift\n');
    assert.throws(() => deployRollback({ revision: HEAD }), /Rollback config drifted/);
  } finally {
    const rel = process.env.DEPLOY_RELEASES_DIR;
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    if (prevRoot === undefined) delete process.env.DEPLOY_REPO_ROOT;
    else process.env.DEPLOY_REPO_ROOT = prevRoot;
    rmSync(rel, { recursive: true, force: true });
    rmSync(wt, { recursive: true, force: true });
  }
});

// Repair 2, blocker 3 — The rollback workflow is gone: deploy-rollback.yml
// could not transport its authorization artifact and its checkout
// followed the dispatch ref, not the restoration revision, so it was not
// an executable restoration path. It is removed from the claimed path;
// the rollback path is the bounded local candidate-script plus
// DEPLOY_REPO_ROOT restoration procedure (proved by the next test).
test('Rollback: no rollback workflow is claimed as an executable path', () => {
  assert.ok(!existsSync(join(ROOT, '.github/workflows/deploy-rollback.yml')),
    'deploy-rollback.yml must not exist: it could not transport its authorization artifact or check out the restoration revision');
});

// Repair 2, blocker 3 — The bounded local restoration procedure, proved:
// the gate script comes from the candidate checkout, while
// DEPLOY_REPO_ROOT points at a checkout of the recorded combined build
// (6c1051a). Authorize + dry-run + stub execute through the gate restore
// that revision's wrangler.toml (hash pinned to the recorded build).
test('Rollback: local candidate-script + DEPLOY_REPO_ROOT restores the recorded combined build', () => {
  const wt = cloneAt(ROLLBACK_REV);
  const rel = mkdtempSync(join(tmpdir(), 'deploy-rollback-restore-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  const prevRoot = process.env.DEPLOY_REPO_ROOT;
  process.env.DEPLOY_RELEASES_DIR = rel;
  process.env.DEPLOY_REPO_ROOT = wt;
  try {
    // The restoration checkout is the recorded combined build, not the candidate.
    assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: wt, encoding: 'utf8' }).trim(), ROLLBACK_REV);
    const cfgSha = createHash('sha256').update(readFileSync(join(wt, 'wrangler.toml'))).digest('hex');
    assert.equal(cfgSha, ROLLBACK_CONFIG_SHA, 'the restoration checkout must be the recorded combined build');
    const record = authorizeRollback({ revision: ROLLBACK_REV, authorizer: 'test', rollbackReason: 'local restoration proof' });
    assert.equal(record.target, 'rollback');
    assert.equal(record.config_sha256, ROLLBACK_CONFIG_SHA);
    assert.equal(record.config_source, 'wrangler.toml');
    const dry = deployRollback({ revision: ROLLBACK_REV });
    assert.equal(dry.dryRun, true);
    assert.equal(dry.command, 'wrangler deploy --config wrangler.toml');
    const exec = stubExecutor();
    const result = deployRollback({ revision: ROLLBACK_REV, execute: true, executor: exec.fn });
    assert.equal(result.deployed, true);
    assert.equal(exec.calls.length, 1);
    assert.equal(exec.calls[0].target, 'rollback');
    // The executor restores the config from the restoration checkout,
    // not from the candidate checkout.
    assert.equal(exec.calls[0].configPath, join(wt, 'wrangler.toml'));
    assert.ok(existsSync(join(rel, `${ROLLBACK_REV}.rollback.deploy-receipt.json`)));
  } finally {
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    if (prevRoot === undefined) delete process.env.DEPLOY_REPO_ROOT;
    else process.env.DEPLOY_REPO_ROOT = prevRoot;
    rmSync(rel, { recursive: true, force: true });
    rmSync(wt, { recursive: true, force: true });
  }
});

// B3 — The lock is shared for the whole release target. Two acquires
// contend: the second refuses. The old per-stage lock (<stage>.lock) let
// two different stages hold locks at once — two lock directories can
// always coexist, which is the hole.
test('B3: release lock is shared across stages, not per-stage', () => {
  const a = mkdtempSync(join(tmpdir(), 'deploy-b3-a-'));
  const b = mkdtempSync(join(tmpdir(), 'deploy-b3-b-'));
  try {
    // Witness: the old per-stage lock paths are independent directories,
    // so both could be held simultaneously.
    mkdirSync(a + '.lock');
    mkdirSync(b + '.lock');
    assert.ok(existsSync(a + '.lock') && existsSync(b + '.lock'));
    rmSync(a + '.lock', { recursive: true, force: true });
    rmSync(b + '.lock', { recursive: true, force: true });
    // Fix: one shared lock — the second acquire refuses while held.
    const lock = acquireReleaseLock();
    try {
      assert.throws(() => acquireReleaseLock(), /Deploy lock held/);
    } finally { lock.release(); }
    const again = acquireReleaseLock();
    again.release();
  } finally {
    rmSync(a, { recursive: true, force: true });
    rmSync(b, { recursive: true, force: true });
  }
});

// B3 — Supersession cannot interleave with execution: while the shared
// lock is held (a deploy in flight), authorizing B refuses; once the lock
// is released, B authorizes and supersedes A, and A's deploy then refuses
// BEFORE the executor — stale A never executes after B.
test('B3: supersession cannot interleave with execution; stale A never executes after B', () => {
  const a = makeStage(OLD);
  const b = makeStage(HEAD);
  try {
    authorizeStage({ stage: a.stage, revision: OLD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const lock = acquireReleaseLock();
    try {
      assert.throws(
        () => authorizeStage({ stage: b.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' }),
        /Deploy lock held/);
    } finally { lock.release(); }
    authorizeStage({ stage: b.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    assert.equal(planState(a.stage), 'superseded');
    const exec = stubExecutor();
    assert.throws(() => deployStaged({ stage: a.stage, revision: OLD, execute: true, executor: exec.fn }), /superseded/);
    assert.equal(exec.calls.length, 0, 'stale A must never reach the executor');
    // And the recheck itself refuses a superseded record even when called directly.
    assert.throws(() => recheckBeforeExecute({ stage: a.stage, revision: OLD, record: null }), /superseded|No authorization record/);
  } finally { cleanup(a); cleanup(b); }
});

// F1 — No workflow input is interpolated into a run: step. Both deploy
// workflows pass inputs via env only, and the gate script accepts them as
// env fallbacks (verified by invoking the CLI with env only, no flags).
test('F1: no expression interpolation in any deploy run step; CLI accepts env', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  // Repair 2: deploy-rollback.yml was removed (it was not an executable
  // restoration path); the remaining two workflows are the claimed
  // gate-only paths, and the rollback path is the bounded local
  // candidate-script plus DEPLOY_REPO_ROOT procedure.
  for (const name of ['deploy.yml', 'deploy-staged.yml']) {
    const lines = read(name).split('\n');
    // A run: block starts at a line matching `run:` and continues while
    // following lines are more indented (or blank). No ${{ }} may appear
    // in any run-block line; env:/with: values may use them.
    let inRun = false;
    let runIndent = 0;
    for (const line of lines) {
      const runMatch = line.match(/^(\s*)run:\s*(.*)$/);
      if (runMatch) {
        inRun = true;
        runIndent = runMatch[1].length;
        assert.ok(!runMatch[2].includes('${{'), `${name} run step must not interpolate expressions`);
        continue;
      }
      if (inRun) {
        if (line.trim() === '') continue;
        const indent = line.match(/^(\s*)\S/)[1].length;
        if (indent <= runIndent) { inRun = false; continue; }
        assert.ok(!line.includes('${{'), `${name} run step must not interpolate expressions: ${line.trim()}`);
      }
    }
  }
  const s = makeStage(HEAD);
  try {
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    const env = {
      ...process.env,
      DEPLOY_STAGE: s.stage, DEPLOY_REVISION: HEAD,
      DEPLOY_WEB_CHECK: WEB, DEPLOY_API_CHECK: API,
    };
    const out = spawnSync(process.execPath, [join(ROOT, 'scripts/deploy-staged-payload.mjs'), '--deploy'],
      { env, encoding: 'utf8' });
    assert.equal(out.status, 0, out.stderr);
    assert.match(out.stdout, /"dryRun":\s*true/);
  } finally { cleanup(s); }
});

// Repair 6, defect 1 — The CI selector must require the NEWEST completed
// Web CI run for the exact revision to be successful. The repair-5 selector
// picked the newest *success* (`.workflow_runs[] | select(.conclusion ==
// "success")`), so an older success overrode a newer failure. The selector
// is extracted from each workflow and run against fixtures with jq.
// (Repair 7: fixtures now carry status + updated_at, since the selector
// requires the newest run/attempt to be completed/success.)
test('Repair 6: CI selector refuses a stale success when a newer run failed', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  const runJq = (filter, input) => {
    const r = spawnSync('jq', ['-r', filter], { input, encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    return r.stdout.trim();
  };
  // GitHub lists runs newest-first. Newer run failed, older run succeeded.
  const stale = JSON.stringify({ workflow_runs: [
    { id: 200, status: 'completed', conclusion: 'failure', created_at: '2026-11-12T09:00:00Z', updated_at: '2026-11-12T09:05:00Z' },
    { id: 100, status: 'completed', conclusion: 'success', created_at: '2026-11-12T08:00:00Z', updated_at: '2026-11-12T08:05:00Z' },
  ] });
  const fresh = JSON.stringify({ workflow_runs: [
    { id: 300, status: 'completed', conclusion: 'success', created_at: '2026-11-12T10:00:00Z', updated_at: '2026-11-12T10:05:00Z' },
    { id: 200, status: 'completed', conclusion: 'failure', created_at: '2026-11-12T09:00:00Z', updated_at: '2026-11-12T09:05:00Z' },
  ] });
  const empty = JSON.stringify({ workflow_runs: [] });
  for (const name of ['deploy.yml', 'deploy-staged.yml']) {
    const filter = read(name).match(/--jq '([^']+)'/)[1];
    assert.equal(runJq(filter, stale), '', `${name}: a newer failure must refuse, not fall back to an older success`);
    assert.equal(runJq(filter, fresh), '300', `${name}: the newest success is accepted`);
    assert.equal(runJq(filter, empty), '', `${name}: no runs must refuse`);
  }
});

// Repair 7 — The selector must fetch ALL statuses (no status=completed
// filter) and select the NEWEST run/attempt (by last update), then require
// completed/success. A newer running or queued check must not be hidden
// by an older success, and a re-run attempt on an older run is the current
// attempt: while it runs the selector refuses, once it passes it is the
// accepted evidence.
test('Repair 7: CI selector refuses while a newer check runs or queues', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  const runJq = (filter, input) => {
    const r = spawnSync('jq', ['-r', filter], { input, encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    return r.stdout.trim();
  };
  const success = { id: 100, status: 'completed', conclusion: 'success', created_at: '2026-11-12T08:00:00Z', updated_at: '2026-11-12T08:05:00Z' };
  // Newer distinct run in progress; older success must not be selected.
  const running = JSON.stringify({ workflow_runs: [
    { id: 400, status: 'in_progress', conclusion: null, created_at: '2026-11-12T11:00:00Z', updated_at: '2026-11-12T11:00:00Z' },
    success,
  ] });
  // Newer distinct run queued; older success must not be selected.
  const queued = JSON.stringify({ workflow_runs: [
    { id: 400, status: 'queued', conclusion: null, created_at: '2026-11-12T11:00:00Z', updated_at: '2026-11-12T11:00:00Z' },
    success,
  ] });
  // Newest run completed success; accepted.
  const fresh = JSON.stringify({ workflow_runs: [
    { id: 300, status: 'completed', conclusion: 'success', created_at: '2026-11-12T10:00:00Z', updated_at: '2026-11-12T10:05:00Z' },
    { id: 200, status: 'completed', conclusion: 'failure', created_at: '2026-11-12T09:00:00Z', updated_at: '2026-11-12T09:05:00Z' },
  ] });
  // Older run re-run: its attempt is the newest activity and is running.
  const rerunRunning = JSON.stringify({ workflow_runs: [
    { id: 200, status: 'completed', conclusion: 'success', created_at: '2026-11-12T09:00:00Z', updated_at: '2026-11-12T09:05:00Z' },
    { id: 100, status: 'in_progress', conclusion: null, created_at: '2026-11-12T08:00:00Z', updated_at: '2026-11-12T11:00:00Z' },
  ] });
  // Same re-run, now passed: the re-run attempt is the accepted evidence.
  const rerunPassed = JSON.stringify({ workflow_runs: [
    { id: 200, status: 'completed', conclusion: 'success', created_at: '2026-11-12T09:00:00Z', updated_at: '2026-11-12T09:05:00Z' },
    { id: 100, status: 'completed', conclusion: 'success', created_at: '2026-11-12T08:00:00Z', updated_at: '2026-11-12T11:00:00Z' },
  ] });
  for (const name of ['deploy.yml', 'deploy-staged.yml']) {
    const text = read(name);
    const filter = text.match(/--jq '([^']+)'/)[1];
    // No status filter in the API query: all statuses must be fetched.
    const query = text.match(/web-ci\.yml\/runs\?[^"']+/)[0];
    assert.ok(!query.includes('status='), `${name}: CI query must fetch all statuses, got ${query}`);
    assert.equal(runJq(filter, running), '', `${name}: a newer running check must refuse, not fall back to an older success`);
    assert.equal(runJq(filter, queued), '', `${name}: a newer queued check must refuse, not fall back to an older success`);
    assert.equal(runJq(filter, fresh), '300', `${name}: the newest completed success is accepted`);
    assert.equal(runJq(filter, rerunRunning), '', `${name}: a re-run attempt in progress must refuse`);
    assert.equal(runJq(filter, rerunPassed), '100', `${name}: a re-run attempt that passed is the accepted evidence`);
  }
});

// Repair 6, defect 2 — The audit upload points at the hidden releases
// directory (.deploy-releases). actions/upload-artifact@v4 excludes hidden
// files unless include-hidden-files is true, so the repair-5 upload found
// no evidence and merely warned. Both lanes must include hidden files.
// (Repair 7: the path is now the resolved absolute path from the releases
// step — it still points at the hidden releases directory, resolved by
// realpath -m from ../.deploy-releases, because upload-artifact rejects
// '..' segments in the path.)
test('Repair 6: audit upload includes hidden .deploy-releases files', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  for (const name of ['deploy.yml', 'deploy-staged.yml']) {
    const text = read(name);
    const block = text.match(/Upload authorization and receipts[\s\S]*?if-no-files-found: \w+/)[0];
    assert.match(block, /path: \$\{\{ steps\.releases\.outputs\.abs \}\}/, `${name}: upload path must be the resolved absolute path`);
    assert.match(block, /include-hidden-files:\s*true/, `${name}: upload must include hidden files`);
    assert.ok(text.includes('realpath -m "$GITHUB_WORKSPACE/../.deploy-releases"'), `${name}: resolve step must target the hidden releases directory`);
  }
});

// Repair 7, fix 2 — The audit upload path must be an absolute path with
// no '..' segments: upload-artifact@v4 rejects the
// `${{ github.workspace }}/../.deploy-releases/` form ("Relative pathing
// '.' and '..' is not allowed"), which is what failed the 381addb API
// dry-run at the final step. A resolve step (id releases, if always)
// computes the absolute path with realpath -m, and the upload uses it.
test('Repair 7: audit upload path is an absolute path with no .. segments', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  for (const name of ['deploy.yml', 'deploy-staged.yml']) {
    const text = read(name);
    assert.ok(/Resolve the releases directory to an absolute path[\s\S]*?id: releases[\s\S]*?if: always\(\)[\s\S]*?realpath -m/.test(text),
      `${name}: must resolve the releases directory (id releases, if always, realpath -m)`);
    const upload = text.match(/Upload authorization and receipts[\s\S]*?if-no-files-found: \w+/)[0];
    assert.match(upload, /path: \$\{\{ steps\.releases\.outputs\.abs \}\}/, `${name}: upload path must be the resolved absolute path`);
    assert.ok(!upload.includes('..'), `${name}: upload path must not contain '..'`);
  }
});

// Repair 7, fix 3 (workflow) — The asset lane's API-exists check runs
// only in real mode. The service is created by the real API deploy,
// which a dry-run never performs, so in dry-run mode the service is
// expected to be absent. The verify + record steps are gated on
// inputs.execute, and authorize + deploy receive the dry-run flag.
test('Repair 7: asset lane skips the API-exists check in dry-run mode only', () => {
  const read = name => readFileSync(join(ROOT, '.github/workflows', name), 'utf8');
  const text = read('deploy-staged.yml');
  assert.ok(/Verify the API service exists[\s\S]*?if: \$\{\{ inputs\.execute \}\}/.test(text),
    'deploy-staged.yml: API-exists verify step must run only when execute=true');
  assert.ok(/Record API readiness evidence[\s\S]*?if: \$\{\{ inputs\.execute \}\}/.test(text),
    'deploy-staged.yml: API readiness record step must run only when execute=true');
  assert.ok(/Authorize the asset candidate[\s\S]*?DEPLOY_DRY_RUN: \$\{\{ !inputs\.execute \}\}/.test(text),
    'deploy-staged.yml: asset authorize must pass DEPLOY_DRY_RUN');
  assert.ok(/Deploy staged payload through the gate[\s\S]*?DEPLOY_DRY_RUN: \$\{\{ !inputs\.execute \}\}/.test(text),
    'deploy-staged.yml: asset deploy must pass DEPLOY_DRY_RUN');
  // The API lane has no API-exists check and needs no dry-run flag.
  assert.ok(!read('deploy.yml').includes('DEPLOY_DRY_RUN'), 'deploy.yml: API lane has no readiness check to skip');
});

// Repair 7, fix 3 (gate) — In dry-run mode the asset authorize/deploy
// succeed without API readiness (the service is expected to be absent);
// in real mode (default and explicit dryRun:false) they still refuse.
// This is what failed the 381addb asset dry-run at the API-exists step.
test('Repair 7: dry-run asset authorize/deploy succeed without API readiness; real mode still refuses', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-dryrun-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    // Real mode (default and explicit) refuses without API evidence.
    assert.throws(() => authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' }),
      /API service not ready/);
    assert.throws(() => authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test', dryRun: false }),
      /API service not ready/);
    // Dry-run mode authorizes without API evidence.
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test', dryRun: true });
    // Dry-run deploy succeeds without API evidence.
    const dry = deployStaged({ stage: s.stage, revision: HEAD, dryRun: true });
    assert.equal(dry.dryRun, true);
    // Real deploy (dryRun false, with and without execute) still refuses.
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, dryRun: false }), /API service not ready/);
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, dryRun: false, executor: stubExecutor().fn }),
      /API service not ready/);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});

// Repair 8 — Contradictory modes (execute:true + dryRun:true) must
// refuse before any authorization or execution. dryRun skips the
// readiness assertion and execute invokes the executor; the combination
// executed a stub deploy with no API evidence (Brian's repair-7
// verdict). The guard is in deploy(), the single choke point every
// caller reaches (CLI, workflows, direct import), and it fires before
// any state is read, so it holds on every target.
test('Repair 8: execute:true with dryRun:true refuses; the executor never runs', () => {
  const fresh = mkdtempSync(join(tmpdir(), 'deploy-modes-'));
  const prevReleases = process.env.DEPLOY_RELEASES_DIR;
  process.env.DEPLOY_RELEASES_DIR = fresh;
  const s = makeStage(HEAD);
  try {
    // Dry-run authorize succeeds without API evidence (repair 7).
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test', dryRun: true });
    // The contradictory combination refuses and the executor never runs.
    const exec = stubExecutor();
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, dryRun: true, executor: exec.fn }),
      /Contradictory modes/);
    assert.equal(exec.calls.length, 0, 'executor must never run for contradictory modes');
    // The legitimate combinations still work: dry-run returns the
    // dry-run command, and real mode still refuses without evidence.
    const dry = deployStaged({ stage: s.stage, revision: HEAD, dryRun: true });
    assert.equal(dry.dryRun, true);
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD, execute: true, dryRun: false, executor: stubExecutor().fn }),
      /API service not ready/);
    // The guard is target-agnostic: the API lane refuses the same way
    // (it fires before any authorization is read).
    const execApi = stubExecutor();
    assert.throws(() => deployApi({ revision: HEAD, execute: true, dryRun: true, executor: execApi.fn }),
      /Contradictory modes/);
    assert.equal(execApi.calls.length, 0, 'API executor must never run for contradictory modes');
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    rmSync(fresh, { recursive: true, force: true });
  }
});
