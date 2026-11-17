// tests/trade_journal_context.test.mjs (OA-238) -- market context for a journal
// position on one New York day, read from the existing R2 archives. All market
// data here is SYNTHETIC (generated below); the R2 binding is an in-memory fake
// with the Workers R2 list/get shape.

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
import {
  FILL_PAD_MS, MAX_PART_GAP_MS, MAX_SNAPSHOT_GAP_MS, MAX_SNAPSHOTS, MAX_TRADIER_PARTS, coveragePlan, nyLocalToUtc,
  positionContext, snapshotKeyTime, splitRuns,
} from '../backend/trade-journal-context.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'trade_journal_notion.json'), 'utf8'));
const QQQ_OCC = 'QQQ261113P00777000', IBIT_OCC = 'IBIT261113P00042500';
const after = () => new Date('2026-11-16T15:00:00Z');
const pad = (n, w = 2) => String(n).padStart(w, '0');
const HEADER = 'TradeDate,Expiration,Strike,Type,OptionSymbol,DTE,OpenInterest,Volume,VolDelta,Bid,Mid,Ask,Last,IV,UnderlyingPrice';

class FakeR2 {
  constructor() { this.objects = new Map(); this.lists = 0; this.gets = 0; }
  put(key, bytes, uploaded) { this.objects.set(key, { bytes: Buffer.from(bytes), uploaded: new Date(uploaded) }); }
  async list({ prefix = '', startAfter = '', cursor, limit = 1000 } = {}) {
    this.lists += 1;
    const keys = [...this.objects.keys()].filter(k => k.startsWith(prefix) && k > startAfter).sort();
    const start = cursor ? Number(cursor) : 0;
    const page = keys.slice(start, start + limit);
    const truncated = start + limit < keys.length;
    return { objects: page.map(key => ({ key, uploaded: this.objects.get(key).uploaded })), truncated, cursor: truncated ? String(start + limit) : undefined };
  }
  async get(key) {
    this.gets += 1;
    const o = this.objects.get(key);
    return o ? { text: async () => o.bytes.toString('utf8'), body: new Blob([o.bytes]).stream() } : null;
  }
}

/** Synthetic QQQ chain snapshots every `stepS` seconds between two ET times on `day` (YYYYMMDD). */
function seedSnapshots(r2, day, fromH, fromM, toH, toM, stepS, occ = QQQ_OCC) {
  let i = 0;
  for (let t = (fromH * 60 + fromM) * 60; t <= (toH * 60 + toM) * 60; t += stepS, i++) {
    const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
    const und = (780 + i * 0.05).toFixed(2);
    const csv = [HEADER, `x,x,776,put,QQQ261113P00776000,0,10,1,0,1.1,1.11,1.12,1.11,0.2,${und}`,
      `x,x,777,put,${occ},0,10,1,0,${(1.5 + i * 0.01).toFixed(2)},0,${(1.52 + i * 0.01).toFixed(2)},0,0.2,${und}`].join('\n');
    const key = `intraday/${day}/snapshot_${pad(h)}${pad(m)}${pad(s)}000000.csv`;
    r2.put(key, csv, nyLocalToUtc(day, h, m, s));
    r2.put(key.replace('.csv', '.provenance.json'), '{}', nyLocalToUtc(day, h, m, s));
  }
}

/** Synthetic Tradier IBIT parts: one every 3 minutes, each holding the prior 3 minutes of events. */
function seedTradier(r2, day, fromH, fromM, toH, toM) {
  let n = 0;
  const iso = `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6)}`;
  for (let end = (fromH * 60 + fromM) * 60; end <= (toH * 60 + toM) * 60; end += 180, n++) {
    const lines = [];
    for (let t = end - 180; t < end; t += 5) {
      const ms = nyLocalToUtc(day, Math.floor(t / 3600), Math.floor(t / 60) % 60, t % 60);
      lines.push(JSON.stringify({ type: 'timesale', symbol: 'IBIT', last: 45 + (t % 100) / 1000, size: 10, date: String(ms) }));
      lines.push(JSON.stringify({ type: 'quote', symbol: IBIT_OCC, bid: 0.15, ask: 0.17, biddate: String(ms), askdate: String(ms) }));
    }
    const uploaded = nyLocalToUtc(day, Math.floor(end / 3600), Math.floor(end / 60) % 60, end % 60) + 2000;
    r2.put(`moo144/tradier-ibit/${iso}/synthetic-part-${pad(n, 4)}.ndjson.gz`, zlib.gzipSync(lines.join('\n')), uploaded);
  }
}

