// tests/trade_journal_loads.test.mjs (OA-238) -- reverse-response-order
// regressions for the trade journal page's day-list and context loads. Runs the
// shipped site/js/trade-journal-loads.js (the exact code the page runs) with a
// fake fetch whose responses are resolved by hand, in any order. No market or
// journal data: responses are tiny synthetic markers.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MODULE = path.join(ROOT, 'site', 'js', 'trade-journal-loads.js');
const PAGE = path.join(ROOT, 'site', 'trade-journal.html');
const { createTradeJournalLoads } = require(MODULE);

// A fetch whose responses stay pending until the test resolves them.
function manualFetch() {
  const pending = [];
  const fetchJson = url => new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
  const take = fragment => {
    const i = pending.findIndex(p => p.url.includes(fragment));
    assert.ok(i >= 0, `no pending request matching ${fragment}; pending: ${pending.map(p => p.url).join(', ')}`);
    return pending.splice(i, 1)[0];
  };
  const reply = (fragment, body, status = 200) => take(fragment).resolve({ ok: status < 400, status, body });
  return { fetchJson, pending, take, reply };
}
const flush = () => new Promise(r => setImmediate(r));
const POS = 'pos:synthetic0001';

test('same-day cutoff change discards later context and partitions the cache',async()=>{
  const f=manualFetch(),loads=createTradeJournalLoads(f.fetchJson);
  let day=loads.loadDay('2026-11-09',2000);f.reply('&cutoff=2000',{positions:[]});await day;
  const later=loads.loadContext('2026-11-09',POS);
  day=loads.loadDay('2026-11-09',1000);f.reply('trade-journal?date=2026-11-09&cutoff=1000',{positions:[]});await day;
  const earlier=loads.loadContext('2026-11-09',POS);
  f.reply('&cutoff=1000',{marker:'earlier'});assert.equal(await earlier,true);
  f.reply('&cutoff=2000',{marker:'later'});assert.equal(await later,false);
  assert.deepEqual(loads.context('2026-11-09',POS),{marker:'earlier'});
});

test('context: Nov 13 resolves before Nov 12; only Nov 13 is applied', async () => {
  const f = manualFetch();
  const loads = createTradeJournalLoads(f.fetchJson);

  const d12 = loads.loadDay('2026-11-12');
  f.reply('trade-journal?date=2026-11-12', { positions: [] });
  await d12;
  const c12 = loads.loadContext('2026-11-12', POS);

  const d13 = loads.loadDay('2026-11-13');
  f.reply('trade-journal?date=2026-11-13', { positions: [] });
  await d13;
  const c13 = loads.loadContext('2026-11-13', POS);

  f.reply('context?position_id=pos%3Asynthetic0001&date=2026-11-13', { marker: 'nov13' });
  assert.equal(await c13, true);
  assert.deepEqual(loads.context('2026-11-13', POS), { marker: 'nov13' });

  f.reply('context?position_id=pos%3Asynthetic0001&date=2026-11-12', { marker: 'nov12-late' });
  assert.equal(await c12, false, 'the late Nov 12 response must be discarded');
  assert.deepEqual(loads.context('2026-11-13', POS), { marker: 'nov13' });
  assert.equal(loads.context('2026-11-12', POS), undefined, 'the stale reply is not stored for later either');
  assert.equal(f.pending.length, 0);
});

test('context: same-day reload; the earlier load\'s late response is discarded', async () => {
  const f = manualFetch();
  const loads = createTradeJournalLoads(f.fetchJson);

  const a = loads.loadDay('2026-11-13');
  f.reply('trade-journal?date=2026-11-13', { positions: [] });
  await a;
  const first = loads.loadContext('2026-11-13', POS);

  const b = loads.loadDay('2026-11-13');                     // "Load" clicked again / sync reload
  f.reply('trade-journal?date=2026-11-13', { positions: [] });
  await b;
  const second = loads.loadContext('2026-11-13', POS);
  assert.equal(f.pending.length, 2, 'the reload sends its own request instead of reusing the old one');

  f.reply('context?position_id=pos%3Asynthetic0001&date=2026-11-13', { marker: 'old' });  // oldest first in the queue
  assert.equal(await first, false);
  assert.deepEqual(loads.context('2026-11-13', POS), { loading: true });
  f.reply('context?position_id=pos%3Asynthetic0001&date=2026-11-13', { marker: 'new' });
  assert.equal(await second, true);
  assert.deepEqual(loads.context('2026-11-13', POS), { marker: 'new' });
});

