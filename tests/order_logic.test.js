const assert = require('node:assert/strict');
const {
  normalizePaperOrder, normalizeShareOrder, isTradeableShareSymbol,
} = require('../docs/shared.js');

assert.deepEqual(
  normalizePaperOrder({ side: 'buy', quantity: '2', orderType: 'market' }),
  { value: { side: 'buy', qty: 2, order_type: 'market' } },
);
assert.deepEqual(
  normalizePaperOrder({ side: 'sell', quantity: '3', orderType: 'limit', limitPrice: '1.25' }),
  { value: { side: 'sell', qty: 3, order_type: 'limit', limit_price: 1.25 } },
);
assert.match(normalizePaperOrder({ side: 'hold', quantity: 1 }).error, /buy or sell/);
assert.match(normalizePaperOrder({ side: 'buy', quantity: '1.5' }).error, /whole number/);
assert.match(normalizePaperOrder({ side: 'buy', quantity: 1, orderType: 'limit', limitPrice: '' }).error, /greater than zero/);
assert.match(normalizePaperOrder({ side: 'buy', quantity: 1, orderType: 'limit', limitPrice: '1.234' }).error, /increments/);
assert.equal(isTradeableShareSymbol('AAPL'), true);
assert.equal(isTradeableShareSymbol('VIX'), false, 'listed non-equities remain read-only');
assert.equal(isTradeableShareSymbol('MSFT'), false, 'arbitrary symbols wait for explicit quote lookup support');
assert.deepEqual(
  normalizeShareOrder({ side: 'sell', quantity: 4, heldQuantity: 10 }),
  { value: { side: 'sell', qty: 4, order_type: 'market' } },
);
assert.match(
  normalizeShareOrder({ side: 'sell', quantity: 11, heldQuantity: 10 }).error,
  /more shares than you hold/,
);
assert.deepEqual(
  normalizeShareOrder({
    side: 'buy', quantity: 3, heldQuantity: 0,
    orderType: 'limit', limitPrice: '210.25',
  }),
  { value: { side: 'buy', qty: 3, order_type: 'limit', limit_price: 210.25 } },
);
assert.deepEqual(
  normalizeShareOrder({
    side: 'sell', quantity: 2, heldQuantity: 5,
    orderType: 'limit', limitPrice: '210.05',
  }),
  { value: { side: 'sell', qty: 2, order_type: 'limit', limit_price: 210.05 } },
);
assert.match(normalizeShareOrder({
  side: 'sell', quantity: 2, heldQuantity: 5,
  orderType: 'limit', limitPrice: '210.257',
}).error, /increments/);

console.log('order logic tests passed');
