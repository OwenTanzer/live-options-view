// Hand-authored engineering fixtures. None of these rows are market observations.
export const demoStart = Date.parse('2026-11-09T14:15:00Z');
export const demoClose = demoStart + 60 * 60_000;
export const demoSelection = { session: 'SYN-1', instrument: 'QQQ', origin: 'synthetic', cutoff: demoStart };
export const demoRows = Array.from({ length: 61 }, (_, minute) => {
  const at = demoStart + minute * 60_000;
  const common = { revision: 1, session: 'SYN-1', instrument: 'QQQ', origin: 'synthetic',
    eventAt: at, availableAt: at + 1000, evidence: `fixture://one-clock/SYN-1/${minute}` };
  return [
    { ...common, source: 'skew', id: `skew-${minute}`, value: {
      imbalance: minute < 5 ? 0.12 : minute < 30 ? 0.36 : minute < 50 ? 0.08 : 0.41, strikes: 5 } },
    { ...common, source: 'price', id: `price-${minute}`, value: { spot: 660 + minute * 0.2 } },
    { ...common, source: 'shortlist', id: `list-${minute}`, value: { present: minute >= 12 && minute < 40 } },
  ];
}).flat();
export const demoRules = [
  { id: 'skew-bullish', version: 1, name: 'Anchored skew turns bullish', instruments: ['QQQ'],
    combine: 'all', transition: 'rising', missingInput: 'hold', conditions: [
      { source: 'skew', field: 'imbalance', op: '>=', value: 0.3, maxAgeMs: 150000 },
      { source: 'skew', field: 'imbalance', op: '>=', value: 0.1, relativeToAnchor: true, maxAgeMs: 150000 },
      { source: 'skew', field: 'strikes', op: '>=', value: 4, maxAgeMs: 150000 },
    ], outcomes: [{ id: 'spot-15m', source: 'price', field: 'spot', measure: 'return', horizonMs: 900000, toleranceMs: 150000, maxAgeMs: 150000 }] },
  { id: 'shortlist-first', version: 1, name: 'First published shortlist appearance', instruments: ['QQQ'],
    combine: 'all', transition: 'once-per-session', missingInput: 'hold',
    conditions: [{ source: 'shortlist', field: 'present', op: '==', value: true, maxAgeMs: 150000 }],
    outcomes: [{ id: 'spot-10m', source: 'price', field: 'spot', measure: 'return', horizonMs: 600000, toleranceMs: 150000, maxAgeMs: 150000 }] },
];
