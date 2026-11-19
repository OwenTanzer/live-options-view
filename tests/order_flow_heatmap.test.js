// Replay heatmap (GEX increment 2, Chej 2026-11-18): a selected cell agrees with the strike
// profile and the table at the same time and strike; cells are as-of-time; missing or stale
// inputs are gaps, not zero; the colour scale is path-independent and shared by 50%/100%.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const M = require('../site/shared/order-flow-model.js');
const P = require('../site/shared/order-flow-profile.js');
const R = require('../site/shared/order-flow-render.js');
const H = require('../site/shared/order-flow-heatmap.js');

const replayText = fs.readFileSync(path.join(__dirname, '..', 'site/order-flow-replay.json'), 'utf8');
const real = M.decodeReplay(JSON.parse(replayText));
const grid = H.replayGrid(real);
const OPEN = real.coverage.open;
const clock = (t) => new Date(t).toISOString().slice(11, 19);

const T0 = Date.UTC(2026, 8, 25, 13, 30);
function synthetic() {
  const contracts = [745, 746].flatMap((k) => ['C', 'P'].map((type) => ({
    symbol: `QQQ260925${type}00${k}000`, strike: k, type, expiration: '2026-09-25' })));
  const snap = (dt, g) => ({ t: T0 + dt, key: 'k' + dt, spot: 745.5, gamma: [g, g, g / 2, g / 2], oi: [100, 50, 80, null] });
  const prints = [[1000, 0, 10, 1], [1000, 1, 4, 0], [5000, 2, 7, 2], [30000, 0, 3, 0], [90000, 1, 20, 1], [250000, 2, 40, 1]];
  let prev = T0;
  const json = {
    schema: 'order-flow-replay/v2', session: '2026-09-25', multiplier: 100, contracts,
    oi: contracts.map(() => []), snapshots: [snap(0, 0.02), snap(60000, 0.04), snap(400000, 0.01)],
    coverage: { open: T0, close: T0 + 600000, first_print: T0 + 1000, last_print: T0 + 250000, gaps: [] },
    prints: { count: prints.length, t0: T0, dt: [], contract: [], size: [], cents: [], side: [] },
  };
  for (const [dt, c, size, side] of prints) {
    json.prints.dt.push(T0 + dt - prev); prev = T0 + dt;
    json.prints.contract.push(c); json.prints.size.push(size); json.prints.cents.push(100); json.prints.side.push(side);
  }
  return M.decodeReplay(json);
}

test('grid covers the session in one-minute bins over the actual strikes', () => {
  assert.equal(grid.times.length, 390);
  assert.equal(grid.times[0], OPEN + 60000);
  assert.equal(grid.times.at(-1), real.coverage.close);
  assert.deepEqual(grid.strikes, [747, 746, 745, 744, 743, 742, 741, 740]);
});

test('selected cell agrees with the strike profile and the table at the same time and strike', () => {
  let checked = 0;
  for (const j of [0, 14, 131, 132, 200, 389]) {
    const t = grid.times[j];
    const view = M.replayAt(real, t); // what the page shows after selecting this cell (end = cell time)
    for (const p of [1, 0.5]) {
      const rows = P.profileRows(view, p, P.replayScale(real)(t));
      const html = R.ladderRowsHtml(view, p, null);
      grid.strikes.forEach((strike, k) => {
        const cell = H.cellAt(grid, j, k, p, t);
        const s = view.strikes.find((x) => x.strike === strike);
        const row = rows.find((x) => x.strike === strike);
        const tr = html.split('<tr').find((h) => h.includes(`data-strike="${strike}"`));
        assert.deepEqual(cell.flow, s.flow[p], `flow ${clock(t)} ${strike}`);
        assert.deepEqual(cell.concentration, s.concentration);
        if (cell.kind === 'value') {
          assert.equal(row.flow.base, cell.flow.base);
          assert.equal(row.flow.low, cell.flow.low);
          assert.equal(row.flow.high, cell.flow.high);
          assert.ok(tr.includes(`<td>${R.money(cell.flow.base)}</td>`));
          assert.ok(tr.includes(R.money(cell.flow.low) + ' … ' + R.money(cell.flow.high)));
          assert.equal(row.flow.sign === 0, cell.flow.uncertain);
        } else {
          assert.equal(row.flow, null);
          assert.match(tr, /no coverage/);
        }
        checked++;
      });
    }
  }
  assert.equal(checked, 6 * 2 * 8);
});

