// backend/trade-journal.js (OA-238) -- read-only reader for the Trade
// Retrospectives journal in Notion, normalised into the shared execution model
// and kept in D1 so the private trade display can serve it without a Notion
// round trip per view.
//
// Boundaries (OA-238 dispatch, 2026-11-16):
// * Read-only. The only Notion calls are data-source query, block-children
//   list and page retrieve. Nothing is ever written to Notion.
// * One data source: Trade Retrospectives (TRADE_JOURNAL_DATA_SOURCE_ID).
// * The integration token is a Worker secret, NOTION_TRADE_JOURNAL_TOKEN. It is
//   sent only in the Authorization header to api.notion.com and never logged,
//   stored or returned.
// * Every route requires a signed-in session whose username is listed in the
//   TRADE_JOURNAL_VIEWERS var. Unset means nobody (fail closed).
// * Zero KV operations. State lives in two D1 tables (migration 0003).
//
// Identity (fixes the R1 weakness a journal correction exposes):
//   execution_id = notion:<page id without dashes>:order:<n>[:part:<i>of<k>]
//   position_id  = pos:<sha256("notion:" + page id without dashes)[0:16]>
// A broker transaction ID documented later is evidence on the execution, not
// its identity, so a fee reconciliation refreshes the same position in place.
//
// Rows whose body matches none of the known fill-line formats are stored and
// shown as unsupported -- never guessed and never silently dropped.

import { checkOrigin, jsonResponse, requireSession } from './common.js';

export const TRADE_JOURNAL_DATA_SOURCE_ID = '2a32c187-f187-4570-9d53-05e611943df0';
export const NOTION_API = 'https://api.notion.com/v1';
export const NOTION_VERSION = '2025-09-03';
export const TOKEN_SECRET_NAME = 'NOTION_TRADE_JOURNAL_TOKEN';
export const VIEWERS_VAR_NAME = 'TRADE_JOURNAL_VIEWERS';
export const JOURNAL_TZ = 'America/New_York';
// Bounded per sync so one invocation stays well inside the Worker subrequest
// limit; anything left over is reported as deferred and picked up next sync.
export const MAX_BODY_REQUESTS = 35;
export const MAX_MISSING_CHECKS = 5;
const MAX_QUERY_PAGES = 20;
// Headroom for the last body started under the cap to finish its own pages.
const BODY_OVERSHOOT = 10;
const SYNC_LEASE_MS = 5 * 60_000;
const FEE_CONVENTION = 0.5; // USD per executed contract until actual charges are documented

// ------------------------------------------------------------------ parsing
const NUM = String.raw`\d+(?:\.\d+)?`;
const TS = String.raw`[0-9T:.\\+\-]+`;
const F1 = new RegExp(String.raw`(Entry|Exit) order (\d+): (${TS}), (\d+) contracts at USD (${NUM})`, 'g');
const F2 = new RegExp(String.raw`\[(Entry|Exit) receipt (\d+)\]\([^)]*\) at (${TS}): USD (${NUM})`, 'g');
const F3 = new RegExp(String.raw`- (Entry|Exit): \[order (\d+) receipt\]\([^)]*\), (?:buy|sell) (\d+) .*? at USD (${NUM})\. Alert timestamp (${TS})\.`, 'g');
const F4 = new RegExp(String.raw`\[(Opening|Closing) order (\d+)\]\([^)]*\); execution-alert timestamp (${TS})\.`, 'g');
const F4_PX = new RegExp(String.raw`bought for USD (${NUM}) per premium unit and sold for USD (${NUM})`);
const BROKER = new RegExp(String.raw`^- ([A-Za-z0-9]+): (\S+Z); (BOT|SOLD) (\d+) (.+?) @ (${NUM});.*?fee USD (${NUM})\.$`, 'gm');
const PARTIALS = new RegExp(String.raw`order (\d+) had (\w+) one-contract partial executions at (${NUM})`, 'g');
const WORDS = { two: 2, three: 3, four: 4 };
const MONTHS = { Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12 };
const pad = (n, w) => String(n).padStart(w, '0');

