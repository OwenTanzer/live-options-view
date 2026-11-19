// Strike profile scale and layout (accepted design, Chej 2026-11-18): the scale at a time
// depends only on inputs through that time, so play, direct seek, backward seek, reload
// and reconnect agree; a snapshot-only change can raise it; 50% shares the 100% axis.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const M = require('../site/shared/order-flow-model.js');
const P = require('../site/shared/order-flow-profile.js');
const R = require('../site/shared/order-flow-render.js');

const replayPath = path.join(__dirname, '..', 'site/order-flow-replay.json');
const replayText = fs.readFileSync(replayPath, 'utf8');
const real = M.decodeReplay(JSON.parse(replayText));
const realScale = P.replayScale(real);
const OPEN = real.coverage.open;
const AT_1142 = OPEN + (2 * 60 + 12) * 60000;

// Reference: maximum of the displayed view over every input time <= t, via replayAt itself.
function bruteForce(r, t) {
  const times = new Set();
  for (let i = 0; i < r.cols.t.length && r.cols.t[i] <= t; i++) times.add(r.cols.t[i]);
  for (const s of r.snapshots) if (s.t <= t) times.add(s.t);
  let flow = 0, conc = 0;
  for (const u of times) {
    const e = P.viewExtent(M.replayAt(r, u));
    flow = Math.max(flow, e.flow);
    conc = Math.max(conc, e.conc);
  }
  return { flow, conc };
}
const close = (a, b) => Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b));
const sameScale = (a, b) => close(a.flow, b.flow) && close(a.conc, b.conc);

// Small replay: two strikes, call+put each; snapshots at +0s, +60s (gamma doubles, no prints
// between), then none until +400s so the chain goes stale.
const T0 = Date.UTC(2026, 8, 25, 13, 30);
function synthetic() {
  const contracts = [
    { symbol: 'QQQ260925C00745000', strike: 745, type: 'C', expiration: '2026-09-25' },
    { symbol: 'QQQ260925P00745000', strike: 745, type: 'P', expiration: '2026-09-25' },
    { symbol: 'QQQ260925C00746000', strike: 746, type: 'C', expiration: '2026-09-25' },
    { symbol: 'QQQ260925P00746000', strike: 746, type: 'P', expiration: '2026-09-25' },
  ];
  const snap = (dt, g) => ({ t: T0 + dt, key: 'k' + dt, spot: 745.5, gamma: [g, g, g / 2, g / 2], oi: [100, 50, 80, null] });
  const prints = [ // [dt ms, contract, size, side 0 buy 1 sell 2 unknown]
    [1000, 0, 10, 1], [1000, 1, 4, 0], [5000, 2, 7, 2], [30000, 0, 3, 0], [30000, 3, 12, 1],
    [90000, 1, 20, 1], [250000, 2, 40, 1], [400000, 0, 5, 2],
  ];
  let prev = T0;
  const json = {
    schema: 'order-flow-replay/v2', session: '2026-09-25', multiplier: 100, contracts,
    oi: contracts.map(() => []), snapshots: [snap(0, 0.02), snap(60000, 0.04), snap(400000, 0.01)],
    coverage: { open: T0, close: T0 + 600000, first_print: T0 + 1000, last_print: T0 + 400000, gaps: [] },
    prints: { count: prints.length, t0: T0, dt: [], contract: [], size: [], cents: [], side: [] },
  };
  for (const [dt, c, size, side] of prints) {
    json.prints.dt.push(T0 + dt - prev); prev = T0 + dt;
    json.prints.contract.push(c); json.prints.size.push(size); json.prints.cents.push(100); json.prints.side.push(side);
  }
  return json;
}

test('synthetic replay: scale equals the brute-force maximum at every input time and between them', () => {
  const r = M.decodeReplay(synthetic()), scale = P.replayScale(r);
  for (let dt = -1000; dt <= 600000; dt += 2500) {
    const t = T0 + dt;
    assert.ok(sameScale(scale(t), bruteForce(r, t)), `t=+${dt}ms`);
  }
});

test('snapshot-only change raises the scale with no new print', () => {
  const r = M.decodeReplay(synthetic()), scale = P.replayScale(r);
  const before = scale(T0 + 59999), at = scale(T0 + 60000);
  assert.ok(at.flow > before.flow, 'flow scale grows when gamma doubles');
  assert.ok(at.conc > before.conc, 'OI scale grows when gamma doubles');
  // No print between 30 s and 90 s: the change came from the snapshot alone.
  assert.ok(r.cols.t.every((t) => t <= T0 + 30000 || t >= T0 + 90000));
  assert.ok(sameScale(at, bruteForce(r, T0 + 60000)));
});

test('stale chain removes values but never shrinks the scale', () => {
  const r = M.decodeReplay(synthetic()), scale = P.replayScale(r);
  const view = M.replayAt(r, T0 + 250000);
  assert.equal(view.snapshot.stale, true);
  assert.ok(view.strikes.every((s) => s.flow[1].base === null));
  assert.deepEqual(scale(T0 + 250000), scale(T0 + 200000));
});