test('11:42 cell at 744: the summary carries value, endpoints, sign, coverage and snapshot', () => {
  const j = H.lastColumn(grid, OPEN + 132 * 60000);
  const cell = H.cellAt(grid, j, grid.strikes.indexOf(744), 1, grid.times[j]);
  const text = H.cellSummary(cell, 1, clock, R.money);
  const s = M.replayAt(real, grid.times[j]).strikes.find((x) => x.strike === 744);
  assert.ok(text.includes(R.money(s.flow[1].base)) && text.includes(R.money(s.flow[1].low) + ' … ' + R.money(s.flow[1].high)));
  assert.match(text, /sign not determined|positive|negative/);
  assert.match(text, /flow covers 2\/2 contracts/);
  assert.match(text, /chain snapshot \d\d:\d\d:\d\d ET, \d+ s old/);
});

test('cells after the displayed time are not shown', () => {
  const t = OPEN + 45 * 60000 + 30000; // 10:15:30
  const j = H.lastColumn(grid, t);
  assert.equal(grid.times[j], OPEN + 45 * 60000);
  assert.equal(H.cellAt(grid, j, 0, 1, t).kind === 'future', false);
  assert.equal(H.cellAt(grid, j + 1, 0, 1, t).kind, 'future');
  assert.equal(H.lastColumn(grid, OPEN + 59999), -1);
});

test('missing or stale inputs are gaps, not zero', () => {
  const r = synthetic(), g = H.replayGrid(r);
  // Chain snapshot at +60 s goes stale after +180 s; the next one is at +400 s.
  const j = H.lastColumn(g, T0 + 240000);
  const cell = H.cellAt(g, j, 0, 1, T0 + 240000);
  assert.equal(cell.kind, 'missing');
  assert.equal(cell.flow.base, null);
  assert.equal(H.cellColor(cell.flow.base, 1), null);
  assert.equal(g.cols[j].snapshot.stale, true);
  assert.match(H.cellSummary(cell, 1, clock, R.money), /no coverage \(missing or stale input, not zero\)/);
  // A real zero is a value, drawn with a colour.
  const zero = H.cellAt(g, 0, 0, 1, T0 + 60000);
  assert.equal(zero.kind, 'value');
  assert.ok(H.cellColor(0, 1));
});

test('colour scale: same at 50% and 100%, never shrinks, and seek order does not matter', () => {
  let prev = 0;
  for (let t = OPEN; t <= real.coverage.close; t += 60000) {
    const s = H.gridScale(grid, t);
    assert.ok(s >= prev);
    prev = s;
  }
  const at = OPEN + 132 * 60000;
  const direct = H.gridScale(grid, at);
  H.gridScale(grid, real.coverage.close);
  assert.equal(H.gridScale(grid, at), direct);
  assert.equal(H.gridScale(H.replayGrid(M.decodeReplay(JSON.parse(replayText))), at), direct);
  // Every 100% and 50% point estimate up to that time fits inside the scale.
  for (let j = 0; j <= H.lastColumn(grid, at); j++)
    for (const c of grid.cols[j].cells) for (const p of [1, 0.5]) if (c.flow[p].base != null) assert.ok(Math.abs(c.flow[p].base) <= direct);
});

test('spot overlay comes from the same snapshot as the cell values', () => {
  for (const j of [0, 100, 389]) {
    const v = M.replayAt(real, grid.times[j]);
    assert.equal(grid.cols[j].spot, v.contracts.find((c) => c.spot != null)?.spot ?? null);
  }
  const r = synthetic(), g = H.replayGrid(r);
  assert.equal(g.cols[H.lastColumn(g, T0 + 240000)].spot, null); // stale chain: no spot, a gap in the line
});

test('five-minute grid (phone) is the one-minute grid sampled at the same times', () => {
  const g5 = H.replayGrid(real, 5 * 60000);
  assert.equal(g5.times.length, 78);
  for (const j of [0, 20, 77]) {
    const j1 = grid.times.indexOf(g5.times[j]);
    assert.deepEqual(g5.cols[j], grid.cols[j1]);
  }
});

test('cell colour is diverging and monotone in magnitude', () => {
  assert.match(H.cellColor(5, 10), /^rgba\(98,227,189,/);
  assert.match(H.cellColor(-5, 10), /^rgba\(255,150,124,/);
  const alpha = (c) => Number(c.match(/,([\d.]+)\)$/)[1]);
  assert.ok(alpha(H.cellColor(9, 10)) > alpha(H.cellColor(1, 10)));
  assert.equal(alpha(H.cellColor(20, 10)), 1);
});
