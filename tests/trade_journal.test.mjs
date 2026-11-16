// tests/trade_journal.test.mjs (OA-238) -- the read-only Trade Retrospectives
// reader: full pagination, page-body fills, refresh in place, archival and
// removal, sync status / read errors, unsupported rows, and the session +
// viewer gate. Real schema and SQL on node:sqlite; Notion is a local fake that
// serves a SYNTHETIC API-shaped fixture (invented trades in the journal's body
// formats; see its _note). No real journal data is in this repository.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { FakeD1 } from './helpers/d1_shim.mjs';
import { seedAccounts, seedSession } from './helpers/test_env.mjs';
import worker from '../backend/index.js';
import {
  TRADE_JOURNAL_DATA_SOURCE_ID, TOKEN_SECRET_NAME, VIEWERS_VAR_NAME, MAX_BODY_REQUESTS, MAX_QUERY_PAGES,
  MAX_MISSING_CHECKS, MAX_BODY_DEPTH, pagePosition,
  etDate, handleTradeJournalSync, journalDay, syncTradeJournal,
} from '../backend/trade-journal.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'trade_journal_notion.json'), 'utf8'));
const TOKEN = 'secret_TEST_TOKEN_never_echoed';
const P673 = 'a0000000-0000-4000-8000-000000000005';   // synthetic QQQ $777 put, Nov 13 (F2 format)
const TLT8050 = 'a0000000-0000-4000-8000-000000000011'; // synthetic TLT $85 put with partials + fee reconciliation
const clone = v => JSON.parse(JSON.stringify(v));

function freshDb() {
  const DB = new FakeD1();
  DB.db.exec(FakeD1.schemaSql());
  DB.db.exec(fs.readFileSync(path.join(ROOT, 'backend', 'migrations', '0003_trade_journal.sql'), 'utf8'));
  return DB;
}

/** In-memory Notion: query (paginated), block children (paginated), page retrieve. */
class FakeNotion {
  constructor(pages, { queryPageSize = 4, blockPageSize = 3 } = {}) {
    this.pages = new Map(pages.map(p => [p.id, clone(p)]));
    this.archived = new Set();
    this.deleted = new Set();
    this.queryPageSize = queryPageSize;
    this.blockPageSize = blockPageSize;
    this.calls = [];
    this.failQueryCall = null;
    this.children = new Map();        // block id -> nested child blocks
    this.rejectCursors = false;       // simulate Notion refusing a saved cursor
    this.fetch = this.fetch.bind(this);
  }
  json(status, body) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
  async fetch(url, init) {
    const u = new URL(url);
    this.calls.push({ method: init.method, path: u.pathname, headers: init.headers, body: init.body });
    assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(init.headers['Notion-Version'], '2025-09-03');
    let m;
    if (init.method === 'POST' && (m = /^\/v1\/data_sources\/([^/]+)\/query$/.exec(u.pathname))) {
      assert.equal(m[1], TRADE_JOURNAL_DATA_SOURCE_ID);
      const n = this.calls.filter(c => c.path.endsWith('/query')).length;
      if (this.failQueryCall === n) return this.json(500, { object: 'error', code: 'internal_server_error', message: 'boom' });
      const body = JSON.parse(init.body);
      assert.deepEqual(body.sorts, [{ timestamp: 'created_time', direction: 'ascending' }]);
      if (body.start_cursor && (this.rejectCursors || !this.pages.has(body.start_cursor))) {
        return this.json(400, { object: 'error', code: 'validation_error', message: 'bad cursor' });
      }
      const live = [...this.pages.values()].filter(p => !this.archived.has(p.id) && !this.deleted.has(p.id))
        .sort((a, b) => (a.id < b.id ? -1 : 1));
      const start = body.start_cursor ? live.findIndex(p => p.id === body.start_cursor) : 0;
      const slice = live.slice(start, start + this.queryPageSize);
      const next = live[start + this.queryPageSize];
      return this.json(200, { object: 'list', results: slice.map(({ _blocks, ...p }) => p), has_more: Boolean(next), next_cursor: next?.id ?? null });
    }
    if (init.method === 'GET' && (m = /^\/v1\/blocks\/([^/]+)\/children$/.exec(u.pathname))) {
      const page = this.pages.get(m[1]) ?? (this.children.has(m[1]) ? { _blocks: this.children.get(m[1]) } : null);
      if (!page) return this.json(404, { object: 'error', code: 'object_not_found' });
      const start = Number(u.searchParams.get('start_cursor') ?? 0);
      const slice = page._blocks.slice(start, start + this.blockPageSize);
      const more = start + this.blockPageSize < page._blocks.length;
      return this.json(200, { object: 'list', results: slice, has_more: more, next_cursor: more ? String(start + this.blockPageSize) : null });
    }
    if (init.method === 'GET' && (m = /^\/v1\/pages\/([^/]+)$/.exec(u.pathname))) {
      if (this.deleted.has(m[1]) || !this.pages.has(m[1])) return this.json(404, { object: 'error', code: 'object_not_found' });
      return this.json(200, { object: 'page', id: m[1], archived: this.archived.has(m[1]), in_trash: this.archived.has(m[1]),
        parent: { type: 'data_source_id', data_source_id: TRADE_JOURNAL_DATA_SOURCE_ID } });
    }
    throw new Error(`unexpected Notion call ${init.method} ${u.pathname}`);
  }
  count(kind) { return this.calls.filter(c => c.path.includes(kind)).length; }
}

