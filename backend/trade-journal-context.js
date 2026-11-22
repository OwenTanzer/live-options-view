// backend/trade-journal-context.js (OA-238) -- market context for one journal
// position on one New York trading day: the underlying's price path and the
// exact option's bid/ask, read from the archives already in the PAPER_TRADES
// bucket. Read-only (R2 list/get); results for finished sessions are cached in
// D1 because archived market data does not change.
//
// Coverage plan for the selected day (bounded, and explicit about the rest):
//   * dense  -- every archived observation within FILL_PAD of each fill that day
//   * sparse -- the rest of the position's life inside that day's session
//               (09:30-16:15 ET), sampled evenly with the remaining read budget
//   * notes  -- entered on an earlier day / exits on a later day / still open
// Series come back as segments. A segment is drawn on its own; nothing is ever
// joined across the gap between two segments. Continuity is decided from the
// observations' own timestamps (snapshots more than ~2 cadences apart, Tradier
// parts whose recorded events do not meet), never from list adjacency; holes
// inside a window are split out and named in `coverage`. Sparse snapshot
// samples and lone observations are single points (kind "points"), never a line.
//
// Sources (each labelled; separate observations from the journal's fills):
//   QQQ  intraday/<YYYYMMDD>/snapshot_<HHMMSSffffff>.csv      chain snapshots (~68 s)
//   SPY  intraday/spy/<YYYYMMDD>/snapshot_<HHMMSSffffff>.csv
//   IBIT moo144/tradier-ibit/<YYYY-MM-DD>/*.ndjson.gz          Tradier quotes + time-and-sales (~3 min parts)
// Anything else has no archive here and is returned as an explicit coverage gap.
//
// While the cutover fence is armed the route still serves reads, but does not
// write the D1 cache.

import { jsonResponse } from './common.js';
import { cutoverFenceActive } from './fence.js';
import { JOURNAL_TZ, authorizeJournalViewer, etDate, loadPosition } from './trade-journal.js';
import { journalCutoff,journalScope } from './one-clock-journal.js';
import { publicChainAt } from './one-clock-chain.js';

export const CONTEXT_VERSION = 'ctx-v4';
export const MAX_SNAPSHOTS = 40;
export const MAX_TRADIER_PARTS = 15;
export const FILL_PAD_MS = 5 * 60_000;
const MAX_LISTS = 6;
const TRADIER_PART_MS = 3 * 60_000;           // a part holds ~3 minutes and is uploaded at its end
const TRADIER_UPLOAD_SLACK_MS = 30_000;
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
const hm = ms => { const p = nyParts(ms); return `${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')}`; };
const iso = ms => new Date(ms).toISOString();

// ------------------------------------------------------------ the plan
function mergeRanges(ranges) {
  const out = [];
  for (const r of [...ranges].sort((a, b) => a[0] - b[0])) {
    if (out.length && r[0] <= out.at(-1)[1]) out.at(-1)[1] = Math.max(out.at(-1)[1], r[1]);
    else out.push([...r]);
  }
  return out;
}
function subtract([s, e], cuts) {
  const out = [];
  let cur = s;
  for (const [a, b] of mergeRanges(cuts)) {
    if (b <= cur || a >= e) continue;
    if (a > cur) out.push([cur, a]);
    cur = Math.max(cur, b);
  }
  if (cur < e) out.push([cur, e]);
  return out;
}

