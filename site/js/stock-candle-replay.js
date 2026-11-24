import {readPublishedCandles} from './candle-cache-one-clock.js';
import {sessionInstant, validSession} from '../shared/one-clock-session.js';

const archiveOrigin = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const intervals = new Set(['1m','3m','5m','9m','15m','30m','1h','2h','4h','1d','1w']);

async function get(path, fetchImpl) {
  const response = await fetchImpl(archiveOrigin + '/' + path, {credentials:'omit', cache:'no-store', signal:AbortSignal.timeout(10000)});
  if (!response.ok) throw new Error('Stock candle archive unavailable (HTTP ' + response.status + ').');
  return response;
}

export async function latestStockCandles(symbol, interval, fetchImpl = fetch) {
  if (!/^[A-Z][A-Z0-9.-]{0,9}$/.test(symbol) || symbol.includes('..') || !intervals.has(interval)) return [];
  const prefix = 'nasdaq/candles/' + symbol;
  const manifest = await (await get(prefix + '/provenance.json', fetchImpl)).json();
  const entry = manifest.files?.[interval];
  if (manifest.symbol !== symbol || !entry || !/^[0-9a-f]{64}$/.test(entry.sha256)) return [];
  const path = entry.version_key ?? prefix + '/' + interval + '.json';
  if (path !== prefix + '/versions/' + entry.sha256 + '.json' && path !== prefix + '/' + interval + '.json') return [];
  // The live chart may show bytes discovered by this GET. The local receipt
  // stays confined to the live path and is never used by recorded replay.
  if (manifest.pending_intervals?.includes(interval)) entry.available_at = new Date().toISOString();
  const bytes = await (await get(path,fetchImpl)).arrayBuffer();
  const rows = await readPublishedCandles(bytes,manifest);
  return rows.slice(-390).map(row=>({...row.value,time:new Date(row.eventAt).toISOString(),source:'Tiingo archive'}));
}

export async function loadStockStudy(params, fetchImpl = fetch) {
  const symbol = (params.get('symbol') || '').toUpperCase();
  const interval = params.get('interval') || '1m';
  const session = params.get('session');
  if (!/^[A-Z][A-Z0-9.-]{0,9}$/.test(symbol) || symbol.includes('..') ||
      !intervals.has(interval) || !validSession(session)) throw new Error('Choose a valid ticker, interval and session.');
  const prefix = 'nasdaq/candles/' + symbol;
  const manifest = await (await get(prefix + '/provenance.json', fetchImpl)).json();
  if (manifest.symbol !== symbol || manifest.publication?.status !== 'published') throw new Error('Published receipt unavailable.');
  const history = manifest.history?.[interval] ?? [manifest.files?.[interval]];
  const rows = [];
  let previous = new Map();
  for (const [index,entry] of history.entries()) {
    if (manifest.pending_intervals?.includes(interval) && index === history.length - 1) continue;
    if (!entry?.available_at || !/^[0-9a-f]{64}$/.test(entry.sha256)) throw new Error('Invalid interval receipt.');
    // Omitted historical sessions in a later snapshot can retract prior bars.
    const overlaps = !(entry.first_bar?.slice(0,10) > session || entry.last_bar?.slice(0,10) < session);
    const current = new Map();
    if (overlaps) {
      const path = entry.version_key ?? prefix + '/' + interval + '.json';
      if (path !== prefix + '/versions/' + entry.sha256 + '.json' && path !== prefix + '/' + interval + '.json')
        throw new Error('Unexpected archive revision key.');
      const bytes = await (await get(path,fetchImpl)).arrayBuffer();
      const receipt = {...manifest,files:{[interval]:entry}};
      const observed = await readPublishedCandles(bytes,receipt);
      if (!observed.length && entry.bars !== 0) throw new Error('The archive bytes could not be qualified.');
      for (const row of observed) {
        if (row.session !== session) continue;
        const old = previous.get(row.id);
        if (old && JSON.stringify(old.value) === JSON.stringify(row.value)) current.set(row.id,old);
        else {
          const revision = {...row,revision:index+1,evidence:path};
          current.set(row.id,revision);
          rows.push(revision);
        }
      }
    }
    for (const [id,old] of previous) {
      if (!current.has(id)) rows.push({...old,revision:index+1,value:null,availableAt:Date.parse(entry.available_at),
        evidence:entry.version_key ?? entry.key});
    }
    previous = current;
  }
  if (!rows.length) throw new Error('No qualified bars for ' + symbol + ' on ' + session + '.');
  const start = sessionInstant(session,0);
  const hours = manifest.sessions?.[session];
  const lastReceipt = rows.reduce((last,row)=>Math.max(last,row.availableAt),start);
  return {selection:{session,instrument:symbol,origin:'recorded'},start,
    close:Math.max(lastReceipt, hours?.[1] ?? sessionInstant(session,23,59,59)),
    sessionClose:hours?.[1] ?? null,sessionHours:hours,rows,
    sources:[{id:'candle-cache',label:symbol+' archived candles',fields:{open:'Open',high:'High',low:'Low',close:'Close'}}],
    rules:[{id:'stock-candle-close',name:symbol+' close above threshold',version:1,instruments:[symbol],
      combine:'all',transition:'rising',missingInput:'reset',conditions:[
        {source:'candle-cache',field:'close',op:'>',value:0,maxAgeMs:60000}],outcomes:[]}]};
}
