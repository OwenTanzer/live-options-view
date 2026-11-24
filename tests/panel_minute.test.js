const test = require('node:test');
const assert = require('node:assert/strict');
const { rows } = require('../site/js/panel-minute.js');
const now = Date.parse('2026-11-24T15:30:00Z');
const old = {symbol:'BTC', source:'coinbase', price:1, high_24h:5};
const baseline = {constructed_at:'2026-11-24T15:00:00Z', coins:[old]};
const overlay = {feed:'crypto',schema_version:'panel_minute.v1',baseline_at:baseline.constructed_at,
  completed_at:new Date(now).toISOString(),prices:{BTC:{source:'coinbase',price_usd:2,provider_observed_at:new Date(now-5000).toISOString()}}};
test('minute marks age independently of unchanged enrichment', () => {
  assert.match(rows('crypto',baseline,overlay,now)[0].minuteLabel,/fresh/);
  assert.match(rows('crypto',baseline,overlay,now+60000)[0].minuteLabel,/stale/);
  assert.equal(rows('crypto',baseline,overlay,now)[0].high_24h,5);
  assert.equal(rows('crypto',baseline,overlay,now+120000)[0],old);
  assert.equal(rows('crypto',baseline,{...overlay, baseline_at:'other'},now)[0],old);
  assert.equal(old.price,1);
});
test('missing price and old provider times cannot promote the hour', () => {
  const marks={...overlay,prices:{BTC:{source:'coinbase',price_usd:null}}};
  assert.equal(rows('crypto',baseline,marks,now)[0].price,null);
  const newer={...baseline,coins:[{...old,provider_observed_at:new Date(now).toISOString()}]};
  assert.equal(rows('crypto',newer,overlay,now)[0].price,1);
});
