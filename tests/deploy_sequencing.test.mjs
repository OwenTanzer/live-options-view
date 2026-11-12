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
import { buildAssets } from '../scripts/build-assets.mjs';
import { prepareRelease, verifyStage } from '../scripts/prepare-asset-release.mjs';
import { authorizeStage, authorizeApi, deployStaged, deployApi, acquireReleaseLock, releaseLockPath, recheckBeforeExecute } from '../scripts/deploy-staged-payload.mjs';

// F3: fileURLToPath, not URL.pathname — the pathname form breaks on
// Windows ("/C:/..." is not a usable path there).
const ROOT = resolve(fileURLToPath(new URL('../', import.meta.url)));
const HEAD = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim();
const OLD = 'bff49eedddbc816df5633bb5f492867caa8036ce';
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
// stages from the proposal-only template. The applied config here is a
// TEST FIXTURE standing in for the real applied-source config, which is a
// C2 precondition and does not yet exist at the repo root — no test here
// pretends the real config exists.
function makeStage(revision, applied = true) {
  const base = mkdtempSync(join(tmpdir(), 'deploy-seq-'));
  const artifact = join(base, 'artifact');
  buildAssets({ out: artifact, revision });
  const configRoot = join(base, 'config');
  mkdirSync(configRoot, { recursive: true });
  if (applied) writeFileSync(join(configRoot, 'wrangler.assets.toml'), PROPOSED);
  const stage = join(base, 'stage');
  prepareRelease({ artifact, out: stage, configRoot: applied ? configRoot : ROOT });
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
// A clean clone of the candidate checkout (an isolated copy of the exact
// committed tree). Used by the B1 tests so the authorization mechanism is
// exercised against a real checkout, not the working tree.
function cleanClone() {
  const wt = mkdtempSync(join(tmpdir(), 'deploy-b1-clone-'));
  rmSync(wt, { recursive: true, force: true });
  execFileSync('git', ['clone', '-q', ROOT, wt]);
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
    assert.ok(existsSync(join(releases, `${HEAD}.deploy-receipt.json`)));
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
    // The A/B test above wrote a HEAD receipt in the shared temp releases
    // dir; clear it so this test can prove a dry-run writes none.
    rmSync(join(releases, `${HEAD}.deploy-receipt.json`), { force: true });
    const result = deployStaged({ stage: s.stage, revision: HEAD, executor: exec.fn });
    assert.equal(result.dryRun, true);
    assert.match(result.command, /wrangler deploy --config/);
    assert.equal(exec.calls.length, 0);
    assert.equal(planState(s.stage), 'authorized');
    assert.equal(existsSync(join(releases, `${HEAD}.deploy-receipt.json`)), false);
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
    authorizeStage({ stage: s.stage, revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
    execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', '-C', wt, 'add', 'docs/releases']);
    execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', '-C', wt, 'commit', '-q', '-m', 'authorize']);
    const newHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: wt, encoding: 'utf8' }).trim();
    assert.notEqual(newHead, HEAD);
    assert.throws(() => deployStaged({ stage: s.stage, revision: HEAD }), /Revision mismatch/);
    assert.throws(() => deployStaged({ stage: s.stage, revision: newHead }), /No authorization record/);
  } finally {
    cleanup(s);
    process.env.DEPLOY_RELEASES_DIR = prevReleases;
    if (prevRoot === undefined) delete process.env.DEPLOY_REPO_ROOT;
    else process.env.DEPLOY_REPO_ROOT = prevRoot;
    rmSync(wt, { recursive: true, force: true });
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

// B2 — The API lane is separately explicit and gated: its own authorization
// record, its own dispatch, wrangler.toml only, no producer effects.
test('B2: API lane is separately explicit and gated (target api)', () => {
  const record = authorizeApi({ revision: HEAD, webCheck: WEB, apiCheck: API, authorizer: 'test' });
  assert.equal(record.target, 'api');
  assert.equal(record.stage_path, null);
  const dry = deployApi({ revision: HEAD });
  assert.equal(dry.dryRun, true);
  assert.equal(dry.command, 'wrangler deploy --config wrangler.toml');
  const exec = stubExecutor();
  const result = deployApi({ revision: HEAD, execute: true, executor: exec.fn });
  assert.equal(result.deployed, true);
  assert.equal(exec.calls.length, 1);
  assert.equal(exec.calls[0].target, 'api');
  assert.ok(exec.calls[0].configPath.endsWith('wrangler.toml'));
  assert.ok(existsSync(join(releases, `${HEAD}.deploy-receipt.json`)));
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
