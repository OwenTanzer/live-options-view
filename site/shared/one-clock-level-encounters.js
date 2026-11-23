// Consume recorded source availability in order. A level must have qualified
// before the first price in a bracket; a new flag never backdates an encounter.
const sides = ['put', 'call'];
export const encounterGroups = Object.freeze(['put-only', 'call-only', 'both']);
export function validateEncounter(config) {
  if (![4, 5].includes(config?.minimumBucket) || !['up', 'down', 'either'].includes(config.approach) ||
      !['exact', 'exact-or-bracketed'].includes(config.contact) ||
      !Number.isSafeInteger(config.maxGapMs) || config.maxGapMs < 1 || config.maxGapMs > 900000 ||
      !Array.isArray(config.groups) || !config.groups.length || config.groups.length > 3 ||
      new Set(config.groups).size !== config.groups.length || config.groups.some(g => !encounterGroups.includes(g))) {
    throw new Error('Invalid calibrated level encounter settings');
  }
}
function ref(row) {
  return { source: row.source, id: row.id, revision: row.revision, evidence: row.evidence,
    eventAt: row.eventAt, availableAt: row.availableAt, ...(row.valueAt == null ? {} : {valueAt: row.valueAt}) };
}
function makeEvent(rule, selection, a, b, flag, strike, side, contact) {
  const cohort = flag.group, at = b.availableAt;
  return { id: JSON.stringify([rule.id,rule.version,selection.origin,selection.session,selection.instrument,
      at,strike,a.id,a.revision,b.id,b.revision,flag.row.id,flag.row.revision]),
    ruleId: rule.id, ruleVersion: rule.version, instrument: selection.instrument, session: selection.session,
    origin: selection.origin, triggeredAt: at, matchingObservations: 1,
    encounter: { strike, cohort, approach: side, contact, lowerTime: a.valueAt ?? a.eventAt,
      upperTime: b.valueAt ?? b.eventAt, calibration: flag.row.value.calibration },
    inputEvidence: [ {...ref(flag.row), value: {strike, cohort, call:flag.call, put:flag.put}},
      {...ref(a), value:a.value.spot}, {...ref(b), value:b.value.spot} ],
    baselines: Object.fromEntries(rule.outcomes.map(o=>[o.id,{value:strike,evidence:ref(b)}])) };
}

export function encounterEvents(rule, rows, selection, close, activationWindows) {
  const config = rule.levelEncounter;
  const events = [], flags = new Map(), seen = new Map();
  // A correction can withdraw a qualifying snapshot. Retain its effective
  // availability interval so earlier touches keep their original evidence.
  const byIdentity=new Map();
  function flagAt(history,cutoff) {
    let latest=null;
    for(let i=history.length-1;i>=0;i--) {
      const flag=history[i];
      if(flag.row.availableAt<=cutoff && cutoff<flag.until &&
        (!latest || flag.row.eventAt>latest.row.eventAt))latest=flag;
    }
    return latest;
  }
  let previous = null;
  const arrivals = [...rows].filter(row=>row.source === 'level-spot' || row.source === 'oi-levels')
    .sort((a,b)=>a.availableAt-b.availableAt || a.eventAt-b.eventAt || a.id.localeCompare(b.id) || a.revision-b.revision);
  for (let i=0; i<arrivals.length;) {
    const at=arrivals[i].availableAt, batch=new Map();
    if (at>close) break;
    while (i<arrivals.length && arrivals[i].availableAt===at) {
      const row=arrivals[i++], key=JSON.stringify([row.source,row.id]);
      if ((seen.get(key)??0) >= row.revision) continue;
      seen.set(key,row.revision);
      batch.set(key,row);
    }
    const fresh=[...batch.values()];
    for(const row of fresh.filter(r=>r.source==='level-spot')) {
      const t=row.valueAt??row.eventAt, prevTime=previous && (previous.valueAt??previous.eventAt);
      if(previous && row.id===previous.id && t===prevTime) {
        previous=Number.isFinite(row.value?.spot) && at-t<=config.maxGapMs?row:null;
        continue;
      }
      if(previous && t<=prevTime) continue;
      if(!Number.isFinite(row.value?.spot) || at-t>config.maxGapMs) {previous=null;continue;}
      if(previous && t-prevTime<=config.maxGapMs) {
        const from=previous.value.spot, to=row.value.spot, approach=from<to?'up':'down';
        const admitted=!activationWindows || activationWindows.some(w=>t>=w.started_at && at>=w.started_at && (w.ended_at===null || at<w.ended_at));
        if(admitted && (config.approach==='either'||config.approach===approach) && from!==to) {
          for(const [strike,history] of flags) {
            if(from===strike || (from-strike)*(to-strike)>0) continue;
            const contact=to===strike?'exact':'bracketed';
            const flag=flagAt(history,contact==='exact'?t-1:prevTime);
            if(!flag || !config.groups.includes(flag.group) || (config.contact==='exact' && contact!=='exact')) continue;
            events.push(makeEvent(rule,selection,previous,row,flag,strike,approach,contact));
          }
        }
      }
      previous=row;
    }
    for(const row of fresh.filter(r=>r.source==='oi-levels')) {
      for(const earlier of byIdentity.get(row.id)??[])earlier.until=at;
      const current=[];
      byIdentity.set(row.id,current);
      if(!row.value?.calibration || !Array.isArray(row.value.levels)) continue;
      for(const level of row.value.levels) {
        const {strike,call,put}=level;
        if(!Number.isFinite(strike) || !sides.every(s=>Number.isInteger(level[s]) && level[s]>=0 && level[s]<=5)) continue;
        const c=call>=config.minimumBucket,p=put>=config.minimumBucket;
        if(!c&&!p) continue;
        const flag={row,call,put,group:c&&p?'both':c?'call-only':'put-only',until:Infinity};
        if(!flags.has(strike))flags.set(strike,[]);
        flags.get(strike).push(flag);
        current.push(flag);
      }
    }
  }
  return events;
}
export function signedOutcome(event, endpoint) {
  const movement=(endpoint-event.encounter.strike)*(event.encounter.approach==='up'?1:-1);
  return movement===0?'tie':movement>0?'continuation':'rejection';
}