export function instrumentFromContract(text, multiplier = 100) {
  // "QQQ Nov 13 '26 $673 Put"
  const m = /^([A-Z]+) (\w{3}) (\d{1,2}) '(\d{2}) \\?\$([\d.]+) (Put|Call)$/.exec(String(text).trim());
  if (!m || !MONTHS[m[2]]) return null;
  const [, und, mon, day, yy, strike, pc] = m;
  const mm = pad(MONTHS[mon], 2), dd = pad(Number(day), 2);
  return {
    underlying: und,
    occ_symbol: `${und}${yy}${mm}${dd}${pc[0]}${pad(Math.round(Number(strike) * 1000), 8)}`,
    option_type: pc.toLowerCase(), strike: Number(strike), expiration: `20${yy}-${mm}-${dd}`, multiplier,
  };
}

export function toUtcIso(ts) {
  const ms = Date.parse(String(ts).replaceAll('\\', ''));
  if (!Number.isFinite(ms)) throw new Error(`unparseable timestamp ${ts}`);
  return new Date(ms).toISOString();
}

const etDateFmt = new Intl.DateTimeFormat('en-CA', { timeZone: JOURNAL_TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
export function etDate(isoUtc) {
  return etDateFmt.format(new Date(isoUtc));
}

function legsOf(body, props) {
  let legs = [...body.matchAll(F1)].map(m => ({ leg: m[1], order: m[2], ts: m[3], qty: Number(m[4]), px: m[5], fmt: 'F1' }));
  if (!legs.length) legs = [...body.matchAll(F2)].map(m => ({ leg: m[1], order: m[2], ts: m[3], qty: Number(props.Quantity), px: m[4], fmt: 'F2' }));
  if (!legs.length) legs = [...body.matchAll(F3)].map(m => ({ leg: m[1], order: m[2], qty: Number(m[3]), px: m[4], ts: m[5], fmt: 'F3' }));
  if (!legs.length) {
    const px = F4_PX.exec(body);
    legs = [...body.matchAll(F4)].map(m => {
      const leg = m[1] === 'Opening' ? 'Entry' : 'Exit';
      return { leg, order: m[2], ts: m[3], qty: Number(props.Quantity), px: px ? (leg === 'Entry' ? px[1] : px[2]) : null, fmt: 'F4' };
    });
  }
  if (!legs.length || legs.some(l => l.px == null || !Number.isFinite(l.qty) || l.qty <= 0)) return null;
  return legs;
}

function firstNumber(v) {
  if (typeof v === 'number') return v;
  const m = /-?\d+(?:\.\d+)?/.exec(String(v ?? '').replaceAll(',', ''));
  return m ? Number(m[0]) : null;
}
const round = (x, d) => Math.round(x * 10 ** d) / 10 ** d;

export function pageExecutions(page) {
  const { properties: props, body } = page;
  const pid = page.page_id.replaceAll('-', '');
  const instrument = instrumentFromContract(props.Contract);
  if (!instrument) return { executions: [], gaps: [`contract text not recognised: ${props.Contract ?? '(empty)'}`] };
  const legs = legsOf(body, props);
  if (!legs) return { executions: [], gaps: ['no supported fill-line format in page body'] };
  const acct = /E\\?\*TRADE(?: Individual Brokerage)? ending (\d+)/.exec(body);
  const account = acct ? `E*TRADE ending ${acct[1]}` : 'unknown account';
  const broker = [...body.matchAll(BROKER)].map(m => ({ txn: m[1], ts: m[2], act: m[3], qty: Number(m[4]), px: Number(m[6]), fee: Number(m[7]) }));
  const partial = Object.fromEntries([...body.matchAll(PARTIALS)].map(m => [m[1], WORDS[m[2]] ?? 0]));
  const used = new Set();
  const executions = [], gaps = [];
  for (const leg of legs) {
    const side = leg.leg === 'Entry' ? 'buy' : 'sell';
    const k = partial[leg.order] || 1;
    for (let i = 0; i < k; i++) {
      const qty = Math.floor(leg.qty / k);
      const execution_id = `notion:${pid}:order:${leg.order}` + (k > 1 ? `:part:${i + 1}of${k}` : '');
      const b = broker.find(x => !used.has(x.txn) && (x.act === 'BOT') === (side === 'buy') && x.px === Number(leg.px) && x.qty === qty);
      const common = {
        execution_id, actor_kind: 'human', owner: 'Owen', account, mode: 'live', strategy_id: null,
        strategy_basis: 'discretionary; setup/thesis as documented on the journal row', instrument,
        side, qty, price: Number(leg.px), source: 'notion:Trade Retrospectives', source_ref: page.page_url,
        order_ref: leg.order, quote: null,
      };
      if (b) {
        used.add(b.txn);
        executions.push({ ...common, ts: toUtcIso(b.ts), ts_kind: 'broker_order_initiation',
          ts_note: `broker order-initiation time; execution alert at ${toUtcIso(leg.ts)}`,
          fee: b.fee, fee_basis: 'actual', evidence: [`broker txn ${b.txn}`, `execution alert order ${leg.order} (${leg.fmt})`] });
      } else {
        executions.push({ ...common, ts: toUtcIso(leg.ts), ts_kind: 'execution_alert',
          ts_note: 'execution-alert time (proxy, not an exchange fill time)' + (k > 1 ? "; this partial executed at or before the order's final alert" : ''),
          fee: FEE_CONVENTION * qty, fee_basis: 'estimated', evidence: [`execution alert order ${leg.order} (${leg.fmt})`] });
        gaps.push(`order ${leg.order}${k > 1 ? ` part ${i + 1}/${k}` : ''}: fee estimated (USD 0.50/contract)`);
      }
    }
  }
  for (const f of ['Setup', 'Thesis', 'Invalidation']) {
    if (/undocumented/i.test(String(props[f] ?? ''))) gaps.push(`${f}: undocumented on the journal row`);
  }
  return { executions, gaps };
}

async function sha256Hex(text) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}

export async function positionIdForPage(pageId) {
  return 'pos:' + (await sha256Hex(`notion:${String(pageId).replaceAll('-', '')}`)).slice(0, 16);
}

/** One journal row -> one position (or an unsupported placeholder that stays visible). */
export async function pagePosition(page) {
  const { executions, gaps } = pageExecutions(page);
  const base = {
    position_id: await positionIdForPage(page.page_id), journal_page_id: page.page_id, journal_url: page.page_url,
    journal_last_edited: page.last_edited_time, report: page.properties.Report ?? null,
    trade_date: page.properties['Trade Date'] ?? null, actor_kind: 'human', owner: 'Owen',
  };
  if (!executions.length) {
    return { ...base, supported: false, unsupported_reason: gaps[0], contract_text: page.properties.Contract ?? null, executions: [], gaps };
  }
  executions.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : a.execution_id < b.execution_id ? -1 : 1));
  const m = executions[0].instrument.multiplier;
  let q = 0, maxQty = 0;
  for (const e of executions) { q += e.side === 'buy' ? e.qty : -e.qty; maxQty = Math.max(maxQty, Math.abs(q)); }
  const gross = executions.reduce((s, e) => s + (e.side === 'buy' ? -1 : 1) * e.price * e.qty * m, 0);
  const fees = round(executions.reduce((s, e) => s + e.fee, 0), 6);
  return {
    ...base, supported: true, account: executions[0].account, mode: 'live', strategy_id: null,
    contract: executions[0].instrument, opened_ts: executions[0].ts, closed_ts: q === 0 ? executions.at(-1).ts : null,
    closed: q === 0, max_qty: maxQty, n_fills: executions.length, gross_pnl: round(gross, 2), fees,
    fee_basis: [...new Set(executions.map(e => e.fee_basis))].sort(), net_pnl: round(gross - fees, 6),
    row_checks: {
      gross_matches_row: Math.abs(gross - (firstNumber(page.properties['Gross P&L']) ?? NaN)) < 0.005,
      fees_match_row: Math.abs(fees - (firstNumber(page.properties.Fees) ?? NaN)) < 0.005,
      flat: q === 0,
    },
    gaps, executions,
  };
}

