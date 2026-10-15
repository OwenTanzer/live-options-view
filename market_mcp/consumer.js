'use strict';
const { randomUUID } = require('node:crypto');
const calendar = require('../docs/squeeze-calendar.json');
const { formatSqueezeScheduleStatus } = require('../docs/shared.js');
const { DataError, LIMITS, UUID, errorRecord } = require('./source');
const overview = require('./overview');

const DATASETS = ['qqq_snapshot', 'spy_snapshot', 'scheduled_squeeze', 'options_returns', 'cross_market_overview', 'options_active'];
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

const units = {
  spy_snapshot: null, // filled below from qqq_snapshot
  qqq_snapshot: { spot: 'USD/share', vwap: 'USD/share (snapshot-weighted approximation)', rvol: 'multiple', momentum_return_pct: 'percentage points', OpenInterest: 'contracts (lagged; zero and missing conflated)', Bid: 'USD/share', Ask: 'USD/share', Strike: 'USD/share' },
  scheduled_squeeze: { scores: 'experimental screening measurements, not probabilities', ranks: 'producer ordinal ranks', first_seen_at: 'publication write-attempt time' },
  options_returns: { '*_pct': 'fractional return (1 = 100%)', '*_entry, *_exit': 'USD/share', '*_ms, t, bid_ms, ask_ms': 'UTC epoch milliseconds', '*_abs_change_per_contract': 'USD/contract', '*_relative_spread': 'spread / midpoint', '*_size': 'contracts at quoted side', '*_entry_delay_s': 'seconds from first archived sample', rank: 'producer ordinal rank' },
};
units.spy_snapshot = units.qqq_snapshot;
units.options_active = units.options_returns;
units.cross_market_overview = overview.units;
const warnings = {
  cross_market_overview: overview.warnings,
  qqq_snapshot: ['Snapshot timestamp is collection time; retrieval is not observation. Publication time is unavailable.', 'VWAP is approximate; historical VWAP/RVOL are unavailable from snapshot CSVs.', 'OI is lagged and zero versus missing is conflated; Greeks and option quotes have no supplied observation timestamps.', 'Archive locator does not establish checksum verification. Shared PCR/max-pain/reference-skew publication remains owned by #107; merged #116 preserves Crassus lineage but does not publish these readings.'],
  scheduled_squeeze: ['Scores are experimental screens, not squeeze probabilities.', 'first_seen_at/is_new describe producer shortlist publication history; unknown values remain unknown.', 'Provider source times can be absent; acquisition and publication times are distinct.'],
  options_returns: ['Sampled returns miss between-sample extremes. Trade-bar backfill covers only already-ranked contracts.', 'Midpoint and ask-entry/bid-exit comparisons describe observations, not achievable fills.', 'Qualified ask-to-later-bid is a hindsight quote comparison; sizes and spreads do not establish obtainable fills.', 'All/clean coverage is the old top 200 all OR clean; qualified coverage is its separate full-universe top 200.', 'Session artifacts may be rebuilt in place; detail detects summary/shortlist changes and asks for a new query.'],
};
warnings.spy_snapshot = [...warnings.qqq_snapshot,
  'SPY RVOL reports insufficient_history/no_data until its own baseline has five completed collection sessions; QQQ baselines are never used for SPY.',
  'SPY is collected 0DTE only (no nearest-weekly archive). Live SPY collection must be observed before it is treated as supported in production.'];

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
    const dataset = ({market_context:marketDataset, squeeze_results:'scheduled_squeeze', return_rankings:'options_returns', active_rankings:'options_returns', market_overview:'cross_market_overview'})[name] || (name === 'discover_sources' ? 'source_catalog' : 'detail');
    let out = this.envelope(dataset, a, q);
    try {
      if (name === 'discover_sources') out = await this.discover(a, q, out);
      else if (name === 'market_context') out = await this.market(a, q, out);
      else if (name === 'squeeze_results') out = await this.squeeze(a, q, out);
      else if (name === 'return_rankings') out = await this.returns(a, q, out);
      else if (name === 'active_rankings') out = await this.active(a, q, out);
      else if (name === 'market_overview') out = await this.overview(a, q, out);
      else if (name === 'result_detail') out = await this.detail(a, q, out);
      else throw new DataError('invalid_filter', 'Unknown tool.');
    } catch (e) { out.errors.push(errorRecord(e)); out.status = e.code === 'missing_artifact' ? 'missing' : 'failed'; }
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
      ['cross_market_overview', overview.KEY]];
    const chainSymbol = id => id === 'qqq_snapshot' ? 'QQQ' : id === 'spy_snapshot' ? 'SPY' : null;
    out.capabilities = [];
    for (const [id, key] of probes) {
      const probe = key ? await q.optionalJson(key) : {value:null,error:null};
      if (key && !probe.error) try { if (chainSymbol(id)) snapshot(probe.value, chainSymbol(id)); else if (id === 'cross_market_overview') overview.validate(probe.value); else if (id === 'scheduled_squeeze') squeeze(probe.value, true); else returnSummary(probe.value, a.session); }
      catch (e) { probe.error = errorRecord(e, key); probe.value = null; }
      if (id === 'cross_market_overview') {
        const p = probe.value;
        const listed = p ? overview.rows(p, this.now()) : null;
        out.capabilities.push({ dataset: id, producer_schema: 'unversioned collector price strip (intraday/prices.json)',
          availability: probe.error ? (probe.error.code === 'missing_artifact' ? 'missing_publication' : 'failed') : 'available',
          actual: p ? { collector_cycle_at: p.timestamp, feed_stale: p.feed_stale } : null,
          freshness: p ? {...(Date.parse(p.timestamp) > this.now() ? {status:'invalid_time',observed_at:p.timestamp,age_seconds:null,stale_after_seconds:overview.STALE_SECONDS} : age(p.timestamp, this.now(), overview.STALE_SECONDS)),basis:'collector cycle time, not publication or quote observation'} : { status: 'unknown' },
          source_locator: key, error: probe.error, units: units[id], warnings: warnings[id],
          fields: ['label','id','asset_class','instrument_type','unit','source','source_symbol','price','price_basis','collector_received_at','observed_at','previous_close','change','freshness','gaps'],
          filters: ['asset_class', 'symbols', 'sort', 'limit', 'offset'], asset_classes: overview.ASSET_CLASSES,
          instruments: listed ? listed.known.map(r => ({ label:r.label, id:r.id, asset_class:r.asset_class, instrument_type:r.instrument_type, unit:r.unit, source:r.source, available:r.price !== null })) : [],
          configured_labels: Object.keys(overview.INSTRUMENTS),
          source_dependent_identities: [{label:'JPY/USD',source:'yfinance',id:'fx.jpyusd',instrument_type:'spot_fx'},{label:'JPY/USD',source:'last-known without retained origin',id:'unknown.jpyusd',instrument_type:'unknown'}],
          gaps: overview.GAPS, cadence: 'collector price loop about every 10 s during collector sessions; yfinance fills cached 60 s',
          coverage: p ? { strip_labels: Object.keys(p.prices).length, identified:listed.known.length, unmapped:listed.unmapped.length } : null, historical_index: 'not_supported' });
        continue;
      }
      const chain = chainSymbol(id);
      out.capabilities.push({ dataset: id, ...(chain ? {underlying: chain} : {}), producer_schema: chain ? 'unversioned collector snapshot (symbol-labeled since #121)' : id === 'scheduled_squeeze' ? 'schema_version=1/display_schema_version=1' : 'oa203-returns-v1',
        availability: probe.error ? (probe.error.code === 'missing_artifact' ? 'missing_publication' : 'failed') : key ? 'available' : 'explicit_session_required',
        actual:probe.value ? chain?{underlying:chain,snapshot_timestamp:probe.value.timestamp,snapshot_key:probe.value.snapshot_key??null,session:probe.value.date}:id==='scheduled_squeeze'?{run_id:probe.value.run_id,archive:probe.value.archive,session:probe.value.session_date??null}:{session:probe.value.trade_date,final:probe.value.final}:null,
        freshness: probe.value ? chain ? age(probe.value.timestamp,this.now()) : id === 'scheduled_squeeze' ? {...age(probe.value.finished_at,this.now()),schedule_confirmation:'query squeeze_results for attempt/schedule/calendar status'} : {status:'historical'} : {status:'unknown'},
        source_locator: key, error: probe.error, units: units[id], warnings: warnings[id],
        fields: chain ? ['symbol','timestamp','snapshot_key','underlying_market','rows'] : id === 'scheduled_squeeze' ? ['candidates','coverage','latest-attempt','latest-schedule','manifest','inputs','results'] : ['summary','universe','leaderboard','qualified_ask_bid_v1','selected_contract_quote_path'],
        filters: chain ? ['underlying','expiry','type','strike','contract','limit','offset'] : id === 'scheduled_squeeze' ? ['limit','offset'] : ['session','underlying','type','view','limit','offset'],
        cadence: chain ? 'collector session cycles; field timestamps govern age' : id === 'scheduled_squeeze' ? '09:00 and 12:00 America/New_York exchange-session calendar' : 'selected finalized session; about five-minute samples',
        coverage: probe.value?.coverage || probe.value?.assessment || (chain ? `latest ${chain} 0DTE snapshot only` : null), historical_index: 'not_supported' });
    }
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
    out.readings = object(m) ? {...m, field_freshness: {spot:age(m.spot_ts,this.now()),vwap:age(m.vwap_ts,this.now()), session_volume:age(m.session_volume_ts,this.now()),
      rvol:{...age(m.session_volume_ts,this.now()),producer_status:m.rvol?.status ?? 'missing'},momentum:{...age(m.momentum?.spot_observed_at,this.now()),producer_status:m.momentum?.status ?? 'missing'}}} : null;
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
  async overview(a, q, out) {
    args(a, ['asset_class','symbols','sort','limit','offset']); const {limit,offset} = page(a);
    if (a.asset_class !== undefined) check(overview.ASSET_CLASSES.includes(a.asset_class), `asset_class must be one of ${overview.ASSET_CLASSES.join(', ')}.`, 'invalid_filter');
    if (a.symbols !== undefined) check(Array.isArray(a.symbols) && a.symbols.length >= 1 && a.symbols.length <= LIMITS.rows && a.symbols.every(s => typeof s === 'string' && overview.SELECTOR.test(s)), `symbols must be 1..${LIMITS.rows} strip labels or instrument ids.`, 'invalid_filter');
    const sort = a.sort ?? 'strip'; check(overview.SORTS.includes(sort), `sort must be one of ${overview.SORTS.join(', ')}.`, 'invalid_filter');
    const p = await q.json(overview.KEY); overview.validate(p);
    const now = this.now();
    out.actual = { collector_cycle_at: p.timestamp, snapshot_time: p.snapshot_time ?? null, feed_stale: p.feed_stale, payload_sha256: q.evidence.at(-1).retrieved_sha256 };
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
    const gapIds = new Set(overview.GAPS.map(g => g.id));
    out.gaps = overview.GAPS.filter(g => (!a.asset_class || g.asset_class === a.asset_class) && (!wanted || wanted.has(g.id)));
    out.unmatched_symbols = wanted ? [...wanted].filter(s => !known.some(r => r.label === s || r.id === s) && !gapIds.has(s)).map(s => ({ symbol: s, status: 'not_in_price_strip' })) : [];
    out.unmapped_labels = unmapped;
    out.coverage = { strip_labels: Object.keys(p.prices).length, identified: known.length, matched_rows: filtered.length, named_gaps: overview.GAPS.length, matching_gaps: out.gaps.length,
      live_rows: filtered.filter(r => r.freshness.live).length, unavailable_changes: filtered.filter(r => r.change.status !== 'ok').length };
    if (p.feed_stale) out.warnings.push('Producer marked the DXLink feed stale when this strip was written.');
    if (unmapped.length) out.warnings.push(`Strip labels without a reviewed identity are withheld: ${unmapped.join(', ')}.`);
    out.status = out.coverage.unavailable_changes || out.coverage.live_rows < filtered.length || out.gaps.length || unmapped.length ? 'partial' : filtered.length ? 'available' : 'empty';
    return out;
  }
  async squeeze(a, q, out) {
    args(a,['limit','offset']); const {limit,offset} = page(a);
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
    if (!p) { out.status = docs[0].error && docs[0].error.code!=='missing_artifact' ? 'failed' : attempt ? ['partial','failed'].includes(attempt.status) ? attempt.status : 'missing' : docs[0].error?.code === 'missing_artifact' ? 'missing' : 'failed'; return out; }
    out.actual = {run_id:p.run_id,session:p.session_date ?? null,archive:p.archive,scoring_version:p.scoring_version,manifest_sha256:p.manifest_sha256 ?? null};
    out.producer_times = {observation:null,acquisition_start:p.started_at ?? null,acquisition_end:p.finished_at ?? null,publication:p.published_at ?? null};
    out.coverage = p.coverage ?? null;
    const paged = slice(p.candidates,limit,offset); out.pagination=paged.pagination;
    out.rows=paged.rows.map(row=>({...row,detail_reference:this.reference({dataset:'scheduled_squeeze',p,row,attempt,schedule,actual:out.actual,evidence:[...q.evidence]})}));
    out.status=out.errors.length ? 'partial' : p.candidates.length ? 'available' : 'empty'; return out;
  }
  async returns(a,q,out) {
    args(a,['session','underlying','type','view','limit','offset'],['session']); const {limit,offset}=page(a);
    check(dates(a.session),'Select a real YYYY-MM-DD session (2020..2100).','invalid_filter');
    if(a.underlying!==undefined) check(SYMBOL.test(a.underlying),'Invalid underlying.','invalid_filter');
    if(a.type!==undefined) check(['call','put'].includes(a.type),'Invalid option type.','invalid_filter');
    check(['all','clean','qualified_ask_bid_v1'].includes(a.view??'all'),'view must be all, clean, or qualified_ask_bid_v1.','invalid_filter');
    const prefix=`oa203/scanner/${a.session}/`; const p=await q.json(prefix+'summary.json'); returnSummary(p,a.session);
    out.actual={session:a.session,final:p.final,return_policy:p.return_policy,qualified_policy:p.qualified_policy??null};
    out.producer_times={observation:null,acquisition_start:null,acquisition_end:null,publication:null,finalized_at:p.finalized_at ?? null};
    out.session_window={open_ms:p.session_open_ms??null,close_ms:p.session_close_ms??null,meaning:'exchange boundaries; not acquisition timestamps'};
    out.freshness={status:'historical',meaning:'Explicit selected session; finalized_at is not a quote observation or publication time.'};
    out.summary=p; out.coverage=p.assessment; out.warnings.push(...(p.assessment.reasons||[]));
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
    out.rows=paged.rows.map(row=>({...row,detail_reference:this.reference({dataset:'options_returns',p,row,prefix,board,actual:out.actual,evidence:q.evidence.slice(-4)})}));
    out.status=p.assessment.status==='partial'||!p.final?'partial':filtered.length?'available':'empty';return out;
  }
  async active(a,q,out) {
    args(a,['session','sweep','underlying','type','view','limit','offset']);
    const {limit,offset}=page(a);
    const session=a.session??'current';
    check(session==='current'||dates(session),'session must be current or one valid date.','invalid_filter');
    if(a.sweep!==undefined) check(Number.isInteger(a.sweep)&&a.sweep>=1&&a.sweep<=9999,'sweep must be 1..9999.','invalid_filter');
    if(a.underlying!==undefined) check(SYMBOL.test(a.underlying),'Invalid underlying.','invalid_filter');
    if(a.type!==undefined) check(['call','put'].includes(a.type),'Invalid option type.','invalid_filter');
    check(['all','clean','qualified_ask_bid_v1'].includes(a.view??'all'),'Invalid active view.','invalid_filter');
    const selected=session==='current'?exchangeDate(this.now()):session;
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
      if(open!==null&&this.now()<open){out.status='before_open';out.session_window={open_ms:open,close_ms:null};return out;}
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
      if(out.attempt?.status==='building'&&this.now()-Date.parse(out.attempt.at)>300000)
        out.warnings.push(`Snapshot attempt ${out.attempt.sweep} has exceeded the 240-second build budget; completion is unconfirmed.`);
    }
    if(!pointer){
      const found=await q.optionalJson(activePrefix(selected,sweep).slice(0,-1)+'.json');
      if(found.error){out.errors.push(found.error);out.status=found.error.code==='missing_artifact'?'missing':'failed';return out;}
      pointer=found.value;
    }
    const prefix=activeRevision(selected,sweep,pointer);
    const p=await q.json(prefix+'summary.json');activeSummary(p,selected,sweep);
    const summaryEvidence=q.evidence.at(-1);
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
      return {...age(new Date(quote).toISOString(),this.now(),600),basis,sample_at:new Date(endpoint).toISOString()};
    };
    out.rows=paged.rows.map(row=>({...row,ranked_evidence_freshness:evidenceAge(row),
      detail_reference:this.reference({dataset:'options_active',p,row,prefix,board,
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
    if(a.sweep!==undefined){
      const latest=await q.optionalJson(`oa203/scanner/${selected}/inprogress/latest.json`);
      if(!latest.error&&object(latest.value)&&Number.isInteger(latest.value.sweep)&&Number.isFinite(Date.parse(latest.value.published_at)))head=latest.value;
      else if(latest.error&&latest.error.code!=='missing_artifact')out.errors.push(latest.error);
      out.newer_snapshot=head?{available:head.sweep>sweep,sweep:head.sweep,published_at:head.published_at}:
        {available:null,sweep:null,published_at:null,reason:'latest snapshot pointer unavailable'};
      if(head&&head.sweep>sweep)out.warnings.push(`Pinned sweep ${sweep} is not the newest snapshot; sweep ${head.sweep} was published at ${head.published_at}.`);
    }
    const sincePublication=head?.published_at?(this.now()-Date.parse(head.published_at))/1000:null;
    const overdue=this.now()<p.session_close_ms&&Number.isFinite(sincePublication)&&sincePublication>lateAfter;
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
    const cutoffAge=age(new Date(p.cutoff_ms).toISOString(),this.now(),3600);
    const rowAges=out.rows.map(row=>row.ranked_evidence_freshness);
    const knownAges=rowAges.map(x=>x.age_seconds).filter(Number.isFinite);
    out.freshness={...cutoffAge,
      status:cutoffAge.status==='stale'||rowAges.some(x=>x.status==='stale')?'stale':
        rowAges.some(x=>x.status==='unknown')?'unknown':cutoffAge.status,
      basis:'cutoff and displayed ranked quote endpoints are assessed separately',cutoff:cutoffAge,
      ranked_evidence:{basis:'actual quote timestamp at the ranked endpoint',stale_after_seconds:600,
        oldest_age_seconds:knownAges.length?Math.max(...knownAges):null,
        stale_rows:rowAges.filter(x=>x.status==='stale').length,unknown_rows:rowAges.filter(x=>x.status==='unknown').length}};
    out.active_state=this.now()>=p.session_close_ms?'closed_pending_final':'in_progress';
    out.status=out.active_state==='closed_pending_final'?'closed_pending_final':filtered.length?'in_progress':'empty';
    if(p.assessment.truncated_sweeps||Object.keys(p.assessment.chain_status_counts??{}).some(s=>s!=='ok')) out.status='partial';
    if(rowAges.some(x=>x.status==='stale'||x.status==='unknown')&&out.status==='in_progress')out.status='partial';
    out.warnings.push('These are sampled, non-simultaneous chain observations; quote comparisons do not establish fills.');
    if(cutoffAge.status==='stale')out.warnings.push('Latest active cutoff is older than the expected cadence.');
    if(rowAges.some(x=>x.status==='stale'))out.warnings.push('Displayed ranked quote evidence is older than two expected five-minute sweeps.');
    return out;
  }
  async detail(a,q,out) {
    args(a,['reference','offset','limit'],['reference']); const {limit,offset}=page(a);
    check(typeof a.reference==='string' && new RegExp('^'+UUID+'$').test(a.reference),'Invalid detail reference.','invalid_reference');
    const entry=this.references.get(a.reference); check(entry && entry.expires>=this.now(),'Reference expired, was evicted by the count/byte limit, or is not from this server; repeat the originating query.','invalid_reference');
    const v=entry.value; Object.assign(out,this.envelope(v.dataset,a,q));out.actual=v.actual;out.row=v.row;out.sources.push(...v.evidence.map(e => ({...e, retained:true})));out.status='available';
    if(v.dataset==='qqq_snapshot'||v.dataset==='spy_snapshot') {
      out.requested_underlying=v.underlying;
      out.producer_times.acquisition_end=v.p.timestamp;out.freshness={...age(v.p.timestamp,this.now()),basis:'collection_timestamp'};out.readings=v.p.underlying_market ?? null;
      out.coverage={scope:'one cached row from the exact queried payload'};out.warnings.push('Detail uses retained queried JSON. The archived CSV has not been read or verified.');return out;
    }
    if(v.dataset==='cross_market_overview') {
      out.producer_times={observation:null,collector_receipt:v.row.collector_received_at,acquisition_start:v.actual.collector_cycle_at,acquisition_end:null,publication:null};
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


