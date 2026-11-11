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
import { execFileSync } from 'node:child_process';
import { buildAssets } from '../scripts/build-assets.mjs';
import { prepareRelease, verifyStage } from '../scripts/prepare-asset-release.mjs';
import { authorizeStage, deployStaged, acquireLock } from '../scripts/deploy-staged-payload.mjs';

const ROOT = resolve(new URL('../', import.meta.url).pathname);
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
// stages from the proposal-only template.
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
    fn: (command) => { calls.push(command); },
  };
};

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
    const lock = acquireLock(s.stage);
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
    const lockPath = s.stage + '.lock';
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
    assert.equal(existsSync(s.stage + '.lock'), false);
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
