import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import worker from '../backend/index.js';
import { createStorage } from '../backend/storage.js';
import { makeStorage } from './helpers/d1_shim.mjs';

async function fixture() {
  const DB = makeStorage();
  DB.db.exec(
    readFileSync(new URL('../backend/migrations/0005_strategy_archive.sql', import.meta.url), 'utf8'),
  );
  // The shim uses a single synchronous SQLite connection. D1 serializes
  // transactions; run each batch in one synchronous transaction here too.
  DB.batch = async (statements) => atomic(statements);
  function atomic(statements) {
    DB.db.exec('BEGIN');
    try {
      const results = statements.map((s) => DB.db.prepare(s.sql).run(...s.params));
      DB.db.exec('COMMIT');
      return results;
    } catch (e) {
      DB.db.exec('ROLLBACK');
      throw e;
    }
  }
  const storage = createStorage({ DB });
  for (const user of ['alice', 'brian', 'guest']) {
    await storage.users.insert(user, { username: user, version: 0 });
    await storage.sessions.create(user + '-token', user, 3600);
  }
  const env = { DB, STRATEGY_ARCHIVE_MEMBERS: 'alice,brian' };
  const call = (user, body, path = '') =>
    worker.fetch(
      new Request('http://localhost:8787/api/strategy-archive' + path, {
        headers: { Cookie: 'session=' + user + '-token', 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }),
      }),
      env,
    );
  return { call, env };
}
test('two authenticated clients share attribution, forks, trial assignments and immutable evidence', async () => {
  const { call } = await fixture();
  assert.equal((await call('unknown')).status, 401);
  assert.equal((await call('guest')).status, 403);
  const { records } = await (await call('alice')).json();
  const parent = records.find((r) => r.id === 'spy-200');
  const historical = records.find((r) => r.kind === 'evidence');
  assert.match(historical.data.summary, /Zero trades/);
  const lead = {
    kind: 'version',
    id: 'external-lead',
    data: {
      family: 'Term structure',
      originator: 'Study author',
      source: 'https://example.org/study',
      stage: 'documented',
      parameters: {},
    },
  };
  const fork = { kind: 'version', id: 'spy-150', data: structuredClone(parent.data) };
  Object.assign(fork.data, { stage: 'proposed', parent: parent.id, reason: 'Test a shorter lookback' });
  fork.data.parameters.lookback.value = 150;
  const batch = [lead, fork, { kind: 'unknown', id: 'bad', data: {} }];
  const intake = async (user, rows) => (await (await call(user, rows)).json()).results;
  const results = await intake('alice', batch);
  assert.deepEqual(results.slice(0, 2), [{ id: lead.id }, { id: fork.id }]);
  assert.ok(results[2].error);
  assert.deepEqual(await intake('alice', batch), results);
  const shared = {
    id: 'alice-citation',
    kind: 'evidence',
    version: lead.id,
    data: {
      kind: 'backtest',
      scope: 'external',
      resultRef: 'table-3',
      source: lead.data.source,
      period: '2001–2005',
      sample: '40 observations',
      assumptions: 'Gross',
      summary: 'Source reports a five-year comparison',
    },
  };
  const [a, b] = await Promise.all([
    intake('alice', shared),
    intake('brian', { ...shared, id: 'brian-citation' }),
  ]);
  assert.equal(a[0].id, b[0].id);
  const conflict = await intake('brian', { ...fork, data: { ...fork.data, reason: 'Changed' } });
  assert.ok(conflict[0].error);
  const trial = {
    id: 'assign-150',
    kind: 'trial',
    version: fork.id,
    data: {
      owner: 'Chej',
      trialId: 'first',
      asOf: '2026-11-21',
      status: 'assigned',
      source: lead.data.source,
    },
  };
  await intake('brian', trial);
  const rows = (await (await call('brian')).json()).records;
  assert.equal(rows.filter((r) => r.kind === 'evidence' && r.version === lead.id).length, 1);
  assert.deepEqual(rows.find((r) => r.id === a[0].id).credits.sort(), ['alice', 'brian']);
  assert.equal(rows.filter((r) => r.kind === 'evidence' && r.version === fork.id).length, 0);
  assert.equal(rows.find((r) => r.id === trial.id).data.owner, 'Chej');
  assert.deepEqual(rows.find((r) => r.id === fork.id).credits, ['alice']);
  assert.equal((await call('alice', undefined, '?after=-1')).status, 400);
});
