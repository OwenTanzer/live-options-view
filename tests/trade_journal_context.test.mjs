// tests/trade_journal_context.test.mjs (OA-238) -- market context for a journal
// position, read from the existing R2 archives. All market data here is
// SYNTHETIC (generated below); the R2 binding is an in-memory fake with the
// Workers R2 list/get shape. Positions come from the synthetic journal fixture.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { FakeD1 } from './helpers/d1_shim.mjs';
import { seedAccounts, seedSession } from './helpers/test_env.mjs';
import worker from '../backend/index.js';
import { TOKEN_SECRET_NAME, VIEWERS_VAR_NAME, syncTradeJournal } from '../backend/trade-journal.js';
import { MAX_SNAPSHOTS, MAX_TRADIER_PARTS, nyLocalToUtc, positionContext, snapshotKeyTime } from '../backend/trade-journal-context.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'trade_journal_notion.json'), 'utf8'));
const DAY = '20261113';
const QQQ_OCC = 'QQQ261113P00777000', IBIT_OCC = 'IBIT261113P00042500';

class FakeR2 {
  constructor() { this.objects = new Map(); this.lists = 0; this.gets = 0; }
  put(key, bytes, uploaded) { this.objects.set(key, { bytes: Buffer.from(bytes), uploaded: new Date(uploaded) }); }
  async list({ prefix = '', startAfter = '', cursor, limit = 1000 } = {}) {
    this.lists += 1;
    const keys = [...this.objects.keys()].filter(k => k.startsWith(prefix) && k > startAfter).sort();
    const start = cursor ? Number(cursor) : 0;
    const page = keys.slice(start, start + limit);
    const truncated = start + limit < keys.length;
    return { objects: page.map(key => ({ key, uploaded: this.objects.get(key).uploaded, size: this.objects.get(key).bytes.length })),
      truncated, cursor: truncated ? String(start + limit) : undefined };
  }
  async get(key) {
    this.gets += 1;
    const o = this.objects.get(key);
    if (!o) return null;
    return { text: async () => o.bytes.toString('utf8'), body: new Blob([o.bytes]).stream() };
  }
}

const pad = (n, w = 2) => String(n).padStart(w, '0');
const HEADER = 'TradeDate,Expiration,Strike,Type,OptionSymbol,DTE,OpenInterest,Volume,VolDelta,Bid,Mid,Ask,Last,IV,UnderlyingPrice';

/** Synthetic QQQ chain snapshots every `stepS` seconds between two ET times. */
function seedSnapshots(r2, fromH, fromM, toH, toM, stepS) {
  let i = 0;
  for (let t = (fromH * 60 + fromM) * 60; t <= (toH * 60 + toM) * 60; t += stepS, i++) {
    const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
    const bid = (1.5 + i * 0.01).toFixed(2), ask = (1.52 + i * 0.01).toFixed(2), und = (780 + i * 0.05).toFixed(2);
    const csv = [HEADER,
      `2026-11-13,2026-11-13,776,put,QQQ261113P00776000,0,10,1,0,1.1,1.11,1.12,1.11,0.2,${und}`,
      `2026-11-13,2026-11-13,777,put,${QQQ_OCC},0,10,1,0,${bid},0,${ask},0,0.2,${und}`].join('\n');
    const key = `intraday/${DAY}/snapshot_${pad(h)}${pad(m)}${pad(s)}000000.csv`;
    r2.put(key, csv, nyLocalToUtc(DAY, h, m, s));
    r2.put(key.replace('.csv', '.provenance.json'), '{}', nyLocalToUtc(DAY, h, m, s));
  }
}