test('Sept 25: play, direct seek, backward seek and reload give the same scale at 11:42', () => {
  const direct = realScale(AT_1142);
  let played = null;
  for (let t = OPEN + 15000; t <= AT_1142; t += 180 * 60) played = realScale(t); // page frames at 60x
  played = realScale(AT_1142);
  const backward = (realScale(real.coverage.close), realScale(AT_1142));
  const reloaded = P.replayScale(M.decodeReplay(JSON.parse(replayText)))(AT_1142);
  assert.deepEqual(played, direct);
  assert.deepEqual(backward, direct);
  assert.deepEqual(reloaded, direct);
  // It bounds what is drawn at 11:42 and is fixed by data through 11:42 only.
  const now = P.viewExtent(M.replayAt(real, AT_1142));
  assert.ok(direct.flow >= now.flow && direct.conc >= now.conc);
  assert.ok(realScale(real.coverage.close).flow > direct.flow, 'later data does not leak into 11:42');
});

test('Sept 25 opening half hour matches the brute-force maximum over replayAt', () => {
  for (const min of [0, 1, 5, 12, 30]) {
    const t = OPEN + min * 60000 + 7000;
    assert.ok(sameScale(realScale(t), bruteForce(real, t)), `09:${30 + min}`);
  }
});

test('scale never decreases through the session', () => {
  let prev = { flow: 0, conc: 0 };
  for (let t = OPEN - 3600000; t <= real.coverage.close; t += 60000) {
    const s = realScale(t);
    assert.ok(s.flow >= prev.flow && s.conc >= prev.conc);
    prev = s;
  }
});

test('50% and 100% participation share one scale; 50% positions are half of 100%', () => {
  const view = M.replayAt(real, AT_1142), scale = realScale(AT_1142);
  const full = P.profileRows(view, 1, scale), half = P.profileRows(view, 0.5, scale);
  for (let i = 0; i < full.length; i++) {
    if (!full[i].flow) { assert.equal(half[i].flow, null); continue; }
    for (const k of ['x', 'xLow', 'xHigh']) assert.ok(close(half[i].flow[k], full[i].flow[k] / 2));
    assert.deepEqual(half[i].conc, full[i].conc);
  }
  for (const r of full) if (r.flow) for (const k of ['x', 'xLow', 'xHigh']) assert.ok(Math.abs(r.flow[k]) <= 1);
});

test('profile rows cross-check against the rendered table', () => {
  const view = M.replayAt(real, AT_1142), scale = realScale(AT_1142);
  const html = R.ladderRowsHtml(view, 1, null);
  for (const r of P.profileRows(view, 1, scale)) {
    const row = html.split('<tr').find((h) => h.includes(`data-strike="${r.strike}"`));
    assert.ok(row.includes(`<td>${R.money(r.flow.base)}</td>`), `flow ${r.strike}`);
    assert.ok(row.includes(R.unsigned(r.conc.value)), `conc ${r.strike}`);
    assert.ok(row.includes(R.money(r.flow.low) + ' … ' + R.money(r.flow.high)), `endpoints ${r.strike}`);
    assert.equal(r.flow.sign === 0, r.flow.uncertain);
  }
});

test('missing inputs are gaps (null), partial coverage carries k/n', () => {
  const r = M.decodeReplay(synthetic());
  const rows = P.profileRows(M.replayAt(r, T0 + 30000), 1, P.replayScale(r)(T0 + 30000));
  const s746 = rows.find((x) => x.strike === 746);
  assert.equal(s746.conc.coverage, '1/2'); // put OI is null
  assert.equal(s746.conc.partial, true);
  const stale = P.profileRows(M.replayAt(r, T0 + 250000), 1, P.replayScale(r)(T0 + 250000));
  assert.ok(stale.every((x) => x.flow === null && x.conc === null));
});

test('live: running maximum survives a disconnect and a reload, and resets on a new day', () => {
  const store = new Map(), storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  const views = [AT_1142 - 120000, AT_1142 - 60000, AT_1142].map((t) => M.replayAt(real, t));
  const a = P.liveScale(storage);
  a.update(views[0], '2026-09-25', 1);
  const peak = a.update(views[1], '2026-09-25', 2);
  // Disconnect: no updates, then the next poll may show smaller values; the scale holds.
  const shrunk = JSON.parse(JSON.stringify(views[2]));
  for (const s of shrunk.strikes) { s.flow[1] = { ...s.flow[1], low: 0, high: 0 }; s.concentration.value = 0; }
  assert.deepEqual(a.update(shrunk, '2026-09-25', 3), peak);
  // Reload: a new tracker on the same storage resumes the same scale and start time.
  const b = P.liveScale(storage);
  assert.deepEqual(b.update(shrunk, '2026-09-25', 4), peak);
  assert.equal(peak.since, 1);
  // New session day starts over.
  assert.equal(b.update(shrunk, '2026-09-28', 5).flow, 0);
  // Broken storage does not break the page.
  const bad = P.liveScale({ getItem() { throw Error('blocked'); }, setItem() { throw Error('blocked'); } });
  assert.ok(bad.update(views[0], '2026-09-25', 1).flow > 0);
});

test('niceCeil is monotone and covers its input', () => {
  let prev = 0;
  for (let v = 1; v < 1e10; v *= 1.37) {
    const n = P.niceCeil(v);
    assert.ok(n >= v && n >= prev);
    prev = n;
  }
  assert.equal(P.niceCeil(0), 0);
  assert.equal(P.niceCeil(2.3e9), 2.5e9);
});

test('text summary names signs, gaps and the selected strike', () => {
  const view = M.replayAt(real, AT_1142);
  const text = P.profileSummary(view, 1, realScale(AT_1142), 744, (t) => new Date(t).toISOString().slice(11, 19), R.money);
  assert.match(text, /Flow scale ±\$[\d.]+[BMk], OI scale 0–\$[\d.]+[BMk] per 1% QQQ\./);
  assert.match(text, /Selected 744: flow [−+]\$/);
});
