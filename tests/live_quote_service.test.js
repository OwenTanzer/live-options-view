const assert = require('node:assert/strict');
const { LiveQuoteService } = require('../docs/shared.js');

const service = new LiveQuoteService();
let notifications = 0;
service.subscribe(() => notifications++);
service.setVisibleContracts(['VISIBLE']);
service.setOpenPositions(['OPEN']);
service.publish([
  { symbol: 'VISIBLE', bid: 1, ask: 1.2, strike: 500, type: 'call', exp: '2026-07-16' },
  { symbol: 'OPEN', bid: 2, ask: 2.4, strike: 501, type: 'put', exp: '2026-07-16' },
  { symbol: 'IGNORED', bid: 3, ask: 3.4, strike: 502, type: 'call', exp: '2026-07-16' },
], { source: 'r2-snapshot', observedAt: '2026-07-16T12:00:00Z' });

assert.equal(service.get('VISIBLE').mid, 1.1);
assert.equal(service.get('OPEN').mid, 2.2);
assert.equal(service.get('VISIBLE').source, 'r2-snapshot');
assert.equal(service.get('VISIBLE').observedAt, '2026-07-16T12:00:00Z');
assert.equal(service.get('IGNORED'), null);
assert.equal(notifications, 1);

service.setVisibleContracts([]);
assert.equal(service.get('VISIBLE'), null);
assert.equal(service.get('OPEN').mid, 2.2);

service.setOpenPositions([]);
assert.equal(service.get('OPEN'), null);

console.log('PASS live quote service interests, metadata, notifications, and pruning');