/** What to cover for `position` on New York day `day` (YYYY-MM-DD). */
export function coveragePlan(position, day) {
  const d = day.replaceAll('-', '');
  const sessionStart = nyLocalToUtc(d, 9, 30, 0), sessionEnd = nyLocalToUtc(d, 16, 15, 0);
  const open = Date.parse(position.opened_ts);
  const close = position.closed_ts ? Date.parse(position.closed_ts) : null;
  const fills = position.executions.filter(e => etDate(e.ts) === day).map(e => Date.parse(e.ts));
  const aliveStart = Math.max(open, sessionStart), aliveEnd = Math.min(close ?? sessionEnd, sessionEnd);
  const dense = mergeRanges(fills.map(t => [t - FILL_PAD_MS, t + FILL_PAD_MS]));
  const parts = [...dense.flat(), ...(aliveStart < aliveEnd ? [aliveStart, aliveEnd] : [])];
  if (!parts.length) return { day, d, interval: null, dense: [], sparse: [], notes: ['no fill or open position on this day'] };
  const interval = [Math.min(...parts), Math.max(...parts)];
  const notes = [];
  if (etDate(position.opened_ts) < day) notes.push(`entered ${etDate(position.opened_ts)}; this day starts at the ${hm(sessionStart)} ET open`);
  if (position.expired_on) notes.push('expired worthless on ' + position.expired_on + '; no sale fill');
  else if (close == null) notes.push('still open in the journal; covered to the session close');
  else if (etDate(position.closed_ts) > day) notes.push(`exits ${etDate(position.closed_ts)}; this day is covered to the ${hm(sessionEnd)} ET close`);
  return { day, d, interval, dense, sparse: subtract(interval, dense), notes, sessionStart, sessionEnd };
}

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
  if (iSym < 0 || iBid < 0 || iAsk < 0) return { underlying: null, quote: null };
  let underlying = null, quote = null;
  for (let i = 1; i < lines.length; i++) {
    const row = csvLine(lines[i]);
    if (underlying == null && iUnd >= 0 && row[iUnd] !== '' && Number.isFinite(+row[iUnd])) underlying = +row[iUnd];
    if (row[iSym] === occ && row[iBid] !== '' && row[iAsk] !== '' && Number.isFinite(+row[iBid]) && Number.isFinite(+row[iAsk])) {
      quote = [+row[iBid], +row[iAsk]];
    }
  }
  return { underlying, quote };
}

async function gunzipText(obj) {
  return new Response(obj.body.pipeThrough(new DecompressionStream('gzip'))).text();
}

const inAny = (t, ranges) => ranges.some(([a, b]) => t >= a && t <= b);
function evenPick(items, n) {
  if (n <= 0 || !items.length) return [];
  if (items.length <= n) return items;
  return Array.from({ length: n }, (_, i) => items[Math.floor((i + 0.5) * items.length / n)]);
}
function stride(items, max) {
  if (items.length <= max) return { picked: items, every: 1 };
  const every = Math.ceil(items.length / max);
  return { picked: items.filter((_, i) => i % every === 0), every };
}

async function listAll(bucket, opts, reads, stopAfter) {
  const objects = [];
  let cursor;
  do {
    if (reads.lists >= MAX_LISTS) return { objects, complete: false };
    reads.lists += 1;
    const r = await bucket.list({ ...opts, ...(cursor ? { cursor } : {}), limit: 1000 });
    objects.push(...r.objects);
    if (stopAfter && r.objects.length && stopAfter(r.objects.at(-1))) return { objects, complete: true };
    cursor = r.truncated ? r.cursor : null;
  } while (cursor);
  return { objects, complete: true };
}

// ------------------------------------------------------------- continuity
const hms = ms => { const p = nyParts(ms); return [p.h, p.mi, p.s].map(v => String(v).padStart(2, '0')).join(':'); };
const mins = ms => `${Math.round(ms / 60_000)} min`;

/**
 * Split time-ordered points into runs wherever consecutive observations are
 * more than `maxGapMs` apart. Continuity comes from the observations' own
 * timestamps, never from list adjacency. Returns runs plus the holes found.
 */
export function splitRuns(points, maxGapMs) {
  const runs = [], holes = [];
  for (const p of points) {
    const t = Date.parse(p[0]);
    const run = runs.at(-1);
    if (run && t - Date.parse(run.at(-1)[0]) <= maxGapMs) run.push(p);
    else {
      if (run) holes.push([Date.parse(run.at(-1)[0]), t]);
      runs.push([p]);
    }
  }
  return { runs, holes };
}

/** Runs of 2+ observations become line/band segments; a lone observation is a point, never stretched. */
function runsToSegments(points, maxGapMs, kind) {
  const { runs, holes } = splitRuns(points, maxGapMs);
  const segments = [], lone = [];
  for (const r of runs) {
    if (r.length >= 2) segments.push({ kind, start: r[0][0], end: r.at(-1)[0], points: r });
    else lone.push(r[0]);
  }
  if (lone.length) segments.push({ kind: 'points', points: lone });
  return { segments, holes };
}

