import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { Miniflare } from 'miniflare';
import { createStorage } from '../../backend/storage.js';
import { advanceSourceCatalog, publicationCatalogPage, CATALOG_PAGE_SIZE } from '../../backend/one-clock-source-catalog.js';
import { squeezeCatalog, catalogSources } from '../../backend/one-clock-catalog-sources.js';
import { archive, budget, writeState, pruneArchive } from '../../backend/kalshi-store.js';
import { limits, warningFraction, checkUsage } from '../../scripts/d1-usage.mjs';

test('account monitor checks both row limits and refuses missing counters', () => {
  const payload = (sum) => ({data: {viewer: {accounts: [{d1AnalyticsAdaptiveGroups: [{sum}]}]}}});
  checkUsage(payload({rowsRead: 3_999_999, rowsWritten: 79_999}), 'fixture');
  for (const sum of [{rowsRead: 4_000_000, rowsWritten: 0}, {rowsRead: 0, rowsWritten: 80_000}])
    assert.throws(() => checkUsage(payload(sum), 'fixture'), /80%/);
  assert.throws(() => checkUsage(payload({rowsRead: 0}), 'fixture'), /rowsWritten/);
  assert.throws(() => checkUsage({errors: [{}]}, 'fixture'), /Analytics/);
});

// Transport only. Miniflare executes real D1 statements and supplies per-statement row counters.
const worker = `export default { async fetch(request, env) {
  const {statements, batch} = await request.json();
  const prepared = statements.map(({sql, args}) => env.DB.prepare(sql).bind(...args));
  const results = batch ? await env.DB.batch(prepared) : [await prepared[0].all()];
  return Response.json(results);
} }`;
function meteredDatabase(runtime) {
  const usage = {rowsRead: 0, rowsWritten: 0};
  async function execute(statements, batch) {
    const response = await runtime.dispatchFetch('https://fixture.invalid/', {
      method: 'POST', body: JSON.stringify({statements, batch})
    });
    assert.equal(response.status, 200, await response.clone().text());
    const results = await response.json();
    for (const {meta} of results) {
      assert.ok(Number.isSafeInteger(meta.rows_read) && Number.isSafeInteger(meta.rows_written));
      usage.rowsRead += meta.rows_read;
      usage.rowsWritten += meta.rows_written;
    }
    return results;
  }
  function statement(sql, args = []) {
    return {sql, args, bind: (...values) => statement(sql, values),
      all: async () => (await execute([{sql, args}], false))[0],
      run: async () => (await execute([{sql, args}], false))[0],
      first: async () => (await execute([{sql, args}], false))[0].results[0] ?? null};
  }
  return {prepare: statement, batch: statements => execute(statements, true), usage};
}

test('modeled daily catalog and authenticated reads fit the shared D1 budget', async t => {
  const runtime = new Miniflare({modules: true, script: worker, cf: false,
    compatibilityDate: '2026-06-23', d1Databases: ['DB']});
  t.after(() => runtime.dispose());
  const db = meteredDatabase(runtime);
  // Let SQLite parse whole migrations (including trigger bodies), then install their schema in D1.
  const schema = new DatabaseSync(':memory:');
  try {
    for (const migration of ['0001_accounts_sessions.sql', '0005_one_clock_catalog.sql', '0006_kalshi_private.sql'])
      schema.exec(readFileSync(new URL('../../backend/migrations/' + migration, import.meta.url), 'utf8'));
    for (const {sql} of schema.prepare('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY rowid').all())
      await db.prepare(sql).run();
  } finally { schema.close(); }
  const now = Date.UTC(2026, 10, 22, 12);
  const objects = Array.from({length: CATALOG_PAGE_SIZE}, (_, i) => ({
    key: squeezeCatalog.prefix + '00000000-0000-4000-8000-' + String(i).padStart(12,'0') +
      '/11111111-1111-4111-8111-111111111111/started.json',
    uploaded: new Date(now), size: 100, etag: String(i)
  }));
  const bucket = {list: async () => ({objects, truncated: false})};
  await advanceSourceCatalog(db, bucket, squeezeCatalog, {now});
  // Retained history from the same source makes source-only scans costly.
  await db.prepare(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<4000)
    INSERT INTO one_clock_catalog_gaps SELECT ?, 'historic', 'past-'||i FROM n`).bind(squeezeCatalog.id).run();
  const storage = createStorage({DB: db});
  await db.prepare(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<4000)
    INSERT INTO users SELECT 'user-'||i, '{}', 1, 'fixture' FROM n`).run();
  await db.prepare(`INSERT INTO sessions SELECT username, username, 'fixture', 2147483647 FROM users`).run();
  await storage.sessions.create('fixture-token', 'user-1', 3600);

  const daily = {rowsRead: 0, rowsWritten: 0};
  async function measure(label, calls, action) {
    const before = {...db.usage};
    await action();
    const costs = {};
    for (const key of Object.keys(daily)) {
      costs[key] = db.usage[key] - before[key];
      daily[key] += costs[key] * calls;
    }
    console.log(label, {dailyCalls: calls, perCall: costs});
    return costs;
  }
  const config = readFileSync(new URL('../../wrangler.api.toml', import.meta.url), 'utf8');
  const cron = config.match(/^crons\s*=\s*\["\*\/(\d+) \* \* \* \*", "\* \* \* \* \*"\]/m);
  assert.ok(cron, 'Update the modeled cadence when the production cron changes');
  const interval = Number(cron[1]);
  assert.ok(interval > 0 && interval <= 60);
  const calls = Math.ceil(60 / interval) * 24 * catalogSources.length;
  await measure('full unchanged catalog page', calls, () => advanceSourceCatalog(db,bucket,squeezeCatalog,{now:now+1000}));
  await measure('historical catalog lookup', 1000, async () => {
    const page = await publicationCatalogPage(db,squeezeCatalog,{start:now-2000,cutoff:now-1000});
    assert.equal(page.truncated,false);
  });
  await measure('session and account lookup', 10000, async () => {
    const session = await storage.sessions.get('fixture-token');
    assert.deepEqual(await storage.users.get(session.username), {});
  });
  await measure('session creation and logout', 500, async () => {
    await storage.sessions.create('another-token', 'user-1', 3600);
    await storage.sessions.delete('another-token');
  });
  const env = {DB:db};
  await measure('minute sample and reserved provider reads', 1440, async () => {
    await budget(env, 'minute-provider-day', 18000, 86400000, now, 11);
    await budget(env, 'provider-minute', 60, 60000, now, 11);
    await archive(env, 'Minute chain', {books:[]}, 'minute');
  });
  await measure('scheduled catalog rollover or discovery retry', 1440, async () => {
    await archive(env, 'Market catalog', {markets:[]}, 'minute');
    await writeState(env, 'minute-catalog', {markets:[]});
  });
  await measure('private displayed snapshot', 2000, () => archive(env, 'Displayed chain quotes', {quotes:{}}, 'display'));
  // Model steady-state expiration, including the evidence indexes.
  await db.prepare("UPDATE kalshi_evidence SET received_at = '2000-01-01'").run();
  await measure('private evidence expiration', Math.ceil((1440+1440+2000+400)/3), () => pruneArchive(env));
  console.log('Modeled rows per UTC day', daily);
  for (const [key, limit] of Object.entries(limits))
    assert.ok(daily[key] < warningFraction * limit, key + ' modeled daily use: ' + daily[key]);
});
