// Proves formatMomentum() (site/shared.js) -- the pure formatting/state
// function both the options-chain header and the QQQ price-strip tile call
// (see site/index.html's renderHeatmap/updateQqqMomentumLine). Mirrors
// tests/vwap_rvol_display.test.js's structure and rationale: pure, DOM-free
// function, testable the simple direct-require way.
const assert = require('node:assert/strict');
const { formatMomentum } = require('../site/shared.js');

const NOW = Date.parse('2026-07-30T14:32:00Z');

function um(overrides = {}) {
  return {
    symbol: 'QQQ',
    spot: 402.0, spot_ts: '2026-07-30T14:32:00Z',
    momentum: {
      status: 'ok', return_pct: 0.42, lookback_minutes: 60.0,
      anchor_age_minutes: 61.0, sample_count: 30, direction: 'up',
    },
    freshness: 'live',
    ...overrides,
  };
}

// -- unavailable: no underlying_market at all -------------------------------
{
  const result = formatMomentum(null, NOW);
  assert.equal(result.state, 'fallback');
  assert.match(result.text, /unavailable/i);
}

// -- closed: the observation is clearly a leftover from a prior session ----
{
  const stale = um({ spot_ts: '2026-07-29T20:00:00Z' });
  const result = formatMomentum(stale, NOW);
  assert.equal(result.state, 'closed');
  assert.match(result.text, /session closed/i);
}

// -- ok, up direction --------------------------------------------------------
{
  const result = formatMomentum(um(), NOW);
  assert.equal(result.state, 'live');
  assert.match(result.text, /▲/);
  assert.match(result.text, /\+0\.42%/);
  assert.match(result.text, /\(60m\)/);
}

// -- ok, down direction, negative return rendered without a double sign ----
{
  const result = formatMomentum(um({ momentum: { status: 'ok', return_pct: -0.75, lookback_minutes: 60.0, direction: 'down' } }), NOW);
  assert.match(result.text, /▼/);
  assert.match(result.text, /-0\.75%/);
  assert.doesNotMatch(result.text, /--0\.75/);
}

// -- ok, flat direction -------------------------------------------------------
{
  const result = formatMomentum(um({ momentum: { status: 'ok', return_pct: 0.01, lookback_minutes: 60.0, direction: 'flat' } }), NOW);
  assert.match(result.text, /→/);
}

// -- warming_up: shown explicitly, not a fabricated return ------------------
{
  const result = formatMomentum(um({ momentum: { status: 'warming_up' } }), NOW);
  assert.match(result.text, /warming up/i);
  assert.doesNotMatch(result.text, /%/);
}

// -- stale_anchor: shown explicitly, not silently identical to "ok" ---------
{
  const result = formatMomentum(um({ momentum: { status: 'stale_anchor' } }), NOW);
  assert.match(result.text, /stale/i);
}

// -- no_data: explicit dash, not blank ---------------------------------------
{
  const result = formatMomentum(um({ momentum: { status: 'no_data' } }), NOW);
  assert.match(result.text, /—/);
}

// -- stale freshness (collector-reported) -- state reflects it, numbers still shown --
{
  const result = formatMomentum(um({ freshness: 'stale' }), NOW);
  assert.equal(result.state, 'stale');
  assert.match(result.text, /\+0\.42%/);
}

console.log('PASS momentum display formatting -- unavailable/closed/live/stale/up/down/flat/warming_up/stale_anchor/no_data all render explicitly');

// Exercise the actual cell renderer with a controlled clock and a minimal DOM.
const fs = require('node:fs'), vm = require('node:vm');
const render = vm.runInNewContext(fs.readFileSync(require.resolve('../site/js/momentum-strip.js'),'utf8') +
  '\npaintMomentumStrip', {formatMomentum});
function element() {
  return {dataset:{}, children:[], ownerDocument:{createElement:element},
    replaceChildren(...items){this.children=items;}, appendChild(item){this.children.push(item);},
    removeAttribute(name){delete this[name];}, setAttribute(name,value){this[name]=value;}};
}
const market = um({momentum_windows:{observed_at:new Date(NOW).toISOString(), readings:{
  '5m':{status:'ok',direction:'down',return_pct:-1.75},
  '9m':{status:'ok',direction:'flat',return_pct:0}, '15m':{status:'warming_up'}}}});
const strip = element(), card = element();
render(strip,market,{at:NOW}); render(card,market,{at:NOW,compact:true});
assert.equal(strip.children.length,7);
assert.equal(strip.children[1].textContent,'5m ▼ -1.75%');
assert.equal(strip.children[2].textContent,'9m → +0.00%');
assert.equal(strip.children[3].textContent,'15m warming');
assert.equal(card.children[1]['aria-label'],'5m down -1.75%');
assert.equal(card.children[1].textContent,'5m ▼');
render(strip,market,{at:NOW+150001});assert.equal(strip.dataset.state,'retained');
render(strip,market,{at:NOW-1});assert.equal(strip.children[1].textContent,'5m —');

const legacyCard = element();
render(legacyCard,um(),{at:NOW,retained:true});
assert.equal(legacyCard.dataset.state,'retained');
assert.match(legacyCard.textContent,/\+0.42%.*retained/);
render(legacyCard,um(),{at:NOW+5*3600000,retained:true});
assert.equal(legacyCard.dataset.state,'closed');
render(legacyCard,null,{at:NOW,retained:true});
assert.equal(legacyCard.dataset.state,'fallback');