// ------------------------------------------------------------- snapshots
export const SNAPSHOT_CADENCE_MS = 68_000;
export const MAX_SNAPSHOT_GAP_MS = 150_000;   // ~2x the cadence; a larger gap is a hole, not a line

async function snapshotContext(bucket, src, position, plan, reads) {
  const occ = position.contract.occ_symbol, und = position.contract.underlying;
  const prefix = src.prefix(plan.d);
  const [start, end] = plan.interval;
  const { objects, complete } = await listAll(bucket, { prefix, startAfter: prefix + hhmmss(start - 1000) }, reads,
    last => (snapshotKeyTime(last.key, plan.d) ?? 0) > end);
  const keys = objects.filter(o => o.key.endsWith('.csv')).map(o => ({ key: o.key, t: snapshotKeyTime(o.key, plan.d) }))
    .filter(o => o.t != null && o.t >= start && o.t <= end);
  const denseAll = keys.filter(k => inAny(k.t, plan.dense));
  const { picked: dense, every } = stride(denseAll, MAX_SNAPSHOTS);
  const sparseAll = keys.filter(k => !inAny(k.t, plan.dense) && inAny(k.t, plan.sparse));
  const sparse = evenPick(sparseAll, MAX_SNAPSHOTS - dense.length);
  const read = new Map();
  for (const k of [...dense, ...sparse]) {
    reads.gets += 1;
    const obj = await bucket.get(k.key);
    if (obj) read.set(k.key, snapshotRow(await obj.text(), occ));
  }
  // Thinned reads widen the normal spacing; anything wider than that is a hole.
  const maxGap = Math.max(MAX_SNAPSHOT_GAP_MS, Math.ceil(every * SNAPSHOT_CADENCE_MS * 1.6));
  const und_segments = [], opt_segments = [], coverage = [];
  const holeNotes = new Set();
  for (const [a, b] of plan.dense) {
    const ks = dense.filter(k => k.t >= a && k.t <= b && read.has(k.key));
    const u = ks.filter(k => read.get(k.key).underlying != null).map(k => [iso(k.t), read.get(k.key).underlying]);
    const o = ks.filter(k => read.get(k.key).quote).map(k => [iso(k.t), ...read.get(k.key).quote]);
    const U = runsToSegments(u, maxGap, 'line'), O = runsToSegments(o, maxGap, 'band');
    und_segments.push(...U.segments);
    opt_segments.push(...O.segments);
    // Holes inside the window, and uncovered edges, are named rather than drawn over.
    const edges = [];
    const times = ks.map(k => k.t);
    if (!times.length) edges.push([a, b]);
    else {
      if (times[0] - a > maxGap) edges.push([a, times[0]]);
      if (b - times.at(-1) > maxGap) edges.push([times.at(-1), b]);
    }
    for (const [x, y] of [...U.holes, ...edges]) holeNotes.add(`${hms(x)}–${hms(y)} ET: no snapshot archived (${mins(y - x)}); not drawn`);
    for (const [x, y] of O.holes) holeNotes.add(`${hms(x)}–${hms(y)} ET: no ${occ} quote in the archived snapshots (${mins(y - x)}); not drawn`);
  }
  const su = sparse.filter(k => read.get(k.key)?.underlying != null).map(k => [iso(k.t), read.get(k.key).underlying]);
  const so = sparse.filter(k => read.get(k.key)?.quote).map(k => [iso(k.t), ...read.get(k.key).quote]);
  if (su.length) und_segments.push({ kind: 'points', points: su });
  if (so.length) opt_segments.push({ kind: 'points', points: so });

  if (every > 1) coverage.push(`around fills: 1 in ${every} snapshots read (${dense.length} of ${denseAll.length})`);
  coverage.push(...holeNotes);
  for (const [a, b] of plan.sparse) {
    const n = sparseAll.filter(k => k.t >= a && k.t <= b).length, m = sparse.filter(k => k.t >= a && k.t <= b).length;
    if (n) coverage.push(`${hm(a)}–${hm(b)} ET: ${m} of ${n} snapshots read, shown as separate points`);
    else if (b - a > 5 * 60_000) coverage.push(`${hm(a)}–${hm(b)} ET: no snapshots archived`);
  }
  if (!complete) coverage.push('snapshot listing stopped at its read limit; the end of the interval is not shown');
  const gaps = [];
  if (!keys.length) gaps.push(`no ${und} chain snapshots archived for this interval`);
  if (read.size && !opt_segments.length) gaps.push(`${occ} not present in the archived chain (outside its strike range)`);
  return {
    underlying: und_segments.length ? { source: `${und} chain snapshot UnderlyingPrice (~68 s)`, segments: und_segments } : null,
    option: opt_segments.length ? { source: 'chain snapshot bid/ask (~68 s). A separate observation from the fill.', segments: opt_segments } : null,
    coverage, gaps,
  };
}