function freshDb() {
  const DB = new FakeD1();
  DB.db.exec(FakeD1.schemaSql());
  DB.db.exec(fs.readFileSync(path.join(ROOT, 'backend', 'migrations', '0003_trade_journal.sql'), 'utf8'));
  return DB;
}

/** A synthetic long position built directly in the stored shape. */
function position(und, occ, fills) {
  const executions = fills.map(([ts, side, price], i) => ({ execution_id: `x:${i}`, ts, side, qty: 1, price, fee: 0.5 }));
  const closed = executions.filter(e => e.side === 'buy').length === executions.filter(e => e.side === 'sell').length;
  return { position_id: 'pos:00000000000000aa', supported: true, contract: { underlying: und, occ_symbol: occ },
    opened_ts: executions[0].ts, closed_ts: closed ? executions.at(-1).ts : null, executions };
}
const et = (day, h, m, s = 0) => new Date(nyLocalToUtc(day, h, m, s)).toISOString();

/** Sync the synthetic journal through a minimal fake Notion; return stored positions by contract. */
async function journal(DB) {
  const pages = new Map(FIXTURE.pages.map(p => [p.id, p]));
  const j = (s, b) => new Response(JSON.stringify(b), { status: s });
  const fetchImpl = async url => {
    const u = new URL(url); let m;
    if (u.pathname.endsWith('/query')) return j(200, { results: [...pages.values()].map(({ _blocks, ...p }) => p), has_more: false });
    if ((m = /\/blocks\/([^/]+)\/children$/.exec(u.pathname))) return j(200, { results: pages.get(m[1])._blocks, has_more: false });
    return j(404, { code: 'object_not_found' });
  };
  await syncTradeJournal({ DB, [TOKEN_SECRET_NAME]: 't' }, { fetchImpl, now: after });
  return Object.fromEntries(DB.raw('SELECT position_json FROM trade_journal_rows').map(r => JSON.parse(r.position_json))
    .filter(p => p.supported).map(p => [p.contract.occ_symbol, p]));
}

const allPoints = s => (s?.segments ?? []).flatMap(g => g.points);
const gapMs = pts => Math.max(0, ...pts.slice(1).map((p, i) => Date.parse(p[0]) - Date.parse(pts[i][0])));

test('New York wall-clock conversion, including daylight time', () => {
  assert.equal(new Date(nyLocalToUtc('20261113', 11, 40, 25)).toISOString(), '2026-11-13T16:40:25.000Z');
  assert.equal(new Date(nyLocalToUtc('20260715', 9, 30, 0)).toISOString(), '2026-07-15T13:30:00.000Z');
  assert.equal(new Date(snapshotKeyTime('intraday/20261113/snapshot_113716800527.csv', '20261113')).toISOString(), '2026-11-13T16:37:16.800Z');
});

test('QQQ short trade: dense lines around the fills, within the read cap, labelled', async () => {
  const DB = freshDb();
  const pos = (await journal(DB))[QQQ_OCC];          // fills 11:40:25 -> 11:45:50 ET
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 11, 0, 12, 30, 20);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.day, '2026-11-13');
  assert.ok(ctx.reads.gets <= MAX_SNAPSHOTS);
  assert.ok(ctx.option.segments.every(s => s.kind === 'band'));
  for (const s of ctx.option.segments) for (const [t] of s.points) assert.ok(t >= s.start && t <= s.end);
  assert.match(ctx.option.source, /separate observation from the fill/);
  assert.equal(r2.lists, 1, 'listing starts at the interval instead of the start of the day');
});