function clock(iso) {
  let t = Date.parse(iso);
  const now = () => new Date(t);
  now.advance = ms => { t += ms; };
  return now;
}

const env = (DB, extra = {}) => ({ DB, [TOKEN_SECRET_NAME]: TOKEN, ...extra });

/** The TLT $80.50 row as it read before its real Nov 13 fee reconciliation was appended. */
function tltBeforeReconciliation() {
  const p = clone(FIXTURE.pages.find(x => x.id === TLT8050));
  const cut = p._blocks.findIndex(b => b.type === 'heading_3' && b.heading_3.rich_text[0].plain_text.startsWith('November 13 fee reconciliation'));
  assert.ok(cut > 0);
  p._blocks = p._blocks.slice(0, cut);
  p.properties.Fees.rich_text[0].plain_text = 'USD 2 estimated';
  p.last_edited_time = '2026-11-12T21:30:00.000Z';
  return p;
}

test('full sync paginates the data source and every page body', async () => {
  const DB = freshDb();
  const notion = new FakeNotion(FIXTURE.pages);
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now: clock('2026-11-16T15:00:00Z') });
  assert.equal(r.ok, true, JSON.stringify(r.error));
  assert.equal(r.summary.rows_listed, 11);
  assert.equal(r.summary.inserted.length, 11);
  assert.equal(r.summary.scan.finished, true);
  assert.equal(r.summary.pending_rows, 0);
  assert.equal(DB.raw('SELECT COUNT(*) AS n FROM trade_journal_scan')[0].n, 0, 'scan state cleared once finished');
  assert.equal(notion.count('/query'), 3);                         // 11 rows at 4 per page
  assert.ok(notion.count('/children') > 11, 'block children were paginated');
  assert.equal(r.summary.unsupported, 0);
});

test('Nov 13 (America/New_York) reconciles to the journal and keeps the importer identities', async () => {
  const DB = freshDb();
  await syncTradeJournal(env(DB), { fetchImpl: new FakeNotion(FIXTURE.pages).fetch, now: clock('2026-11-16T15:00:00Z') });
  const day = await journalDay(DB, '2026-11-13');
  const j = FIXTURE.journal_day_totals;
  assert.deepEqual([day.totals.positions, day.totals.fills, day.totals.gross, day.totals.net],
    [j.positions, j.fills, j.gross, j.net_estimated]);
  for (const p of day.positions) assert.ok(Object.values(p.row_checks).every(Boolean), p.report);
  const p673 = day.positions.find(p => p.journal_page_id === P673);
  // Same identity rule as the OA-238 Python importer; this hash was computed independently in Python.
  assert.equal(p673.position_id, 'pos:bcf7388aa9c37540');
  assert.deepEqual(p673.executions.map(e => [e.execution_id, e.side, e.qty, e.price, e.ts]), [
    ['notion:a0000000000040008000000000000005:order:7212', 'buy', 1, 1.95, '2026-11-13T16:40:25.000Z'],
    ['notion:a0000000000040008000000000000005:order:7213', 'sell', 1, 1.71, '2026-11-13T16:45:50.000Z'],
  ]);
  // Rows entered earlier on Nov-13 contracts all executed before Nov 13.
  const all = DB.raw('SELECT position_json FROM trade_journal_rows').map(r => JSON.parse(r.position_json));
  const ids = new Set(day.positions.map(p => p.position_id));
  for (const p of all.filter(p => !ids.has(p.position_id))) assert.ok(p.executions.every(e => etDate(e.ts) < '2026-11-13'), p.report);
});