/** Synthetic Tradier IBIT parts: one every 3 minutes, each holding the prior 3 minutes of events. */
function seedTradier(r2, fromH, fromM, toH, toM) {
  let n = 0;
  for (let end = (fromH * 60 + fromM) * 60; end <= (toH * 60 + toM) * 60; end += 180, n++) {
    const lines = [];
    for (let t = end - 180; t < end; t += 5) {
      const ms = nyLocalToUtc(DAY, Math.floor(t / 3600), Math.floor(t / 60) % 60, t % 60);
      lines.push(JSON.stringify({ type: 'timesale', symbol: 'IBIT', last: 45 + (t % 100) / 1000, size: 10, date: String(ms) }));
      lines.push(JSON.stringify({ type: 'quote', symbol: IBIT_OCC, bid: 0.15, ask: 0.17, biddate: String(ms), askdate: String(ms) }));
      lines.push(JSON.stringify({ type: 'quote', symbol: 'IBIT261113C00045000', bid: 1, ask: 1.1, biddate: String(ms), askdate: String(ms) }));
    }
    const uploaded = nyLocalToUtc(DAY, Math.floor(end / 3600), Math.floor(end / 60) % 60, end % 60) + 2000;
    r2.put(`moo144/tradier-ibit/2026-11-13/synthetic-part-${pad(n, 4)}.ndjson.gz`, zlib.gzipSync(lines.join('\n')), uploaded);
  }
}

function freshDb() {
  const DB = new FakeD1();
  DB.db.exec(FakeD1.schemaSql());
  DB.db.exec(fs.readFileSync(path.join(ROOT, 'backend', 'migrations', '0003_trade_journal.sql'), 'utf8'));
  return DB;
}

/** Sync the synthetic journal through a minimal fake Notion and return the stored positions by contract. */
async function journal(DB) {
  const pages = new Map(FIXTURE.pages.map(p => [p.id, p]));
  const j = (s, b) => new Response(JSON.stringify(b), { status: s });
  const fetchImpl = async (url, init) => {
    const u = new URL(url); let m;
    if (u.pathname.endsWith('/query')) return j(200, { results: [...pages.values()].map(({ _blocks, ...p }) => p), has_more: false });
    if ((m = /\/blocks\/([^/]+)\/children$/.exec(u.pathname))) return j(200, { results: pages.get(m[1])._blocks, has_more: false });
    return j(404, { code: 'object_not_found' });
  };
  await syncTradeJournal({ DB, [TOKEN_SECRET_NAME]: 't' }, { fetchImpl, now: () => new Date('2026-11-16T15:00:00Z') });
  const rows = DB.raw('SELECT position_json FROM trade_journal_rows').map(r => JSON.parse(r.position_json));
  return Object.fromEntries(rows.filter(p => p.supported).map(p => [p.contract.occ_symbol, p]));
}

const after = () => new Date('2026-11-16T15:00:00Z');

test('New York wall-clock conversion, including daylight time', () => {
  assert.equal(new Date(nyLocalToUtc('20261113', 11, 40, 25)).toISOString(), '2026-11-13T16:40:25.000Z');   // EST
  assert.equal(new Date(nyLocalToUtc('20260715', 9, 30, 0)).toISOString(), '2026-07-15T13:30:00.000Z');    // EDT
  assert.equal(new Date(snapshotKeyTime('intraday/20261113/snapshot_113716800527.csv', '20261113')).toISOString(), '2026-11-13T16:37:16.800Z');
});

test('QQQ: only the position window is read, sampled within the cap, with labelled series', async () => {
  const DB = freshDb();
  const pos = (await journal(DB))[QQQ_OCC];      // fills 11:40:25 -> 11:45:50 ET
  const r2 = new FakeR2();
  seedSnapshots(r2, 11, 0, 12, 30, 20);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.window.start, '2026-11-13T16:30:25.000Z');
  assert.equal(ctx.window.end, '2026-11-13T16:55:50.000Z');
  assert.ok(ctx.reads.gets <= MAX_SNAPSHOTS, `${ctx.reads.gets} reads`);
  assert.ok(ctx.option.points.length > 20);
  for (const [t] of [...ctx.option.points, ...ctx.underlying.points]) assert.ok(t >= ctx.window.start && t <= ctx.window.end, t);
  assert.match(ctx.option.source, /chain snapshot bid\/ask .*1 in 2 read/);
  assert.match(ctx.option.source, /separate observation from the fill/);
  assert.match(ctx.underlying.source, /UnderlyingPrice/);
  const [t0, bid, ask] = ctx.option.points[0];
  assert.ok(bid < ask && t0.endsWith('Z'));
  assert.deepEqual(ctx.gaps, []);
  assert.equal(r2.lists, 1, 'listing starts at the window instead of the start of the day');
});