// ---------------------------------------------------------------- Tradier
export const MAX_PART_GAP_MS = 30_000;   // consecutive parts join only if their recorded events meet within this

async function tradierContext(bucket, src, position, plan, reads) {
  const occ = position.contract.occ_symbol, und = position.contract.underlying;
  const [start, end] = plan.interval;
  const { objects, complete } = await listAll(bucket, { prefix: src.prefix(plan.d) }, reads);
  const parts = objects.filter(o => o.key.endsWith('.ndjson.gz'))
    .map(o => { const up = new Date(o.uploaded).getTime(); return { key: o.key, a: up - TRADIER_PART_MS - TRADIER_UPLOAD_SLACK_MS, b: up }; })
    .filter(p => p.b >= start && p.a <= end).sort((x, y) => x.a - y.a);
  const overlaps = (p, ranges) => ranges.some(([a, b]) => p.b >= a && p.a <= b);
  const denseAll = parts.filter(p => overlaps(p, plan.dense));
  const { picked: dense, every } = stride(denseAll, MAX_TRADIER_PARTS);
  const sparseAll = parts.filter(p => !overlaps(p, plan.dense));
  const sparse = evenPick(sparseAll, MAX_TRADIER_PARTS - dense.length);
  const chosen = [...dense, ...sparse];

  // Read each chosen part; its real span is the first..last event it recorded (any symbol).
  const loaded = [];
  for (const p of chosen) {
    reads.gets += 1;
    const obj = await bucket.get(p.key);
    if (!obj) continue;
    let first = Infinity, last = -Infinity;
    const u = [], q = [];
    for (const line of (await gunzipText(obj)).split('\n')) {
      if (!line) continue;
      let x;
      try { x = JSON.parse(line); } catch { continue; }
      const t = x.type === 'timesale' ? +x.date : x.type === 'quote' ? Math.max(+x.biddate || 0, +x.askdate || 0) : NaN;
      if (!Number.isFinite(t) || t <= 0) continue;
      first = Math.min(first, t); last = Math.max(last, t);
      if (x.symbol === und && x.type === 'timesale' && x.last != null) u.push([t, +x.last]);
      else if (x.symbol === occ && x.type === 'quote' && x.bid != null && x.ask != null) q.push([t, +x.bid, +x.ask]);
    }
    if (Number.isFinite(first)) loaded.push({ ...p, first, last, u, q, isDense: dense.includes(p) });
  }
  loaded.sort((x, y) => x.first - y.first);

  // Parts join only when their recorded events actually meet; a missing part leaves a hole.
  const groups = [], holes = [];
  for (const p of loaded) {
    const g = groups.at(-1);
    if (g && p.first - g.at(-1).last <= MAX_PART_GAP_MS) g.push(p);
    else {
      if (g && (g.at(-1).isDense || p.isDense)) holes.push([g.at(-1).last, p.first]);
      groups.push([p]);
    }
  }
  const und_segments = [], opt_segments = [];
  for (const g of groups) {
    const a = Math.max(start, g[0].first), b = Math.min(end, g.at(-1).last);
    const bucket15 = rows => {
      const m = new Map();
      for (const r of rows) { if (r[0] < a || r[0] > b) continue; const k = Math.floor(r[0] / BUCKET_MS); if (!m.has(k) || m.get(k)[0] <= r[0]) m.set(k, r); }
      return [...m.values()].sort((x, y) => x[0] - y[0]);
    };
    const u = bucket15(g.flatMap(p => p.u)), q = bucket15(g.flatMap(p => p.q));
    if (u.length) und_segments.push({ kind: 'line', start: iso(a), end: iso(b), points: u.map(([t, px]) => [iso(t), px]) });
    if (q.length) opt_segments.push({ kind: 'band', start: iso(a), end: iso(b), points: q.map(([t, bi, as]) => [iso(t), bi, as]) });
  }
  const coverage = [];
  if (every > 1) coverage.push(`around fills: 1 in ${every} archive parts read (${dense.length} of ${denseAll.length})`);
  for (const [x, y] of holes) coverage.push(`${hms(x)}–${hms(y)} ET: no Tradier archive recorded (${mins(y - x)}); not drawn`);
  for (const [a, b] of plan.dense) {
    const inside = loaded.filter(p => p.isDense && p.last >= a && p.first <= b);
    if (!inside.length) coverage.push(`${hms(a)}–${hms(b)} ET: no Tradier archive around this fill`);
  }
  if (sparseAll.length) coverage.push(`holding period: ${sparse.length} of ${sparseAll.length} ~3-minute archive parts read; unread stretches are left blank`);
  if (!complete) coverage.push('Tradier listing stopped at its read limit');
  const gaps = parts.length ? [] : [`no ${und} Tradier archive parts for this interval`];
  return {
    underlying: und_segments.length ? { source: `Tradier ${und} time-and-sales, last trade per 15 s`, segments: und_segments } : null,
    option: opt_segments.length ? { source: 'Tradier quotes bid/ask, last per 15 s. A separate observation from the fill.', segments: opt_segments } : null,
    coverage, gaps,
  };
}