test('ET date boundary', () => {
  assert.equal(etDate('2026-11-14T04:59:59.000Z'), '2026-11-13');
  assert.equal(etDate('2026-11-14T05:00:00.000Z'), '2026-11-14');
});

test('a repeat sync of an unchanged journal reads no bodies and changes nothing', async () => {
  const DB = freshDb();
  const notion = new FakeNotion(FIXTURE.pages);
  const now = clock('2026-11-16T15:00:00Z');
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  const before = DB.raw('SELECT * FROM trade_journal_rows ORDER BY page_id');
  now.advance(5 * 60_000);
  notion.calls = [];
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(r.summary.unchanged, 11);
  assert.equal(notion.count('/children'), 0);
  assert.deepEqual(DB.raw('SELECT * FROM trade_journal_rows ORDER BY page_id'), before);
});

test('a real journal correction refreshes the existing position in place', async () => {
  const DB = freshDb();
  const notion = new FakeNotion([tltBeforeReconciliation()]);
  const now = clock('2026-11-12T22:00:00Z');
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  const [old] = DB.raw('SELECT * FROM trade_journal_rows');
  const oldPos = JSON.parse(old.position_json);
  assert.equal(oldPos.fees, 2);
  notion.pages.set(TLT8050, clone(FIXTURE.pages.find(x => x.id === TLT8050)));   // the page as it reads after 11-13
  now.advance(24 * 3600_000);
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(r.summary.inserted.length, 0);
  assert.equal(r.summary.refreshed.length, 1);
  const rows = DB.raw('SELECT * FROM trade_journal_rows');
  assert.equal(rows.length, 1);
  const pos = JSON.parse(rows[0].position_json);
  assert.equal(pos.position_id, oldPos.position_id);
  assert.deepEqual(pos.executions.map(e => e.execution_id), oldPos.executions.map(e => e.execution_id));
  assert.equal(pos.fees, 2.035724);
  assert.equal(rows[0].revision, 2);
  assert.ok(JSON.parse(rows[0].revisions_json)[1].changed_fields.includes('fees'));
});

test('an edit inside the same reported minute is still picked up', async () => {
  const DB = freshDb();
  const page = clone(FIXTURE.pages.find(x => x.id === P673));
  page.last_edited_time = '2026-11-16T15:00:00.000Z';
  const notion = new FakeNotion([page]);
  const now = clock('2026-11-16T15:00:20Z');
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  // Edit at 15:00:40 -- Notion still reports 15:00:00.
  notion.pages.get(P673)._blocks[0].paragraph.rich_text.at(-1).plain_text =
    notion.pages.get(P673)._blocks[0].paragraph.rich_text.at(-1).plain_text.replace('USD 1.71', 'USD 1.72');
  now.advance(30_000);
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(r.summary.refreshed.length, 1);
  // That read (15:00:50) could itself precede a later edit in minute 15:00, so
  // one more read follows; after a read past 15:01:00 it stops.
  now.advance(5 * 60_000);
  notion.calls = [];
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(notion.count('/children'), 1);
  now.advance(5 * 60_000);
  notion.calls = [];
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(notion.count('/children'), 0, 'stops re-reading once a read is past the edit minute');
});

test('archived and removed rows stay visible, flagged, and out of the counted totals', async () => {
  const DB = freshDb();
  const notion = new FakeNotion(FIXTURE.pages);
  const now = clock('2026-11-16T15:00:00Z');
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  const ibit = FIXTURE.pages.find(p => p.properties.Contract.rich_text[0].plain_text.startsWith('IBIT')).id;
  notion.archived.add(P673);
  notion.deleted.add(ibit);
  now.advance(5 * 60_000);
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.deepEqual(r.summary.status_changes.map(s => s.to).sort(), ['archived', 'removed']);
  const day = await journalDay(DB, '2026-11-13');
  assert.equal(day.positions.length, 5);
  assert.equal(day.positions.find(p => p.journal_page_id === P673).source_status, 'archived');
  assert.equal(day.positions.find(p => p.journal_page_id === ibit).source_status, 'removed');
  assert.equal(day.totals.positions, 3);
  assert.equal(day.totals.excluded_not_active, 2);
  // Restored in Notion -> active again on the next sync.
  notion.archived.delete(P673);
  now.advance(5 * 60_000);
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal((await journalDay(DB, '2026-11-13')).positions.find(p => p.journal_page_id === P673).source_status, 'active');
});

