// Floor / session-lane views of the Automated tab (site/js/bot-floor.js) and
// the `session_fills` projection on /api/bots (backend/bots.js) they read.
// The view module is loaded the way bots_panel.test.js loads its module: the
// shipped source evaluated against a minimal DOM shim, with its ?v= hash
// pinned in index.html.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

(async () => {
  const { sessionFills, MAX_SESSION_FILLS } = await import('../backend/bots.js');

  // ── sessionFills ────────────────────────────────────────────────────────────
  {
    const now = new Date('2026-11-20T19:52:00Z'); // 14:52 New York (EST)
    const t = (ts, extra = {}) => ({ ts, sym: 'QQQ261120C00610000', side: 'buy', qty: 1, price: 1.5,
      status: 'filled', type: 'call', strike: 610, quote_ts: 'x', execution_request_id: 'r', bid: 1, ask: 2, ...extra });
    const out = sessionFills([
      t('2026-11-20T04:30:00Z'),                       // 23:30 on the 19th in New York
      t('2026-11-20T05:10:00Z', { side: 'sell' }),     // 00:10 on the 20th
      t('2026-11-20T15:00:00Z', { status: 'rejected' }),
      t('2026-11-20T15:01:00Z', { side: 'short' }),
      t('not a time'),
      t('2026-11-20T15:02:00Z', { qty: 0 }),
      t('2026-11-20T15:03:00Z', { price: null }),
      t('2026-11-20T15:04:00Z', { execution_id: 'settle:example', status: undefined,
        instrument_type: { unexpected: true }, type: ['call'], strike: { unexpected: true } }),
    ], now);
    assert.equal(out.session_fills_truncated, false);
    assert.deepEqual(out.session_fills.map(f => f.ts),
      ['2026-11-20T05:10:00.000Z', '2026-11-20T15:04:00.000Z'],
      'only filled buy/sell executions from the New York day, with valid qty and price');
    assert.deepEqual(Object.keys(out.session_fills[0]).sort(),
      ['instrument_type', 'kind', 'price', 'qty', 'side', 'strike', 'sym', 'ts', 'type'],
      'no quote internals or request ids reach the public roster');

    assert.deepEqual(out.session_fills.map(f => f.kind), ['fill', 'settlement']);
    assert.deepEqual([out.session_fills[1].instrument_type, out.session_fills[1].type, out.session_fills[1].strike],
      ['option', null, null], 'unexpected historical values cannot propagate through display fields');
    const many = Array.from({ length: MAX_SESSION_FILLS + 5 }, (_, i) =>
      t(new Date(Date.parse('2026-11-20T15:00:00Z') + i * 1000).toISOString(), { price: i }));
    const capped = sessionFills(many.reverse(), now);
    assert.equal(capped.session_fills.length, MAX_SESSION_FILLS);
    assert.equal(capped.session_fills_truncated, true);
    assert.equal(capped.session_fills[0].price, 5, 'the newest fills are kept');
    assert.deepEqual(sessionFills([], now), { session_fills: [], session_fills_truncated: false });
  }

  // ── view module ─────────────────────────────────────────────────────────────
  const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
  const source = fs.readFileSync(path.join(__dirname, '..', 'site', 'js', 'bot-floor.js'), 'utf8');
  const hash = createHash('sha256').update(source).digest('hex').slice(0, 16);
  assert.ok(html.includes('js/bot-floor.js?v=' + hash), 'bot-floor script URL must match its content hash');
  assert.ok(html.indexOf('js/bots-panel.js?v=') < html.indexOf('js/bot-floor.js?v='),
    'bot-floor.js loads after bots-panel.js');
  for (const id of ['bot-floor', 'bots-cards', 'bots-view']) assert.ok(html.includes(`id="${id}"`), id);

  const els = {};
  const el = (id) => (els[id] ||= { id, innerHTML: '', classList: { hidden: false, toggle(c, on) { this.hidden = on; } } });
  const store = new Map();
  let renders = 0;
  const scope = {
    document: { getElementById: el, querySelectorAll: () => [] },
    localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) },
    renderBots: () => { renders++; },
    fmtQ: (v) => (v == null ? '' : v < 10 ? v.toFixed(2) : v.toFixed(1)),
    escapeHtml: (s) => String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  };
  const names = Object.keys(scope);
  const floor = new Function(...names, `${source}
    return { bfReplay, bfContract, bfSide, bfEtMinutes, bfFloorHtml, bfLanesHtml, renderBotFloor, setBotsView,
      get view() { return bfView; } };`)(...names.map(n => scope[n]));

  assert.equal(floor.view, 'cards', 'cards stay the default view');
  assert.equal(floor.bfContract('QQQ261120P00609500', -2), 'QQQ 609.5P ×2');
  assert.equal(floor.bfContract('IBIT261120C00058000'), 'IBIT 58C');
  assert.equal(floor.bfEtMinutes(Date.parse('2026-07-01T13:30:00Z')), 9 * 60 + 30, 'EDT open');
  assert.equal(floor.bfEtMinutes(Date.parse('2026-11-20T14:30:00Z')), 9 * 60 + 30, 'EST open');

  const C = 'QQQ261120C00611000';
  const P = 'QQQ261120P00609000';
  const fill = (ts, sym, side, qty = 1, price = 1.5) => ({ ts, sym, side, qty, price });

  // Entry, add, reduce, exit, and a position carried in from before the day.
  {
    const bot = {
      alias: 'Ankit', positions: [{ sym: P, qty: 1, avg_price: 1.1, type: 'put' }],
      session_fills: [
        fill('2026-11-20T15:00:00Z', C, 'buy'),
        fill('2026-11-20T15:10:00Z', C, 'buy'),
        fill('2026-11-20T15:20:00Z', C, 'sell'),
        fill('2026-11-20T15:30:00Z', C, 'sell'),
      ],
    };
    const r = floor.bfReplay(bot, Date.parse('2026-11-20T19:52:00Z'));
    assert.deepEqual(r.events.map(e => e.kind), ['entry', 'add', 'reduce', 'exit']);
    const call = r.segments.find(s => s.sym === C);
    assert.equal(call.startMs, Date.parse('2026-11-20T15:00:00Z'));
    assert.equal(call.endMs, Date.parse('2026-11-20T15:30:00Z'));
    const put = r.segments.find(s => s.sym === P);
    assert.deepEqual([put.carried, put.endMs], [true, null], 'held all day with no fills = carried in, still open');
    assert.equal(floor.bfSide(bot), 'PUT');
  }

  // A sign flip closes one segment and opens another; truncation is surfaced.
  {
    const bot = {
      alias: 'Flip', positions: [{ sym: C, qty: -1 }], session_fills_truncated: true,
      session_fills: [fill('2026-11-20T15:00:00Z', C, 'sell', 2)],
    };
    const r = floor.bfReplay(bot, Date.parse('2026-11-20T19:52:00Z'));
    assert.deepEqual(r.events.map(e => [e.kind, e.fill.qty]), [['exit', 1], ['entry', 1]]);
    assert.equal(r.segments.length, 2);
    assert.equal(r.coverage, 'partial');
    assert.equal(r.truncatedFromMs, Date.parse('2026-11-20T15:00:00Z'));
    assert.equal(floor.bfSide(bot), 'SHORT CALL');
    const lanes = floor.bfLanesHtml([{ bot, complete: true, equity: 100 }], null, r.nowMs);
    assert.ok(lanes.includes('call held (long or short)') && lanes.includes('title="Short QQQ 611C"'));
    const exit = lanes.match(/bf-mk-reversal-out" style="left:([\d.]+)%"/);
    const entry = lanes.match(/bf-mk-reversal-in" style="left:([\d.]+)%"/);
    assert.ok(exit && entry && exit[1] === entry[1], 'reversal markers share the real timestamp');
    assert.ok(html.includes('.bf-mk-reversal-out { transform: translateX(calc(-100% - 2px)); }'));
    assert.ok(html.includes('.bf-mk-reversal-in { transform: translateX(2px); }'));

  }

  // Rendering: escaping, recent-entry edge, tape, counters, empty sessions.
  {
    const now = Date.parse('2026-11-20T19:52:00Z');
    const evil = '<img src=x onerror=alert(1)>';
    const scored = [
      { complete: true, equity: 10170, bot: { alias: evil, starting_balance: 10000, positions: [{ sym: C, qty: 1, avg_price: 1.7 }],
        session_fills: [fill('2026-11-20T19:44:00Z', C, 'buy', 1, 1.7)] } },
      { complete: true, equity: 9950, bot: { alias: 'Stop', starting_balance: 10000, positions: [],
        session_fills: [fill('2026-11-20T15:05:00Z', C, 'buy', 1, 1.42), fill('2026-11-20T19:41:00Z', C, 'sell', 1, 0.92)] } },
      { complete: false, equity: null, bot: { alias: 'Pending', starting_balance: 10000, positions: [{ sym: P, qty: 1 }], session_fills: [] } },
    ];
    const out = floor.bfFloorHtml(scored, { as_of: '2026-11-20T19:51:30Z' }, () => 1.77, now);
    assert.ok(!out.includes(evil) && out.includes('&lt;img'), 'aliases are escaped');
    assert.ok(out.includes('bf-edge-in') && out.includes('bf-edge-out'), 'recent entry and exit tiles are edged');
    assert.ok(out.includes('14:44') && out.includes('14:41'), 'events carry New York times');
    assert.ok(/ENTRIES TODAY<\/div>\s*<div class="bf-stat-value bf-red">2</.test(out));
    assert.ok(/EXITS TODAY<\/div>\s*<div class="bf-stat-value bf-blue">1</.test(out));
    assert.ok(out.includes('1 pending'), 'unmarked accounts are counted, not summed');
    assert.ok(out.includes('+$120.00'), 'floor P&L sums only marked accounts');
    assert.ok(out.includes('aria-hidden="true"'), 'the crawling tape is decorative; the log is the accessible copy');

    const lanes = floor.bfLanesHtml(scored, null, now);
    assert.ok(lanes.includes('bf-seg-closed') && lanes.includes('bf-seg-put'));
    assert.ok(!lanes.includes(evil));
    assert.ok(floor.bfFloorHtml([], null, () => null, now).includes('NO FILLS YET THIS SESSION'));
  }

  // Missing backend fields and partial results must not look like full-day zeroes.
  {
    const now = Date.parse('2026-11-20T19:52:00Z');
    const scored = [{ complete: true, equity: 100, bot: { alias: 'Old API', starting_balance: 100,
      positions: [{ sym: C, qty: 1 }] } }];
    const oldFloor = floor.bfFloorHtml(scored, null, () => null, now);
    const oldLanes = floor.bfLanesHtml(scored, null, now);
    assert.ok(oldFloor.includes('Fill history unavailable.'));
    assert.ok(!oldFloor.includes('NO FILLS TODAY'));
    assert.ok(oldLanes.includes('History unavailable') && !oldLanes.includes('class="bf-seg '));
    const bot = { alias: 'Settled', starting_balance: 100, positions: [], session_fills_truncated: true,
      session_fills: [{ ...fill('2026-11-20T19:00:00Z', C, 'sell', 1, 0), kind: 'settlement' }] };
    const replay = floor.bfReplay(bot, now);
    assert.deepEqual(replay.events.map(e => e.kind), ['settlement']);
    assert.equal(replay.segments[0].settled, true, 'settlement closes the book lane without showing an execution exit');
    const partial = [{ ...scored[0], bot }];
    assert.ok(floor.bfFloorHtml(partial, null, () => null, now).includes('0<small> · partial</small>'));
    assert.ok(floor.bfLanesHtml(partial, null, now).includes('Partial fill history.'));
  }

  // View switching persists per viewer and only draws when selected.
  {
    floor.renderBotFloor([], null, () => null);
    assert.equal(el('bot-floor').innerHTML, '', 'nothing drawn while cards are showing');
    floor.setBotsView('lanes');
    assert.equal(store.get('bots-view'), 'lanes');
    assert.equal(el('bots-cards').classList.hidden, true);
    assert.equal(el('bot-floor').classList.hidden, false);
    assert.ok(renders > 0, 'switching re-renders from the current roster');
    floor.renderBotFloor([], null, () => null);
    assert.ok(el('bot-floor').innerHTML.includes('SESSION LANES'));
    floor.setBotsView('bogus');
    assert.equal(floor.view, 'cards');
  }

  console.log('PASS bot floor: session fills projection, replay, floor + lanes rendering, view switch');
})().catch((err) => { console.error(err); process.exit(1); });
