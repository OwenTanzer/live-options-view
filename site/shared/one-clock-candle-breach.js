import {sessionInstant} from './one-clock-session.js';

export const candleSource = 'qqq-minute-bar';
const minute = 60000;
export function candleDefinition() {
  return {instruments:['QQQ'], combine:'all', transition:'per-candle-pair', missingInput:'reset',
    conditions:[{source:candleSource, field:'close', op:'>', value:0, maxAgeMs:0}],
    outcomes:[{id:'signed-5m', source:candleSource, field:'close', measure:'directional-change',
      horizonMs:5*minute, toleranceMs:0, maxAgeMs:0}]};
}

export function validateCandleRule(rule) {
  const expected = candleDefinition();
  if(rule.instruments.length!==1 || rule.instruments[0]!=='QQQ')throw new Error('QQQ required');
  for (const key of ['transition','combine','missingInput']) {
    if (String(rule[key]) !== String(expected[key])) throw new Error('Invalid QQQ candle study');
  }
  for (const key of ['conditions','outcomes']) {
    if (rule[key].length !== 1 || Object.entries(expected[key][0]).some(([k,v])=>rule[key][0][k] !== v))
      throw new Error('The QQQ candle study uses consecutive minutes and the exact five-minute close');
  }
  if (rule.levelEncounter || rule.conditions[0].relativeToAnchor) throw new Error('Invalid candle study condition');
}

export function validBar(b) {
  return b && [b.open,b.high,b.low,b.close].every(n=>Number.isFinite(n) && n>0) &&
    b.low <= Math.min(b.open,b.close) && b.high >= Math.max(b.open,b.close);
}

// Inputs are already scoped to a session and resolved to the latest visible revision.
export function candleTrials(rows, selection, close) {
  if(!rows.some(r=>r.source===candleSource))return [];
  const open = sessionInstant(selection.session,9,30);
  const midnight=sessionInstant(selection.session,0);
  const offset=(Date.parse(selection.session+'T00:00:00Z')-midnight)/3600000;
  const suffix='-'+String(Math.abs(offset)).padStart(2,'0')+':00';
  const bars = new Map(rows.filter(r=>r.source===candleSource && r.eventAt>open && r.eventAt<=close &&
    r.eventAt%minute===0 && validBar(r.value)).map(r=>[r.eventAt,r]));
  const trials = [];
  for (const second of bars.values()) {
    const first = bars.get(second.eventAt-minute);
    if (!first || first.value.open===first.value.close) continue;
    const a=first.value, b=second.value, up=a.close<a.open;
    const level=up?a.high:a.low;
    const depth=up?b.high-level:level-b.low;
    const event=(up?b.close>b.open:b.close<b.open) && depth>0;
    let atr=null;
    if(event) {
      let sum=0;
      for(let k=14;k>=1;k--) {
        const prev=bars.get(second.eventAt-(k+1)*minute), bar=bars.get(second.eventAt-k*minute);
        if(!prev || !bar){sum=null;break;}
        sum+=Math.max(bar.value.high,prev.value.close)-Math.min(bar.value.low,prev.value.close);
      }
      if(sum!==null)atr=sum/14;
    }
    trials.push({trialId:'qqq.1m.opposite_color_breach.v1/QQQ/'+new Date(second.eventAt+offset*3600000).toISOString().slice(0,19)+suffix,
      priorAtr14:atr,penetrationAtr14:atr?depth/atr:null,first,second,endpoint:bars.get(second.eventAt+5*minute),event,
      direction:up?'up':'down',level,penetration:event?depth:null,
      closeClass:event?((up?b.close>level:b.close<level)?'beyond':'at_or_inside'):null,
      openBeyond:event?(up?b.open>level:b.open<level):null,
      bothExtremes:event?b.high>a.high && b.low<a.low:null});
  }
  return trials.sort((a,b)=>a.second.eventAt-b.second.eventAt);
}

export function candleEvents(rule, rows, selection, close, windows, evidence) {
  const events=[];
  for(const {first,second,endpoint,event,...breach} of candleTrials(rows,selection,close)) {
    const at=second.eventAt;
    if(!event || windows && !windows.some(w=>at>=w.started_at && (w.ended_at===null || at<w.ended_at)))continue;
    const triggeredAt=Math.max(first.availableAt,second.availableAt);
    const id=rule.outcomes[0].id, targetAt=at+5*minute;
    const outcome={id,targetAt,value:null,measuredAt:null,availableAt:null,evidence:null};
    if(endpoint) Object.assign(outcome,{state:'measured',reason:null,
      value:(breach.direction==='up'?1:-1)*(endpoint.value.close-second.value.close),
      measuredAt:targetAt,availableAt:Math.max(triggeredAt,endpoint.availableAt),evidence:evidence(endpoint)});
    else {
      const deadline=Math.min(targetAt,close);
      const missing=selection.cutoff>=deadline;
      Object.assign(outcome,{state:missing?'missing':'pending',reason:missing?
        (targetAt>close?'session-closed-before-horizon':'no-exact-minute-close'):null});
    }
    events.push({id:JSON.stringify([rule.id,rule.version,selection.origin,selection.session,'QQQ',at]),
      ruleId:rule.id,ruleVersion:rule.version,session:selection.session,instrument:'QQQ',origin:selection.origin,
      decisionAt:at,triggeredAt,matchingObservations:1,breach,
      inputEvidence:[first,second].map(r=>({...evidence(r),value:r.value})),
      baselines:{[id]:{value:second.value.close,ageMs:0,evidence:evidence(second)}},outcomes:[outcome]});
  }
  return events;
}
