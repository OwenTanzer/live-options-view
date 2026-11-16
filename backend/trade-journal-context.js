// backend/trade-journal-context.js (OA-238) -- market context for one journal
// position: the underlying's price path and the exact option's bid/ask, read
// from the archives already in the PAPER_TRADES bucket, limited to the
// position's own window. Read-only (R2 list/get); results for past sessions are
// cached in D1 because archived market data does not change.
//
// Sources (each series carries its label; they are separate observations from
// the journal's fills and are never used to re-price them):
//   QQQ  intraday/<YYYYMMDD>/snapshot_<HHMMSSffffff>.csv      chain snapshots (~68 s)
//   SPY  intraday/spy/<YYYYMMDD>/snapshot_<HHMMSSffffff>.csv
//   IBIT moo144/tradier-ibit/<YYYY-MM-DD>/*.ndjson.gz          Tradier quotes + time-and-sales
// Anything else has no archive here and is returned as an explicit coverage gap.

import { jsonResponse } from './common.js';
import { JOURNAL_TZ, authorizeJournalViewer, etDate, loadPosition } from './trade-journal.js';

export const CONTEXT_VERSION = 'ctx-v1';
export const MAX_SNAPSHOTS = 40;
export const MAX_TRADIER_PARTS = 15;
const MAX_LISTS = 6;
const PAD_MS = 10 * 60_000;
const OPEN_WINDOW_MS = 60 * 60_000;
const MAX_WINDOW_MS = 3 * 3600_000;
const TRADIER_PART_SPAN_MS = 3.5 * 60_000;   // a part is uploaded at the end of its ~3 minute span
const BUCKET_MS = 15_000;

const SOURCES = {
  QQQ: { kind: 'snapshot', prefix: d => `intraday/${d}/snapshot_` },
  SPY: { kind: 'snapshot', prefix: d => `intraday/spy/${d}/snapshot_` },
  IBIT: { kind: 'tradier', prefix: d => `moo144/tradier-ibit/${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}/` },
};

// ------------------------------------------------------------- time helpers
const partsFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: JOURNAL_TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});
function nyParts(ms) {
  const o = Object.fromEntries(partsFmt.formatToParts(new Date(ms)).map(p => [p.type, p.value]));
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour, mi: +o.minute, s: +o.second };
}
function nyOffsetMs(ms) {
  const p = nyParts(ms);
  return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) - Math.floor(ms / 1000) * 1000;
}
/** America/New_York wall-clock time on day YYYYMMDD -> UTC milliseconds. */
export function nyLocalToUtc(day, h, mi, s, ms = 0) {
  const local = Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6), h, mi, s, ms);
  const guess = local - nyOffsetMs(local);
  return local - nyOffsetMs(guess);
}
export function snapshotKeyTime(key, day) {
  const m = /snapshot_(\d{2})(\d{2})(\d{2})(\d{6})\.csv$/.exec(key);
  return m ? nyLocalToUtc(day, +m[1], +m[2], +m[3], Math.floor(+m[4] / 1000)) : null;
}
function hhmmss(ms) {
  const p = nyParts(ms);
  return [p.h, p.mi, p.s].map(v => String(v).padStart(2, '0')).join('');
}
const iso = ms => new Date(ms).toISOString();

// ---------------------------------------------------------------- parsing
export function csvLine(line) {
  const out = [];
  let cur = '', q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c;
    } else if (c === '"') q = true;
    else if (c === ',') { out.push(cur); cur = ''; } else cur += c;
  }
  out.push(cur);
  return out;
}

function snapshotRow(text, occ) {
  const lines = text.split(/\r?\n/).filter(Boolean);
  if (!lines.length) return { underlying: null, quote: null };
  const head = csvLine(lines[0]);
  const ix = name => head.indexOf(name);
  const [iSym, iBid, iAsk, iUnd] = [ix('OptionSymbol'), ix('Bid'), ix('Ask'), ix('UnderlyingPrice')];
  if (iSym < 0 || iBid < 0 || iAsk < 0) return { underlying: null, quote: null, error: 'snapshot header missing OptionSymbol/Bid/Ask' };
  let underlying = null, quote = null;
  for (let i = 1; i < lines.length; i++) {
    const row = csvLine(lines[i]);
    if (underlying == null && iUnd >= 0 && Number.isFinite(+row[iUnd]) && row[iUnd] !== '') underlying = +row[iUnd];
    if (row[iSym] === occ) {
      const bid = +row[iBid], ask = +row[iAsk];
      if (Number.isFinite(bid) && Number.isFinite(ask) && row[iBid] !== '' && row[iAsk] !== '') quote = [bid, ask];
    }
  }
  return { underlying, quote };
}

