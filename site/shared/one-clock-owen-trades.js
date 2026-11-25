import {sessionAt} from './one-clock-session.js';

export const journalTransition = 'per-journal-fill';
export function journalDefinition(instrument, outcomes) {
  return {instruments:[instrument], combine:'all', transition:journalTransition, missingInput:'reset',
    conditions:[{source:'owen-trades',field:'fill',op:'==',value:true,maxAgeMs:0}], outcomes};
}

export function validateJournalRule(rule) {
  const [c] = rule.conditions;
  if (rule.conditions.length !== 1 || c.source !== 'owen-trades' || c.field !== 'fill' ||
      c.op !== '==' || c.value !== true || c.relativeToAnchor || rule.instruments.length !== 1 ||
      !/^[A-Z][A-Z0-9.-]{0,9}$/.test(rule.instruments[0]) || rule.combine !== 'all' ||
      rule.missingInput !== 'reset' || rule.levelEncounter) throw new Error('Invalid Owen Trades rule');
  for (const o of rule.outcomes) {
    if (o.source !== 'price' || o.field !== 'spot' || !['return','change','value'].includes(o.measure) ||
        [o.horizonMs,o.toleranceMs,o.maxAgeMs].some(n=>n>86400000))
      throw new Error('Choose a numeric underlying price outcome for Owen Trades');
  }
}

export function journalEvents(positions, rule, session, cutoff) {
  const [instrument] = rule.instruments;
  const events = [];
  for (const p of positions) {
    if (!p.supported || p.source_status !== 'active' || p.owner !== 'Owen' ||
        p.actor_kind !== 'human' || p.mode !== 'live' || p.available_at > cutoff) continue;
    for (const f of p.executions) {
      const at = Date.parse(f.ts);
      if (!Number.isSafeInteger(at) || at > cutoff || sessionAt(at) !== session ||
          f.instrument?.underlying !== instrument) continue;
      events.push({id:JSON.stringify([rule.id,rule.version,p.position_id,f.execution_id]),ruleId:rule.id,
        ruleVersion:rule.version,session,instrument,origin:'recorded',collection:'retrospective',
        triggeredAt:at,recordedAt:p.available_at,baselines:{},outcomes:[],
        journalFill:{positionId:p.position_id,executionId:f.execution_id,fillAt:at,
          contract:f.instrument.occ_symbol ?? instrument, side:f.side, quantity:f.qty, price:f.price,
          timestampKind:f.ts_kind, revision:p.revision}});
    }
  }
  return events.sort((a,b)=>a.triggeredAt-b.triggeredAt || a.id.localeCompare(b.id));
}