// --------------------------------------------------------------- Notion I/O
export function richText(rt) {
  // Links keep their markdown shape (the fill-line formats depend on it); the
  // destination is dropped because receipt links point into a private mailbox.
  return (rt ?? []).map(t => (t.href ? `[${t.plain_text}](link)` : t.plain_text)).join('');
}

const BLOCK_PREFIX = { heading_1: '# ', heading_2: '## ', heading_3: '### ', bulleted_list_item: '- ', numbered_list_item: '1. ', to_do: '- ' };
export function blockLine(block) {
  const payload = block[block.type];
  if (!payload || !Array.isArray(payload.rich_text)) return null;
  return (BLOCK_PREFIX[block.type] ?? '') + richText(payload.rich_text);
}

export function propertyValues(properties) {
  const out = {};
  for (const [name, p] of Object.entries(properties ?? {})) {
    switch (p.type) {
      case 'title': case 'rich_text': out[name] = richText(p[p.type]).replace(/\]\(link\)/g, ']'); break;
      case 'number': out[name] = p.number; break;
      case 'select': case 'status': out[name] = p[p.type]?.name ?? null; break;
      case 'date': out[name] = p.date?.start ?? null; break;
      case 'created_time': out[name] = p.created_time; break;
      case 'last_edited_time': out[name] = p.last_edited_time; break;
      default: out[name] = null;
    }
  }
  return out;
}