async function gunzipText(obj) {
  const stream = obj.body.pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).text();
}

// ------------------------------------------------------------- the reader
function windowFor(position) {
  const open = Date.parse(position.opened_ts);
  const close = position.closed_ts ? Date.parse(position.closed_ts) : open + OPEN_WINDOW_MS;
  const start = open - PAD_MS;
  const end = Math.min(close + PAD_MS, start + MAX_WINDOW_MS);
  return { start, end };
}

async function listAll(bucket, opts, reads, until) {
  const objects = [];
  let cursor;
  do {
    if (reads.lists >= MAX_LISTS) return { objects, complete: false };
    reads.lists += 1;
    const r = await bucket.list({ ...opts, ...(cursor ? { cursor } : {}), limit: 1000 });
    objects.push(...r.objects);
    if (until && r.objects.length && until(r.objects.at(-1))) return { objects, complete: true };
    cursor = r.truncated ? r.cursor : null;
  } while (cursor);
  return { objects, complete: true };
}

function stride(items, max) {
  if (items.length <= max) return { picked: items, every: 1 };
  const every = Math.ceil(items.length / max);
  return { picked: items.filter((_, i) => i % every === 0), every };
}

async function snapshotContext(bucket, src, position, win, reads) {
  const occ = position.contract.occ_symbol, und = position.contract.underlying;
  const day = etDate(position.opened_ts).replaceAll('-', '');
  const prefix = src.prefix(day);
  const { objects, complete } = await listAll(bucket, { prefix, startAfter: prefix + hhmmss(win.start - 1000) }, reads,
    last => (snapshotKeyTime(last.key, day) ?? 0) > win.end);
  const inWindow = objects.filter(o => o.key.endsWith('.csv'))
    .map(o => ({ key: o.key, t: snapshotKeyTime(o.key, day) }))
    .filter(o => o.t != null && o.t >= win.start && o.t <= win.end);
  const { picked, every } = stride(inWindow, MAX_SNAPSHOTS);
  const und_pts = [], opt_pts = [];
  let missingRow = 0;
  for (const s of picked) {
    reads.gets += 1;
    const obj = await bucket.get(s.key);
    if (!obj) continue;
    const { underlying, quote } = snapshotRow(await obj.text(), occ);
    if (underlying != null) und_pts.push([iso(s.t), underlying]);
    if (quote) opt_pts.push([iso(s.t), quote[0], quote[1]]); else missingRow += 1;
  }
  const sampling = `${inWindow.length} snapshots in window${every > 1 ? `, 1 in ${every} read (${picked.length})` : ''}`;
  const gaps = [];
  if (!inWindow.length) gaps.push(`no ${und} chain snapshots archived in this window`);
  if (!complete) gaps.push('snapshot listing stopped at its read limit; later part of the window not shown');
  if (picked.length && !opt_pts.length) gaps.push(`${occ} not present in the archived chain (outside its strike range)`);
  return {
    underlying: und_pts.length ? { source: `${und} chain snapshot UnderlyingPrice (~68 s; ${sampling})`, points: und_pts } : null,
    option: opt_pts.length ? { source: `chain snapshot bid/ask (~68 s; ${sampling}${missingRow ? `; contract absent in ${missingRow}` : ''}). A separate observation from the fill.`, points: opt_pts } : null,
    gaps,
  };
}

