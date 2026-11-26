import assert from 'node:assert/strict';
import {regimeFlow} from '../backend/one-clock-regime-flow.js';
import {regimeComponents} from '../site/shared/one-clock-regime.js';
import {API_PATHS} from '../frontend/preview-policy.mjs';
assert.ok(API_PATHS.has('/api/one-clock/regime-flow'));
assert.ok(!API_PATHS.has('/api/one-clock/regime-flow/arbitrary'));
const t=Date.parse('2026-11-25T15:00:00Z'), session='2026-11-25';
const sample={session,capturedAt:t+1,lastPrintAt:t-100,bullish:30,bearish:10,unclassified:5,partial:false};
const objects=new Map();
function store(minute, s, uploaded) {
  objects.set('order-flow/qqq/regime/v1/'+minute+'.json',{size:220,uploaded:new Date(uploaded),json:async()=>s});
}
const reads=[];
async function query(cutoff) {
  reads.length=0;
  const url=new URL('https://test/?session='+session+'&cutoff='+cutoff);
  const res=await regimeFlow(new Request(url),{PAPER_TRADES:{get:async k=>{reads.push(k);return objects.get(k);}}},url);
  assert.equal(res.status,200);return (await res.json()).rows;
}
store(t,sample,t+350);
assert.deepEqual(await query(t+349),[]);assert.equal(reads.length,2);
store(t-60000,{...sample,capturedAt:t-59000,lastPrintAt:t-60000},t-58000);
assert.equal((await query(t+349))[0].eventAt,t-59000);
const [flow]=await query(t+350);assert.equal(flow.eventAt,t+1);assert.equal(reads.length,1);
const opts={instrument:'QQQ',session,cutoff:t+350}, iso=new Date(t).toISOString();
const chain={eventAt:t,availableAt:t+350,value:{underlying_price:101,spotAt:t,expiration:session,
  rows:[{Volume:0},{Volume:18}],market_readings:[{symbol:'QQQ',reading_id:'reference_oi_skew',
    status:{code:'ok'},freshness:{status:'live'},values:{imbalance_ratio:0},published_at:iso,observed_at:iso}],
  underlying_market:{momentum_windows:{session,observed_at:iso,readings:{'5m':{status:'ok',return_pct:1.25}}}}}};
const values=(scope,row,f)=>regimeComponents(scope,{row},{row:f},()=>100).map(x=>x.value);
assert.deepEqual(values(opts,chain,flow),['+1.25%','18 contracts','0.000','+50.0%','+1.00% · pin $100.00']);
assert.deepEqual(values({...opts,cutoff:t+349},chain,flow),Array(5).fill('Unavailable'));
chain.value.rows[0].Volume=null; assert.equal(values(opts,chain,flow)[1],'Unavailable');
assert.deepEqual(values(opts,null,null),Array(5).fill('Unavailable'));
console.log('Regime source availability, cursor rewind, signed and missing components passed.');

const archived = structuredClone(chain);
archived.value.snapshot_key = 'intraday/20261125/snapshot.csv';
const published = new Date(t + 100).toISOString();
const source = Object.assign(archived.value.market_readings[0], {
  observed_at: null, calculated_at: iso, published_at: published, publication: {status:'published'},
  input: {snapshot_key:archived.value.snapshot_key, snapshot_key_status:'valid', snapshot_timestamp:iso},
  values: {imbalance_ratio:-0.177725},
});
const skewAt = (cutoff=t+350) => regimeComponents({...opts,cutoff},{row:archived},null,()=>100)[2];
assert.deepEqual(skewAt(),{name:'Reference OI skew',value:'-0.178',observed:t,observedLabel:'Calculated',
  available:t+350,note:'OI observation time unknown'});
assert.equal(skewAt(t+349).value,'Unavailable');
source.published_at = new Date(t+400).toISOString();
assert.equal(skewAt().value,'Unavailable');
assert.equal(skewAt(t+400).available,t+400);
source.published_at = published;
for (const clock of ['invalid',new Date(t+500).toISOString(),undefined]) {
  source.observed_at = clock;
  assert.equal(skewAt().value,'Unavailable');
}
source.observed_at = null;
source.input.snapshot_key = 'wrong';
assert.equal(skewAt().value,'Unavailable');