test('long trade 10:00-15:55 ET: covered to the exit, hold shown as points, nothing silently dropped', async () => {
  const DB = freshDb();
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 9, 30, 16, 15, 68);
  const pos = position('QQQ', QQQ_OCC, [[et('20261113', 10, 0), 'buy', 1.5], [et('20261113', 15, 55), 'sell', 1.9]]);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.ok(ctx.reads.gets <= MAX_SNAPSHOTS);
  assert.equal(ctx.interval.end, et('20261113', 16, 0), 'interval reaches exit + pad (Chej saw it stop at 12:50)');
  const lines = ctx.option.segments.filter(s => s.kind === 'band');
  assert.equal(lines.length, 2, 'one dense segment per fill, not one band across the day');
  for (const s of lines) assert.ok(Date.parse(s.end) - Date.parse(s.start) <= 2 * FILL_PAD_MS);
  const pts = ctx.option.segments.find(s => s.kind === 'points').points;
  assert.ok(pts.length >= 10);
  assert.ok(pts.some(p => p[0] > et('20261113', 15, 0)), 'sparse samples reach late in the hold');
  assert.ok(allPoints(ctx.option).some(p => p[0] >= et('20261113', 15, 55)), 'the exit window is drawn');
  assert.match(ctx.coverage.join(' '), /10:05–15:50 ET: \d+ of \d+ snapshots read, shown as separate points/);
});

test('exit on a later day: each day covers its own session and says where the trade continues', async () => {
  const DB = freshDb();
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261112', 9, 30, 16, 15, 68);
  seedSnapshots(r2, '20261113', 9, 30, 16, 15, 68);
  const pos = position('QQQ', QQQ_OCC, [[et('20261112', 15, 0), 'buy', 1.5], [et('20261113', 10, 15), 'sell', 1.2]]);
  const d1 = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { day: '2026-11-12', now: after });
  assert.equal(d1.interval.start, et('20261112', 14, 55));
  assert.equal(d1.interval.end, et('20261112', 16, 15));
  assert.match(d1.notes.join(' '), /exits 2026-11-13/);
  assert.ok(allPoints(d1.option).every(p => p[0].startsWith('2026-11-12')));
  const d2 = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { day: '2026-11-13', now: after });
  assert.equal(d2.interval.start, et('20261113', 9, 30));
  assert.equal(d2.interval.end, et('20261113', 10, 20));
  assert.match(d2.notes.join(' '), /entered 2026-11-12/);
  assert.ok(allPoints(d2.option).every(p => p[0].startsWith('2026-11-13')), 'reads the exit day, not only the entry date');
  assert.ok(allPoints(d2.option).some(p => p[0] >= et('20261113', 10, 15)));
});

test('separated windows on one underlying stay separate segments (Tradier)', async () => {
  const DB = freshDb();
  const r2 = new FakeR2();
  seedTradier(r2, '20261113', 9, 33, 16, 15);
  const pos = position('IBIT', IBIT_OCC, [[et('20261113', 10, 0), 'buy', 0.16], [et('20261113', 14, 0), 'sell', 0.14]]);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.ok(ctx.reads.gets <= MAX_TRADIER_PARTS);
  const segs = ctx.option.segments;
  assert.ok(segs.length >= 3, `${segs.length} segments`);
  for (const s of segs) {
    assert.equal(s.kind, 'band');
    assert.ok(gapMs(s.points) <= 60_000, 'no segment bridges an unread archive part');
  }
  const ordered = [...segs].sort((x, y) => (x.start < y.start ? -1 : 1));
  for (let i = 1; i < ordered.length; i++) assert.ok(ordered[i].start >= ordered[i - 1].end, 'segments do not overlap');
  assert.match(ctx.coverage.join(' '), /holding period: \d+ of \d+ ~3-minute archive parts read; unread stretches are left blank/);
  const plan = coveragePlan(pos, '2026-11-13');
  assert.equal(plan.dense.length, 2);
});

test('TLT: no archive -> explicit gaps and no R2 reads', async () => {
  const DB = freshDb();
  const pos = (await journal(DB)).TLT261113P00085500;
  const r2 = new FakeR2();
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.underlying, null);
  assert.equal(ctx.option, null);
  assert.match(ctx.gaps.join(' '), /no option history in any archive \(unsupported premium path\)/);
  // Archived TLT values exist but are constant: say there is no validated time-varying series, not "no observations".
  assert.match(ctx.gaps[0], /^TLT underlying: no validated time-varying price series/);
  assert.doesNotMatch(ctx.gaps.join(' '), /no archived price series|no observations/);
  assert.equal(r2.lists + r2.gets, 0);
});