async function tradierContext(bucket, src, position, win, reads) {
  const occ = position.contract.occ_symbol, und = position.contract.underlying;
  const day = etDate(position.opened_ts).replaceAll('-', '');
  const { objects, complete } = await listAll(bucket, { prefix: src.prefix(day) }, reads);
  const parts = objects.filter(o => o.key.endsWith('.ndjson.gz'))
    .map(o => ({ key: o.key, t: new Date(o.uploaded).getTime() }))
    .filter(o => o.t >= win.start && o.t <= win.end + TRADIER_PART_SPAN_MS)
    .sort((a, b) => a.t - b.t);
  const { picked, every } = stride(parts, MAX_TRADIER_PARTS);
  const u = new Map(), q = new Map();
  for (const p of picked) {
    reads.gets += 1;
    const obj = await bucket.get(p.key);
    if (!obj) continue;
    for (const line of (await gunzipText(obj)).split('\n')) {
      if (!line) continue;
      let d;
      try { d = JSON.parse(line); } catch { continue; }
      if (d.symbol === und && d.type === 'timesale' && d.last != null && d.date) {
        const t = +d.date; if (t < win.start || t > win.end) continue;
        const b = Math.floor(t / BUCKET_MS);
        if (!u.has(b) || u.get(b)[0] <= t) u.set(b, [t, +d.last]);
      } else if (d.symbol === occ && d.type === 'quote' && d.bid != null && d.ask != null && d.biddate) {
        const t = Math.max(+d.biddate, +(d.askdate ?? 0)); if (t < win.start || t > win.end) continue;
        const b = Math.floor(t / BUCKET_MS);
        if (!q.has(b) || q.get(b)[0] <= t) q.set(b, [t, +d.bid, +d.ask]);
      }
    }
  }
  const sampling = `${parts.length} archive parts in window${every > 1 ? `, 1 in ${every} read (${picked.length}); gaps between them` : ''}`;
  const gaps = [];
  if (!parts.length) gaps.push(`no ${und} Tradier archive parts in this window`);
  if (!complete) gaps.push('Tradier listing stopped at its read limit');
  const sorted = m => [...m.values()].sort((a, b) => a[0] - b[0]);
  return {
    underlying: u.size ? { source: `Tradier ${und} time-and-sales, last trade per 15 s (${sampling})`, points: sorted(u).map(([t, px]) => [iso(t), px]) } : null,
    option: q.size ? { source: `Tradier quotes bid/ask, last per 15 s (${sampling}). A separate observation from the fill.`, points: sorted(q).map(([t, b, a]) => [iso(t), b, a]) } : null,
    gaps,
  };
}

async function sha(text) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}

/** Market context for one supported journal position. */
export async function positionContext(env, position, { now = () => new Date() } = {}) {
  const win = windowFor(position);
  const { underlying: und, occ_symbol: occ } = position.contract;
  const cacheKey = await sha(`${CONTEXT_VERSION}|${occ}|${win.start}|${win.end}`);
  const sessionOver = etDate(position.opened_ts) < etDate(now().toISOString());
  if (sessionOver) {
    const hit = await env.DB.prepare('SELECT context_json FROM trade_journal_context WHERE cache_key = ?').bind(cacheKey).first();
    if (hit) return { ...JSON.parse(hit.context_json), cached: true };
  }
  const src = SOURCES[und];
  const reads = { lists: 0, gets: 0 };
  let ctx;
  if (!src) {
    ctx = { underlying: null, option: null,
      gaps: [`${und} underlying: no archived price series`, `${occ}: no option history in any archive (no premium path)`] };
  } else if (!env.PAPER_TRADES) {
    return { position_id: position.position_id, error: 'R2 binding PAPER_TRADES is not configured' };
  } else {
    ctx = src.kind === 'snapshot'
      ? await snapshotContext(env.PAPER_TRADES, src, position, win, reads)
      : await tradierContext(env.PAPER_TRADES, src, position, win, reads);
  }
  const out = { position_id: position.position_id, occ_symbol: occ, underlying_symbol: und,
    window: { start: iso(win.start), end: iso(win.end) }, ...ctx, reads, built_at: now().toISOString(), version: CONTEXT_VERSION };
  if (sessionOver) {
    await env.DB.prepare('INSERT OR REPLACE INTO trade_journal_context (cache_key, position_id, built_at, context_json) VALUES (?, ?, ?, ?)')
      .bind(cacheKey, position.position_id, out.built_at, JSON.stringify(out)).run();
  }
  return { ...out, cached: false };
}

const POSITION_ID = /^pos:[0-9a-f]{16}$/;

export async function handleTradeJournalContext(request, env, url) {
  const auth = await authorizeJournalViewer(request, env);
  if (auth.response) return auth.response;
  const id = url.searchParams.get('position_id') ?? '';
  if (!POSITION_ID.test(id)) return jsonResponse({ error: 'position_id must look like pos:<16 hex>' }, 400);
  const position = await loadPosition(env.DB, id);
  if (!position) return jsonResponse({ error: 'unknown position' }, 404);
  if (!position.supported) return jsonResponse({ error: 'position has no readable fills', reason: position.unsupported_reason }, 409);
  const ctx = await positionContext(env, position);
  return jsonResponse(ctx, ctx.error ? 503 : 200);
}