test('a failed read records a clear error and keeps serving the last good rows', async () => {
  const DB = freshDb();
  const notion = new FakeNotion(FIXTURE.pages);
  const now = clock('2026-11-16T15:00:00Z');
  const ok = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  now.advance(5 * 60_000);
  notion.failQueryCall = notion.count('/query') + 2;          // second page of the next scan fails
  notion.pages.delete(P673);                                   // would look "missing" if the partial scan were trusted
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(r.ok, false);
  assert.equal(r.error.status, 500);
  assert.equal(r.error.code, 'internal_server_error');
  const day = await journalDay(DB, '2026-11-13');
  assert.equal(day.totals.positions, 5, 'no row flagged from an incomplete scan');
  assert.equal(day.sync.last_success_at, ok.summary.finished_at);
  assert.equal(day.sync.last_error.status, 500);
  assert.equal(day.sync.running, false);
});

test('a row in an unknown format stays visible as unsupported', async () => {
  const DB = freshDb();
  const odd = clone(FIXTURE.pages.find(x => x.id === P673));
  odd.id = '0000c519-147b-81aa-bbbb-ccccdddd0001';
  odd._blocks = [{ object: 'block', id: 'x', type: 'paragraph', has_children: false,
    paragraph: { rich_text: [{ type: 'text', plain_text: 'Bought puts around the open; sold before lunch.', href: null }] } }];
  await syncTradeJournal(env(DB), { fetchImpl: new FakeNotion([odd]).fetch, now: clock('2026-11-16T15:00:00Z') });
  const day = await journalDay(DB, '2026-11-13');
  assert.equal(day.positions.length, 0);
  assert.equal(day.unsupported.length, 1);
  assert.match(day.unsupported[0].unsupported_reason, /no supported fill-line format/);
});

test('the per-sync request budget defers rows and the next sync finishes them', async () => {
  const DB = freshDb();
  // 22 rows (the journal plus a renamed copy of each) at one block per request: ~70 body requests.
  const copies = FIXTURE.pages.map(p => ({ ...clone(p), id: p.id.replace(/^.{4}/, 'ffff') }));
  const notion = new FakeNotion([...FIXTURE.pages, ...copies], { blockPageSize: 1 });
  const now = clock('2026-11-16T15:00:00Z');
  const first = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.ok(first.summary.pending_rows > 0);
  assert.equal(first.summary.scan.finished, false);
  assert.ok(notion.count('/children') <= MAX_BODY_REQUESTS + 10, 'body reads stop at the cap (+ one page finishing)');
  now.advance(5 * 60_000);
  const second = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(second.summary.scan.resumed, true);
  assert.equal(second.summary.pending_rows, 0);
  assert.equal(second.summary.scan.finished, true);
  assert.equal(DB.raw('SELECT COUNT(*) AS n FROM trade_journal_rows')[0].n, 22);
});

test('overlapping syncs do not race', async () => {
  const DB = freshDb();
  DB.db.exec(`INSERT INTO trade_journal_sync (id, running_since) VALUES (1, '2026-11-16T14:59:00.000Z')`);
  const r = await syncTradeJournal(env(DB), { fetchImpl: new FakeNotion(FIXTURE.pages).fetch, now: clock('2026-11-16T15:00:00Z') });
  assert.equal(r.error.code, 'busy');
});

// ------------------------------------------------- review fixes (Chej, 11-16)
function syntheticPage(id, body, props = {}) {
  const base = clone(FIXTURE.pages.find(x => x.id === P673));
  base.id = id;
  base._blocks = body.map((text, i) => ({ object: 'block', id: `${id}-b${i}`, type: 'paragraph', has_children: false,
    paragraph: { rich_text: [{ type: 'text', plain_text: text, href: null }] } }));
  for (const [k, v] of Object.entries(props)) base.properties[k] = v;
  return base;
}
const num = n => ({ type: 'number', number: n });
const txt = v => ({ type: 'rich_text', rich_text: [{ type: 'text', plain_text: v, href: null }] });

