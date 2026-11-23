import { sessionInstant } from './one-clock-session.js';
import { observationKey } from './one-clock.js';

export function oiSkewAuditRule(id) {
  return {
    id, version: 1, name: 'OI Skew decisions · ten-minute QQQ change',
    instruments: ['QQQ'], combine: 'all', transition: 'per-decision', missingInput: 'hold',
    conditions: [{source: 'bot-decision', field: 'bot', op: '==', value: 'crassus_oiskew', maxAgeMs: 0}],
    outcomes: [{id: 'qqq-change', source: 'price', field: 'spot', measure: 'change',
      horizonMs: 600000, maxAgeMs: 60000, toleranceMs: 60000}],
  };
}

export function validateDecisionRule(rule) {
  if (rule.conditions.length !== 1 || rule.conditions[0].source !== 'bot-decision' ||
      rule.conditions[0].field !== 'bot' || rule.conditions[0].op !== '==' ||
      typeof rule.conditions[0].value !== 'string' || !rule.conditions[0].value.trim() ||
      rule.conditions[0].relativeToAnchor || rule.outcomes.some(o =>
        o.source !== 'price' || o.field !== 'spot' || !['change', 'return'].includes(o.measure))) {
    throw new Error('Per-decision rules require one bot identity and underlying price-change or return outcomes.');
  }
}

const sampleAt = row => row.valueAt ?? row.eventAt;
function lowerBound(rows, at) {
  let low = 0, high = rows.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (sampleAt(rows[mid]) < at) low = mid + 1;
    else high = mid;
  }
  return low;
}

// The caller has already selected the highest visible revision of each ID.
// Corrections update that occurrence in place. A retraction removes it from
// the current view while the observer retains earlier as-of revisions.
export function decisionEvents(rule, rows, selection, close, windows, evidence) {
  const open = sessionInstant(selection.session, 9, 30);
  const bot = rule.conditions[0].value;
  const quotes = rows.filter(r => r.source === 'price' && sampleAt(r) >= open && sampleAt(r) <= close)
    .sort((a, b) => sampleAt(a) - sampleAt(b) || a.availableAt - b.availableAt || a.id.localeCompare(b.id));
  const result = [];
  for (const receipt of rows) {
    const at = receipt.eventAt;
    if (receipt.source !== 'bot-decision' || receipt.value?.bot !== bot || at < open || at >= close) continue;
    if (windows && !windows.some(w => at >= w.started_at && (w.ended_at === null || at < w.ended_at))) continue;
    const quote = quotes[lowerBound(quotes, at + 1) - 1];
    const age = quote ? at - sampleAt(quote) : null;
    const baselines = {};
    const outcomes = rule.outcomes.map(o => {
      const value = quote?.value?.spot;
      const baseline = quote && age <= o.maxAgeMs && Number.isFinite(value) ? value : null;
      baselines[o.id] = {value: baseline, ageMs: age, evidence: evidence(quote),
        visibleAtDecision: quote ? quote.availableAt <= at : null};
      const targetAt = at + o.horizonMs;
      const empty = {id: o.id, targetAt, value: null, measuredAt: null, availableAt: null, evidence: null};
      if (targetAt > close) return {...empty, state: selection.cutoff >= close ? 'missing' : 'pending',
        reason: selection.cutoff >= close ? 'session-closed-before-horizon' : null};
      if (baseline === null || (o.measure === 'return' && baseline === 0))
        return {...empty, state: 'missing', reason: 'trigger-baseline-unavailable'};
      const deadline = Math.min(targetAt + o.toleranceMs, close);
      for (let i = lowerBound(quotes, targetAt); i < quotes.length && sampleAt(quotes[i]) <= deadline; i++) {
        const endpoint = quotes[i], spot = endpoint.value?.spot;
        if (!Number.isFinite(spot)) continue;
        const change = o.measure === 'change' ? spot - baseline : (spot / baseline - 1) * 100;
        if (!Number.isFinite(change)) return {...empty, state: 'missing', reason: 'nonfinite-measurement'};
        return {...empty, state: 'measured', reason: null, value: change, endpointValue: spot,
          measuredAt: sampleAt(endpoint), offsetMs: sampleAt(endpoint) - targetAt,
          availableAt: Math.max(quote.availableAt, receipt.availableAt, endpoint.availableAt),
          evidence: evidence(endpoint)};
      }
      return {...empty, state: selection.cutoff > deadline ? 'missing' : 'pending',
        reason: selection.cutoff > deadline ? 'no-qualified-endpoint' : null};
    });
    result.push({id: JSON.stringify([rule.id, rule.version, observationKey(receipt)]),
      ruleId: rule.id, ruleVersion: rule.version, session: selection.session, instrument: selection.instrument,
      origin: selection.origin, triggeredAt: receipt.availableAt, decisionAt: at, matchingObservations: 1,
      decisionEvidence: 'published-audit-receipt', predictionCommitment: null,
      inputEvidence: [{...evidence(receipt), value: receipt.value}], baselines, outcomes});
  }
  return result;
}