async function sha(text) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}

/** Market context for one supported journal position on New York day `day` (defaults to the entry day). */
export async function positionContext(env, position, { day = etDate(position.opened_ts), now = () => new Date(), writeCache = true } = {}) {
  if (position.legs) {
    const legs = [];
    for (const leg of position.legs) legs.push(await positionContext(env, leg, { day, now, writeCache }));
    return { position_id: position.position_id, day, legs };
  }
  if (position.contract.instrument_type === 'shares') return { position_id: position.position_id, day,
    underlying: null, option: null, gaps: [position.contract.underlying + ' underlying: no validated share-price archive for this view'] };
  const plan = coveragePlan(position, day);
  const { underlying: und, occ_symbol: occ } = position.contract;
  const base = { position_id: position.position_id, occ_symbol: occ, underlying_symbol: und, day, version: CONTEXT_VERSION };
  if (!plan.interval) return { ...base, underlying: null, option: null, coverage: [], gaps: [], notes: plan.notes, reads: { lists: 0, gets: 0 } };
  const cacheKey = await sha(`${CONTEXT_VERSION}|${occ}|${day}|${plan.interval.join('-')}|${JSON.stringify(plan.dense)}`);
  const sessionOver = day < etDate(now().toISOString());
  if (sessionOver) {
    const hit = await env.DB.prepare('SELECT context_json FROM trade_journal_context WHERE cache_key = ?').bind(cacheKey).first();
    if (hit) return { ...JSON.parse(hit.context_json), cached: true };
  }
  const src = SOURCES[und];
  const reads = { lists: 0, gets: 0 };
  let ctx;
  if (!src) {
    // Some archives carry a value for these underlyings (e.g. the chain snapshots' TLT column), but it does not
    // vary through the session, so it is not a validated price series and is not drawn.
    ctx = { underlying: null, option: null, coverage: [],
      gaps: [`${und} underlying: no validated time-varying price series (archived values, if any, are not a supported intraday series; not drawn)`,
        `${occ}: no option history in any archive (unsupported premium path)`] };
  } else if (!env.PAPER_TRADES) {
    return { ...base, error: 'R2 binding PAPER_TRADES is not configured' };
  } else {
    ctx = src.kind === 'snapshot'
      ? await snapshotContext(env.PAPER_TRADES, src, position, plan, reads)
      : await tradierContext(env.PAPER_TRADES, src, position, plan, reads);
  }
  const out = { ...base, interval: { start: iso(plan.interval[0]), end: iso(plan.interval[1]) },
    dense_ranges: plan.dense.map(([a, b]) => [iso(a), iso(b)]), notes: plan.notes, ...ctx, reads, built_at: now().toISOString() };
  let cache = 'not_cached_session_open';
  if (sessionOver) {
    if (writeCache) {
      await env.DB.prepare('INSERT OR REPLACE INTO trade_journal_context (cache_key, position_id, built_at, context_json) VALUES (?, ?, ?, ?)')
        .bind(cacheKey, position.position_id, out.built_at, JSON.stringify(out)).run();
      cache = 'stored';
    } else cache = 'skipped_cutover_fence';
  }
  return { ...out, cached: false, cache };
}