test('an open position reports remaining cost and fees, not a realised loss', async () => {
  const p = await pagePosition({
    page_id: 'b0000000-0000-4000-8000-000000000001', page_url: 'u', last_edited_time: '2026-11-13T21:00:00.000Z',
    properties: { Contract: "QQQ Nov 13 '26 $777 Put", Quantity: 1, 'Gross P&L': null, Fees: 'USD 0.50 estimated', Report: 'open' },
    body: 'Entry order 7300: 2026-11-13T11:37:25.000-05:00, 1 contracts at USD 1.83; entry receipt.',
  });
  assert.equal(p.closed, false);
  assert.equal(p.gross_pnl, null);
  assert.equal(p.net_pnl, null);
  assert.equal(p.open_qty, 1);
  assert.equal(p.open_side, 'long');
  assert.equal(p.open_cost_basis, 183);
  assert.equal(p.fees, 0.5);
  assert.equal(p.realized_gross_pnl, 0);
  assert.equal(p.row_checks.gross_matches_row, null);
});

test('a partial close realises only the closed quantity', async () => {
  const p = await pagePosition({
    page_id: 'b0000000-0000-4000-8000-000000000002', page_url: 'u', last_edited_time: '2026-11-13T21:00:00.000Z',
    properties: { Contract: "QQQ Nov 13 '26 $777 Put", Quantity: 2, Fees: 'USD 1.50 estimated', Report: 'partial' },
    body: 'Entry order 7301: 2026-11-13T10:00:00.000-05:00, 2 contracts at USD 1.00; x. Exit order 7302: 2026-11-13T10:30:00.000-05:00, 1 contracts at USD 1.50; y.',
  });
  assert.equal(p.closed, false);
  assert.equal(p.realized_gross_pnl, 50);
  assert.equal(p.open_qty, 1);
  assert.equal(p.open_cost_basis, 100);
  assert.equal(p.gross_pnl, null);
});

test('day totals keep open positions out of realised gross and net', async () => {
  const DB = freshDb();
  const open = syntheticPage('b0000000-0000-4000-8000-000000000003',
    ['E*TRADE ending 0000. [Entry receipt 7400](link) at 2026-11-13T12:00:00.000-05:00: USD 1.83.'],
    { Quantity: num(1), Fees: txt('USD 0.50 estimated'), 'Gross P&L': num(null) });
  await syncTradeJournal(env(DB), { fetchImpl: new FakeNotion([...FIXTURE.pages, open]).fetch, now: clock('2026-11-16T15:00:00Z') });
  const day = await journalDay(DB, '2026-11-13');
  const j = FIXTURE.journal_day_totals;
  assert.equal(day.totals.positions, 6);
  assert.equal(day.totals.closed_positions, 5);
  assert.equal(day.totals.gross, j.gross);
  assert.equal(day.totals.net, j.net_estimated);
  assert.equal(day.totals.open_positions, 1);
  assert.equal(day.totals.open_cost_basis, 183);
  assert.equal(day.totals.open_fees_paid, 0.5);
});

test('a data source larger than one sync resumes from its saved cursor', async () => {
  const DB = freshDb();
  const many = [...FIXTURE.pages, ...FIXTURE.pages.map(p => ({ ...clone(p), id: p.id.replace(/^.{4}/, 'eeee') }))];   // 22 rows
  const notion = new FakeNotion(many, { queryPageSize: 1 });                                                        // 22 query pages
  const now = clock('2026-11-16T15:00:00Z');
  const first = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(first.ok, true);
  assert.equal(notion.count('/query'), MAX_QUERY_PAGES);
  assert.equal(first.summary.scan.query_complete, false);
  assert.equal(first.summary.scan.finished, false);
  assert.equal(first.summary.status_changes.length, 0, 'an unfinished scan flags nothing');
  const saved = DB.raw('SELECT scan_cursor, scan_started_at FROM trade_journal_sync')[0];
  assert.ok(saved.scan_cursor);
  notion.calls = [];
  now.advance(5 * 60_000);
  const second = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  const firstQuery = notion.calls.find(c => c.path.endsWith('/query'));
  assert.equal(JSON.parse(firstQuery.body).start_cursor, saved.scan_cursor, 'resumed from the saved cursor, not from the start');
  assert.equal(second.summary.scan.resumed, true);
  assert.equal(second.summary.scan.started_at, saved.scan_started_at);
  assert.equal(second.summary.scan.finished, true);
  assert.equal(DB.raw('SELECT COUNT(*) AS n FROM trade_journal_rows')[0].n, 22);
  assert.equal(DB.raw('SELECT scan_cursor FROM trade_journal_sync')[0].scan_cursor, null);
});

