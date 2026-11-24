'use strict';
const { randomUUID } = require('node:crypto');
const calendar = require('../site/squeeze-calendar.json');
const { formatSqueezeScheduleStatus } = require('../site/shared.js');
const { DataError, LIMITS, UUID, errorRecord } = require('./source');
const overview = require('./overview');

const DATASETS = ['qqq_snapshot', 'spy_snapshot', 'scheduled_squeeze', 'options_returns', 'cross_market_overview', 'options_active', 'options_windows', 'nasdaq_snapshot'];
// Chain underlyings (#121). QQQ keeps its original artifact and dataset id, so
// an omitted selector behaves exactly as before; SPY has its own tree.
const UNDERLYINGS = Object.freeze({
  QQQ: Object.freeze({ dataset: 'qqq_snapshot', key: 'intraday/latest.json', archive: /^intraday\/\d{8}\/snapshot_\d{6,12}\.csv$/ }),
  SPY: Object.freeze({ dataset: 'spy_snapshot', key: 'intraday/spy/latest.json', archive: /^intraday\/spy\/\d{8}\/snapshot_\d{6,12}\.csv$/ }),
});
const underlyingOf = a => (object(a) && Object.hasOwn(a, 'underlying') ? a.underlying : 'QQQ');
// Strict: Object.hasOwn would coerce ['SPY'] to 'SPY'.
const supportedUnderlying = symbol => typeof symbol === 'string' && Object.hasOwn(UNDERLYINGS, symbol);
const occRoot = symbol => /^[A-Z]+/.exec(symbol || '')?.[0] ?? '';
const SYMBOL = /^[A-Z][A-Z0-9.-]{0,9}$/;
const CONTRACT = /^[A-Z0-9.]{1,10}\d{6}[CP]\d{8}$/;
const QUALIFIED_RANK = 'qualified_first_ask_to_later_bid_rank';
// A real expanded sweep is about 20 MiB. Two leave room for the pinned
// summary/board/manifest inside the existing 64 MiB per-query ceiling.
const ACTIVE_PATH_SWEEPS = 2;
// #130 multi-session windows (F2). The consumer reads producer-written
// oa203/windows/ artifacts only; it never builds windows, never publishes
// them, and never invents an exit. Live R2 publication is Chej's release
// step (see docs/131-window-consumer.md): the first live window
// (overnight-2026-10-05_2026-10-06, rev_c1abd845...) was published to R2
// on 2026-11-10 by Chej's conditional trial. This consumer reads whatever
// the publisher writes; it does not publish, set any env flag, start a
// collector, or deploy.
const WINDOW_POLICY_VERSION = 'oa203-windows-v1';
const WINDOW_KINDS = ['weekly_cheapest', 'weekly_fixed', 'weekly_cheapest_best_exit', 'overnight', 'sessions_2', 'sessions_3'];
const WINDOW_BASES = ['mid', 'exec', 'qualified'];
const WINDOW_SHORTLIST = 200;
const WINDOW_STATES = ['final', 'partial', 'in_progress'];
const WINDOW_ID = Object.freeze({
  weekly_cheapest: /^weekly-\d{4}-W\d{2}-cheapest$/,
  weekly_fixed: /^weekly-\d{4}-W\d{2}-fixed$/,
  weekly_cheapest_best_exit: /^weekly-\d{4}-W\d{2}-cheapest_best_exit$/,
  overnight: /^overnight-\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}$/,
  sessions_2: /^sessions2-\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}$/,
  sessions_3: /^sessions3-\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}$/,
});
const WINDOW_RANKING_COLUMNS = ['basis', 'rank', 'symbol', 'underlying', 'option_type', 'strike', 'expiration', 'pct', 'entry', 'exit', 'abs_change_per_contract', 'entry_session', 'exit_session', 'flags'];
// Exact copy of market_data/options_scanner/windows_outputs.py PATH_COLUMNS
// plus per-basis BASIS_COLUMNS. A paths row whose header differs is rejected.
const WINDOW_PATH_COLUMNS = ['symbol', 'underlying', 'root', 'option_type', 'strike', 'expiration', 'contract_size', 'flags', 'contract_exclusions',
  ...['mid', 'exec', 'qualified'].flatMap(b => ['status', 'reasons', 'pct', 'entry', 'exit', 'abs_change_per_contract', 'entry_ms', 'exit_ms', 'entry_session', 'exit_session', 'entry_delay_from_boundary_s', 'exit_lead_before_close_s', 'entry_bid', 'entry_ask', 'entry_bid_ms', 'entry_ask_ms', 'entry_ask_size', 'entry_bid_quote_age_s', 'entry_ask_quote_age_s', 'entry_quote_age_s', 'entry_relative_spread', 'exit_bid', 'exit_ask', 'exit_bid_ms', 'exit_ask_ms', 'exit_bid_size', 'exit_bid_quote_age_s', 'exit_ask_quote_age_s', 'exit_quote_age_s', 'exit_relative_spread'].map(c => `${b}_${c}`))];

