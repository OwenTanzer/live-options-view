import {sessionInstant} from './one-clock-session.js';

export const headlineTickers=['QQQ','SPY','NVDA','MSFT','AAPL','GOOGL','AMZN','META','AMD','TSM','ORCL','XOM','CVX','COP','BP','SHEL'];
export const hasHeadlinePriceRoute=ticker=>headlineTickers.includes(ticker) && !['TSM','BP','SHEL'].includes(ticker);
export const headlineDay=session=>({open:sessionInstant(session,0),close:sessionInstant(session,23,59,59,999)});
const minute = 60000;
export const headlineSource = 'company-headline';
export const quoteSource = 'company-quote';
const horizons = [5, 30, 60, 1440];

export function headlineDefinition(instrument) {
  return {
    instruments:[instrument], combine:'all', transition:'per-headline', missingInput:'reset',
    conditions:[{source:headlineSource,field:'ticker',op:'==',value:instrument,maxAgeMs:0}],
    outcomes:horizons.map(minutes=>({id:'return-'+minutes,source:quoteSource,field:'price',measure:'return',
      horizonMs:minutes*minute,toleranceMs:86400000,maxAgeMs:120000})),
  };
}

export function validateHeadlineRule(rule) {
  const expected=headlineDefinition(rule.instruments[0]);
  const same=(value,spec)=>Object.entries(spec).every(([key,v])=>value?.[key]===v);
  if(rule.instruments.length!==1 || rule.combine!=='all' || rule.missingInput!=='reset' ||
    rule.conditions.length!==1 || !same(rule.conditions[0],expected.conditions[0]) ||
    rule.outcomes.length!==horizons.length || !rule.outcomes.every((o,i)=>same(o,expected.outcomes[i])))
    throw Error('Invalid mapped headline study');
}

export function headlineEvents(rule,rows,selection,close,windows,evidence) {
  if(!rows.some(r=>r.source===headlineSource))return [];
  const ticker=selection.instrument, day=headlineDay(selection.session);
  const prices=rows.filter(r=>r.source===quoteSource && r.value?.ticker===ticker &&
    r.value.price>0 && Number.isFinite(r.value.price)).sort((a,b)=>a.eventAt-b.eventAt || a.availableAt-b.availableAt || a.id.localeCompare(b.id));
  const proof=r=>({...evidence(r),instrument:r.instrument,instrumentId:r.value.instrumentId,value:r.value});
  return rows.filter(r=>r.source===headlineSource && r.value?.ticker===ticker &&
    r.availableAt>=day.open && r.availableAt<=day.close && (!windows || windows.some(w=>r.availableAt>=w.started_at && (w.ended_at===null || r.availableAt<w.ended_at))))
    .map(story=>{
      const at=story.availableAt;
      const quotes=prices.filter(r=>r.value.instrumentId===story.value.instrumentId);
      const base=quotes.filter(r=>r.availableAt<=at && r.eventAt<=at).at(-1);
      const baselines={},outcomes=rule.outcomes.map(spec=>{
        const targetAt=at+spec.horizonMs, endpoint=quotes.find(r=>r.eventAt>=targetAt);
        const age=base?at-base.eventAt:null;
        baselines[spec.id]={value:base?.value.price??null,ageMs:age,evidence:base?proof(base):null};
        const result={id:spec.id,targetAt,value:null,measuredAt:null,availableAt:null,evidence:null,elapsedMs:null,lagMs:null,baseStale:age!==null && age>spec.maxAgeMs};
        if(!base)return {...result,state:'missing',reason:hasHeadlinePriceRoute(ticker)?'no-event-known-mapped-price':'no-configured-price-route'};
        if(!endpoint)return {...result,state:selection.cutoff>targetAt+spec.toleranceMs?'missing':'pending',
          reason:selection.cutoff>=targetAt?'awaiting-eligible-quote':null};
        return {...result,state:'measured',value:(endpoint.value.price/base.value.price-1)*100,
          measuredAt:endpoint.eventAt,availableAt:endpoint.availableAt,evidence:proof(endpoint),
          elapsedMs:endpoint.eventAt-at,lagMs:endpoint.eventAt-targetAt,reason:null};
      });
      return {id:JSON.stringify([rule.id,rule.version,selection.origin,selection.session,ticker,story.id]),
        ruleId:rule.id,ruleVersion:rule.version,session:selection.session,instrument:ticker,origin:selection.origin,
        triggeredAt:at,headline:story.value,matchingObservations:1,inputEvidence:[proof(story)],baselines,outcomes};
    });
}
