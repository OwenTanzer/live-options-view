const test = require('node:test');
const assert = require('node:assert/strict');
const createWindow = require('../site/shared/order-flow-window.js');
const strikes = Array.from({length:193}, (_, i) => 712 - i);
test('follow, manual inspection and zoom survive spot and universe updates', () => {
  const w = createWindow();
  assert.equal(w.update(strikes, 638).prices[8], 638);
  assert.equal(w.update(strikes, 608).prices[8], 608);
  w.goTo(685);
  w.zoom(-1);
  assert.equal(w.update([714, ...strikes], 638).prices[4], 685);
  w.resume();
  assert.equal(w.update(strikes, 638).prices[4], 638);
  assert.equal(w.update(strikes, null).prices[4], 638);
  for(let i=0;i<8;i++) w.zoom(1);
  assert.deepEqual(w.update(strikes, 610).prices, strikes);
});