const OPTIONAL_STAMP_VALUE = Object.freeze({bid_ms:'bid',ask_ms:'ask',trade_ms:'last'});
const dates = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value && value >= '2020-01-01' && value <= '2100-12-31';
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function check(condition, message, code = 'incompatible_schema') { if (!condition) throw new DataError(code, message); }
function args(value, allowed, required = []) {
  check(object(value) && Object.keys(value).every(k => allowed.includes(k)) && required.every(k => value[k] !== undefined), 'Invalid or unknown filters.', 'invalid_filter');
}
function page(a) {
  const limit = a.limit ?? 10, offset = a.offset ?? 0;
  check(Number.isInteger(limit) && limit >= 1 && limit <= LIMITS.rows && Number.isInteger(offset) && offset >= 0 && offset <= 10000, 'limit must be 1..50 and offset 0..10000.', 'invalid_filter');
  return { limit, offset };
}
function slice(rows, limit, offset) {
  return { rows: rows.slice(offset, offset + limit), pagination: { offset, limit, total: rows.length,
    truncated: offset + limit < rows.length, next_offset: offset + limit < rows.length ? offset + limit : null } };
}
function age(timestamp, now, staleSeconds = 120) {
  const time = Date.parse(timestamp);
  if (!Number.isFinite(time) || time > now + 30000) return { status: 'unknown', observed_at: timestamp ?? null, age_seconds: null,stale_after_seconds:staleSeconds };
  return { status: now - time > staleSeconds * 1000 ? 'stale' : 'fresh', observed_at: timestamp, age_seconds: Math.max(0, (now - time) / 1000),stale_after_seconds:staleSeconds };
}
function csv(text) {
  // RFC 4180, including quoted commas/newlines. Never interpret cells as code.
  if(!text.trim())return Object.assign([],{columns:[]});
  const records = []; let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    check(cell.length <= LIMITS.csv_cell_chars && row.length < LIMITS.csv_columns && records.length <= LIMITS.csv_rows, 'CSV parsing limit exceeded.', 'excessive_response');
    const c = text[i];
    if (c === '"') { if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted; }
    else if (!quoted && (c === ',' || c === '\n')) {
      row.push(cell.replace(/\r$/, '')); cell = '';
      if (c === '\n') { records.push(row); row = []; }
    } else cell += c;
  }
  check(cell.length <= LIMITS.csv_cell_chars && row.length < LIMITS.csv_columns && records.length <= LIMITS.csv_rows, 'CSV parsing limit exceeded.', 'excessive_response');
  check(!quoted, 'Unclosed CSV quote.');
  if (cell || row.length) records.push([...row, cell.replace(/\r$/, '')]);
  const headers = records.shift() || [];
  check(headers.length > 0 && new Set(headers).size === headers.length && !headers.some(h => ['__proto__', 'constructor', 'prototype'].includes(h)), 'Invalid CSV header.');
  return Object.assign(records.filter(r => r.some(c => c !== '')).map(r => {
    check(r.length === headers.length, 'CSV column count mismatch.');
    return Object.fromEntries(headers.map((h, i) => [h, r[i] === '' ? null : r[i]]));
  }),{columns:headers});
}
function jsonl(text, { predicate = () => true, maxRows = LIMITS.jsonl_rows, maxLineBytes = LIMITS.jsonl_line_bytes } = {}) {
  // Never allocate an array for every line of an expanded gzip. Parse and discard
  // unrelated quotes one at a time; even blank physical lines consume the bound.
  const rows = []; let start = 0, lines = 0;
  while (start < text.length) {
    check(++lines <= LIMITS.jsonl_lines, 'JSONL physical-line limit exceeded.', 'excessive_response');
    const next = text.indexOf('\n', start), end = next < 0 ? text.length : next;
    check(end - start <= maxLineBytes, 'JSONL line exceeds the parsing limit.', 'excessive_response');
    const line = text.slice(start, end); start = end + 1;
    check(Buffer.byteLength(line) <= maxLineBytes, 'JSONL line exceeds the parsing limit.', 'excessive_response');
    if (!line.trim()) continue;
    let row; try { row = JSON.parse(line); } catch { throw new DataError('incompatible_schema', 'Malformed JSONL artifact.'); }
    check(object(row), 'JSONL records must be objects.');
    if (predicate(row)) { check(rows.length < maxRows, 'JSONL selected-record limit exceeded.', 'excessive_response'); rows.push(row); }
  }
  return rows;
}
function snapshot(p, symbol = 'QQQ') {
  check(object(p) && typeof p.timestamp === 'string' && dates(p.date) && Array.isArray(p.rows), 'Unsupported snapshot contract.');
  check(p.schema_version === undefined || p.schema_version === 1, 'Unsupported snapshot schema version.');
  check(p.rows.length <= 10000 && p.rows.every(r => object(r) && CONTRACT.test(r.OptionSymbol) && ['call', 'put'].includes(r.Type) && dates(r.Expiration) && typeof r.Strike === 'number' && Number.isFinite(r.Strike)), 'Invalid snapshot rows.');
  // Identity: pre-#121 QQQ payloads carry no top-level symbol and are only
  // accepted as QQQ. Any other mismatch fails closed -- never substituted.
  const declared = p.symbol ?? (symbol === 'QQQ' ? 'QQQ' : null);
  check(declared === symbol, `Snapshot is labeled ${declared ?? 'without a symbol'}, not ${symbol}.`, 'symbol_mismatch');
  check(!object(p.underlying_market) || p.underlying_market.symbol == null || p.underlying_market.symbol === symbol, `Underlying readings belong to ${p.underlying_market?.symbol}, not ${symbol}.`, 'symbol_mismatch');
  check(p.rows.every(r => occRoot(r.OptionSymbol) === symbol), `Snapshot contains non-${symbol} contracts.`, 'symbol_mismatch');
  check(p.snapshot_key == null || UNDERLYINGS[symbol].archive.test(p.snapshot_key), 'Invalid snapshot locator.');
}
function squeeze(p, isSuccess = false) {
  check(object(p) && p.schema_version === 1 && new RegExp('^' + UUID + '$').test(p.run_id) &&
    p.archive === `squeeze-scanner/v1/scheduled/runs/${p.run_id}` && ['complete', 'empty', 'partial', 'failed'].includes(p.status), 'Unsupported squeeze pointer contract.');
  if (isSuccess) check(['complete', 'empty'].includes(p.status) && p.display_schema_version === 1 && Array.isArray(p.candidates) && p.candidates.length <= 10 &&
    p.candidates.every(r => object(r) && SYMBOL.test(r.ticker) && object(r.scores) && object(r.ranks) && Number.isInteger(r.ranks.combined)), 'Invalid squeeze shortlist.');
}
const NASDAQ_KEY = 'nasdaq/latest.json';
const NASDAQ_STALE_SECONDS = 3600; // hourly collector cadence
function nasdaqSnapshot(p) {
  check(object(p) && p.schema_version === 1 && p.dataset === 'nasdaq_snapshot' &&
    typeof p.as_of === 'string' && Number.isFinite(Date.parse(p.as_of)) &&
    object(p.universe) && Number.isInteger(p.universe.listings) && p.universe.listings > 0 &&
    Array.isArray(p.stocks) &&
    p.stocks.length <= p.universe.listings && object(p.index) && p.index.symbol === 'NDX' &&
    object(p.coverage), 'Unsupported NASDAQ snapshot contract.');
  check(p.stocks.every(r => object(r) && SYMBOL.test(r.symbol) && typeof r.name === 'string' &&
    (r.price === null || typeof r.price === 'number') &&
    (r.percent_change === null || typeof r.percent_change === 'number') &&
    typeof r.percent_change_basis === 'string' && typeof r.status === 'string' &&
    ['available', 'source_unavailable'].includes(r.status) &&
    Array.isArray(r.news) && r.news.every(n => object(n) && typeof n.title === 'string')),
    'Invalid NASDAQ snapshot rows.');
  check(typeof p.index.percent_change_basis === 'string' &&
    p.index.percent_change_basis !== (p.stocks[0]?.percent_change_basis ?? null) &&
    p.stocks.every(r => r.percent_change_basis === (p.stocks[0]?.percent_change_basis ?? r.percent_change_basis)),
    'NASDAQ per-stock and index percent-change bases must be labelled differently.');
}
// Last success and latest attempt side by side. The pointer carries no
// producer reason for a partial/failed outcome, so only the coverage counts
// that can account for one are reported, labeled as such.
function squeezeRun(x) {
  if (!x) return null;
  const c = object(x.coverage) ? x.coverage : null, os = object(c?.options_status) ? c.options_status : {};
  const unresolved = ['complete','empty'].includes(x.status) ? [] : [
    ...Object.entries(os).filter(([k,n]) => !['valid','illiquid_options','no_options'].includes(k) && n > 0).map(([k,n]) => `${k}: ${n}`),
    ...(c?.excluded_rows > 0 ? [`excluded_rows: ${c.excluded_rows}`] : []),
    ...(Number.isInteger(c?.discovered_rows) && Number.isInteger(c?.candidates) && c.discovered_rows < c.candidates ? [`discovered_rows ${c.discovered_rows} < candidates ${c.candidates}`] : [])];
  return {run_id:x.run_id,status:x.status,sampling_mode:x.sampling_mode ?? null,scheduled_for:x.scheduled_for ?? null,started_at:x.started_at ?? null,
    finished_at:x.finished_at ?? null,published_at:x.published_at ?? null,archive:x.archive,manifest_sha256:x.manifest_sha256 ?? null,
    denominators:c ? {candidates:c.candidates ?? null,discovered_rows:c.discovered_rows ?? null,eligible:c.eligible ?? null,excluded_rows:c.excluded_rows ?? null,options_status:os} : null,
    unresolved_coverage:unresolved};
}
function squeezeRuns(p, attempt) {
  const a = Date.parse(attempt?.started_at || ''), s = Date.parse(p?.started_at || '');
  const relation = !attempt ? 'no_attempt' : !p ? 'no_success' : attempt.run_id === p.run_id ? 'same_run' :
    !Number.isFinite(a) || !Number.isFinite(s) ? 'unknown' : a > s ? 'attempt_newer' : 'attempt_older';
  return {last_success:squeezeRun(p),latest_attempt:squeezeRun(attempt),attempt_relation:relation,
    reason_basis:'unresolved_coverage lists published coverage counts that can account for a non-complete status; the pointer carries no producer reason field'};
}
function returnSummary(p, session) {
  check(object(p) && p.trade_date === session && typeof p.final === 'boolean' && object(p.assessment) &&
    ['complete', 'partial'].includes(p.assessment.status) && p.return_policy?.version === 'oa203-returns-v1', 'Unsupported selected-session return contract.');
}
function exchangeDate(ms) {
  const parts = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(ms);
  const get = name => parts.find(p=>p.type===name)?.value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
const activePrefix = (session, sweep) => `oa203/scanner/${session}/inprogress/sweep_${String(sweep).padStart(4,'0')}/`;
const activeRevision = (session, sweep, pointer) => {
  const base=activePrefix(session,sweep);
  check(object(pointer)&&pointer.schema_version===1&&pointer.trade_date===session&&pointer.state==='in_progress'&&
    pointer.sweep===sweep&&typeof pointer.archive==='string'&&
    new RegExp('^'+base+'rev_[0-9a-f]{64}$').test(pointer.archive)&&
    object(pointer.members)&&['summary.json','leaderboard.csv','qualified_ask_bid_v1.csv','manifest.jsonl']
      .every(name=>typeof pointer.members[name]==='string'&&/^[0-9a-f]{64}$/.test(pointer.members[name]))&&
    pointer.archive.endsWith('rev_'+pointer.members['summary.json']), 'Unsupported active snapshot pointer.');
  return pointer.archive+'/';
};
function activeSummary(p, session, sweep) {
  returnSummary(p, session);
  check(p.schema_version===1 && p.final===false && p.state==='in_progress' && p.cutoff_sweep===sweep &&
    Number.isSafeInteger(p.cutoff_ms) && p.cutoff_ms>p.session_open_ms && p.cutoff_ms<=p.session_close_ms+60000 &&
    object(p.member_sha256) && p.qualified_policy?.version==='oa203-qualified-ask-bid-v1', 'Unsupported active snapshot summary.');
}
function windowSpan(kind, window_id) {
  const m = /(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$/.exec(window_id);
  return m ? [m[1], m[2]] : null;
}
function windowSummary(p, kind, window_id) {
  check(object(p) && p.schema_version===1 && p.kind===kind && p.window_id===window_id &&
    typeof p.state==='string' && WINDOW_STATES.includes(p.state) && Array.isArray(p.state_reasons) &&
    p.state_reasons.every(r=>typeof r==='string'), 'Unsupported window summary identity or state.');
  check(object(p.policy) && p.policy.version===WINDOW_POLICY_VERSION && p.policy.close_tolerance_s===900 &&
    p.policy.open_tolerance_s===900 && p.policy.qualified_endpoint_policy?.max_quote_age_s===1800,
    'Unsupported window policy.');
  check(Array.isArray(p.sessions) && p.sessions.length>=1 && p.sessions.every(s=>object(s) && dates(s.day) &&
    Number.isSafeInteger(s.open_ms) && Number.isSafeInteger(s.close_ms) && s.open_ms<s.close_ms &&
    typeof s.early_close==='boolean' && typeof s.final==='boolean' && typeof s.assessment==='string') &&
    p.sessions.every((s,i)=>i===0||p.sessions[i-1].day<s.day), 'Unsupported window sessions.');
  const days=p.sessions.map(s=>s.day);
  check(Array.isArray(p.entry_sessions) && p.entry_sessions.length>=1 &&
    p.entry_sessions.every((d,i)=>d===days[i]) && p.exit_session===days.at(-1) &&
    Array.isArray(p.early_closes) && p.early_closes.every(d=>days.includes(d)), 'Unsupported window entry/exit sessions.');
  const pending=p.sessions.filter(s=>!s.final).map(s=>s.day);
  if(p.state==='final') check(pending.length===0 && p.state_reasons.length===0 && p.sessions.every(s=>s.assessment==='complete'), 'Final window has pending or partial sessions.');
  if(p.state==='partial') check(pending.length===0 && p.state_reasons.length>0, 'Partial window names no reason.');
  if(p.state==='in_progress') check(pending.length>0 && p.state_reasons.some(r=>r.startsWith('session_not_final:')), 'In-progress window names no pending session.');
  check(Number.isSafeInteger(p.contracts_evaluated) && p.contracts_evaluated>=0 && object(p.counts) &&
    WINDOW_BASES.every(b=>{
      const c=p.counts[b];
      return object(c) && Number.isSafeInteger(c.eligible) && c.eligible>=0 && Number.isSafeInteger(c.negative) && c.negative>=0 &&
        c.negative<=c.eligible && Number.isSafeInteger(c.excluded) && c.excluded>=0 &&
        c.eligible+c.excluded===p.contracts_evaluated && Number.isSafeInteger(c.shortlist) && c.shortlist>=0 &&
        c.shortlist===Math.min(WINDOW_SHORTLIST, c.eligible) && object(c.exclusion_reasons) &&
        Object.values(c.exclusion_reasons).every(n=>Number.isSafeInteger(n)&&n>=0) &&
        Object.values(c.exclusion_reasons).reduce((a,n)=>a+n,0)===c.excluded;
    }), 'Unsupported window per-basis counts.');
  check(object(p.outputs) && p.outputs.rankings==='rankings.csv' && p.outputs.paths==='paths.csv.gz' &&
    Array.isArray(p.labels) && p.labels.length>0 && p.labels.every(l=>typeof l==='string') &&
    (['weekly_cheapest','weekly_cheapest_best_exit'].includes(kind) ? p.labels.some(l=>l.toLowerCase().includes('hindsight')) : true) &&
    typeof p.generated_at==='string' && Number.isFinite(Date.parse(p.generated_at)) && object(p.scope) &&
    typeof p.calendar==='string' && Array.isArray(p.sources) &&
    p.sources.every(s=>object(s)&&typeof s.key==='string'), 'Unsupported window outputs, labels or provenance.');
}
function windowPathsRow(text, symbol) {
  // One contract's path row without materializing all paths: producer rows
  // never contain a quoted newline (reasons/flags are ';'-joined), so the
  // row is located by its leading symbol cell and parsed alone.
  const lines=text.split('\n');
  const head=csv((lines[0] ?? '')+'\n');
  check(head.columns.length===WINDOW_PATH_COLUMNS.length && head.columns.every((c,i)=>c===WINDOW_PATH_COLUMNS[i]), 'Unsupported window paths header.');
  const matches=lines.slice(1).filter(l=>l.startsWith(symbol+','));
  check(matches.length<=1, 'Paths file carries conflicting attributions for this symbol; no canonical path is asserted.');
  if(!matches.length) return null;
  const rows=csv(lines[0]+'\n'+matches[0]+'\n');
  check(rows.length===1, 'Unsupported window paths row.');
  return rows[0];
}


const units = {
  spy_snapshot: null, // filled below from qqq_snapshot
  qqq_snapshot: { spot: 'USD/share', vwap: 'USD/share (snapshot-weighted approximation)', rvol: 'multiple', momentum_return_pct: 'percentage points', OpenInterest: 'contracts (lagged; zero and missing conflated)', Bid: 'USD/share', Ask: 'USD/share', Strike: 'USD/share' },
  scheduled_squeeze: { scores: 'experimental screening measurements, not probabilities', ranks: 'producer ordinal ranks', first_seen_at: 'publication write-attempt time' },
  options_returns: { '*_pct': 'fractional return (1 = 100%)', '*_entry, *_exit': 'USD/share', '*_ms, t, bid_ms, ask_ms': 'UTC epoch milliseconds', '*_abs_change_per_contract': 'USD/contract', '*_relative_spread': 'spread / midpoint', '*_size': 'contracts at quoted side', '*_entry_delay_s': 'seconds from first archived sample', rank: 'producer ordinal rank' },
};
units.spy_snapshot = units.qqq_snapshot;
units.options_active = units.options_returns;
units.nasdaq_snapshot = { price: 'USD/share', change: 'USD/share', percent_change: 'percentage points', market_cap: 'USD', volume: 'shares', change_1h_pct: 'percentage points', change_7d_pct: 'percentage points', 'index.level': 'index points', 'index.percent_change': 'percentage points' };
units.options_windows = { '*_pct': 'fractional return (1 = 100%)', '*_entry, *_exit': 'USD/share', '*_ms': 'UTC epoch milliseconds', '*_entry_delay_from_boundary_s, *_exit_lead_before_close_s': 'seconds from the session boundary', '*_quote_age_s': 'seconds between quote observation and sample', '*_relative_spread': 'spread / midpoint', '*_size': 'contracts at quoted side', rank: 'producer ordinal rank within the selected basis', contracts_evaluated: 'contracts evaluated in this window, eligible plus excluded' };
units.cross_market_overview = overview.units;
const warnings = {
  cross_market_overview: overview.warnings,
  qqq_snapshot: ['Snapshot timestamp is collection time; retrieval is not observation. Publication time is unavailable.', 'VWAP is approximate; historical VWAP/RVOL are unavailable from snapshot CSVs.', 'OI is lagged and zero versus missing is conflated; Greeks and option quotes have no supplied observation timestamps.', 'Archive locator does not establish checksum verification. Shared PCR/max-pain/reference-skew publication remains owned by #107; merged #116 preserves Crassus lineage but does not publish these readings.'],
  scheduled_squeeze: ['Scores are experimental screens, not squeeze probabilities.', 'first_seen_at/is_new describe producer shortlist publication history; unknown values remain unknown.', 'Provider source times can be absent; acquisition and publication times are distinct.'],
  options_returns: ['Sampled returns miss between-sample extremes. Trade-bar backfill covers only already-ranked contracts.', 'Midpoint and ask-entry/bid-exit comparisons describe observations, not achievable fills.', 'Qualified ask-to-later-bid is a hindsight quote comparison; sizes and spreads do not establish obtainable fills.', 'All/clean coverage is the old top 200 all OR clean; qualified coverage is its separate full-universe top 200.', 'Session artifacts may be rebuilt in place; detail detects summary/shortlist changes and asks for a new query.'],
};
warnings.options_windows = ['Sampled quotes (~5-minute sweeps); between-sample moves are missed.', 'Quote comparisons, not executed fills; spread/size qualification does not establish fills, fees or capacity.', 'Ranks are cut only after a full-universe ranking; the shortlist holds at most 200 rows per basis.', 'Window availability is determined for the requested window; partial replay coverage is not full-universe coverage.', 'In-progress windows have no final exit; no ranking is served for them.'];
warnings.spy_snapshot = [...warnings.qqq_snapshot,
  'SPY RVOL reports insufficient_history/no_data until its own baseline has five completed collection sessions; QQQ baselines are never used for SPY.',
  'SPY is collected 0DTE only (no nearest-weekly archive). Live SPY collection must be observed before it is treated as supported in production.'];
warnings.nasdaq_snapshot = ['Per-stock percent_change is Nasdaq\'s latest reported move (after-hours move after the close); the index percent_change is the regular-session move vs previous close. The two bases differ and are labelled on every row.',
  'Freshness is measured from Nasdaq\'s last-trade timestamp when published, else collector receipt; date-only stamps (as for NDX) carry no usable time.',
  'News headlines are evidence only; they do not establish a cause for any price move.',
  'A source-unavailable stock carries null price fields; no price is substituted or carried forward.'];

class Consumer {
  constructor(source, { now = () => Date.now(), scheduleCalendar = calendar } = {}) { this.source = source; this.now = now; this.calendar = scheduleCalendar; this.references = new Map(); this.retainedBytes = 0; }
  dropReference(id) { const entry = this.references.get(id); if (entry) this.retainedBytes -= entry.bytes; this.references.delete(id); }
  pruneReferences() { for (const [id, entry] of this.references) if (entry.expires < this.now()) this.dropReference(id); }
  clearReferences() { this.references.clear(); this.retainedBytes = 0; }
  reference(value) {
    this.pruneReferences(); const bytes = Buffer.byteLength(JSON.stringify(value));
    check(bytes <= LIMITS.reference_bytes, 'Retained detail exceeds the byte limit; narrow the query.', 'excessive_response');
    while (this.references.size >= LIMITS.references || this.retainedBytes + bytes > LIMITS.reference_bytes) this.dropReference(this.references.keys().next().value);
    const id = randomUUID(); this.references.set(id, { value, bytes, expires: this.now() + LIMITS.reference_ttl_ms }); this.retainedBytes += bytes; return id;
  }
  envelope(dataset, requested, q) { return { schema: 'options-view-mcp.v1', dataset, requested_filters: requested, actual: null,
    source_mode: this.source.mode, retrieval_time: new Date(this.now()).toISOString(), producer_times: { observation: null, acquisition_start: null, acquisition_end: null, publication: null },
    status: 'missing', freshness: { status: 'unknown' }, coverage: null, units: units[dataset] || {}, warnings: [...(warnings[dataset] || [])], sources: q.evidence, pagination: null, errors: [] }; }
  async call(name, a = {}) {
    const q = this.source.query();
    const marketDataset = supportedUnderlying(underlyingOf(a)) ? UNDERLYINGS[underlyingOf(a)].dataset : 'unsupported_underlying';
    const dataset = ({market_context:marketDataset, squeeze_results:'scheduled_squeeze', return_rankings:'options_returns', active_rankings:'options_returns', window_rankings:'options_windows', market_overview:'cross_market_overview', nasdaq_snapshot:'nasdaq_snapshot'})[name] || (name === 'discover_sources' ? 'source_catalog' : 'detail');
    let out = this.envelope(dataset, a, q);
    try {
      if (name === 'discover_sources') out = await this.discover(a, q, out);
      else if (name === 'market_context') out = await this.market(a, q, out);
      else if (name === 'squeeze_results') out = await this.squeeze(a, q, out);
      else if (name === 'return_rankings') out = await this.returns(a, q, out);
      else if (name === 'active_rankings') out = await this.active(a, q, out);
      else if (name === 'window_rankings') out = await this.window(a, q, out);
      else if (name === 'market_overview') out = await this.overview(a, q, out);
      else if (name === 'nasdaq_snapshot') out = await this.nasdaq(a, q, out);
      else if (name === 'result_detail') out = await this.detail(a, q, out);
      else throw new DataError('invalid_filter', 'Unknown tool.');
    } catch (e) { out.errors.push(errorRecord(e)); out.status = e.code === 'missing_artifact' ? 'missing' : 'failed'; }
    // An absent publication is a typed answer, not a failed call: same
    // vocabulary as discover_sources, and not flagged isError, which clients
    // can relabel as a generic argument error.
    const cutoff = name === 'result_detail' ? this.references.get(a?.reference)?.value.cursor : a?.as_of_ms;
    if(out.errors.some(e=>e.code==='unavailable_at_cursor')) {
      // Keep only the scoped request and eligible revision receipts. A later
      // detail artifact cannot leave a half-assembled retrospective payload.
      const unavailable=this.envelope(out.dataset,a,q);
      unavailable.status='unavailable'; unavailable.availability='unproven_at_cursor';
      unavailable.errors=out.errors; out=unavailable;
    }
    if(Number.isSafeInteger(cutoff) && ['active_rankings','return_rankings','result_detail'].includes(name) && out.status!=='failed') {
      const proofs=out.sources.map(s=>s.available_by_ms).filter(Number.isSafeInteger);
      out.as_of={cutoff_ms:cutoff,available_by_ms:proofs.length && !['missing','unavailable'].includes(out.status)
        ? Math.max(...proofs) : null,scope:'retained revisions read for this result'};
      if(name==='active_rankings' && out.producer_times.publication) {
        out.producer_times.reported_publication=out.producer_times.publication;
        out.producer_times.publication=null;
      }
    }
    if (out.status === 'missing') out.availability = 'missing_publication';
    if (name === 'result_detail') out.sources = out.sources.map(e => ({...e, retained:e.retained === true}));
    if (Buffer.byteLength(JSON.stringify(out)) > LIMITS.output_bytes) {
      out = this.envelope(out.dataset, a, {evidence: []}); out.status = 'failed';
      out.errors.push(errorRecord(new DataError('excessive_response', 'Output limit exceeded; narrow filters or reduce limit.')));
    }
    return out;
  }
  async discover(a, q, out) {
    args(a, ['session']); if (a.session !== undefined) check(dates(a.session), 'Select a real YYYY-MM-DD session (2020..2100).', 'invalid_filter');
    // spy_snapshot is appended so existing clients' capability positions stay put.
    const probes = [['qqq_snapshot', 'intraday/latest.json'], ['scheduled_squeeze', 'squeeze-scanner/v1/scheduled/latest.json'],
      ['options_returns', a.session ? `oa203/scanner/${a.session}/summary.json` : null], ['spy_snapshot', UNDERLYINGS.SPY.key],
      ['cross_market_overview', overview.KEY], ['nasdaq_snapshot', NASDAQ_KEY]];
    const chainSymbol = id => id === 'qqq_snapshot' ? 'QQQ' : id === 'spy_snapshot' ? 'SPY' : null;
    out.capabilities = [];
    for (const [id, key] of probes) {
      const probe = key ? await q.optionalJson(key) : {value:null,error:null};
      if (key && !probe.error) try { if (chainSymbol(id)) snapshot(probe.value, chainSymbol(id)); else if (id === 'cross_market_overview') overview.validate(probe.value); else if (id === 'scheduled_squeeze') squeeze(probe.value, true); else if (id === 'nasdaq_snapshot') nasdaqSnapshot(probe.value); else returnSummary(probe.value, a.session); }
      catch (e) { probe.error = errorRecord(e, key); probe.value = null; }
      if (id === 'cross_market_overview') {
        const p = probe.value;
        const listed = p ? overview.rows(p, this.now()) : null;
        out.capabilities.push({ dataset: id, producer_schema: 'collector price strip v2; legacy unversioned/v1 accepted',
          availability: probe.error ? (probe.error.code === 'missing_artifact' ? 'missing_publication' : 'failed') : 'available',
          actual: p ? { collector_cycle_at: p.timestamp, feed_stale: p.feed_stale } : null,
          freshness: p ? {...(Date.parse(p.timestamp) > this.now() ? {status:'invalid_time',observed_at:p.timestamp,age_seconds:null,stale_after_seconds:overview.STALE_SECONDS} : age(p.timestamp, this.now(), overview.STALE_SECONDS)),basis:'collector cycle time, not publication or quote observation'} : { status: 'unknown' },
          source_locator: key, error: probe.error, units: units[id], warnings: warnings[id],
          fields: ['label','id','asset_class','instrument_type','unit','source','source_symbol','price','price_basis','collector_received_at','observed_at','previous_close','change','freshness','gaps'],
          filters: ['asset_class', 'symbols', 'sort', 'limit', 'offset'], asset_classes: overview.ASSET_CLASSES,
          universe_version:overview.universe.universe_version, universe_size:overview.universe.instruments.length,
          instruments:(listed?.known ?? overview.rows({prices:{},timestamp:new Date(this.now()).toISOString(),feed_stale:true},this.now()).known).map(r => ({label:r.label,id:r.id,asset_class:r.asset_class,instrument_type:r.instrument_type,unit:r.unit,source:r.source,available:r.price !== null,enabled:r.enabled,coverage:r.coverage})),
          configured_labels: Object.keys(overview.INSTRUMENTS),
          source_dependent_identities: [{label:'JPY/USD',source:'yfinance',id:'fx.jpyusd',instrument_type:'spot_fx'},{label:'JPY/USD',source:'last-known without retained origin',id:'unknown.jpyusd',instrument_type:'unknown'}],
          gaps:overview.gaps(listed?.known ?? overview.rows({prices:{},timestamp:new Date(this.now()).toISOString(),feed_stale:true},this.now()).known), cadence: 'collector price loop about every 10 s during collector sessions; yfinance fills cached 60 s',
          coverage: p ? { strip_labels: Object.keys(p.prices).length, identified:listed.known.length, unmapped:listed.unmapped.length } : null, historical_index: 'not_supported' });
        continue;
      }
      const chain = chainSymbol(id);
      out.capabilities.push({ dataset: id, ...(chain ? {underlying: chain} : {}), producer_schema: chain ? 'unversioned collector snapshot (symbol-labeled since #121)' : id === 'scheduled_squeeze' ? 'schema_version=1/display_schema_version=1' : id === 'nasdaq_snapshot' ? 'schema_version=1, dataset nasdaq_snapshot' : 'oa203-returns-v1',
        availability: probe.error ? (probe.error.code === 'missing_artifact' ? 'missing_publication' : 'failed') : key ? 'available' : 'explicit_session_required',
        actual:probe.value ? chain?{underlying:chain,snapshot_timestamp:probe.value.timestamp,snapshot_key:probe.value.snapshot_key??null,session:probe.value.date}:id==='scheduled_squeeze'?{run_id:probe.value.run_id,archive:probe.value.archive,session:probe.value.session_date??null}:id==='nasdaq_snapshot'?{as_of:probe.value.as_of,listings:probe.value.universe?.listings??null,available:probe.value.coverage?.available??null,index_level:probe.value.index?.level??null,index_status:probe.value.index?.status??null}:{session:probe.value.trade_date,final:probe.value.final}:null,
        freshness: probe.value ? chain ? age(probe.value.timestamp,this.now()) : id === 'scheduled_squeeze' ? {...age(probe.value.finished_at,this.now()),schedule_confirmation:'query squeeze_results for attempt/schedule/calendar status'} : id === 'nasdaq_snapshot' ? {...age(probe.value.as_of,this.now(),NASDAQ_STALE_SECONDS),basis:'collector receipt; hourly cadence'} : {status:'historical'} : {status:'unknown'},
        source_locator: key, error: probe.error, units: units[id], warnings: warnings[id],
        fields: chain ? ['symbol','timestamp','snapshot_key','underlying_market','rows'] : id === 'scheduled_squeeze' ? ['candidates','coverage','latest-attempt','latest-schedule','manifest','inputs','results'] : id === 'nasdaq_snapshot' ? ['symbol','name','price','change','percent_change','percent_change_basis','market_cap','rank','day_high','day_low','volume','change_1h_pct','change_7d_pct','news','status','index','coverage'] : ['summary','universe','leaderboard','qualified_ask_bid_v1','selected_contract_quote_path'],
        filters: chain ? ['underlying','expiry','type','strike','contract','limit','offset'] : id === 'scheduled_squeeze' ? ['limit','offset'] : id === 'nasdaq_snapshot' ? ['symbols','sort','limit','offset'] : ['session','underlying','type','view','limit','offset'],
        cadence: chain ? 'collector session cycles; field timestamps govern age' : id === 'scheduled_squeeze' ? '09:00 and 12:00 America/New_York exchange-session calendar' : id === 'nasdaq_snapshot' ? 'hourly collector run; snapshot as_of governs age' : 'selected finalized session; about five-minute samples',
        coverage: probe.value?.coverage || probe.value?.assessment || (chain ? `latest ${chain} 0DTE snapshot only` : null), historical_index: 'not_supported' });
    }
    // Windows have no global index or window list to probe: the producer writes
    // one directory per window, each with its own latest.json pointer that pins
    // a read to one revision. Live R2 publication is Chej's release step.
    // Discovery therefore advertises the fixed contract and requires an
    // explicit window; it never claims live data.
    out.capabilities.push({dataset:'options_windows',producer_schema:'oa203-windows-v1',
      availability:'explicit_window_required',source_locator:'oa203/windows/oa203-windows-v1/<kind>/<window_id>/{summary.json,rankings.csv,paths.csv.gz}',
      error:null,actual:null,fields:['summary','rankings','paths','counts','labels'],
      filters:['window_id','kind','basis','underlying','type','limit','offset'],
      kinds:WINDOW_KINDS,bases:WINDOW_BASES,states:WINDOW_STATES,shortlist_per_basis:WINDOW_SHORTLIST,
      units:units.options_windows,warnings:warnings.options_windows,
      publication:'query_explicit_window',historical_index:'explicit window only; no global index'});
    const activeSession=a.session??exchangeDate(this.now());
    const activeKey=`oa203/scanner/${activeSession}/inprogress/latest.json`;
    const activeProbe=await q.optionalJson(activeKey);
    out.capabilities.push({dataset:'options_active',producer_schema:'active snapshot v1',
      availability:activeProbe.error?(activeProbe.error.code==='missing_artifact'?'missing_publication':'failed'):'available',
      source_locator:activeKey,error:activeProbe.error,actual:activeProbe.value?{session:activeSession,
        sweep:activeProbe.value.sweep,cutoff_ms:activeProbe.value.cutoff_ms,archive:activeProbe.value.archive}:null,
      fields:['summary','leaderboard','qualified_ask_bid_v1','pinned_manifest','selected_contract_quote_path'],
      filters:['session','sweep','underlying','type','view','limit','offset'],cadence:'every six completed sweeps, approximately 30 minutes when builds succeed',
      views:['all','clean','qualified_ask_bid_v1'],units:units.options_active,
      warnings:['In-progress ranks are provisional; follow one detail_reference for its exact cutoff.'],
      historical_index:'explicit retained sweep only; no broad history listing'});
    out.underlyings = out.capabilities.filter(c => c.underlying).map(c => ({ symbol: c.underlying, dataset: c.dataset,
      availability: c.availability, default: c.underlying === 'QQQ', selector: 'market_context.underlying' }));
    out.unsupported = ['shared_PCR','shared_max_pain','shared_reference_OI_skew','historical_VWAP_RVOL','account_trading_momentum','buy_only_Black_Scholes','private_accounts_positions_orders'];
    out.limits = LIMITS; out.status = out.capabilities.some(c => ['missing_publication','failed'].includes(c.availability)) ? 'partial' : 'available'; return out;
  }
  // Field ages are measured at retrieval, so a detail call reports the same
  // fields as market_context, aged to the detail's own retrieval time.
  readings(m) {
    return object(m) ? {...m, field_freshness: {spot:age(m.spot_ts,this.now()),vwap:age(m.vwap_ts,this.now()), session_volume:age(m.session_volume_ts,this.now()),
      rvol:{...age(m.session_volume_ts,this.now()),producer_status:m.rvol?.status ?? 'missing'},momentum:{...age(m.momentum?.spot_observed_at,this.now()),producer_status:m.momentum?.status ?? 'missing'}}} : null;
  }
  async market(a, q, out) {
    args(a, ['underlying','expiry','type','strike','contract','limit','offset']); const {limit,offset} = page(a);
    const symbol = underlyingOf(a);
    check(supportedUnderlying(symbol), `Unsupported underlying; supported: ${Object.keys(UNDERLYINGS).join(', ')}.`, 'invalid_filter');
    if (a.expiry !== undefined) check(dates(a.expiry), 'Invalid expiry.', 'invalid_filter');
    if (a.type !== undefined) check(['call','put'].includes(a.type), 'Invalid option type.', 'invalid_filter');
    if (a.contract !== undefined) check(CONTRACT.test(a.contract), 'Invalid contract.', 'invalid_filter');
    if (a.contract !== undefined) check(occRoot(a.contract) === symbol, `Contract is not a ${symbol} option; set underlying to match it.`, 'invalid_filter');
    if (a.strike !== undefined) check(typeof a.strike === 'number' && Number.isFinite(a.strike) && a.strike > 0 && a.strike <= 100000, 'Invalid strike.', 'invalid_filter');
    const p = await q.json(UNDERLYINGS[symbol].key); snapshot(p, symbol);
    out.requested_underlying = symbol;
    out.actual = { underlying:symbol, snapshot_timestamp:p.timestamp, snapshot_key:p.snapshot_key ?? null, session:p.date, payload_sha256:q.evidence.at(-1).retrieved_sha256 };
    out.producer_times.acquisition_end = p.timestamp; out.freshness = {...age(p.timestamp, this.now()),basis:'collection_timestamp'};
    const m = p.underlying_market;
    out.readings = this.readings(m);
    if (!object(m)) out.warnings.push('underlying_market is absent; derived readings are unavailable.');
    const rows = p.rows.filter(r => (!a.expiry || r.Expiration === a.expiry) && (!a.type || r.Type === a.type) && (a.strike === undefined || r.Strike === a.strike) && (!a.contract || r.OptionSymbol === a.contract));
    const paged = slice(rows,limit,offset); out.pagination = paged.pagination;
    out.rows = paged.rows.map(row => ({...row, detail_reference:this.reference({dataset:UNDERLYINGS[symbol].dataset,underlying:symbol,p:{timestamp:p.timestamp,underlying_market:p.underlying_market??null},row,actual:out.actual,evidence:[...q.evidence]})}));
    out.coverage = {source_rows:p.rows.length, matched_rows:rows.length, expiration:p.expiration ?? null};
    const incomplete = !object(m) || m.spot == null || m.vwap == null || m.vwap_partial_session || m.rvol?.status !== 'ok' || m.momentum?.status !== 'ok';
    if(incomplete && object(m)) out.warnings.push(`Partial readings: VWAP ${m.vwap==null?'missing':m.vwap_partial_session?'partial_session':'available'}; RVOL ${m.rvol?.status??'missing'}; reference momentum ${m.momentum?.status??'missing'}.`);
    out.status = incomplete ? 'partial' : p.rows.length ? 'available' : 'empty';
    return out;
  }
  // NASDAQ-100 snapshot: read-only, same envelope/freshness shape as
  // market_context. Per-stock percent_change is Nasdaq's latest reported
  // move (after-hours move after the close); the index percent_change is
  // the regular-session move vs previous close. The two bases are labelled
  // on the payload and never reconciled into one number.
  async nasdaq(a, q, out) {
    args(a, ['symbols', 'sort', 'limit', 'offset']); const {limit, offset} = page(a);
    if (a.symbols !== undefined) check(Array.isArray(a.symbols) && a.symbols.length >= 1 && a.symbols.length <= LIMITS.rows && a.symbols.every(s => typeof s === 'string' && SYMBOL.test(s)), `symbols must be 1..${LIMITS.rows} NASDAQ-100 tickers.`, 'invalid_filter');
    const sort = a.sort ?? 'strip'; check(['strip', 'percent_desc', 'percent_asc'].includes(sort), 'sort must be strip, percent_desc or percent_asc.', 'invalid_filter');
    const p = await q.json(NASDAQ_KEY); nasdaqSnapshot(p);
    const now = this.now();
    out.actual = { universe_version: `${p.universe.listings} listings as of ${p.universe.as_of}`, as_of: p.as_of, generated_at: p.generated_at ?? null, payload_sha256: q.evidence.at(-1).retrieved_sha256 };
    out.producer_times = { observation: null, acquisition_start: p.as_of, acquisition_end: null, publication: null };
    out.freshness = {...age(p.as_of, now, NASDAQ_STALE_SECONDS), basis: 'collector receipt; hourly cadence'};
    out.index = p.index;
    const wanted = a.symbols ? new Set(a.symbols) : null;
    const filtered = p.stocks.filter(r => !wanted || wanted.has(r.symbol));
    if (sort !== 'strip') {
      const sign = sort === 'percent_desc' ? -1 : 1;
      filtered.sort((x, y) => (x.percent_change === null) - (y.percent_change === null) || sign * ((x.percent_change ?? 0) - (y.percent_change ?? 0)) || x.symbol.localeCompare(y.symbol));
    }
    const paged = slice(filtered, limit, offset); out.pagination = { ...paged.pagination, sort, ranked_over: sort === 'strip' ? null : 'all matching rows; rows without a percent change sort last' };
    out.rows = paged.rows.map(row => ({ ...row, detail_reference: this.reference({ dataset: 'nasdaq_snapshot', row, actual: out.actual, evidence: [...q.evidence] }) }));
    const available = p.stocks.filter(r => r.status === 'available').length;
    out.coverage = { listings: p.universe.listings, available, source_unavailable: p.stocks.length - available,
      with_news: p.stocks.filter(r => r.news.length).length, matched_rows: filtered.length,
      advancers: p.index.breadth?.advancers ?? null, decliners: p.index.breadth?.decliners ?? null,
      unchanged: p.index.breadth?.unchanged ?? null, index_status: p.index.status };
    out.unmatched_symbols = wanted ? [...wanted].filter(s => !p.stocks.some(r => r.symbol === s)).map(s => ({ symbol: s, status: 'not_in_nasdaq_100' })) : [];
    if (out.unmatched_symbols.length) out.warnings.push(`Symbols outside the NASDAQ-100 universe: ${out.unmatched_symbols.map(s => s.symbol).join(', ')}.`);
    if (p.index.status !== 'available') out.warnings.push('NDX index block is source-unavailable; stock rows still stand on their own.');
    out.status = p.stocks.length - available || p.index.status !== 'available' ? 'partial' : filtered.length ? 'available' : 'empty';
    return out;
  }
  async overview(a, q, out) {
    args(a, ['asset_class','symbols','sort','limit','offset']); const {limit,offset} = page(a);
    if (a.asset_class !== undefined) check(overview.ASSET_CLASSES.includes(a.asset_class), `asset_class must be one of ${overview.ASSET_CLASSES.join(', ')}.`, 'invalid_filter');
    if (a.symbols !== undefined) check(Array.isArray(a.symbols) && a.symbols.length >= 1 && a.symbols.length <= LIMITS.rows && a.symbols.every(s => typeof s === 'string' && overview.SELECTOR.test(s)), `symbols must be 1..${LIMITS.rows} strip labels or instrument ids.`, 'invalid_filter');
    const sort = a.sort ?? 'strip'; check(overview.SORTS.includes(sort), `sort must be one of ${overview.SORTS.join(', ')}.`, 'invalid_filter');
    const p = await q.json(overview.KEY); overview.validate(p);
    const now = this.now();
    out.actual = { universe_version:overview.universe.universe_version, producer_schema_version:p.schema_version ?? 1, collector_cycle_at: p.timestamp, snapshot_time: p.snapshot_time ?? null, feed_stale: p.feed_stale, payload_sha256: q.evidence.at(-1).retrieved_sha256 };
    out.producer_times = { observation: null, acquisition_start: p.timestamp, acquisition_end: null, publication: null };
    out.freshness = { ...(Date.parse(p.timestamp) > now ? {status:'invalid_time',observed_at:p.timestamp,age_seconds:null,stale_after_seconds:overview.STALE_SECONDS} : age(p.timestamp, now, overview.STALE_SECONDS)), basis: 'collector cycle start, before fallbacks and artifact upload; each row carries its own collector receipt age' };
    const { known, unmapped } = overview.rows(p, now);
    const wanted = a.symbols ? new Set(a.symbols) : null;
    const filtered = known.filter(r => (!a.asset_class || r.asset_class === a.asset_class) && (!wanted || wanted.has(r.label) || wanted.has(r.id)));
    if (sort !== 'strip') {
      const sign = sort === 'percent_desc' ? -1 : 1;
      filtered.sort((x, y) => (x.change.percent === null) - (y.change.percent === null) || sign * ((x.change.percent ?? 0) - (y.change.percent ?? 0)) || x.label.localeCompare(y.label));
    }
    const paged = slice(filtered, limit, offset); out.pagination = { ...paged.pagination, sort, ranked_over: sort === 'strip' ? null : 'all matching rows; rows without a percent change sort last' };
    out.rows = paged.rows.map(row => ({ ...row, detail_reference: this.reference({ dataset: 'cross_market_overview', row, actual: out.actual, evidence: [...q.evidence] }) }));
    const gaps = overview.gaps(known);
    const gapIds = new Set(gaps.map(g => g.id));
    out.gaps = gaps.filter(g => (!a.asset_class || g.asset_class === a.asset_class) && (!wanted || wanted.has(g.id) || wanted.has(g.label)));
    out.unmatched_symbols = wanted ? [...wanted].filter(s => !known.some(r => r.label === s || r.id === s) && !gapIds.has(s)).map(s => ({ symbol: s, status: 'not_in_price_strip' })) : [];
    out.unmapped_labels = unmapped;
    const counts = rows => ({rows:rows.length, available_prices:rows.filter(r => r.price !== null).length, missing_prices:rows.filter(r => r.price === null).length,
      live_rows:rows.filter(r => r.freshness.live).length, ok_changes:rows.filter(r => r.change.status === 'ok').length,
      partial_changes:rows.filter(r => r.change.status === 'partial').length, unavailable_changes:rows.filter(r => r.change.status === 'unavailable').length,
      numeric_changes:rows.filter(r => r.change.absolute !== null).length});
    out.coverage = { universe_version:overview.universe.universe_version, universe_size:known.length, full_universe:counts(known),
      strip_labels:Object.keys(p.prices).length, identified:known.length, published_identified:known.filter(r => Object.hasOwn(p.prices,r.label)).length,
      matched_rows:filtered.length, named_gaps:gaps.length, matching_gaps:out.gaps.length, ...counts(filtered) };
    if (p.feed_stale) out.warnings.push('Producer marked the DXLink feed stale when this strip was written.');
    if (unmapped.length) out.warnings.push(`Strip labels without a reviewed identity are withheld: ${unmapped.join(', ')}.`);
    out.status = out.coverage.unavailable_changes || out.coverage.live_rows < filtered.length || out.gaps.length || unmapped.length ? 'partial' : filtered.length ? 'available' : 'empty';
    return out;
  }
  async squeeze(a, q, out) {
    args(a,['limit','offset','run']); const {limit,offset} = page(a);
    check(['last_success','latest_attempt'].includes(a.run??'last_success'),'run must be last_success or latest_attempt.','invalid_filter');
    const docs = [];
    for (const suffix of ['latest','latest-attempt','latest-schedule']) {
      const doc = await q.optionalJson(`squeeze-scanner/v1/scheduled/${suffix}.json`);
      if (!doc.error) try { if (suffix !== 'latest-schedule') squeeze(doc.value,suffix === 'latest'); else check(object(doc.value) && (doc.value.schema_version===undefined||doc.value.schema_version===1) && ['claimed','finished','missed','interrupted'].includes(doc.value.status) && Number.isFinite(Date.parse(doc.value.scheduled_for)), 'Unsupported squeeze schedule contract.'); }
      catch(e) { doc.error=errorRecord(e,`squeeze-scanner/v1/scheduled/${suffix}.json`); doc.value=null; }
      docs.push(doc);
      if (doc.error) out.errors.push(doc.error);
    }
    const [p, attempt, schedule] = docs.map(d=>d.value);
    out.attempt = attempt; out.schedule = schedule;
    const state = formatSqueezeScheduleStatus(p,attempt,schedule,this.calendar,this.now());
    out.freshness = {...age(p?.finished_at,this.now()),status:state.state === 'live' ? 'fresh' : state.warning?.includes('calendar') || state.warning?.includes('coverage') ? 'unknown' : 'stale', schedule_status:state};
    if (state.warning) out.warnings.push(state.warning);
    out.runs = squeezeRuns(p, attempt);
    if (a.run === 'latest_attempt') return this.squeezeAttempt(q, out, attempt, schedule, state, limit, offset);
    out.rows_basis = p ? {run:'last_success',run_id:p.run_id,scheduled_for:p.scheduled_for ?? null} : null;
    if (out.runs.attempt_relation === 'attempt_newer')
      out.warnings.push(`Rows are from the last successful run ${p.run_id} (scheduled ${p.scheduled_for ?? 'unknown'}), not from the newer ${attempt.status} attempt ${attempt.run_id} (scheduled ${attempt.scheduled_for ?? 'unknown'}). Call squeeze_results with run=latest_attempt to read that attempt's rows.`);
    if (!p) { out.status = docs[0].error && docs[0].error.code!=='missing_artifact' ? 'failed' : attempt ? ['partial','failed'].includes(attempt.status) ? attempt.status : 'missing' : docs[0].error?.code === 'missing_artifact' ? 'missing' : 'failed';
      if (attempt?.status === 'partial') out.warnings.push(`No successful run is published; call squeeze_results with run=latest_attempt to read partial attempt ${attempt.run_id}.`);
      return out; }
    out.actual = {run_id:p.run_id,session:p.session_date ?? null,archive:p.archive,scoring_version:p.scoring_version,manifest_sha256:p.manifest_sha256 ?? null};
    out.producer_times = {observation:null,acquisition_start:p.started_at ?? null,acquisition_end:p.finished_at ?? null,publication:p.published_at ?? null};
    out.coverage = p.coverage ?? null;
    const paged = slice(p.candidates,limit,offset); out.pagination=paged.pagination;
    out.rows=paged.rows.map(row=>({...row,detail_reference:this.reference({dataset:'scheduled_squeeze',p,row,attempt,schedule,actual:out.actual,evidence:[...q.evidence]})}));
    out.status=out.errors.length ? 'partial' : p.candidates.length ? 'available' : 'empty'; return out;
  }
  // Reads the latest attempt's own archive (any outcome), so a newer partial
  // run is never hidden behind the retained last success.
  async squeezeAttempt(q, out, attempt, schedule, state, limit, offset) {
    if (!attempt) { out.status = out.errors.some(e => e.code !== 'missing_artifact') ? 'failed' : 'missing'; out.rows_basis = null; return out; }
    out.rows_basis = {run:'latest_attempt',run_id:attempt.run_id,scheduled_for:attempt.scheduled_for ?? null};
    out.actual = {run_id:attempt.run_id,session:attempt.session_date ?? attempt.valuation_date ?? null,archive:attempt.archive,scoring_version:attempt.scoring_version ?? null,manifest_sha256:attempt.manifest_sha256 ?? null};
    out.producer_times = {observation:null,acquisition_start:attempt.started_at ?? null,acquisition_end:attempt.finished_at ?? null,publication:attempt.published_at ?? null};
    const slots = Array.isArray(this.calendar?.slots) ? this.calendar.slots.map(Date.parse).filter(Number.isFinite) : [];
    const now = this.now(), due = slots.filter(v => v <= now).at(-1);
    // Fresh only when the attempt is the due slot of today's session, its
    // acquisition times are coherent, and the schedule confirms it finished
    // with the same outcome; otherwise stale or unconfirmed, never fresh.
    const started = Date.parse(attempt.started_at || ''), finished = Date.parse(attempt.finished_at || '');
    const timesKnown = Number.isFinite(started) && Number.isFinite(finished) && started <= finished && finished <= now;
    const dueSlot = due != null && Date.parse(attempt.scheduled_for || '') === due;
    const confirmed = dueSlot && schedule?.status === 'finished' && schedule.run_id === attempt.run_id &&
      Date.parse(schedule.scheduled_for || '') === due && schedule.acquisition_status === attempt.status;
    const freshness = state.warning?.includes('calendar') ? ['unknown','schedule calendar unavailable'] :
      !timesKnown ? ['unknown','attempt acquisition times missing or inconsistent'] :
      !dueSlot ? ['stale','attempt is not the latest due scheduled slot'] :
      exchangeDate(due) !== exchangeDate(now) ? ['stale','attempt belongs to a previous session'] :
      !confirmed ? ['unknown',`schedule does not confirm this attempt finished (${schedule?.status ?? 'no schedule'})`] : ['fresh',null];
    out.freshness = {...age(attempt.finished_at,now),basis:'latest_attempt',status:freshness[0],reason:freshness[1],schedule_status:state};
    let manifest, results;
    try {
      manifest = await q.json(attempt.archive+'/manifest.json');
      check(manifest.run_id===attempt.run_id && manifest.schema_version===1,'Archive manifest identity/schema mismatch.');
      if (attempt.manifest_sha256) { check(q.evidence.at(-1).retrieved_sha256===attempt.manifest_sha256,'Manifest digest differs from the attempt pointer.','source_changed'); q.evidence.at(-1).checksum_verification='matches_attempt_pointer_manifest_sha256'; }
      else out.warnings.push('Attempt pointer has no manifest_sha256; the manifest is unverified.');
      results = await q.json(attempt.archive+'/results.json');
      check(manifest.files?.['results.json']?.sha256===q.evidence.at(-1).retrieved_sha256,'Attempt results digest differs from its manifest.','source_changed');
      q.evidence.at(-1).checksum_verification='matches_manifest_member_sha256';
      check(Array.isArray(results) && results.every(r => object(r) && SYMBOL.test(r.ticker) && typeof r.options_status === 'string'),'Unsupported squeeze results shape.');
    } catch (e) {
      out.errors.push(errorRecord(e)); out.rows = [];
      out.status = e.code === 'missing_artifact' ? attempt.status === 'failed' ? 'failed' : 'missing' : 'failed';
      if (e.code === 'missing_artifact') out.warnings.push(`Attempt ${attempt.run_id} (${attempt.status}) has no readable results archive; that is not an empty screen.`);
      return out;
    }
    out.coverage = manifest.coverage ?? attempt.coverage ?? null;
    const rank = r => Number.isInteger(r.ranks?.combined) ? r.ranks.combined : Infinity;
    const ordered = [...results].sort((x,y) => rank(x)-rank(y) || (x.ticker < y.ticker ? -1 : x.ticker > y.ticker ? 1 : 0));
    const counts = {}; for (const r of results) counts[r.options_status] = (counts[r.options_status] ?? 0) + 1;
    const eligibleRows = results.filter(r => r.comparison_eligible === true).length;
    const reported = out.coverage?.options_status ?? null;
    const matches = out.coverage?.candidates === results.length && out.coverage?.eligible === eligibleRows &&
      object(reported) && Object.keys({...reported,...counts}).every(k => (reported[k] ?? 0) === (counts[k] ?? 0));
    out.reconciliation = {rows:results.length,candidates_reported:out.coverage?.candidates ?? null,eligible_rows:eligibleRows,eligible_reported:out.coverage?.eligible ?? null,
      options_status_rows:counts,options_status_reported:reported,matches};
    if (!matches) out.warnings.push('Attempt result rows do not reconcile with its published coverage counts; missing rows remain missing, not "no candidates".');
    const paged = slice(ordered,limit,offset); out.pagination = {...paged.pagination,scope:'every result row of the attempt: eligible rows by producer combined rank, then excluded rows by ticker'};
    out.rows = paged.rows.map(row => ({...row,detail_reference:this.reference({dataset:'scheduled_squeeze',p:attempt,row,attempt,schedule,actual:out.actual,evidence:[...q.evidence]})}));
    if (!['complete','empty'].includes(attempt.status)) out.warnings.push(`Attempt ${attempt.run_id} is ${attempt.status}; its rows are what that run recorded, not a successful scan.`);
    out.status = attempt.status === 'failed' ? 'failed' : attempt.status === 'partial' || !matches || out.errors.length ? 'partial' : results.length ? 'available' : 'empty';
    return out;
  }
  scannerCutoff(a,q) {
    if (a.as_of_ms === undefined) return;
    check(Number.isSafeInteger(a.as_of_ms) && a.as_of_ms >= 1e12 && a.as_of_ms <= this.now(),
      'as_of_ms must be a past or present UTC epoch millisecond cutoff.', 'invalid_filter');
    q.atCutoff(a.as_of_ms);
  }
  async returns(a,q,out) {
    args(a,['session','underlying','type','view','limit','offset','as_of_ms'],['session']);
    this.scannerCutoff(a,q); const {limit,offset}=page(a);
    check(dates(a.session),'Select a real YYYY-MM-DD session (2020..2100).','invalid_filter');
    if(a.underlying!==undefined) check(SYMBOL.test(a.underlying),'Invalid underlying.','invalid_filter');
    if(a.type!==undefined) check(['call','put'].includes(a.type),'Invalid option type.','invalid_filter');
    check(['all','clean','qualified_ask_bid_v1'].includes(a.view??'all'),'view must be all, clean, or qualified_ask_bid_v1.','invalid_filter');
    const prefix=`oa203/scanner/${a.session}/`; const p=await q.json(prefix+'summary.json'); returnSummary(p,a.session);
    if(a.as_of_ms !== undefined) check(p.final && Number.isFinite(Date.parse(p.finalized_at)) &&
      Date.parse(p.finalized_at)<=a.as_of_ms, 'Finalized ranking unavailable at this cutoff.', 'unavailable_at_cursor');
    out.actual={session:a.session,final:p.final,return_policy:p.return_policy,qualified_policy:p.qualified_policy??null};
    out.producer_times={observation:null,acquisition_start:null,acquisition_end:null,publication:null,finalized_at:p.finalized_at ?? null};
    out.session_window={open_ms:p.session_open_ms??null,close_ms:p.session_close_ms??null,meaning:'exchange boundaries; not acquisition timestamps'};
    out.freshness={status:'historical',meaning:'Explicit selected session; finalized_at is not a quote observation or publication time.'};
    out.summary=p; out.coverage=p.assessment; out.warnings.push(...(p.assessment.reasons||[]));
    // "complete" covers session-level checks; a sweep cut short (usually the
    // last, at the close) is reported separately. Older summaries lack the
    // producer fields, so derive the count from the sweep totals.
    const cut=Number.isSafeInteger(p.assessment.truncated_sweeps)?p.assessment.truncated_sweeps:
      Number.isSafeInteger(p.assessment.sweeps_in_session)&&Number.isSafeInteger(p.assessment.complete_sweeps)?
        p.assessment.sweeps_in_session-p.assessment.complete_sweeps:null;
    if(cut>0){out.coverage={...p.assessment,truncated_sweeps:cut};
      const last=p.assessment.final_sweep;
      out.warnings.push(`${cut} of ${p.assessment.sweeps_in_session} in-session sweeps did not finish`+
        (last?.truncated?` (final sweep ${last.sweep}: ${last.chains_recorded} of ${last.chains_selected} chains recorded)`:'')+
        `; coverage status "${p.assessment.status}" reflects session-level checks, not every sweep finishing.`);}
    if(!p.final) out.warnings.push('Session is not final.');
    const qualified=a.view==='qualified_ask_bid_v1';
    if(qualified && p.qualified_policy?.version!=='oa203-qualified-ask-bid-v1') {
      out.status='unavailable';out.warnings.push('Selected legacy session has no qualified ask-to-bid policy or shortlist.');return out;
    }
    const board=qualified?'qualified_ask_bid_v1.csv':'leaderboard.csv';
    let boardText;
    try { boardText=await q.get(prefix+board); }
    catch(e) { if(qualified && e.code==='missing_artifact') {out.status='partial';out.errors.push(errorRecord(e,prefix+board));return out;} throw e; }
    const rows=csv(boardText);
    if(qualified) {
      check(p.outputs?.qualified_ask_bid_v1===board && (p.contracts===0 && rows.length===0 ||
        rows.columns.includes(QUALIFIED_RANK) && rows.columns.includes('qualified_first_ask_to_later_bid_status') && rows.columns.includes('qualified_first_ask_to_later_bid_reasons')), 'Missing qualified shortlist columns.');
      check(rows.every(r=>r.qualified_first_ask_to_later_bid_status==='eligible' && /^[1-9]\d*$/.test(r[QUALIFIED_RANK]??'') && Number.isSafeInteger(Number(r[QUALIFIED_RANK])) && r.qualified_first_ask_to_later_bid_pct!==null && Number.isFinite(Number(r.qualified_first_ask_to_later_bid_pct))), 'Invalid qualified shortlist or producer ranks.');
    }
    check(p.contracts===0&&rows.length===0 || ['symbol','underlying','option_type','rank','clean_rank','clean'].every(k=>rows.columns.includes(k)), 'Missing required leaderboard columns.');
    check(rows.every(r=>CONTRACT.test(r.symbol) && SYMBOL.test(r.underlying) && ['call','put'].includes(r.option_type) &&
      ['rank','clean_rank'].every(k=>Object.hasOwn(r,k)&&(r[k]===null || (/^[1-9]\d*$/.test(r[k])&&Number.isSafeInteger(Number(r[k]))))) &&
      ['True','False'].includes(r.clean) && (r.clean_rank===null || r.clean==='True')), 'Invalid leaderboard rows or missing/malformed producer ranks.');
    const field=qualified?QUALIFIED_RANK:a.view==='clean'?'clean_rank':'rank';
    const baseline=q.evidence.slice(-2).map(e=>e.retrieved_sha256);
    await q.get(prefix+'summary.json');await q.get(prefix+board);
    check(q.evidence.slice(-2).every((e,i)=>e.retrieved_sha256===baseline[i]),'Selected session changed during rankings read; retry this session.','source_changed');
    out.warnings.push('Matching before/after summary/shortlist digests detect observed rebuilds; no atomic producer session revision is available.');
    const filtered=rows.filter(r=>r[field]!==null && (!a.underlying||r.underlying===a.underlying) && (!a.type||r.option_type===a.type));
    filtered.sort((x,y)=>Number(x[field])-Number(y[field])); // producer rank, never re-score
    const paged=slice(filtered,limit,offset); out.pagination=paged.pagination;
    out.rows=paged.rows.map(row=>({...row,detail_reference:this.reference({dataset:'options_returns',cursor:a.as_of_ms,p,row,prefix,board,actual:out.actual,evidence:q.evidence.slice(-4)})}));
    out.status=p.assessment.status==='partial'||!p.final?'partial':filtered.length?'available':'empty';return out;
  }
  async active(a,q,out) {
    args(a,['session','sweep','underlying','type','view','limit','offset','as_of_ms']);
    this.scannerCutoff(a,q);
    if(a.as_of_ms !== undefined) check(dates(a.session) && Number.isInteger(a.sweep),
      'A cursor lookup requires an explicit session and retained sweep.', 'invalid_filter');
    const clockAt = a.as_of_ms ?? this.now();
    const {limit,offset}=page(a);
    const session=a.session??'current';
    check(session==='current'||dates(session),'session must be current or one valid date.','invalid_filter');
    if(a.sweep!==undefined) check(Number.isInteger(a.sweep)&&a.sweep>=1&&a.sweep<=9999,'sweep must be 1..9999.','invalid_filter');
    if(a.underlying!==undefined) check(SYMBOL.test(a.underlying),'Invalid underlying.','invalid_filter');
    if(a.type!==undefined) check(['call','put'].includes(a.type),'Invalid option type.','invalid_filter');
    check(['all','clean','qualified_ask_bid_v1'].includes(a.view??'all'),'Invalid active view.','invalid_filter');
    const selected=session==='current'?exchangeDate(clockAt):session;
    out.actual={session:selected,selector:session,sweep:a.sweep??null,final:false};
    const calendarPoint=Date.parse(selected+'T12:00:00Z');
    const calendarValid=Date.parse(this.calendar?.valid_from)<=calendarPoint &&
      calendarPoint<Date.parse(this.calendar?.valid_until);
    const slots=calendarValid?(this.calendar?.slots??[]).filter(t=>exchangeDate(Date.parse(t))===selected):[];
    if(session==='current'&&!calendarValid){out.status='unavailable';out.warnings.push('Exchange calendar coverage is unavailable for current-session selection.');return out;}
    if(!calendarValid)out.warnings.push('Exchange calendar coverage for this explicit date is unavailable; session bounds come only from the producer snapshot.');
    if(calendarValid&&!slots.length){out.status='holiday';out.warnings.push('No exchange session is scheduled for the selected date.');return out;}
    const open=slots.length?Date.parse(slots[0])+30*60000:null;
    if(a.sweep===undefined) {
      const final=await q.optionalJson(`oa203/scanner/${selected}/summary.json`);
      if(!final.error) {
        returnSummary(final.value,selected);
        if(final.value.final) {
          const result=await this.returns({session:selected,underlying:a.underlying,type:a.type,view:a.view,limit,offset},q,out);
          result.active_state='final';result.status=result.status==='available'?'final':result.status;
          return result;
        }
      } else if(final.error.code!=='missing_artifact') out.errors.push(final.error);
      if(open!==null&&clockAt<open){out.status='before_open';out.session_window={open_ms:open,close_ms:null};return out;}
    }
    let pointer=null, sweep=a.sweep;
    if(sweep===undefined) {
      const attempt=await q.optionalJson(`oa203/scanner/${selected}/inprogress/latest-attempt.json`);
      if(!attempt.error) {
        check(object(attempt.value)&&attempt.value.schema_version===1&&attempt.value.trade_date===selected&&
          Number.isInteger(attempt.value.sweep)&&attempt.value.sweep>=1&&attempt.value.sweep<=9999&&
          ['building','complete','failed'].includes(attempt.value.status)&&Number.isFinite(Date.parse(attempt.value.at)),
        'Unsupported active attempt status.');
        out.attempt=attempt.value;
      } else if(attempt.error.code!=='missing_artifact')out.errors.push(attempt.error);
      const found=await q.optionalJson(`oa203/scanner/${selected}/inprogress/latest.json`);
      if(found.error){out.errors.push(found.error);out.status=found.error.code==='missing_artifact'?
        out.attempt?.status==='failed'?'failed':'missing':'failed';return out;}
      pointer=found.value;sweep=pointer?.sweep;
      check(Number.isInteger(sweep)&&sweep>=1&&sweep<=9999,'Unsupported active snapshot sweep.');
      activeRevision(selected,sweep,pointer);
      if(out.attempt?.sweep>sweep||out.attempt?.sweep===sweep&&out.attempt.status==='failed')
        out.warnings.push(`Newer or matching snapshot attempt ${out.attempt.sweep} is ${out.attempt.status}: ${out.attempt.reason??'reason unavailable'}.`);
      if(out.attempt?.status==='building'&&clockAt-Date.parse(out.attempt.at)>300000)
        out.warnings.push(`Snapshot attempt ${out.attempt.sweep} has exceeded the 240-second build budget; completion is unconfirmed.`);
    }
    if(!pointer){
      const found=await q.optionalJson(activePrefix(selected,sweep).slice(0,-1)+'.json');
      if(found.error){throw new DataError(found.error.code,found.error.message,found.error.locator);}
      pointer=found.value;
    }
    const prefix=activeRevision(selected,sweep,pointer);
    const p=await q.json(prefix+'summary.json');activeSummary(p,selected,sweep);
    const summaryEvidence=q.evidence.at(-1);
    if(a.as_of_ms !== undefined) check(p.cutoff_ms<=a.as_of_ms,
      'Pinned sweep was acquired after this cutoff.', 'unavailable_at_cursor');
    if(pointer) check(pointer.members['summary.json']===summaryEvidence.retrieved_sha256&&pointer.cutoff_ms===p.cutoff_ms,
      'Active pointer and summary disagree.','source_changed');
    const board=a.view==='qualified_ask_bid_v1'?'qualified_ask_bid_v1.csv':'leaderboard.csv';
    const text=await q.get(prefix+board), boardEvidence=q.evidence.at(-1);
    const expected=pointer?.members[board]??p.member_sha256[board];
    check(expected===boardEvidence.retrieved_sha256&&p.member_sha256[board]===boardEvidence.retrieved_sha256,
      'Active snapshot member digest mismatch.','source_changed');
    boardEvidence.checksum_verification=pointer?'matches_pointer_and_summary':'matches_snapshot_summary';
    const rows=csv(text), qualified=a.view==='qualified_ask_bid_v1';
    check(p.contracts===0&&rows.length===0||['symbol','underlying','option_type','rank','clean_rank','clean'].every(k=>rows.columns.includes(k)),
      'Missing active leaderboard columns.');
    check(rows.every(r=>CONTRACT.test(r.symbol)&&SYMBOL.test(r.underlying)&&['call','put'].includes(r.option_type)&&
      ['rank','clean_rank'].every(k=>r[k]===null||/^[1-9]\d*$/.test(r[k]??'')&&Number.isSafeInteger(Number(r[k])))&&['True','False'].includes(r.clean)&&
      (r.clean_rank===null||r.clean==='True')), 'Invalid active leaderboard rows.');
    if(qualified) check(rows.every(r=>r.qualified_first_ask_to_later_bid_status==='eligible'&&
      /^[1-9]\d*$/.test(r[QUALIFIED_RANK]??'')&&Number.isSafeInteger(Number(r[QUALIFIED_RANK]))&&
      r.qualified_first_ask_to_later_bid_pct!==null&&Number.isFinite(Number(r.qualified_first_ask_to_later_bid_pct))),
    'Invalid active qualified rows.');
    const field=qualified?QUALIFIED_RANK:a.view==='clean'?'clean_rank':'rank';
    const filtered=rows.filter(r=>r[field]!==null&&(!a.underlying||r.underlying===a.underlying)&&(!a.type||r.option_type===a.type));
    filtered.sort((x,y)=>Number(x[field])-Number(y[field]));
    const paged=slice(filtered,limit,offset);
    out.pagination=paged.pagination;out.actual={session:selected,selector:session,sweep,cutoff_ms:p.cutoff_ms,archive:prefix.slice(0,-1),
      built_at:pointer.built_at??null,
      final:false,return_policy:p.return_policy,qualified_policy:p.qualified_policy};
    const endpointField=qualified?'qualified_first_ask_to_later_bid_exit_ms':'mid_first_to_max_exit_ms';
    const evidenceAge=row=>{
      const endpoint=row[endpointField]===null?null:Number(row[endpointField]);
      const quoteAge=row.mid_first_to_max_exit_quote_age_s===null?null:Number(row.mid_first_to_max_exit_quote_age_s);
      const quote=qualified?Number(row.qualified_first_ask_to_later_bid_exit_bid_ms):
        quoteAge===null?null:Math.round(endpoint-quoteAge*1000);
      const basis=qualified?'qualified_first_ask_to_later_bid_exit_bid_ms':
        'mid_first_to_max_exit_ms minus mid_first_to_max_exit_quote_age_s';
      if(!Number.isSafeInteger(endpoint)||endpoint<p.session_open_ms||endpoint>p.cutoff_ms||
         !Number.isSafeInteger(quote)||quote<946684800000||quote>endpoint||
         !qualified&&(!Number.isFinite(quoteAge)||quoteAge<0))
        return {status:'unknown',observed_at:null,age_seconds:null,stale_after_seconds:600,basis,
          sample_at:Number.isSafeInteger(endpoint)?new Date(endpoint).toISOString():null};
      return {...age(new Date(quote).toISOString(),clockAt,600),basis,sample_at:new Date(endpoint).toISOString()};
    };
    out.rows=paged.rows.map(row=>({...row,ranked_evidence_freshness:evidenceAge(row),
      detail_reference:this.reference({dataset:'options_active',cursor:a.as_of_ms,p,row,prefix,board,
      actual:out.actual,evidence:[...q.evidence],digests:{summary:summaryEvidence.retrieved_sha256,board:boardEvidence.retrieved_sha256,
        manifest:p.member_sha256['manifest.jsonl']}})}));
    out.summary=p;out.coverage=p.assessment;
    // Status comes from elapsed publication time, never from sweep counts:
    // degraded provider latency can stretch a fixed number of sweeps far
    // past the expected interval.
    const expectedCadence=Number.isFinite(pointer?.expected_cadence_s)?pointer.expected_cadence_s:1800;
    const observed=Number.isFinite(pointer?.observed_cadence_s)?pointer.observed_cadence_s:null;
    const lateAfter=expectedCadence*1.5, reported=object(pointer?.cadence)?pointer.cadence:null;
    // A pinned sweep's own age says nothing about whether a newer snapshot
    // exists; overdue is judged only from the current latest pointer.
    let head=a.sweep===undefined?pointer:null;
    if(a.sweep!==undefined && a.as_of_ms===undefined){
      const latest=await q.optionalJson(`oa203/scanner/${selected}/inprogress/latest.json`);
      if(!latest.error&&object(latest.value)&&Number.isInteger(latest.value.sweep)&&Number.isFinite(Date.parse(latest.value.published_at)))head=latest.value;
      else if(latest.error&&latest.error.code!=='missing_artifact')out.errors.push(latest.error);
      out.newer_snapshot=head?{available:head.sweep>sweep,sweep:head.sweep,published_at:head.published_at}:
        {available:null,sweep:null,published_at:null,reason:'latest snapshot pointer unavailable'};
      if(head&&head.sweep>sweep)out.warnings.push(`Pinned sweep ${sweep} is not the newest snapshot; sweep ${head.sweep} was published at ${head.published_at}.`);
    }
    const sincePublication=head?.published_at?(clockAt-Date.parse(head.published_at))/1000:null;
    const overdue=clockAt<p.session_close_ms&&Number.isFinite(sincePublication)&&sincePublication>lateAfter;
    out.cadence={expected_seconds:expectedCadence,observed_seconds:observed,previous_publication:pointer?.previous_published_at??null,
      late_after_seconds:lateAfter,seconds_since_publication:Number.isFinite(sincePublication)?Math.round(sincePublication):null,
      since_publication_of:head?{sweep:head.sweep,basis:'latest snapshot pointer'}:null,
      status:overdue?'overdue':observed===null?'unknown':observed>lateAfter?'late':'on_time',
      basis:reported?.basis??'publication_interval',
      degradation:reported?{sweeps:reported.sweeps??null,max_sweep_seconds:reported.max_sweep_s??null,
        budget_exhausted_sweeps:reported.budget_exhausted_sweeps??null,server_errors:reported.server_errors??null,
        transport_errors:reported.transport_errors??null}:null};
    if(out.cadence.status==='late')out.warnings.push(`This snapshot refreshed ${observed}s after the previous one, beyond the ${expectedCadence}s expectation; provider degradation may have slowed collection.`);
    if(overdue)out.warnings.push(`No newer snapshot has been published for ${out.cadence.seconds_since_publication}s, beyond the ${expectedCadence}s expectation.`);
    out.session_window={open_ms:p.session_open_ms,close_ms:p.session_close_ms,meaning:'exchange boundaries; not acquisition timestamps'};
    out.producer_times={observation:null,acquisition_start:null,acquisition_end:new Date(p.cutoff_ms).toISOString(),
      publication:pointer?.published_at??null,built_at:pointer?.built_at??p.built_at??null};
    const cutoffAge=age(new Date(p.cutoff_ms).toISOString(),clockAt,3600);
    const rowAges=out.rows.map(row=>row.ranked_evidence_freshness);
    const knownAges=rowAges.map(x=>x.age_seconds).filter(Number.isFinite);
    out.freshness={...cutoffAge,
      status:cutoffAge.status==='stale'||rowAges.some(x=>x.status==='stale')?'stale':
        rowAges.some(x=>x.status==='unknown')?'unknown':cutoffAge.status,
      basis:'cutoff and displayed ranked quote endpoints are assessed separately',cutoff:cutoffAge,
      ranked_evidence:{basis:'actual quote timestamp at the ranked endpoint',stale_after_seconds:600,
        oldest_age_seconds:knownAges.length?Math.max(...knownAges):null,
        stale_rows:rowAges.filter(x=>x.status==='stale').length,unknown_rows:rowAges.filter(x=>x.status==='unknown').length}};
    out.active_state=clockAt>=p.session_close_ms?'closed_pending_final':'in_progress';
    out.status=out.active_state==='closed_pending_final'?'closed_pending_final':filtered.length?'in_progress':'empty';
    if(p.assessment.truncated_sweeps||Object.keys(p.assessment.chain_status_counts??{}).some(s=>s!=='ok')) out.status='partial';
    if(rowAges.some(x=>x.status==='stale'||x.status==='unknown')&&out.status==='in_progress')out.status='partial';
    out.warnings.push('These are sampled, non-simultaneous chain observations; quote comparisons do not establish fills.');
    if(cutoffAge.status==='stale')out.warnings.push('Latest active cutoff is older than the expected cadence.');
    if(rowAges.some(x=>x.status==='stale'))out.warnings.push('Displayed ranked quote evidence is older than two expected five-minute sweeps.');
    return out;
  }
  async window(a,q,out) {
    args(a,['window_id','kind','basis','underlying','type','limit','offset'],['window_id','kind']);
    const {limit,offset}=page(a);
    const basis=a.basis??'qualified';
    check(WINDOW_KINDS.includes(a.kind),'kind must be one of '+WINDOW_KINDS.join(', ')+'.','invalid_filter');
    check(WINDOW_ID[a.kind].test(a.window_id),'window_id does not match kind '+a.kind+'.','invalid_filter');
    check(WINDOW_BASES.includes(basis),'basis must be mid, exec, or qualified.','invalid_filter');
    if(a.underlying!==undefined) check(SYMBOL.test(a.underlying),'Invalid underlying.','invalid_filter');
    if(a.type!==undefined) check(['call','put'].includes(a.type),'Invalid option type.','invalid_filter');
    const span=windowSpan(a.kind,a.window_id);
    if(span) check(dates(span[0])&&dates(span[1])&&span[0]<span[1],'window entry day must precede its exit day.','invalid_filter');
    const prefix=`oa203/windows/${WINDOW_POLICY_VERSION}/${a.kind}/${a.window_id}/`;
    const p=await q.json(prefix+'summary.json');
    // A newer producer policy is a typed unsupported answer, not a failure:
    // the artifacts exist, this consumer does not understand them.
    if(object(p)&&p.policy?.version!==undefined&&p.policy.version!==WINDOW_POLICY_VERSION) {
      out.status='unsupported';out.summary=p;out.window_state={state:p.state??null,reasons:p.state_reasons??[]};
      out.warnings.push(`Window policy ${p.policy.version} is not supported by this consumer; supported: ${WINDOW_POLICY_VERSION}.`);
      out.actual={window_id:a.window_id,kind:a.kind,basis,state:p.state??null,policy:p.policy.version};
      return out;
    }
    if(object(p)&&typeof p.state==='string'&&!WINDOW_STATES.includes(p.state)) {
      out.status='unsupported';out.summary=p;out.window_state={state:p.state,reasons:p.state_reasons??[]};
      out.warnings.push(`Window state ${p.state} is not supported by this consumer; supported: ${WINDOW_STATES.join(', ')}.`);
      out.actual={window_id:a.window_id,kind:a.kind,basis,state:p.state,policy:p.policy?.version??null};
      return out;
    }
    windowSummary(p,a.kind,a.window_id);
    const days=p.sessions.map(s=>s.day);
    out.actual={window_id:a.window_id,kind:a.kind,basis,state:p.state,sessions:days,entry_sessions:p.entry_sessions,
      exit_session:p.exit_session,policy:WINDOW_POLICY_VERSION,generated_at:p.generated_at,
      summary_sha256:q.evidence.at(-1).retrieved_sha256};
    out.producer_times={observation:null,acquisition_start:null,acquisition_end:null,publication:null,generated_at:p.generated_at};
    out.session_window={open_ms:p.sessions[0].open_ms,close_ms:p.sessions.at(-1).close_ms,meaning:'exchange boundaries; not acquisition timestamps'};
    out.freshness={status:'historical',meaning:'Explicit selected window; generated_at is not a quote observation or publication time.'};
    out.summary=p;out.coverage=p.counts;out.window_state={state:p.state,reasons:p.state_reasons};
    out.warnings.push(...p.state_reasons,...p.labels);
    if(p.state==='in_progress') {
      // The exit is not invented: an in-progress window has no final ranking.
      out.rows=[];out.pagination={offset,limit,total:0,truncated:false,next_offset:null};
      out.status='in_progress';
      out.warnings.push('Window has no final exit; no ranking is served for an in-progress window.');
      return out;
    }
    const boardText=await q.get(prefix+'rankings.csv');
    const rows=csv(boardText);
    check(rows.columns.length===WINDOW_RANKING_COLUMNS.length && rows.columns.every((c,i)=>c===WINDOW_RANKING_COLUMNS[i]), 'Missing window rankings columns.');
    // Ranks are cut only after a full-universe ranking: per basis the file
    // must hold exactly ranks 1..shortlist, pct-descending, symbol tiebreak.
    for(const b of WINDOW_BASES) {
      const brow=rows.filter(r=>r.basis===b);
      check(brow.length===p.counts[b].shortlist, `Window rankings hold ${brow.length} ${b} rows, not the published shortlist ${p.counts[b].shortlist}.`);
      check(brow.every((r,i)=>Number(r.rank)===i+1), `Window ${b} ranks are not exactly 1..${p.counts[b].shortlist}.`);
      check(brow.every((r,i)=>i===0||Number(r.pct)<Number(brow[i-1].pct)||(Number(r.pct)===Number(brow[i-1].pct)&&r.symbol>brow[i-1].symbol)), `Window ${b} rows are not pct-descending with symbol tiebreak.`);
      if(p.counts[b].eligible===p.counts[b].shortlist)
        check(brow.filter(r=>Number(r.pct)<0).length===p.counts[b].negative, `Window ${b} negative count disagrees with its published count.`);
      else check(brow.filter(r=>Number(r.pct)<0).length<=p.counts[b].negative, `Window ${b} shortlist holds more negative rows than its published count.`);
    }
    check(rows.every(r=>WINDOW_BASES.includes(r.basis) && CONTRACT.test(r.symbol) && SYMBOL.test(r.underlying) &&
      ['call','put'].includes(r.option_type) && Number.isFinite(Number(r.strike)) && Number(r.strike)>0 &&
      dates(r.expiration) && Number.isFinite(Number(r.pct)) && Number.isFinite(Number(r.entry)) && Number(r.entry)>0 &&
      Number.isFinite(Number(r.exit)) && Number(r.exit)>0 && Number.isFinite(Number(r.abs_change_per_contract)) &&
      days.includes(r.entry_session) && days.includes(r.exit_session) && r.entry_session<=r.exit_session &&
      Math.abs(Number(r.pct)-(Number(r.exit)/Number(r.entry)-1))<2e-6), 'Invalid window rankings rows.');
    let basisOrder=0;
    check(rows.every(r=>{ const i=WINDOW_BASES.indexOf(r.basis); if(i<basisOrder) return false; basisOrder=i; return true; }), 'Window rankings are not grouped by basis.');
    const baseline=q.evidence.slice(-2).map(e=>e.retrieved_sha256);
    await q.get(prefix+'summary.json');await q.get(prefix+'rankings.csv');
    check(q.evidence.slice(-2).every((e,i)=>e.retrieved_sha256===baseline[i]),'Selected window changed during rankings read; retry this window.','source_changed');
    // The flat keys are written one at a time, so a concurrent republish
    // can leave a stable mixed revision on disk (new summary, old rankings
    // or vice versa) that the before/after check above cannot see: it only
    // detects changes DURING this read. The publisher writes latest.json
    // last with the member digests of the revision it just published, so a
    // present pointer pins this read to one revision; a mismatch is a
    // refusal, never a served mix. A window with no pointer keeps the old
    // behaviour (served, with a warning): the publisher always writes one,
    // so a missing pointer means a legacy or hand-placed window.
    const pointer=await q.optionalJson(prefix+'latest.json');
    if(pointer.value) {
      check(object(pointer.value) && pointer.value.schema_version===1 && pointer.value.kind===a.kind &&
        pointer.value.window_id===a.window_id && pointer.value.state===p.state && object(pointer.value.members) &&
        pointer.value.members['summary.json']===baseline[0] && pointer.value.members['rankings.csv']===baseline[1],
        'Published window pointer disagrees with the flat summary/rankings just read; the window is being republished.','source_changed');
    } else out.warnings.push('No published window pointer for this window; the flat members are not revision-pinned.');
    out.actual.rankings_sha256=baseline[1];
    out.warnings.push('Matching before/after summary/rankings digests detect observed rebuilds; the published pointer pins the read to one revision when present.');
    const filtered=rows.filter(r=>r.basis===basis && (!a.underlying||r.underlying===a.underlying) && (!a.type||r.option_type===a.type));
    filtered.sort((x,y)=>Number(x.rank)-Number(y.rank)); // producer rank, never re-score
    const paged=slice(filtered,limit,offset); out.pagination=paged.pagination;
    out.rows=paged.rows.map(row=>({...row,detail_reference:this.reference({dataset:'options_windows',p,row,prefix,board:'rankings.csv',basis,
      actual:out.actual,evidence:q.evidence.slice(-4),digests:{summary:baseline[0],rankings:baseline[1]}})}));
    out.status=p.state==='partial'?'partial':filtered.length?'available':'empty';return out;
  }
  async detail(a,q,out) {
    args(a,['reference','offset','limit'],['reference']); const {limit,offset}=page(a);
    check(typeof a.reference==='string' && new RegExp('^'+UUID+'$').test(a.reference),'Invalid detail reference.','invalid_reference');
    const entry=this.references.get(a.reference); check(entry && entry.expires>=this.now(),'Reference expired, was evicted by the count/byte limit, or is not from this server; repeat the originating query.','invalid_reference');
    const v=entry.value;
    if(v.cursor!==undefined) q.atCutoff(v.cursor);
    Object.assign(out,this.envelope(v.dataset,a,q));out.actual=v.actual;out.row=v.row;out.sources.push(...v.evidence.map(e => ({...e, retained:true})));out.status='available';
    if(v.dataset==='qqq_snapshot'||v.dataset==='spy_snapshot') {
      out.requested_underlying=v.underlying;
      out.producer_times.acquisition_end=v.p.timestamp;out.freshness={...age(v.p.timestamp,this.now()),basis:'collection_timestamp'};out.readings=this.readings(v.p.underlying_market);
      out.coverage={scope:'one cached row from the exact queried payload'};out.warnings.push('Detail uses retained queried JSON. The archived CSV has not been read or verified.');return out;
    }
    if(v.dataset==='cross_market_overview') {
      out.producer_times={observation:v.row.observed_at,observation_date:v.row.observation_date ?? null,observation_timezone:v.row.observation_timezone ?? null,
        collector_receipt:v.row.collector_received_at,acquisition_start:v.actual.collector_cycle_at,acquisition_end:v.row.acquired_at ?? null,publication:null,
        last_success_at:v.row.last_success_at ?? null,last_attempt_at:v.row.last_attempt_at ?? null};
      out.freshness={...v.row.freshness,note:'freshness as of the originating query; repeat market_overview for current age'};
      out.coverage={scope:'one retained row from the exact queried price strip'};
      out.warnings.push('Detail uses the retained queried row; a newer strip publication does not change it.');return out;
    }
    if(v.dataset==='scheduled_squeeze') {
      out.producer_times={observation:null,acquisition_start:v.p.started_at??null,acquisition_end:v.p.finished_at??null,publication:v.p.published_at??null};
      const state=formatSqueezeScheduleStatus(v.p,v.attempt,v.schedule,this.calendar,this.now());
      out.freshness={...age(v.p.finished_at,this.now()),status:state.state==='live'?'fresh':state.warning?.includes('calendar')||state.warning?.includes('coverage')?'unknown':'stale',schedule_status:state};
      if(state.warning)out.warnings.push(state.warning);out.warnings.push('Attempt/schedule evidence is retained from the originating query; use squeeze_results to refresh it.');
      const manifest=await q.json(v.p.archive+'/manifest.json');check(manifest.run_id===v.p.run_id && manifest.schema_version===1,'Archive manifest identity/schema mismatch.');
      check(v.p.manifest_sha256 && q.evidence.at(-1).retrieved_sha256===v.p.manifest_sha256,'Manifest digest differs from returned pointer.','source_changed');
      q.evidence.at(-1).checksum_verification='matches_queried_pointer_manifest_sha256';
      const results=await q.json(v.p.archive+'/results.json');const inputs=await q.json(v.p.archive+'/inputs.json');
      for(const name of ['results.json','inputs.json']) {
        const evidence=q.evidence.findLast(e=>e.locator===v.p.archive+'/'+name);
        check(manifest.files?.[name]?.sha256===evidence.retrieved_sha256,'Selected archive member digest mismatch.','source_changed');
        evidence.checksum_verification='matches_manifest_member_sha256';
      }
      check(Array.isArray(results)&&Array.isArray(inputs),'Unsupported squeeze detail shape.');
      out.result=results.find(r=>r.ticker===v.row.ticker)??null;out.inputs=inputs.find(r=>r.ticker===v.row.ticker)??null;out.manifest=manifest;out.coverage=manifest.coverage??v.p.coverage??null;
      if(!out.result||!out.inputs) {out.status='partial';out.warnings.push('Selected candidate detail is absent from the run archive.');}return out;
    }
    if(v.dataset==='options_windows') {
      const summary=await q.json(v.prefix+'summary.json');windowSummary(summary,v.p.kind,v.p.window_id);
      check(q.evidence.at(-1).retrieved_sha256===v.digests.summary,'Pinned window summary changed.','source_changed');
      await q.get(v.prefix+v.board);
      check(q.evidence.at(-1).retrieved_sha256===v.digests.rankings,'Pinned window rankings changed.','source_changed');
      const pointer=await q.optionalJson(v.prefix+'latest.json');
      out.summary=summary;out.coverage=summary.counts;out.window_state={state:summary.state,reasons:summary.state_reasons};
      out.producer_times={observation:null,acquisition_start:null,acquisition_end:null,publication:null,generated_at:summary.generated_at??null};
      out.freshness={status:'historical',basis:'exact pinned window summary and rankings'};
      out.session_window={open_ms:summary.sessions[0].open_ms,close_ms:summary.sessions.at(-1).close_ms};
      out.pagination={offset:0,limit:1,total:1,truncated:false,next_offset:null,scope:'one pinned contract path from paths.csv.gz'};
      out.warnings.push(...(summary.labels??[]));
      // Paths are consistency-verified against the pinned ranking row, not
      // digest-pinned to a prior paths read: no prior paths read exists and
      // the summary carries no member digests. A missing paths file leaves
      // the pinned ranking standing with an explicit warning; a mismatching
      // paths row fails closed.
      let pathsRow=null, pathsRead=false;
      try {
        const pathsText=await q.get(v.prefix+'paths.csv.gz',v.row.symbol);
        pathsRead=true;
        const pathsEvidence=q.evidence.at(-1);
        pathsRow=windowPathsRow(pathsText,v.row.symbol);
        if(pathsRow) {
          check(pathsRow.underlying===v.row.underlying && pathsRow.option_type===v.row.option_type &&
            Number(pathsRow.strike)===Number(v.row.strike) && pathsRow.expiration===v.row.expiration &&
            pathsRow[`${v.basis}_status`]==='eligible' && Number(pathsRow[`${v.basis}_pct`])===Number(v.row.pct) &&
            Number(pathsRow[`${v.basis}_entry`])===Number(v.row.entry) &&
            Number(pathsRow[`${v.basis}_exit`])===Number(v.row.exit) &&
            pathsRow[`${v.basis}_entry_session`]===v.row.entry_session &&
            pathsRow[`${v.basis}_exit_session`]===v.row.exit_session &&
            (pathsRow.contract_exclusions===null||pathsRow.contract_exclusions===''),
            'Window paths row disagrees with the pinned ranking row.','source_changed');
          const entryMs=Number(pathsRow[`${v.basis}_entry_ms`]), exitMs=Number(pathsRow[`${v.basis}_exit_ms`]);
          const entrySession=summary.sessions.find(s=>s.day===v.row.entry_session);
          const exitSession=summary.sessions.find(s=>s.day===v.row.exit_session);
          check(Number.isSafeInteger(entryMs) && Number.isSafeInteger(exitMs) && exitMs>entryMs &&
            entrySession && entryMs>=entrySession.open_ms && entryMs<entrySession.close_ms &&
            exitSession && exitMs>=exitSession.open_ms && exitMs<exitSession.close_ms,
            'Window path endpoints fall outside their sessions or the exit does not follow the entry.');
          for(const [stamp,at] of [[`${v.basis}_entry_bid_ms`,entryMs],[`${v.basis}_entry_ask_ms`,entryMs],[`${v.basis}_exit_bid_ms`,exitMs],[`${v.basis}_exit_ask_ms`,exitMs]]) {
            const t=pathsRow[stamp];
            check(t===null||(Number.isSafeInteger(Number(t))&&Number(t)>=946684800000&&Number(t)<=at), 'Window path quote stamp is missing, future, or before 2000.');
          }
          for(const col of [`${v.basis}_entry_quote_age_s`,`${v.basis}_exit_quote_age_s`,`${v.basis}_entry_bid_quote_age_s`,`${v.basis}_entry_ask_quote_age_s`,`${v.basis}_exit_bid_quote_age_s`,`${v.basis}_exit_ask_quote_age_s`]) {
            const n=pathsRow[col];
            check(n===null||(Number.isFinite(Number(n))&&Number(n)>=0), 'Window path quote age is negative.');
          }
          check(Number.isSafeInteger(Number(pathsRow.contract_size)) && Number(pathsRow.contract_size)>0 &&
            Math.abs(Number(pathsRow[`${v.basis}_pct`])-(Number(pathsRow[`${v.basis}_exit`])/Number(pathsRow[`${v.basis}_entry`])-1))<2e-6 &&
            Math.abs(Number(pathsRow[`${v.basis}_abs_change_per_contract`])-(Number(pathsRow[`${v.basis}_exit`])-Number(pathsRow[`${v.basis}_entry`]))*Number(pathsRow.contract_size))<1e-3,
            'Window path arithmetic disagrees with its own endpoints.');
          if(pointer.value) {
            // The pointer pins the paths bytes to the published revision:
            // a paths file from another revision is refused even when its
            // row still matches the pinned ranking row.
            check(object(pointer.value.members) && pointer.value.members['paths.csv.gz']===pathsEvidence.retrieved_sha256,
              'Pinned window paths digest disagrees with the published pointer.','source_changed');
            pathsEvidence.checksum_verification='matches_published_pointer';
            out.paths={digest:pathsEvidence.retrieved_sha256,verified:'matches_published_pointer'};
          } else {
            pathsEvidence.checksum_verification='matches_pinned_ranking_row';
            out.paths={digest:pathsEvidence.retrieved_sha256,verified:'matches_pinned_ranking_row'};
          }
        }
      } catch(e) {
        if(e.code==='missing_artifact') {
          out.errors.push(errorRecord(e,v.prefix+'paths.csv.gz'));
          out.status='partial';
          out.warnings.push('Window paths file is unavailable; this detail is rankings-only.');
          out.path=null;
        } else throw e;
      }
      if(pathsRead && !pathsRow && out.status!=='partial') {
        out.status='partial';
        out.warnings.push('Contract path is absent from paths.csv.gz; the pinned ranking row stands without its full path.');
        out.path=null;
      }
      if(pathsRow) out.path=pathsRow;
      return out;
    }
    if(v.dataset==='options_active') {
      const summary=await q.json(v.prefix+'summary.json');activeSummary(summary,v.actual.session,v.actual.sweep);
      check(q.evidence.at(-1).retrieved_sha256===v.digests.summary,'Pinned active summary changed.','source_changed');
      await q.get(v.prefix+v.board);
      check(q.evidence.at(-1).retrieved_sha256===v.digests.board,'Pinned active board changed.','source_changed');
      const manifest=jsonl(await q.get(v.prefix+'manifest.jsonl'));
      check(q.evidence.at(-1).retrieved_sha256===v.digests.manifest,'Pinned active manifest changed.','source_changed');
      check(manifest.length===v.actual.sweep&&manifest.every((m,i)=>m.sweep===i+1&&
        m.file===`sweep_${String(m.sweep).padStart(4,'0')}.jsonl.gz`&&
        typeof m.sha256==='string'&&/^[0-9a-f]{64}$/.test(m.sha256)&&object(m.chains)&&
        Number.isSafeInteger(m.started_ms)&&Number.isSafeInteger(m.ended_ms)&&
        m.started_ms<=m.ended_ms&&m.ended_ms<=v.actual.cutoff_ms),
      'Invalid pinned active manifest.');
      const selected=manifest.filter(m=>Object.hasOwn(m.chains,v.row.underlying));
      const paged=slice(selected,Math.min(limit,ACTIVE_PATH_SWEEPS),offset);
      out.pagination={...paged.pagination,scope:'pinned chain sweeps; quote rows filtered to this contract'};
      const path=[];const sweeps=[];const retryOffsets=[];
      out.coverage=summary.assessment;out.summary=summary;out.active_state='in_progress';
      out.producer_times={observation:null,acquisition_start:null,acquisition_end:new Date(summary.cutoff_ms).toISOString(),
        publication:null,built_at:v.actual.built_at??summary.built_at??null};
      out.freshness={status:'historical',basis:'exact pinned active cutoff'};
      out.session_window={open_ms:summary.session_open_ms,close_ms:summary.session_close_ms};
      for(const [index,m] of paged.rows.entries()) {
        sweeps.push({...m,chains:{[v.row.underlying]:m.chains[v.row.underlying]}});
        const key=`oa203/scanner/${v.actual.session}/sweeps/${m.file}`;
        try {
          const rows=jsonl(await q.get(key),{predicate:r=>r.symbol===v.row.symbol,maxRows:LIMITS.rows,maxLineBytes:LIMITS.quote_line_bytes});
          check(q.evidence.at(-1).retrieved_sha256===m.sha256,'Pinned source sweep digest changed.','source_changed');
          q.evidence.at(-1).checksum_verification='matches_pinned_manifest_sweep_sha256';
          check(rows.every(r=>r.underlying===v.row.underlying&&r.sweep===m.sweep&&
            m.chains[v.row.underlying].status==='ok'&&Number.isSafeInteger(r.t)&&
            r.t>=m.started_ms&&r.t<=m.ended_ms&&r.t>=summary.session_open_ms&&
            r.t<Math.min(summary.cutoff_ms,summary.session_close_ms)&&
            ['bid_ms','ask_ms','spot_ms','trade_ms'].every(field=>r[field]==null||
              r[field]===0&&OPTIONAL_STAMP_VALUE[field]&&r[OPTIONAL_STAMP_VALUE[field]]==null||
              Number.isSafeInteger(r[field])&&r[field]>=946684800000&&r[field]<=r.t)&&
            ['call','put'].includes(r.type)&&[r.bid,r.ask].every(n=>n===null||
              typeof n==='number'&&Number.isFinite(n)&&n>=0)), 'Active quote row violates cutoff/session contract.');
          path.push(...rows.map(r=>({ ...r,
            ...Object.fromEntries(Object.entries(OPTIONAL_STAMP_VALUE)
              .filter(([stamp,value])=>r[stamp]===0&&r[value]==null).map(([stamp])=>[stamp,null])) })));
          if(!rows.length||m.truncated||m.chains[v.row.underlying].status!=='ok') {
            out.status='partial';out.warnings.push(`Sweep ${m.sweep} has incomplete selected-contract coverage.`);
          }
        } catch(e) {
          if(e.code==='source_changed')throw e;
          out.errors.push(errorRecord(e,key));out.status='partial';retryOffsets.push(offset+index);
        }
      }
      out.quote_path=path;out.sweeps=sweeps;out.pagination.retry_offsets=retryOffsets;
      if(out.status!=='partial')out.status='in_progress';
      out.warnings.push('Calculation cells and ranks are producer values from the exact immutable cutoff; no after-cutoff observations are used.');
      return out;
    }
    if(v.dataset==='nasdaq_snapshot') {
      out.producer_times={observation:null,acquisition_start:v.actual.as_of??null,acquisition_end:null,publication:null};
      out.freshness={...age(v.actual.as_of,this.now(),NASDAQ_STALE_SECONDS),basis:'collector receipt; hourly cadence',note:'freshness as of the originating query; repeat nasdaq_snapshot for current age'};
      out.coverage={scope:'one retained row from the exact queried snapshot'};
      out.warnings.push('Detail uses the retained queried row; a newer snapshot publication does not change it.');return out;
    }
    // OA-203 session files are mutable rebuild products. Detect change instead
    // of attaching a new quote path to an old ranking/reference.
    const summary=await q.json(v.prefix+'summary.json');returnSummary(summary,v.p.trade_date);
    await q.get(v.prefix+(v.board??'leaderboard.csv'));
    check(q.evidence.at(-2).retrieved_sha256===v.evidence[0].retrieved_sha256 && q.evidence.at(-1).retrieved_sha256===v.evidence[1].retrieved_sha256,'Session was rebuilt since query; repeat return_rankings.','source_changed');
    out.producer_times={observation:null,acquisition_start:null,acquisition_end:null,publication:null,finalized_at:summary.finalized_at??null};out.freshness={status:'historical'};
    out.session_window={open_ms:summary.session_open_ms??null,close_ms:summary.session_close_ms??null,meaning:'exchange boundaries; not acquisition timestamps'};
    out.coverage=summary.assessment;out.status=summary.assessment.status==='partial'||!summary.final?'partial':'available';out.summary=summary;
    out.warnings.push(...(summary.assessment.reasons||[]));if(!summary.final)out.warnings.push('Session is not final.');
    const manifest=jsonl(await q.get(v.prefix+'sweeps/manifest.jsonl'));
    check(manifest.every(m=>Number.isInteger(m.sweep)&&m.sweep>=0&&m.sweep<=9999&&object(m.chains)&&m.file===`sweep_${String(m.sweep).padStart(4,'0')}.jsonl.gz`) && new Set(manifest.map(m=>m.sweep)).size===manifest.length, 'Invalid sweep manifest.');
    const baseline=q.evidence.slice(-3).map(e=>e.retrieved_sha256);
    const manifests=manifest.filter(m=>Object.hasOwn(m.chains,v.row.underlying));
    const sweepLimit=Math.min(limit,LIMITS.path_sweeps); const paged=slice(manifests,sweepLimit,offset);
    out.pagination={...paged.pagination,scope:'chain sweep entries; quote rows are filtered to this contract'};
    out.quote_path=[];out.sweeps=paged.rows.map(m=>({...m,chains:{[v.row.underlying]:m.chains[v.row.underlying]}}));
    for(const m of paged.rows) {
      const key=v.prefix+`sweeps/sweep_${String(m.sweep).padStart(4,'0')}.jsonl.gz`;
      if(m.chains[v.row.underlying].status!=='ok'||m.truncated){out.status='partial';out.warnings.push(`Sweep ${m.sweep}: chain ${m.chains[v.row.underlying].status??'unknown'}${m.truncated?'; truncated':''}.`);}
      try {
        const rows=jsonl(await q.get(key), { predicate:r=>r.symbol===v.row.symbol, maxRows:LIMITS.rows, maxLineBytes:LIMITS.quote_line_bytes });
        check(rows.every(r=>r.underlying===v.row.underlying&&r.sweep===m.sweep&&Number.isSafeInteger(r.t)&&
          r.t>=summary.session_open_ms&&r.t<summary.session_close_ms&&['call','put'].includes(r.type)&&
          [r.bid,r.ask].every(n=>n===null||(typeof n==='number'&&Number.isFinite(n)&&n>=0))), 'Selected contract quote rows violate the sweep/session contract.');
        if(!rows.length){out.status='partial';out.warnings.push(`Sweep ${m.sweep}: selected contract has no quote rows.`);}
        out.quote_path.push(...rows);
      }
      catch(e){out.errors.push(errorRecord(e,key));out.status='partial';}
    }
    try {await q.get(v.prefix+'summary.json');await q.get(v.prefix+(v.board??'leaderboard.csv'));await q.get(v.prefix+'sweeps/manifest.jsonl');}
    catch(e){delete out.quote_path;delete out.sweeps;throw e;}
    if(!q.evidence.slice(-3).every((e,i)=>e.retrieved_sha256===baseline[i])){
      delete out.quote_path;delete out.sweeps;throw new DataError('source_changed','Session changed during detail read; repeat return_rankings.');
    }
    out.warnings.push('Quote path is bounded by sweep pagination; calculation inputs/flags in row are preserved producer CSV cells, not recomputed. Trade-bar raw detail is deferred.');
    out.warnings.push('Matching before/after digests detect observed session changes; the producer supplies no atomic immutable session revision or per-sweep checksums here.');
    return out;
  }
}
module.exports={Consumer, DATASETS, UNDERLYINGS, csv, jsonl, age, dates};