test('IBIT: Tradier parts for the window give the underlying and the exact option, capped', async () => {
  const DB = freshDb();
  const pos = (await journal(DB))[IBIT_OCC];     // 10:29:02 -> 10:52:28 ET
  const r2 = new FakeR2();
  seedTradier(r2, 9, 33, 16, 0);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.ok(ctx.reads.gets <= MAX_TRADIER_PARTS);
  assert.ok(ctx.underlying.points.length > 50 && ctx.option.points.length > 50);
  assert.match(ctx.underlying.source, /^Tradier IBIT time-and-sales/);
  assert.match(ctx.option.source, /^Tradier quotes bid\/ask/);
  for (const [t] of ctx.option.points) assert.ok(t >= ctx.window.start && t <= ctx.window.end);
  assert.deepEqual(ctx.option.points[0].slice(1), [0.15, 0.17]);
});

test('TLT: no archive -> explicit gaps for both series and no R2 reads', async () => {
  const DB = freshDb();
  const pos = (await journal(DB)).TLT261113P00085500;
  const r2 = new FakeR2();
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.underlying, null);
  assert.equal(ctx.option, null);
  assert.equal(ctx.gaps.length, 2);
  assert.match(ctx.gaps[1], /no option history/);
  assert.equal(r2.lists + r2.gets, 0);
});

test('a contract outside the archived strikes is a stated gap, not an empty chart', async () => {
  const DB = freshDb();
  const pos = (await journal(DB)).QQQ261113P00780000;   // 10:12 -> 10:18 ET; seed only rows without that strike
  const r2 = new FakeR2();
  seedSnapshots(r2, 10, 0, 10, 40, 68);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.option, null);
  assert.ok(ctx.underlying.points.length > 0);
  assert.match(ctx.gaps.join(' '), /not present in the archived chain/);
});

test('past sessions are cached in D1; the current session is not', async () => {
  const DB = freshDb();
  const pos = (await journal(DB))[QQQ_OCC];
  const r2 = new FakeR2();
  seedSnapshots(r2, 11, 0, 12, 30, 68);
  const first = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  const reads = r2.gets + r2.lists;
  const second = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(first.cached, false);
  assert.equal(second.cached, true);
  assert.equal(r2.gets + r2.lists, reads, 'cache hit reads nothing from R2');
  assert.deepEqual(second.option, first.option);
  const DB2 = freshDb();
  const pos2 = (await journal(DB2))[QQQ_OCC];
  await positionContext({ DB: DB2, PAPER_TRADES: r2 }, pos2, { now: () => new Date('2026-11-13T18:00:00Z') });
  assert.equal(DB2.raw('SELECT COUNT(*) AS n FROM trade_journal_context')[0].n, 0, 'same-day context is not frozen');
});

test('context route: viewer-only, validated, and refuses unsupported rows', async () => {
  const DB = freshDb();
  const byOcc = await journal(DB);
  const r2 = new FakeR2();
  seedSnapshots(r2, 11, 0, 12, 30, 68);
  const e = { DB, PAPER_TRADES: r2, [VIEWERS_VAR_NAME]: 'viewer_a' };
  await seedAccounts(e, [{ username: 'viewer_a', balance_cash: 0, trades: [] }, { username: 'someone', balance_cash: 0, trades: [] }]);
  await seedSession(e, 'tok-v', 'viewer_a');
  await seedSession(e, 'tok-o', 'someone');
  const get = (q, tok) => worker.fetch(new Request(`https://options.moopertonic.net/api/trade-journal/context?${q}`,
    { headers: tok ? { Cookie: `session=${tok}` } : {} }), e);
  const id = byOcc[QQQ_OCC].position_id;
  assert.equal((await get(`position_id=${id}`)).status, 401);
  assert.equal((await get(`position_id=${id}`, 'tok-o')).status, 403);
  assert.equal((await get('position_id=../etc', 'tok-v')).status, 400);
  assert.equal((await get('position_id=pos:0000000000000000', 'tok-v')).status, 404);
  const ok = await get(`position_id=${id}`, 'tok-v');
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).occ_symbol, QQQ_OCC);
});
