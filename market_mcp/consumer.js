'use strict';
const { randomUUID } = require('node:crypto');
const calendar = require('../docs/squeeze-calendar.json');
const { formatSqueezeScheduleStatus } = require('../docs/shared.js');
const { DataError, LIMITS, UUID, errorRecord } = require('./source');

const DATASETS = ['qqq_snapshot', 'scheduled_squeeze', 'options_returns'];
const SYMBOL = /^[A-Z][A-Z0-9.-]{0,9}$/;
const CONTRACT = /^[A-Z0-9.]{1,10}\d{6}[CP]\d{8}$/;
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
    const c = text[i];
    if (c === '"') { if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted; }
    else if (!quoted && (c === ',' || c === '\n')) {
      row.push(cell.replace(/\r$/, '')); cell = '';
      if (c === '\n') { records.push(row); row = []; }
    } else cell += c;
  }
  check(!quoted, 'Unclosed CSV quote.');
  if (cell || row.length) records.push([...row, cell.replace(/\r$/, '')]);
  const headers = records.shift() || [];
  check(headers.length > 0 && new Set(headers).size === headers.length && !headers.some(h => ['__proto__', 'constructor', 'prototype'].includes(h)), 'Invalid CSV header.');
  return Object.assign(records.filter(r => r.some(c => c !== '')).map(r => {
    check(r.length === headers.length, 'CSV column count mismatch.');
    return Object.fromEntries(headers.map((h, i) => [h, r[i] === '' ? null : r[i]]));
  }),{columns:headers});
}
function jsonl(text) { return text.split(/\r?\n/).filter(s => s.trim()).map(s => {
  try { return JSON.parse(s); } catch { throw new DataError('incompatible_schema', 'Malformed JSONL artifact.'); }
}); }
function snapshot(p) {
  check(object(p) && typeof p.timestamp === 'string' && dates(p.date) && Array.isArray(p.rows), 'Unsupported snapshot contract.');
  check(p.schema_version === undefined || p.schema_version === 1, 'Unsupported snapshot schema version.');
  check(p.rows.length <= 10000 && p.rows.every(r => object(r) && CONTRACT.test(r.OptionSymbol) && ['call', 'put'].includes(r.Type) && dates(r.Expiration) && typeof r.Strike === 'number' && Number.isFinite(r.Strike)), 'Invalid snapshot rows.');
  check(p.snapshot_key == null || /^intraday\/\d{8}\/snapshot_\d{6,12}\.csv$/.test(p.snapshot_key), 'Invalid snapshot locator.');
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

const units = {
  qqq_snapshot: { spot: 'USD/share', vwap: 'USD/share (snapshot-weighted approximation)', rvol: 'multiple', momentum_return_pct: 'percentage points', OpenInterest: 'contracts (lagged; zero and missing conflated)', Bid: 'USD/share', Ask: 'USD/share', Strike: 'USD/share' },
  scheduled_squeeze: { scores: 'experimental screening measurements, not probabilities', ranks: 'producer ordinal ranks', first_seen_at: 'publication write-attempt time' },
  options_returns: { '*_pct': 'fractional return (1 = 100%)', '*_entry, *_exit': 'USD/share', '*_ms, t, bid_ms, ask_ms': 'UTC epoch milliseconds', '*_abs_change_per_contract': 'USD/contract', rank: 'producer ordinal rank' },
};
const warnings = {
  qqq_snapshot: ['Snapshot timestamp is collection time; retrieval is not observation. Publication time is unavailable.', 'VWAP is approximate; historical VWAP/RVOL are unavailable from snapshot CSVs.', 'OI is lagged and zero versus missing is conflated; Greeks and option quotes have no supplied observation timestamps.', 'Archive locator does not establish checksum verification. Shared PCR/max-pain/reference-skew publication remains owned by #107; #116 is unmerged.'],
  scheduled_squeeze: ['Scores are experimental screens, not squeeze probabilities.', 'first_seen_at/is_new describe producer shortlist publication history; unknown values remain unknown.', 'Provider source times can be absent; acquisition and publication times are distinct.'],
  options_returns: ['Sampled returns miss between-sample extremes. Trade-bar backfill covers only already-ranked contracts.', 'Midpoint and ask-entry/bid-exit comparisons describe observations, not achievable fills.', 'Coverage is the published leaderboard subset (top 200 all OR clean); absent filtered rows do not prove no sampled contracts exist.', 'Session artifacts may be rebuilt in place; detail detects summary/leaderboard changes and asks for a new query.'],
};

class Consumer {
  constructor(source, { now = () => Date.now(), scheduleCalendar = calendar } = {}) { this.source = source; this.now = now; this.calendar = scheduleCalendar; this.references = new Map(); }
  reference(value) {
    for (const [key, entry] of this.references) if (entry.expires < this.now()) this.references.delete(key);
    while (this.references.size >= LIMITS.references) this.references.delete(this.references.keys().next().value);
    const id = randomUUID(); this.references.set(id, { value, expires: this.now() + LIMITS.reference_ttl_ms }); return id;
  }
  envelope(dataset, requested, q) { return { schema: 'options-view-mcp.v1', dataset, requested_filters: requested, actual: null,
    source_mode: this.source.mode, retrieval_time: new Date(this.now()).toISOString(), producer_times: { observation: null, acquisition_start: null, acquisition_end: null, publication: null },
    status: 'missing', freshness: { status: 'unknown' }, coverage: null, units: units[dataset] || {}, warnings: [...(warnings[dataset] || [])], sources: q.evidence, pagination: null, errors: [] }; }
  async call(name, a = {}) {
    const q = this.source.query(); const dataset = ({market_context:'qqq_snapshot', squeeze_results:'scheduled_squeeze', return_rankings:'options_returns'})[name] || (name === 'discover_sources' ? 'source_catalog' : 'detail');
    let out = this.envelope(dataset, a, q);
    try {
      if (name === 'discover_sources') out = await this.discover(a, q, out);
      else if (name === 'market_context') out = await this.market(a, q, out);
      else if (name === 'squeeze_results') out = await this.squeeze(a, q, out);
      else if (name === 'return_rankings') out = await this.returns(a, q, out);
      else if (name === 'result_detail') out = await this.detail(a, q, out);
      else throw new DataError('invalid_filter', 'Unknown tool.');
    } catch (e) { out.errors.push(errorRecord(e)); out.status = e.code === 'missing_artifact' ? 'missing' : 'failed'; }
    if (Buffer.byteLength(JSON.stringify(out)) > LIMITS.output_bytes) {
      out = this.envelope(out.dataset, a, {evidence: []}); out.status = 'failed';
      out.errors.push(errorRecord(new DataError('excessive_response', 'Output limit exceeded; narrow filters or reduce limit.')));
    }
    return out;
  }
  async discover(a, q, out) {
    args(a, ['session']); if (a.session !== undefined) check(dates(a.session), 'Select a real YYYY-MM-DD session (2020..2100).', 'invalid_filter');
    const probes = [['qqq_snapshot', 'intraday/latest.json'], ['scheduled_squeeze', 'squeeze-scanner/v1/scheduled/latest.json'],
      ['options_returns', a.session ? `oa203/scanner/${a.session}/summary.json` : null]];
    out.capabilities = [];
    for (const [id, key] of probes) {
      const probe = key ? await q.optionalJson(key) : {value:null,error:null};
      if (key && !probe.error) try { if (id === 'qqq_snapshot') snapshot(probe.value); else if (id === 'scheduled_squeeze') squeeze(probe.value, true); else returnSummary(probe.value, a.session); }
      catch (e) { probe.error = errorRecord(e, key); probe.value = null; }
      out.capabilities.push({ dataset: id, producer_schema: id === 'qqq_snapshot' ? 'unversioned collector snapshot (master fbbc345)' : id === 'scheduled_squeeze' ? 'schema_version=1/display_schema_version=1' : 'oa203-returns-v1',
        availability: probe.error ? (probe.error.code === 'missing_artifact' ? 'missing_publication' : 'failed') : key ? 'available' : 'explicit_session_required',
        actual:probe.value ? id==='qqq_snapshot'?{snapshot_timestamp:probe.value.timestamp,snapshot_key:probe.value.snapshot_key??null,session:probe.value.date}:id==='scheduled_squeeze'?{run_id:probe.value.run_id,archive:probe.value.archive,session:probe.value.session_date??null}:{session:probe.value.trade_date,final:probe.value.final}:null,
        freshness: probe.value ? id === 'qqq_snapshot' ? age(probe.value.timestamp,this.now()) : id === 'scheduled_squeeze' ? {...age(probe.value.finished_at,this.now()),schedule_confirmation:'query squeeze_results for attempt/schedule/calendar status'} : {status:'historical'} : {status:'unknown'},
        source_locator: key, error: probe.error, units: units[id], warnings: warnings[id],
        fields: id === 'qqq_snapshot' ? ['timestamp','snapshot_key','underlying_market','rows'] : id === 'scheduled_squeeze' ? ['candidates','coverage','latest-attempt','latest-schedule','manifest','inputs','results'] : ['summary','universe','leaderboard','selected_contract_quote_path'],
        filters: id === 'qqq_snapshot' ? ['expiry','type','strike','contract','limit','offset'] : id === 'scheduled_squeeze' ? ['limit','offset'] : ['session','underlying','type','view','limit','offset'],
        cadence: id === 'qqq_snapshot' ? 'collector session cycles; field timestamps govern age' : id === 'scheduled_squeeze' ? '09:00 and 12:00 America/New_York exchange-session calendar' : 'selected finalized session; about five-minute samples',
        coverage: probe.value?.coverage || probe.value?.assessment || (id === 'qqq_snapshot' ? 'latest QQQ 0DTE snapshot only' : null), historical_index: 'not_supported' });
    }
    out.unsupported = ['shared_PCR','shared_max_pain','shared_reference_OI_skew','historical_VWAP_RVOL','account_trading_momentum','buy_only_Black_Scholes','private_accounts_positions_orders'];
    out.limits = LIMITS; out.status = out.capabilities.some(c => ['missing_publication','failed'].includes(c.availability)) ? 'partial' : 'available'; return out;
  }
  async market(a, q, out) {
    args(a, ['expiry','type','strike','contract','limit','offset']); const {limit,offset} = page(a);
    if (a.expiry !== undefined) check(dates(a.expiry), 'Invalid expiry.', 'invalid_filter');
    if (a.type !== undefined) check(['call','put'].includes(a.type), 'Invalid option type.', 'invalid_filter');
    if (a.contract !== undefined) check(CONTRACT.test(a.contract), 'Invalid contract.', 'invalid_filter');
    if (a.strike !== undefined) check(typeof a.strike === 'number' && Number.isFinite(a.strike) && a.strike > 0 && a.strike <= 100000, 'Invalid strike.', 'invalid_filter');
    const p = await q.json('intraday/latest.json'); snapshot(p);
    out.actual = { snapshot_timestamp:p.timestamp, snapshot_key:p.snapshot_key ?? null, session:p.date, payload_sha256:q.evidence.at(-1).retrieved_sha256 };
    out.producer_times.acquisition_end = p.timestamp; out.freshness = {...age(p.timestamp, this.now()),basis:'collection_timestamp'};
    const m = p.underlying_market;
    out.readings = object(m) ? {...m, field_freshness: {spot:age(m.spot_ts,this.now()),vwap:age(m.vwap_ts,this.now()), session_volume:age(m.session_volume_ts,this.now()),
      rvol:{...age(m.session_volume_ts,this.now()),producer_status:m.rvol?.status ?? 'missing'},momentum:{...age(m.momentum?.spot_observed_at,this.now()),producer_status:m.momentum?.status ?? 'missing'}}} : null;
    if (!object(m)) out.warnings.push('underlying_market is absent; derived readings are unavailable.');
    const rows = p.rows.filter(r => (!a.expiry || r.Expiration === a.expiry) && (!a.type || r.Type === a.type) && (a.strike === undefined || r.Strike === a.strike) && (!a.contract || r.OptionSymbol === a.contract));
    const paged = slice(rows,limit,offset); out.pagination = paged.pagination;
    out.rows = paged.rows.map(row => ({...row, detail_reference:this.reference({dataset:'qqq_snapshot',p,row,actual:out.actual,evidence:[...q.evidence]})}));
    out.coverage = {source_rows:p.rows.length, matched_rows:rows.length, expiration:p.expiration ?? null};
    const incomplete = !object(m) || m.spot == null || m.vwap == null || m.vwap_partial_session || m.rvol?.status !== 'ok' || m.momentum?.status !== 'ok';
    if(incomplete && object(m)) out.warnings.push(`Partial readings: VWAP ${m.vwap==null?'missing':m.vwap_partial_session?'partial_session':'available'}; RVOL ${m.rvol?.status??'missing'}; reference momentum ${m.momentum?.status??'missing'}.`);
    out.status = incomplete ? 'partial' : p.rows.length ? 'available' : 'empty';
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
    check(['all','clean'].includes(a.view??'all'),'view must be all or clean.','invalid_filter');
    const prefix=`oa203/scanner/${a.session}/`; const p=await q.json(prefix+'summary.json'); returnSummary(p,a.session);
    out.actual={session:a.session,final:p.final,return_policy:p.return_policy};
    out.producer_times={observation:null,acquisition_start:null,acquisition_end:null,publication:null,finalized_at:p.finalized_at ?? null};
    out.session_window={open_ms:p.session_open_ms??null,close_ms:p.session_close_ms??null,meaning:'exchange boundaries; not acquisition timestamps'};
    out.freshness={status:'historical',meaning:'Explicit selected session; finalized_at is not a quote observation or publication time.'};
    out.summary=p; out.coverage=p.assessment; out.warnings.push(...(p.assessment.reasons||[]));
    if(!p.final) out.warnings.push('Session is not final.');
    const rows=csv(await q.get(prefix+'leaderboard.csv'));
    check(p.contracts===0&&rows.length===0 || ['symbol','underlying','option_type','rank','clean_rank','clean'].every(k=>rows.columns.includes(k)), 'Missing required leaderboard columns.');
    check(rows.every(r=>CONTRACT.test(r.symbol) && SYMBOL.test(r.underlying) && ['call','put'].includes(r.option_type) &&
      ['rank','clean_rank'].every(k=>Object.hasOwn(r,k)&&(r[k]===null || (/^[1-9]\d*$/.test(r[k])&&Number.isSafeInteger(Number(r[k]))))) &&
      ['True','False'].includes(r.clean) && (r.clean_rank===null || r.clean==='True')), 'Invalid leaderboard rows or missing/malformed producer ranks.');
    const field=a.view==='clean'?'clean_rank':'rank';
    const baseline=q.evidence.slice(-2).map(e=>e.retrieved_sha256);
    await q.get(prefix+'summary.json');await q.get(prefix+'leaderboard.csv');
    check(q.evidence.slice(-2).every((e,i)=>e.retrieved_sha256===baseline[i]),'Selected session changed during rankings read; retry this session.','source_changed');
    out.warnings.push('Matching before/after summary/leaderboard digests detect observed rebuilds; no atomic producer session revision is available.');
    const filtered=rows.filter(r=>r[field]!==null && (!a.underlying||r.underlying===a.underlying) && (!a.type||r.option_type===a.type));
    filtered.sort((x,y)=>Number(x[field])-Number(y[field])); // producer rank, never re-score
    const paged=slice(filtered,limit,offset); out.pagination=paged.pagination;
    out.rows=paged.rows.map(row=>({...row,detail_reference:this.reference({dataset:'options_returns',p,row,prefix,actual:out.actual,evidence:[...q.evidence]})}));
    out.status=p.assessment.status==='partial'||!p.final?'partial':filtered.length?'available':'empty';return out;
  }
  async detail(a,q,out) {
    args(a,['reference','offset','limit'],['reference']); const {limit,offset}=page(a);
    check(typeof a.reference==='string' && new RegExp('^'+UUID+'$').test(a.reference),'Invalid detail reference.','invalid_reference');
    const entry=this.references.get(a.reference); check(entry && entry.expires>=this.now(),'Reference expired or is not from this server; repeat the query.','invalid_reference');
    const v=entry.value; Object.assign(out,this.envelope(v.dataset,a,q));out.actual=v.actual;out.row=v.row;out.sources.push(...v.evidence);out.status='available';
    if(v.dataset==='qqq_snapshot') {
      out.producer_times.acquisition_end=v.p.timestamp;out.freshness={...age(v.p.timestamp,this.now()),basis:'collection_timestamp'};out.readings=v.p.underlying_market ?? null;
      out.coverage={scope:'one cached row from the exact queried payload'};out.warnings.push('Detail uses retained queried JSON. The archived CSV has not been read or verified.');return out;
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
    // OA-203 session files are mutable rebuild products. Detect change instead
    // of attaching a new quote path to an old ranking/reference.
    const summary=await q.json(v.prefix+'summary.json');returnSummary(summary,v.p.trade_date);
    await q.get(v.prefix+'leaderboard.csv');
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
        const rows=jsonl(await q.get(key)).filter(r=>r.symbol===v.row.symbol);
        check(rows.every(r=>r.underlying===v.row.underlying&&r.sweep===m.sweep&&Number.isSafeInteger(r.t)&&
          r.t>=summary.session_open_ms&&r.t<summary.session_close_ms&&['call','put'].includes(r.type)&&
          [r.bid,r.ask].every(n=>n===null||(typeof n==='number'&&Number.isFinite(n)&&n>=0))), 'Selected contract quote rows violate the sweep/session contract.');
        if(!rows.length){out.status='partial';out.warnings.push(`Sweep ${m.sweep}: selected contract has no quote rows.`);}
        out.quote_path.push(...rows);
      }
      catch(e){out.errors.push(errorRecord(e,key));out.status='partial';}
    }
    try {await q.get(v.prefix+'summary.json');await q.get(v.prefix+'leaderboard.csv');await q.get(v.prefix+'sweeps/manifest.jsonl');}
    catch(e){delete out.quote_path;delete out.sweeps;throw e;}
    if(!q.evidence.slice(-3).every((e,i)=>e.retrieved_sha256===baseline[i])){
      delete out.quote_path;delete out.sweeps;throw new DataError('source_changed','Session changed during detail read; repeat return_rankings.');
    }
    out.warnings.push('Quote path is bounded by sweep pagination; calculation inputs/flags in row are preserved producer CSV cells, not recomputed. Trade-bar raw detail is deferred.');
    out.warnings.push('Matching before/after digests detect observed session changes; the producer supplies no atomic immutable session revision or per-sweep checksums here.');
    return out;
  }
}
module.exports={Consumer, DATASETS, csv, jsonl, age, dates};