test('a saved cursor Notion refuses restarts the scan visibly', async () => {
  const DB = freshDb();
  const many = [...FIXTURE.pages, ...FIXTURE.pages.map(p => ({ ...clone(p), id: p.id.replace(/^.{4}/, 'eeee') }))];
  const notion = new FakeNotion(many, { queryPageSize: 1 });
  const now = clock('2026-11-16T15:00:00Z');
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  notion.rejectCursors = true;
  now.advance(5 * 60_000);
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(r.ok, true);
  assert.equal(r.summary.scan.restarted_after, 'validation_error');
  assert.equal(r.summary.scan.finished, false);
});

test('nested blocks are read within the depth limit; deeper bodies are marked, not silently cut', async () => {
  const DB = freshDb();
  // Fill lines nested two levels down (e.g. inside toggles) are read.
  const nested = syntheticPage('b0000000-0000-4000-8000-000000000004', ['E*TRADE ending 0000. Fills below.']);
  nested._blocks[0].has_children = true;
  const fills = { object: 'block', id: 'nest-2', type: 'paragraph', has_children: false, paragraph: { rich_text: [
    { type: 'text', plain_text: 'Entry receipt 7500', href: 'https://example.invalid/receipt' },
    { type: 'text', plain_text: ' at 2026-11-13T10:00:00.000-05:00: USD 1.00. ', href: null },
    { type: 'text', plain_text: 'Exit receipt 7501', href: 'https://example.invalid/receipt' },
    { type: 'text', plain_text: ' at 2026-11-13T10:05:00.000-05:00: USD 1.20.', href: null }] } };
  const toggle = { object: 'block', id: 'nest-1', type: 'paragraph', has_children: true, paragraph: { rich_text: [{ type: 'text', plain_text: 'Details', href: null }] } };
  // A page nested past the limit.
  const deep = syntheticPage('b0000000-0000-4000-8000-000000000005', ['E*TRADE ending 0000.']);
  deep._blocks[0].has_children = true;
  const notion = new FakeNotion([nested, deep]);
  notion.children.set(nested._blocks[0].id, [toggle]);
  notion.children.set('nest-1', [fills]);
  let parent = deep._blocks[0].id;
  for (let d = 1; d <= MAX_BODY_DEPTH + 1; d++) {
    const b = { object: 'block', id: `deep-${d}`, type: 'paragraph', has_children: d <= MAX_BODY_DEPTH, paragraph: { rich_text: [{ type: 'text', plain_text: `level ${d}`, href: null }] } };
    notion.children.set(parent, [b]);
    parent = b.id;
  }
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now: clock('2026-11-16T15:00:00Z') });
  assert.equal(r.ok, true);
  const rows = Object.fromEntries(DB.raw('SELECT page_id, position_json FROM trade_journal_rows').map(x => [x.page_id, JSON.parse(x.position_json)]));
  const n = rows['b0000000-0000-4000-8000-000000000004'];
  assert.equal(n.supported, true);
  assert.equal(n.n_fills, 2);
  const d = rows['b0000000-0000-4000-8000-000000000005'];
  assert.equal(d.supported, false);
  assert.match(d.unsupported_reason, /page body not fully read: blocks nested deeper than/);
});

test('removal checks are capped per sync and the rest stay flagged for the next scan', async () => {
  const DB = freshDb();
  const notion = new FakeNotion(FIXTURE.pages);
  const now = clock('2026-11-16T15:00:00Z');
  await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  const gone = FIXTURE.pages.slice(0, MAX_MISSING_CHECKS + 2).map(p => p.id);
  for (const id of gone) notion.deleted.add(id);
  now.advance(5 * 60_000);
  notion.calls = [];
  const r = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(notion.count('/pages/'), MAX_MISSING_CHECKS);
  assert.equal(r.summary.status_changes.filter(s => s.to === 'removed').length, MAX_MISSING_CHECKS);
  assert.equal(r.summary.unverified_missing.length, 2);
  const statuses = () => DB.raw('SELECT source_status FROM trade_journal_rows').map(x => x.source_status);
  assert.equal(statuses().filter(s => s === 'missing_unverified').length, 2, 'unchecked rows are visibly flagged');
  now.advance(5 * 60_000);
  const next = await syncTradeJournal(env(DB), { fetchImpl: notion.fetch, now });
  assert.equal(next.summary.status_changes.filter(s => s.from === 'missing_unverified' && s.to === 'removed').length, 2);
  assert.equal(statuses().filter(s => s === 'removed').length, MAX_MISSING_CHECKS + 2);
});