const POSITION_ID = /^pos:[0-9a-f]{16}$/;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

// A qualified snapshot at the selected cutoff, never the retrospective cache
// or sampling plan. Missing contracts remain gaps, even if a later cache has them.
export async function journalContextAsOf(env,position,day,cutoff) {
  if(position.legs)return {position_id:position.position_id,day,legs:await Promise.all(
    position.legs.map(leg=>journalContextAsOf(env,leg,day,cutoff)))};
  const instrument=position.contract.underlying;
  const base={position_id:position.position_id,day,cutoff,view:'recorded-as-of',cached:false,
    underlying:null,option:null,gaps:[],notes:['Only a publication-qualified snapshot at the cutoff is shown.']};
  if(!['QQQ','SPY','IBIT'].includes(instrument))return {...base,gaps:[`${instrument}: no qualified archive adapter`]};
  const result=await publicChainAt(env,{session:day,instrument,cutoff});
  const row=result.rows[0];if(!row)return {...base,gaps:[`${instrument}: ${result.coverage.reason}`],coverage:[],sourceCoverage:result.coverage};
  const at=new Date(row.eventAt).toISOString(),price=row.value.underlying_price;
  const occ=position.contract.occ_symbol?.replaceAll(' ','');
  const quote=occ?row.value.rows.find(r=>r.OptionSymbol?.replaceAll(' ','')===occ):null;
  return {...base,available_at:row.availableAt,coverage:['One qualified snapshot; not a complete premium path.'],sourceCoverage:result.coverage,
    underlying:price>0?{source:row.evidence,segments:[{kind:'points',points:[[at,price]]}]}:null,
    option:quote && quote.Bid!==null && quote.Ask!==null && quote.Bid>=0 && quote.Ask>=quote.Bid?{source:row.evidence,segments:[{kind:'points',points:[[at,quote.Bid,quote.Ask]]}]}:null,
    gaps:quote?[]:[`${occ??instrument}: no qualified exact-contract quote`]};
}

export async function handleTradeJournalContext(request, env, url) {
  const auth = await authorizeJournalViewer(request, env);
  if (auth.response) return auth.response;
  const id = url.searchParams.get('position_id') ?? '';
  if (!POSITION_ID.test(id)) return jsonResponse({ error: 'position_id must look like pos:<16 hex>' }, 400);
  const day = url.searchParams.get('date');
  if (day != null && !ISO_DAY.test(day)) return jsonResponse({ error: 'date must be YYYY-MM-DD' }, 400);
  let cutoff;try{cutoff=journalCutoff(url);if(cutoff!==null&&!day)throw new Error('date required with cutoff');if(day)journalScope(day,cutoff);}catch(error){return jsonResponse({error:error.message},400);}
  const position = await loadPosition(env.DB, id,cutoff);
  if (!position) return jsonResponse({ error: 'unknown position' }, 404);
  if (!position.supported) return jsonResponse({ error: 'position has no readable fills', reason: position.unsupported_reason }, 409);
  if(cutoff!==null)return jsonResponse(await journalContextAsOf(env,position,day,cutoff),200);
  // Reads keep working while the cutover fence is armed; only the cache write is withheld.
  const fenced = await cutoverFenceActive(env);
  const ctx = await positionContext(env, position, { day: day ?? undefined, writeCache: !fenced });
  return jsonResponse(ctx, ctx.error ? 503 : 200);
}
