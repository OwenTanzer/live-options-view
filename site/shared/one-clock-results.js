// Descriptive projections of an already-selected, availability-gated ledger.
// No transport, timer or source-specific synchronization belongs in this module.
import { instant } from './one-clock.js';

export const ruleKey = rule => JSON.stringify([rule.ruleId ?? rule.id, rule.ruleVersion ?? rule.version]);
function measurement(outcome,rule) {
  return rule?.transition==='per-candle-pair' && outcome.measure==='change'?'directional-change':outcome.measure;
}
export function measureLabel(outcome,rule) {
  return measurement(outcome,rule)==='directional-change'?'Change in breach direction (QQQ points)':outcome.measure;
}
export function outcomeSignature(outcome,rule) {
  return JSON.stringify([outcome.source,outcome.field,measurement(outcome,rule),outcome.horizonMs,outcome.toleranceMs,outcome.maxAgeMs]);
}
const finite = value => typeof value === 'number' && Number.isFinite(value);
const categoryKey = value => JSON.stringify([typeof value,value]);
const accepted = (values,value) => !values?.length || values.includes(value);

export function projectResults({events,rules,cutoff,origin,filters={},outcome=null}) {
  instant(cutoff);
  if(!['recorded','reconstructed','synthetic'].includes(origin))throw new Error('Choose one evidence cohort.');
  if(!Array.isArray(events)||events.length>10000)throw new Error('Result scope exceeds 10000 occurrences.');
  const definitions=new Map(rules.map(rule=>[ruleKey(rule),rule]));
  const reference=outcome?definitions.get(outcome.ruleKey)?.outcomes.find(o=>o.id===outcome.id):null;
  if(outcome&&!reference)throw new Error('Outcome definition is unavailable.');
  const signature=reference?outcomeSignature(reference,definitions.get(outcome.ruleKey)):null;
  const query=(filters.query??'').trim().toLowerCase();
  let excludedTiming=0;
  const seen=new Set(),rows=[];
  for(const event of events) {
    if(event.origin!==origin||event.triggeredAt>cutoff||(event.recordedAt??event.triggeredAt)>cutoff)continue;
    if(seen.has(event.id))throw new Error('Duplicate occurrence in result scope.');seen.add(event.id);
    const key=ruleKey(event),rule=definitions.get(key);
    if(!accepted(filters.ruleKeys,key)||!accepted(filters.instruments,event.instrument)||!accepted(filters.sessions,event.session)||
      !accepted(filters.collections,event.collection??'replay')||filters.from&&event.session<filters.from||filters.to&&event.session>filters.to)continue;
    if(query&&!JSON.stringify([event.id,event.session,event.instrument,event.ruleId,event.ruleVersion,rule?.name,event.headline?.title]).toLowerCase().includes(query))continue;
    // Outcome IDs are local to a rule. Pool only identical measurement specs,
    // never unlike horizons/units just because their human labels look alike.
    const candidates=signature?(rule?.outcomes??[]).filter(o=>outcomeSignature(o,rule)===signature):[];
    const spec=key===outcome?.ruleKey?reference:candidates.length===1?candidates[0]:null;
    const measured=spec?event.outcomes.find(o=>o.id===spec.id)??null:null;
    if(measured&&!['measured','pending','missing'].includes(measured.state))throw new Error('Invalid retained outcome state.');
    if(measured?.state==='measured'&&(!Number.isSafeInteger(measured.availableAt)||!Number.isSafeInteger(measured.measuredAt)||measured.availableAt>cutoff||measured.measuredAt>cutoff))throw new Error('Outcome has not passed the selected availability cutoff.');
    if(filters.endpointTiming && event.headline && measured?.state==='measured' &&
       (measured.lagMs>0?'delayed':'exact')!==filters.endpointTiming){excludedTiming++;continue;}
    if(!accepted(filters.states,measured?.state??'unscored'))continue;
    rows.push({event,outcome:measured});
  }
  rows.sort((a,b)=>a.event.triggeredAt-b.event.triggeredAt||(a.event.id<b.event.id?-1:a.event.id>b.event.id?1:0));
  return {rows,excludedTiming,summary:summarizeResults(rows,reference?.measure==='category'?'category':'numeric'),spec:reference,cutoff,origin};
}