test('a contract outside the archived strikes is a stated gap', async () => {
  const DB = freshDb();
  const pos = (await journal(DB)).QQQ261113P00780000;
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 10, 0, 10, 40, 68);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.option, null);
  assert.ok(allPoints(ctx.underlying).length > 0);
  assert.match(ctx.gaps.join(' '), /not present in the archived chain/);
});

test('past sessions are cached; the current session is not', async () => {
  const DB = freshDb();
  const pos = (await journal(DB))[QQQ_OCC];
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 11, 0, 12, 30, 68);
  const first = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  const reads = r2.gets + r2.lists;
  const second = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(first.cache, 'stored');
  assert.equal(second.cached, true);
  assert.equal(r2.gets + r2.lists, reads);
  const today = await positionContext({ DB: freshDb(), PAPER_TRADES: r2 }, pos, { now: () => new Date('2026-11-13T18:00:00Z') });
  assert.equal(today.cache, 'not_cached_session_open');
});

// ------------------------------------------- holes inside a window (Chej, 11:20)
const lineLike = s => s.kind === 'line' || s.kind === 'band';
function assertNoBridge(series, maxGap) {
  for (const s of (series?.segments ?? []).filter(lineLike)) {
    assert.ok(s.points.length >= 2, 'a line needs two observations');
    assert.ok(gapMs(s.points) <= maxGap, `segment ${s.start}..${s.end} bridges a ${gapMs(s.points) / 1000}s hole`);
  }
}

test('splitRuns breaks on observation time, not on list position', () => {
  const p = (h, m, s) => [et('20261113', h, m, s), 1];
  const { runs, holes } = splitRuns([p(10, 0, 0), p(10, 1, 8), p(10, 6, 0), p(10, 7, 8)], MAX_SNAPSHOT_GAP_MS);
  assert.deepEqual(runs.map(r => r.length), [2, 2]);
  assert.equal(holes.length, 1);
});

test("Chej's probe: a 10:00-10:01 trade with only the 09:55 and 10:06 snapshots draws no line", async () => {
  const DB = freshDb();
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 9, 55, 9, 55, 60);
  seedSnapshots(r2, '20261113', 10, 6, 10, 6, 60);
  const pos = position('QQQ', QQQ_OCC, [[et('20261113', 10, 0), 'buy', 1.5], [et('20261113', 10, 1), 'sell', 1.6]]);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.option.segments.filter(lineLike).length, 0, 'no band across the 11-minute hole');
  assert.equal(ctx.underlying.segments.filter(lineLike).length, 0, 'no line across the 11-minute hole');
  assert.deepEqual(ctx.option.segments.map(s => s.kind), ['points']);
  assert.equal(allPoints(ctx.option).length, 2);
  assert.match(ctx.coverage.join(' '), /09:55:00–10:06:00 ET: no snapshot archived \(11 min\); not drawn/);
});

test('missing snapshots inside a dense window split the band and are named', async () => {
  const DB = freshDb();
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 9, 50, 10, 15, 68);
  for (const k of [...r2.objects.keys()]) {
    const m = /snapshot_(\d{6})/.exec(k);
    if (m && m[1] >= '095800' && m[1] < '100300') r2.objects.delete(k);      // archive hole 09:58-10:03
  }
  const pos = position('QQQ', QQQ_OCC, [[et('20261113', 10, 0), 'buy', 1.5], [et('20261113', 10, 5), 'sell', 1.6]]);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.option.segments.filter(lineLike).length, 2);
  assertNoBridge(ctx.option, MAX_SNAPSHOT_GAP_MS);
  assertNoBridge(ctx.underlying, MAX_SNAPSHOT_GAP_MS);
  assert.match(ctx.coverage.join(' '), /09:57:\d\d–10:03:\d\d ET: no snapshot archived \(\d+ min\); not drawn/);
});