test('context: errors are tied to their request too', async () => {
  const f = manualFetch();
  const loads = createTradeJournalLoads(f.fetchJson);
  await (async () => { const d = loads.loadDay('2026-11-12'); f.reply('trade-journal?date=2026-11-12', {}); await d; })();
  const c12 = loads.loadContext('2026-11-12', POS);
  await (async () => { const d = loads.loadDay('2026-11-13'); f.reply('trade-journal?date=2026-11-13', {}); await d; })();
  const c13 = loads.loadContext('2026-11-13', POS);
  f.reply('date=2026-11-13', { marker: 'nov13' });
  await c13;
  f.take('date=2026-11-12').reject(new Error('network'));    // late failure for the old day
  assert.equal(await c12, false);
  assert.deepEqual(loads.context('2026-11-13', POS), { marker: 'nov13' });
  const c = loads.loadContext('2026-11-13', POS);             // already loaded for this date+generation: no new request
  assert.equal(await c, false);
  assert.equal(f.pending.length, 0);
});

test('day list: Nov 13 resolves before Nov 12; the late Nov 12 list is stale', async () => {
  const f = manualFetch();
  const loads = createTradeJournalLoads(f.fetchJson);
  const d12 = loads.loadDay('2026-11-12');
  const d13 = loads.loadDay('2026-11-13');

  f.reply('trade-journal?date=2026-11-13', { date: '2026-11-13', positions: [{ position_id: 'b' }] });
  const r13 = await d13;
  assert.equal(r13.stale, false);
  assert.equal(r13.date, '2026-11-13');
  assert.deepEqual(r13.body.positions, [{ position_id: 'b' }]);

  f.reply('trade-journal?date=2026-11-12', { date: '2026-11-12', positions: [{ position_id: 'a' }] });
  const r12 = await d12;
  assert.deepEqual(r12, { stale: true, date: '2026-11-12' }, 'the late Nov 12 list carries no body to apply');
  assert.equal(loads.loadedDate, '2026-11-13');
  assert.equal(loads.currentDate, '2026-11-13');
});

test('day list: in-order responses still apply, and a late failure of an old load is stale', async () => {
  const f = manualFetch();
  const loads = createTradeJournalLoads(f.fetchJson);
  const d12 = loads.loadDay('2026-11-12');
  f.reply('trade-journal?date=2026-11-12', { positions: [] });
  assert.equal((await d12).stale, false);
  assert.equal(loads.loadedDate, '2026-11-12');

  const a = loads.loadDay('2026-11-13');
  const b = loads.loadDay('2026-11-13');                     // same day, newer generation
  f.take('trade-journal?date=2026-11-13').reject(new Error('network'));
  assert.equal((await a).stale, true);
  f.reply('trade-journal?date=2026-11-13', { positions: [] });
  const rb = await b;
  assert.equal(rb.stale, false);
  assert.equal(loads.loadedDate, '2026-11-13');
});

test('day switch invalidates contexts requested under the previous load', async () => {
  const f = manualFetch();
  const loads = createTradeJournalLoads(f.fetchJson);
  const d = loads.loadDay('2026-11-12');
  f.reply('trade-journal?date=2026-11-12', {});
  await d;
  const c = loads.loadContext('2026-11-12', POS);
  loads.loadDay('2026-11-13');                               // still pending
  f.reply('context?', { marker: 'late' });
  assert.equal(await c, false);
  assert.equal(loads.context('2026-11-12', POS), undefined);
  await flush();
});

test('page loads the shipped module by content hash and keys nothing by position alone', () => {
  const html = fs.readFileSync(PAGE, 'utf8');
  const hash = createHash('sha256').update(fs.readFileSync(MODULE)).digest('hex').slice(0, 16);
  assert.ok(html.includes(`js/trade-journal-loads.js?v=${hash}`), 'script URL must match the module content hash');
  assert.ok(html.indexOf('js/trade-journal-loads.js') < html.indexOf('createTradeJournalLoads('), 'module loads before the page script uses it');
  assert.ok(!/contexts\.(get|set|has)\(p\.position_id/.test(html), 'contexts must not be keyed by position_id alone');
  assert.ok(!/fetch\(`\/api\/trade-journal(\/context)?\?/.test(html), 'day and context requests go through the guarded loader');
  assert.match(html, /loads\.loadDay\(/);
  assert.match(html, /if \(res\.stale\) return/);
  assert.match(html, /loads\.loadContext\(date, p\.position_id\)\.then\(applied => \{ if \(applied\) renderLanes\(\); \}\)/);
});