export function summarizeResults(rows,kind='numeric') {
  const measured=rows.filter(r=>r.outcome?.state==='measured');
  const values=measured.map(r=>r.outcome.value);
  if(kind==='numeric'&&values.some(v=>!finite(v)))throw new Error('Numeric outcome contains a nonnumeric value.');
  if(kind==='category'&&values.some(v=>!['string','boolean','number'].includes(typeof v)||typeof v==='number'&&!finite(v)))throw new Error('Invalid categorical outcome.');
  const categories=new Map();
  if(kind==='category')for(const value of values) {
    const key=categoryKey(value),old=categories.get(key);categories.set(key,{value,count:(old?.count??0)+1});
  }
  const sorted=kind==='numeric'?[...values].sort((a,b)=>a-b):[],n=values.length;
  return {occurrences:rows.length,sessions:new Set(rows.map(r=>r.event.session)).size,
    measured:n,pending:rows.filter(r=>r.outcome?.state==='pending').length,
    missing:rows.filter(r=>r.outcome?.state==='missing').length,unscored:rows.filter(r=>!r.outcome).length,
    mean:sorted.length?sorted.reduce((mean,value,i)=>mean*(i/(i+1))+value/(i+1),0):null,
    median:sorted.length?sorted[Math.floor(n/2)]/2+sorted[Math.floor((n-1)/2)]/2:null,
    distribution:histogram(sorted),categories:[...categories.values()].map(c=>({...c,rate:c.count/n})),kind};
}

function histogram(sorted) {
  if(!sorted.length)return [];
  const low=sorted[0],high=sorted.at(-1);
  if(low===high)return [{low,high,count:sorted.length,includesHigh:true}];
  if(!Number.isFinite(high-low))return [{low,high,count:sorted.length,includesHigh:true}];
  const count=Math.min(10,Math.ceil(Math.sqrt(sorted.length))),width=(high-low)/count;
  if(width===0)return [{low,high,count:sorted.length,includesHigh:true}];
  const bins=Array.from({length:count},(_,i)=>({low:low+i*width,high:i===count-1?high:low+(i+1)*width,count:0,includesHigh:i===count-1}));
  for(const value of sorted)bins[Math.min(count-1,Math.floor((value-low)/width))].count++;
  return bins;
}

// Difference of occurrence-weighted means, with one influence sum per session.
// Both groups share the same session blocks, preserving within-session covariance.
// Report a standard error, not a significance verdict or a small-sample CI.
export function compareResults(rows,{field='rule',a,b,kind='numeric',category}={}) {
  if(!['rule','instrument','collection'].includes(field)||typeof a!=='string'||typeof b!=='string'||a===b)throw new Error('Choose two distinct comparison groups.');
  if(!['numeric','category'].includes(kind)||kind==='category'&&!(['string','boolean'].includes(typeof category)||finite(category)))throw new Error('Choose a supported comparison outcome and category.');
  const group=event=>field==='rule'?ruleKey(event):event[field]??'replay';
  const clusters=new Map(),totals={a:{sum:0,n:0,sessions:new Set()},b:{sum:0,n:0,sessions:new Set()}};
  for(const {event,outcome} of rows) {
    if(outcome?.state!=='measured')continue;
    const label=group(event),arm=label===a?'a':label===b?'b':null;if(!arm)continue;
    const value=kind==='category'?(categoryKey(outcome.value)===categoryKey(category)?100:0):outcome.value;
    if(!finite(value))throw new Error('Comparison requires compatible measured outcomes.');
    const cluster=clusters.get(event.session)??{a:{sum:0,n:0},b:{sum:0,n:0}};
    cluster[arm].sum+=value;cluster[arm].n++;clusters.set(event.session,cluster);
    totals[arm].sum+=value;totals[arm].n++;totals[arm].sessions.add(event.session);
  }
  const A=totals.a,B=totals.b,estimate=A.n&&B.n?A.sum/A.n-B.sum/B.n:null;
  const base={estimate,standardError:null,sessions:clusters.size,aCount:A.n,bCount:B.n,aSessions:A.sessions.size,bSessions:B.sessions.size,
    unit:kind==='category'?'percentage points':'outcome units',method:'Session-clustered standard error; occurrence-weighted difference A minus B.'};
  if(estimate===null)return {...base,reason:'Both groups need a measured outcome.'};
  if(!finite(estimate))return {...base,estimate:null,reason:'Numeric range exceeds the supported comparison precision.'};
  if(A.sessions.size<2||B.sessions.size<2)return {...base,reason:'Between-session uncertainty is not estimable: each group needs at least two measured sessions. The descriptive difference is available.'};
  const influences=[...clusters.values()].map(c=>(c.a.sum-c.a.n*A.sum/A.n)/A.n-(c.b.sum-c.b.n*B.sum/B.n)/B.n);
  const standardError=Math.hypot(...influences)*Math.sqrt(clusters.size/(clusters.size-1));
  if(!finite(estimate)||!finite(standardError))return {...base,estimate:finite(estimate)?estimate:null,reason:'Numeric range exceeds the supported comparison precision.'};
  return {...base,standardError,reason:'Assumes independent sessions. Few sessions, serial dependence and adaptive exploration can make uncertainty unreliable; this is not a predictive test.'};
}