test('a missing Tradier part splits the series even though its neighbours are adjacent in the listing', async () => {
  const DB = freshDb();
  const r2 = new FakeR2();
  seedTradier(r2, '20261113', 9, 45, 10, 30);
  // Remove the part ending 10:03 (events 10:00-10:03): the remaining list is still "adjacent".
  const victim = [...r2.objects.keys()].find(k => {
    const up = r2.objects.get(k).uploaded.getTime();
    return Math.abs(up - (nyLocalToUtc('20261113', 10, 3, 0) + 2000)) < 1000;
  });
  assert.ok(victim);
  r2.objects.delete(victim);
  const pos = position('IBIT', IBIT_OCC, [[et('20261113', 10, 0), 'buy', 0.16], [et('20261113', 10, 4), 'sell', 0.15]]);
  const ctx = await positionContext({ DB, PAPER_TRADES: r2 }, pos, { now: after });
  assert.equal(ctx.option.segments.filter(lineLike).length, 2);
  assertNoBridge(ctx.option, MAX_PART_GAP_MS + 15_000);
  assertNoBridge(ctx.underlying, MAX_PART_GAP_MS + 15_000);
  assert.ok(!allPoints(ctx.option).some(p => p[0] > et('20261113', 10, 0) && p[0] < et('20261113', 10, 2, 55)), 'nothing drawn inside the missing part');
  assert.match(ctx.coverage.join(' '), /09:59:5\d–10:03:00 ET: no Tradier archive recorded \(3 min\); not drawn/);
});

async function routeEnv() {
  const DB = freshDb();
  const byOcc = await journal(DB);
  const r2 = new FakeR2();
  seedSnapshots(r2, '20261113', 11, 0, 12, 30, 68);
  const e = { DB, PAPER_TRADES: r2, [VIEWERS_VAR_NAME]: 'viewer_a' };
  await seedAccounts(e, [{ username: 'viewer_a', balance_cash: 0, trades: [] }, { username: 'someone', balance_cash: 0, trades: [] }]);
  await seedSession(e, 'tok-v', 'viewer_a');
  await seedSession(e, 'tok-o', 'someone');
  const get = (q, tok) => worker.fetch(new Request(`https://options.moopertonic.net/api/trade-journal/context?${q}`,
    { headers: tok ? { Cookie: `session=${tok}` } : {} }), e);
  return { e, get, id: byOcc[QQQ_OCC].position_id };
}

test('context route: viewer-only and validated', async () => {
  const { get, id } = await routeEnv();
  assert.equal((await get(`position_id=${id}`)).status, 401);
  assert.equal((await get(`position_id=${id}`, 'tok-o')).status, 403);
  assert.equal((await get('position_id=../etc', 'tok-v')).status, 400);
  assert.equal((await get(`position_id=${id}&date=13-11-2026`, 'tok-v')).status, 400);
  assert.equal((await get('position_id=pos:0000000000000000', 'tok-v')).status, 404);
  const ok = await get(`position_id=${id}&date=2026-11-13`, 'tok-v');
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).occ_symbol, QQQ_OCC);
});

test('armed cutover fence: the context GET still serves but writes nothing', async () => {
  const { e, get, id } = await routeEnv();
  e.DB.db.exec(`INSERT INTO migration_state (name, value) VALUES ('fence', '{"armed":true}')`);
  const before = e.DB.raw('SELECT COUNT(*) AS n FROM trade_journal_context')[0].n;
  const writesBefore = e.DB.queries.filter(q => /^\s*(INSERT|UPDATE|DELETE)/i.test(q)).length;
  const res = await get(`position_id=${id}&date=2026-11-13`, 'tok-v');
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.ok(allPoints(body.option).length > 0, 'reads keep working while fenced');
  assert.equal(body.cache, 'skipped_cutover_fence');
  assert.equal(e.DB.raw('SELECT COUNT(*) AS n FROM trade_journal_context')[0].n, before);
  assert.equal(e.DB.queries.filter(q => /^\s*(INSERT|UPDATE|DELETE)/i.test(q)).length, writesBefore, 'no write statement prepared');
  e.DB.db.exec(`UPDATE migration_state SET value = '{"armed":false}' WHERE name = 'fence'`);
  const unfenced = await (await get(`position_id=${id}&date=2026-11-13`, 'tok-v')).json();
  assert.equal(unfenced.cache, 'stored');
});