// ---------------------------------------------------------------- routes
async function routeEnv({ viewers = 'viewer_a', token = TOKEN } = {}) {
  const DB = freshDb();
  const e = { DB, [VIEWERS_VAR_NAME]: viewers, ...(token ? { [TOKEN_SECRET_NAME]: token } : {}) };
  await seedAccounts(e, [{ username: 'viewer_a', balance_cash: 0, trades: [] }, { username: 'someone', balance_cash: 0, trades: [] }]);
  await seedSession(e, 'tok-viewer', 'viewer_a');
  await seedSession(e, 'tok-other', 'someone');
  return e;
}
const req = (p, cookie, init = {}) => new Request(`https://options.moopertonic.net${p}`, {
  ...init, headers: { ...(cookie ? { Cookie: `session=${cookie}` } : {}), ...(init.headers ?? {}) } });

test('routes require a session and the viewer allowlist (fail closed)', async () => {
  const e = await routeEnv();
  assert.equal((await worker.fetch(req('/api/trade-journal?date=2026-11-13'), e)).status, 401);
  assert.equal((await worker.fetch(req('/api/trade-journal?date=2026-11-13', 'tok-other'), e)).status, 403);
  assert.equal((await worker.fetch(req('/api/trade-journal?date=2026-11-13', 'tok-viewer'), e)).status, 200);
  assert.equal((await worker.fetch(req('/api/trade-journal?date=13-11-2026', 'tok-viewer'), e)).status, 400);
  const unset = await routeEnv({ viewers: '' });
  const res = await worker.fetch(req('/api/trade-journal?date=2026-11-13', 'tok-viewer'), unset);
  assert.equal(res.status, 403);
  assert.equal((await res.json()).setting, VIEWERS_VAR_NAME);
  const post = await worker.fetch(req('/api/trade-journal/sync', 'tok-viewer', { method: 'POST', headers: { Origin: 'https://evil.example' } }), e);
  assert.equal(post.status, 403);
});

test('missing secret is reported by name, never guessed', async () => {
  const e = await routeEnv({ token: null });
  const res = await worker.fetch(req('/api/trade-journal/sync', 'tok-viewer', { method: 'POST' }), e);
  assert.equal(res.status, 503);
  const body = await res.json();
  assert.equal(body.error.code, 'not_configured');
  assert.match(body.error.message, new RegExp(TOKEN_SECRET_NAME));
});

test('sync + row read through the routes; the token never leaves the Authorization header', async () => {
  const e = await routeEnv();
  const notion = new FakeNotion(FIXTURE.pages);
  const syncRes = await handleTradeJournalSync(req('/api/trade-journal/sync', 'tok-viewer', { method: 'POST' }), e, { fetchImpl: notion.fetch });
  const syncText = await syncRes.text();
  assert.equal(syncRes.status, 200, syncText);
  const rowRes = await worker.fetch(req(`/api/trade-journal/row?page_id=${P673.replaceAll('-', '')}`, 'tok-viewer'), e);
  const rowText = await rowRes.text();
  assert.equal(rowRes.status, 200);
  const { row, served_at } = JSON.parse(rowText);
  assert.equal(row.source_last_edited, '2026-11-13T21:36:00.000Z');
  assert.ok(row.app_read_at && served_at);
  assert.equal(row.contract.occ_symbol, 'QQQ261113P00777000');
  const dayText = await (await worker.fetch(req('/api/trade-journal?date=2026-11-13', 'tok-viewer'), e)).text();
  const stored = JSON.stringify(e.DB.raw('SELECT * FROM trade_journal_rows')) + JSON.stringify(e.DB.raw('SELECT * FROM trade_journal_sync'));
  for (const text of [syncText, rowText, dayText, stored]) {
    assert.ok(!text.includes(TOKEN), 'token leaked');
    assert.ok(!text.includes('example.invalid'), 'receipt link destination stored or served');
  }
});