export class JournalReadError extends Error {
  constructor(message, { status = null, code = null, step = null } = {}) {
    super(message);
    this.status = status; this.code = code; this.step = step;
  }
}

function notionClient(env, fetchImpl, budget) {
  const token = env[TOKEN_SECRET_NAME];
  async function call(step, method, path, body) {
    if (budget.requests <= 0) throw new JournalReadError('subrequest budget exhausted', { step, code: 'budget' });
    budget.requests -= 1;
    if (step === 'blocks') budget.bodyRequests += 1;
    let res;
    try {
      res = await fetchImpl(`${NOTION_API}${path}`, {
        method,
        headers: { Authorization: `Bearer ${token}`, 'Notion-Version': NOTION_VERSION, 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new JournalReadError(`network error calling Notion (${step})`, { step, code: 'network' });
    }
    if (!res.ok) {
      let code = null;
      try { code = (await res.json())?.code ?? null; } catch { /* non-JSON error body */ }
      throw new JournalReadError(`Notion ${step} failed: HTTP ${res.status}${code ? ` ${code}` : ''}`, { status: res.status, code, step });
    }
    return res.json();
  }
  return {
    async queryAll() {
      const rows = [];
      let cursor;
      for (let i = 0; i < MAX_QUERY_PAGES; i++) {
        const r = await call('query', 'POST', `/data_sources/${TRADE_JOURNAL_DATA_SOURCE_ID}/query`,
          { page_size: 100, ...(cursor ? { start_cursor: cursor } : {}) });
        rows.push(...(r.results ?? []).filter(x => x.object === 'page'));
        if (!r.has_more) return { rows, complete: true };
        cursor = r.next_cursor;
      }
      return { rows, complete: false };
    },
    async bodyText(blockId, depth = 0) {
      const lines = [];
      let cursor;
      do {
        const qs = new URLSearchParams({ page_size: '100', ...(cursor ? { start_cursor: cursor } : {}) });
        const r = await call('blocks', 'GET', `/blocks/${blockId}/children?${qs}`);
        for (const b of r.results ?? []) {
          const line = blockLine(b);
          if (line != null) lines.push(line);
          if (b.has_children && depth < 2) lines.push(...(await this.bodyText(b.id, depth + 1)).split('\n').filter(Boolean));
        }
        cursor = r.has_more ? r.next_cursor : null;
      } while (cursor);
      return lines.join('\n');
    },
    async pageStatus(pageId) {
      try {
        const p = await call('page', 'GET', `/pages/${pageId}`);
        if (p.in_trash || p.archived) return 'archived';
        const parent = p.parent?.data_source_id ?? p.parent?.database_id ?? null;
        return parent && parent.replaceAll('-', '') === TRADE_JOURNAL_DATA_SOURCE_ID.replaceAll('-', '') ? 'active' : 'removed_from_source';
      } catch (e) {
        if (e instanceof JournalReadError && e.status === 404) return 'removed';
        throw e;
      }
    },
  };
}

// --------------------------------------------------------------- D1 storage
async function loadRows(db) {
  const { results } = await db.prepare('SELECT * FROM trade_journal_rows').all();
  return new Map(results.map(r => [r.page_id, r]));
}

async function loadSync(db) {
  return (await db.prepare('SELECT * FROM trade_journal_sync WHERE id = 1').first()) ?? null;
}

function contentKey(position) {
  return JSON.stringify(position);
}

function changedFields(a, b) {
  return [...new Set([...Object.keys(a), ...Object.keys(b)])]
    .filter(k => JSON.stringify(a[k]) !== JSON.stringify(b[k])).sort();
}

/**
 * Pull the whole data source, refresh changed rows in place, flag rows that
 * left the source, and record the outcome. Never throws for a Notion error:
 * the error is stored as last_error and the previous rows keep serving.
 */
export async function syncTradeJournal(env, { fetchImpl = fetch, now = () => new Date() } = {}) {
  const db = env.DB;
  if (!db) throw new Error('D1 binding DB is not configured');
  if (!env[TOKEN_SECRET_NAME]) {
    return { ok: false, error: { code: 'not_configured', message: `Worker secret ${TOKEN_SECRET_NAME} is not set on live-options-api` } };
  }
  const startedAt = now().toISOString();
  // A single-row lease: an overlapping sync returns instead of racing.
  const lease = await db.prepare(
    `INSERT INTO trade_journal_sync (id, running_since, last_attempt_at) VALUES (1, ?, ?)
     ON CONFLICT(id) DO UPDATE SET running_since = excluded.running_since, last_attempt_at = excluded.last_attempt_at
     WHERE trade_journal_sync.running_since IS NULL OR trade_journal_sync.running_since < ?`,
  ).bind(startedAt, startedAt, new Date(Date.parse(startedAt) - SYNC_LEASE_MS).toISOString()).run();
  if (!lease.meta?.changes) return { ok: false, error: { code: 'busy', message: 'another sync is running' } };

  const budget = { requests: MAX_QUERY_PAGES + MAX_BODY_REQUESTS + BODY_OVERSHOOT + MAX_MISSING_CHECKS, bodyRequests: 0 };
  const notion = notionClient(env, fetchImpl, budget);
  const summary = { started_at: startedAt, rows_in_source: 0, inserted: [], refreshed: [], unchanged: 0, deferred: [],
    status_changes: [], unsupported: 0, complete_scan: false };
  try {
    const { rows, complete } = await notion.queryAll();
    summary.rows_in_source = rows.length;
    summary.complete_scan = complete;
    const stored = await loadRows(db);
    rows.sort((a, b) => (a.last_edited_time < b.last_edited_time ? 1 : -1)); // newest edits first
    for (const page of rows) {
      const s = stored.get(page.id);
      // Notion reports last_edited_time to the minute, so an edit in the same
      // minute as our previous body read would keep the same value. Re-read
      // until our read is a full minute past the reported edit minute.
      const sameMinuteRisk = s && Date.parse(page.last_edited_time) + 60_000 > Date.parse(s.body_fetched_at);
      const need = !s || s.source_last_edited !== page.last_edited_time || sameMinuteRisk || s.source_status !== 'active';
      if (!need) { summary.unchanged += 1; continue; }
      if (budget.bodyRequests >= MAX_BODY_REQUESTS) { summary.deferred.push(page.id); continue; }
      const fetchedAt = now().toISOString();
      const body = await notion.bodyText(page.id);
      const position = await pagePosition({
        page_id: page.id, page_url: page.url, last_edited_time: page.last_edited_time,
        properties: propertyValues(page.properties), body,
      });
      if (!position.supported) summary.unsupported += 1;
      const key = contentKey(position);
      const hash = await sha256Hex(key);
      if (!s) {
        await db.prepare(
          `INSERT INTO trade_journal_rows (page_id, position_id, source_status, supported, source_last_edited, body_fetched_at,
             content_hash, revision, position_json, revisions_json, first_seen_at, status_changed_at)
           VALUES (?, ?, 'active', ?, ?, ?, ?, 1, ?, ?, ?, NULL)`,
        ).bind(page.id, position.position_id, position.supported ? 1 : 0, page.last_edited_time, fetchedAt, hash, key,
          JSON.stringify([{ rev: 1, hash, source_last_edited: page.last_edited_time, read_at: fetchedAt }]), fetchedAt).run();
        summary.inserted.push(position.position_id);
      } else if (s.content_hash === hash) {
        await db.prepare(
          `UPDATE trade_journal_rows SET source_last_edited = ?, body_fetched_at = ?, source_status = 'active',
             status_changed_at = CASE WHEN source_status = 'active' THEN status_changed_at ELSE ? END WHERE page_id = ?`,
        ).bind(page.last_edited_time, fetchedAt, fetchedAt, page.id).run();
        if (s.source_status !== 'active') summary.status_changes.push({ position_id: s.position_id, from: s.source_status, to: 'active' });
        summary.unchanged += 1;
      } else {
        const rev = s.revision + 1;
        const changed = changedFields(JSON.parse(s.position_json), position);
        const revisions = [...JSON.parse(s.revisions_json),
          { rev, hash, source_last_edited: page.last_edited_time, read_at: fetchedAt, changed_fields: changed }];
        await db.prepare(
          `UPDATE trade_journal_rows SET supported = ?, source_last_edited = ?, body_fetched_at = ?, content_hash = ?, revision = ?,
             position_json = ?, revisions_json = ?, source_status = 'active',
             status_changed_at = CASE WHEN source_status = 'active' THEN status_changed_at ELSE ? END
           WHERE page_id = ? AND revision = ?`,
        ).bind(position.supported ? 1 : 0, page.last_edited_time, fetchedAt, hash, rev, key, JSON.stringify(revisions),
          fetchedAt, page.id, s.revision).run();
        summary.refreshed.push({ position_id: position.position_id, revision: rev, changed_fields: changed });
      }
    }
    // Only a complete scan can say a row has left the source.
    if (complete) {
      const seen = new Set(rows.map(r => r.id));
      for (const s of stored.values()) {
        if (seen.has(s.page_id) || ['archived', 'removed', 'removed_from_source'].includes(s.source_status)) continue;
        let status = 'missing_unverified';
        if (budget.requests > 0) status = await notion.pageStatus(s.page_id);
        if (status === 'active') status = 'missing_unverified'; // retrievable yet not returned by the query
        if (status !== s.source_status) {
          await db.prepare('UPDATE trade_journal_rows SET source_status = ?, status_changed_at = ? WHERE page_id = ?')
            .bind(status, now().toISOString(), s.page_id).run();
          summary.status_changes.push({ position_id: s.position_id, from: s.source_status, to: status });
        }
      }
    }
    const finishedAt = now().toISOString();
    await db.prepare(
      `UPDATE trade_journal_sync SET running_since = NULL, last_success_at = ?, last_error_json = NULL,
         last_complete_scan_at = CASE WHEN ? THEN ? ELSE last_complete_scan_at END, last_summary_json = ? WHERE id = 1`,
    ).bind(finishedAt, complete ? 1 : 0, finishedAt, JSON.stringify(summary)).run();
    return { ok: true, summary: { ...summary, finished_at: finishedAt } };
  } catch (e) {
    const error = e instanceof JournalReadError
      ? { code: e.code ?? 'notion_error', status: e.status, step: e.step, message: e.message }
      : { code: 'internal', message: 'unexpected error while reading the journal' };
    error.at = now().toISOString();
    await db.prepare('UPDATE trade_journal_sync SET running_since = NULL, last_error_json = ?, last_summary_json = ? WHERE id = 1')
      .bind(JSON.stringify(error), JSON.stringify(summary)).run();
    if (!(e instanceof JournalReadError)) console.error(JSON.stringify({ event: 'trade_journal_sync_internal_error', name: e?.name }));
    return { ok: false, error, summary };
  }
}

// ------------------------------------------------------------------ reading
function rowView(r) {
  return {
    ...JSON.parse(r.position_json), source_status: r.source_status, revision: r.revision,
    revisions: JSON.parse(r.revisions_json), source_last_edited: r.source_last_edited, app_read_at: r.body_fetched_at,
    status_changed_at: r.status_changed_at,
  };
}

function syncView(s) {
  if (!s) return { last_success_at: null, last_attempt_at: null, last_complete_scan_at: null, last_error: null, running: false };
  return {
    last_success_at: s.last_success_at, last_attempt_at: s.last_attempt_at, last_complete_scan_at: s.last_complete_scan_at,
    last_error: s.last_error_json ? JSON.parse(s.last_error_json) : null, running: Boolean(s.running_since),
    last_summary: s.last_summary_json ? JSON.parse(s.last_summary_json) : null,
  };
}

/** Every stored position with an execution on `day` (America/New_York), plus unsupported rows dated that day. */
export async function journalDay(db, day) {
  const { results } = await db.prepare('SELECT * FROM trade_journal_rows').all();
  const positions = [], unsupported = [];
  for (const r of results) {
    const v = rowView(r);
    if (v.supported) {
      if (v.executions.some(e => etDate(e.ts) === day)) positions.push(v);
    } else if (v.trade_date && etDate(v.trade_date) === day) {
      unsupported.push(v);
    }
  }
  positions.sort((a, b) => (a.opened_ts < b.opened_ts ? -1 : 1));
  const counted = positions.filter(p => p.source_status === 'active');
  return {
    date: day, tz: JOURNAL_TZ, positions, unsupported,
    totals: {
      positions: counted.length, fills: counted.reduce((s, p) => s + p.n_fills, 0),
      gross: round(counted.reduce((s, p) => s + p.gross_pnl, 0), 2),
      fees: round(counted.reduce((s, p) => s + p.fees, 0), 6),
      net: round(counted.reduce((s, p) => s + p.net_pnl, 0), 6),
      excluded_not_active: positions.length - counted.length,
    },
    sync: syncView(await loadSync(db)),
  };
}

// ------------------------------------------------------------------- routes
async function authorize(request, env) {
  const session = await requireSession(request, env);
  if (!session) return { response: jsonResponse({ error: 'Not signed in' }, 401) };
  const allowed = String(env[VIEWERS_VAR_NAME] ?? '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  if (!allowed.length) return { response: jsonResponse({ error: 'Trade journal viewers are not configured', code: 'not_configured', setting: VIEWERS_VAR_NAME }, 403) };
  if (!allowed.includes(String(session.username).toLowerCase())) return { response: jsonResponse({ error: 'Not authorised for the trade journal' }, 403) };
  return { session };
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const PAGE_ID = /^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$/i;

export async function handleTradeJournalDay(request, env, url) {
  const auth = await authorize(request, env);
  if (auth.response) return auth.response;
  const day = url.searchParams.get('date') ?? '';
  if (!ISO_DAY.test(day)) return jsonResponse({ error: 'date must be YYYY-MM-DD' }, 400);
  return jsonResponse(await journalDay(env.DB, day), 200);
}

export async function handleTradeJournalRow(request, env, url) {
  const auth = await authorize(request, env);
  if (auth.response) return auth.response;
  const raw = url.searchParams.get('page_id') ?? '';
  if (!PAGE_ID.test(raw)) return jsonResponse({ error: 'page_id must be a Notion page id' }, 400);
  const hex = raw.replaceAll('-', '').toLowerCase();
  const pageId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  const r = await env.DB.prepare('SELECT * FROM trade_journal_rows WHERE page_id = ?').bind(pageId).first();
  if (!r) return jsonResponse({ error: 'not in the synced journal', page_id: pageId, sync: syncView(await loadSync(env.DB)) }, 404);
  return jsonResponse({ row: rowView(r), served_at: new Date().toISOString(), sync: syncView(await loadSync(env.DB)) }, 200);
}

export async function handleTradeJournalSync(request, env, { fetchImpl = fetch } = {}) {
  const forbidden = checkOrigin(request);
  if (forbidden) return forbidden;
  const auth = await authorize(request, env);
  if (auth.response) return auth.response;
  const result = await syncTradeJournal(env, { fetchImpl });
  const status = result.ok ? 200 : result.error?.code === 'busy' ? 409 : result.error?.code === 'not_configured' ? 503 : 502;
  return jsonResponse({ ...result, sync: syncView(await loadSync(env.DB)) }, status);
}
