import { instant, observation } from './one-clock.js';

export const regimeFlowSource = {
  id:'regime-flow',version:'1',owner:'market_data.tradier',maxAgeMs:150000,
  async read({session,instrument,cutoff,signal}) {
    if (instrument !== 'QQQ') return [];
    const query = new URLSearchParams({session,cutoff:String(cutoff)});
    const res = await fetch('/api/one-clock/regime-flow?' + query, {signal,cache:'no-store',credentials:'same-origin'});
    if (!res.ok) throw new Error('Flow source unavailable');
    const {rows} = await res.json();
    if (!Array.isArray(rows) || rows.length > 1) throw new Error('Invalid flow response');
    return rows.map(observation);
  },
};

const signed = (n, digits) => (n > 0 ? '+' : '') + n.toFixed(digits);
const count = n => n.toLocaleString('en-US');
function at(value) {
  try { return instant(value); } catch { return null; }
}

export function regimeComponents(selection, chain, flow, findPin) {
  const cutoff = selection.cutoff;
  const rows = [];
  function add(name, value, observed, available, note = '', observedLabel = '') {
    const eligible = Number.isSafeInteger(observed) && observed <= cutoff
      && Number.isSafeInteger(available) && available <= cutoff && observed <= available;
    rows.push({name,value:eligible && value != null ? value : 'Unavailable',
      observed:eligible ? observed : null,observedLabel:eligible ? observedLabel : '',available:eligible ? available : null,
      note:eligible ? [note,cutoff-observed > 150000 ? 'retained' : ''].filter(Boolean).join(' · ') : ''});
  }
  const row = chain?.row;
  const data = row?.value;
  const windows = data?.underlying_market?.momentum_windows;
  const m = windows?.readings?.['5m'];
  const mAt = at(windows?.observed_at);
  const momentum = windows?.session === selection.session && m?.status === 'ok' && Number.isFinite(m.return_pct);
  add('Momentum · 5 min', momentum ? signed(m.return_pct,2)+'%' : null, mAt, row?.availableAt,
    m?.status === 'warming_up' ? 'warming up' : '');
  const volume = data?.rows?.length && data.rows.every(r=>Number.isSafeInteger(r.Volume) && r.Volume>=0)
    ? data.rows.reduce((sum,r)=>sum+r.Volume,0) : null;
  add('Reported chain option volume', Number.isSafeInteger(volume) ? count(volume)+' contracts' : null,
    row?.eventAt,row?.availableAt,'expiry '+(data?.expiration ?? 'unknown')+' · missing provider counts can be stored as zero');
  const skew = data?.market_readings?.find(r=>r.reading_id==='reference_oi_skew' && r.symbol===selection.instrument);
  const ratio = skew?.values?.imbalance_ratio;
  const published = at(skew?.published_at);
  // Settled OI often has no provider event clock. The archive validates the
  // reading's publication and input. Identify its calculation clock explicitly.
  const calculated = at(skew?.calculated_at);
  const useCalculation = skew?.observed_at === null && calculated !== null && calculated <= published
    && skew.publication?.status === 'published' && skew.input?.snapshot_key_status === 'valid'
    && skew.input.snapshot_key === data.snapshot_key && at(skew.input.snapshot_timestamp) === row.eventAt;
  add('Reference OI skew', skew?.status?.code==='ok' && Number.isFinite(ratio) && Math.abs(ratio)<=1 ? signed(ratio,3) : null,
    useCalculation ? calculated : at(skew?.observed_at),published===null ? null : Math.max(row.availableAt,published),
    [useCalculation && 'OI observation time unknown', skew?.freshness?.status === 'live' ? '' : skew?.freshness?.status].filter(Boolean).join(' · '),
    useCalculation ? 'Calculated' : '');
  const f = flow?.row, fv = f?.value;
  const total = fv ? fv.bullish+fv.bearish : 0;
  add('Session signed flow',total>0 ? signed(100*(fv.bullish-fv.bearish)/total,1)+'%' : null,
    f?.valueAt,f?.availableAt,fv ? count(total)+' quote-signed contracts · '+count(fv.unclassified)+' unclassified'+(fv.partial?' · partial coverage':'') : '');
  const spot = data?.underlying_price;
  const pin = data?.rows?.length && Number.isFinite(spot) && spot>0 ? findPin(data) : null;
  const spotAt = data?.spotAt ?? row?.eventAt;
  add('Spot relative to Max Pain',Number.isFinite(pin) && pin>0 ? signed(100*(spot-pin)/pin,2)+'% · pin $'+pin.toFixed(2) : null,
    spotAt,row?.availableAt, data?.spotAt==null ? 'snapshot time' : '');
  return rows;
}
