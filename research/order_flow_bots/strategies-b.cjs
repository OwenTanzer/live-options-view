'use strict';

function pressure(rows) {
  let signed = 0, gross = 0, unknown = 0, quantity = 0, ambiguous = 0;
  for (const c of rows) {
    const direction = c.type === 'C' ? 1 : -1;
    signed += direction * (c.buyPremium - c.sellPremium);
    gross += c.buyPremium + c.sellPremium + c.unknownPremium;
    unknown += c.unknownPremium;
    quantity += c.buy + c.sell + c.unknown;
    ambiguous += c.unknown;
  }
  return gross > 0 && unknown <= 0.3 * gross && ambiguous <= 0.3 * quantity ? signed / gross : null;
}

function centroid(frames) {
  let moment = 0, volume = 0;
  for (const f of frames) for (const c of f.contracts) {
    const size = c.buy + c.sell + c.unknown;
    moment += c.strike * size;
    volume += size;
  }
  return volume > 0 ? moment / volume : null;
}

function residuals(frame) {
  const values = new Map();
  if (frame.t - frame.snapshotTime > 5000) return values;
  const puts = new Map(frame.contracts.filter(c => c.type === 'P').map(c => [c.strike,c]));
  for (const c of frame.contracts.filter(c => c.type === 'C' && Math.abs(c.strike-frame.spot) <= 2)) {
    const p = puts.get(c.strike);
    if (!p || [c,p].some(x => x.time == null || x.price <= 0 || frame.t-x.time > 5000 ||
        Math.abs(x.time-frame.snapshotTime) > 3000) || Math.abs(c.time-p.time) > 1000) continue;
    values.set(c.strike, c.strike+c.price-p.price-frame.spot);
  }
  return values;
}

module.exports = function evaluateB(history) {
  const now = history.at(-1);
  const result = (id,horizon,reason,signal=null) => ({id,horizon,reason,signal});
  const specs = [[7,10],[8,15],[9,15],[10,5],[11,5]];
  if (!now.complete) return specs.map(([id,h]) => result(id,h,'missing decision inputs'));
  const prior = history.at(-2);
  function window(n) {
    const f = history.slice(-n);
    return f.length === n && f.every((v,i) => v.complete && (!i || v.t-f[i-1].t===60000)) ? f : null;
  }
  let absorb = result(7,10,'no classified pressure and reclaim');
  const tail = window(12);
  if (tail) {
    const prices = tail.slice(0,-2).map(f => f.spot);
    const lo = Math.min(...prices), hi = Math.max(...prices);
    const p = pressure(prior.contracts);
    if (p !== null && p < -0.12 && prior.spot <= lo && now.spot > lo) absorb = result(7,10,'selling at low then reclaim','long');
    if (p !== null && p > 0.12 && prior.spot >= hi && now.spot < hi) absorb = result(7,10,'buying at high then rejection','short');
  }
  let breadth = result(8,15,'insufficient agreeing strikes');
  const five = window(6)?.slice(1);
  if (five) {
    const rows = five.flatMap(f => f.contracts);
    const strikes = [...new Set(rows.map(c => c.strike))];
    const buckets = strikes.map(k => rows.filter(c => c.strike===k));
    for (const sign of [1,-1]) {
      const votes = buckets.filter(b => {const p = pressure(b); return p !== null && p*sign > 0.1;});
      if (votes.length < 3 || votes.length < Math.ceil(strikes.length/2)) continue;
      const strongest = votes.reduce((a,b) => {
        const contribution = xs => xs.reduce((sum,c) => sum+(c.type==='C'?1:-1)*(c.buyPremium-c.sellPremium)*sign,0);
        return contribution(a)>=contribution(b)?a:b;
      });
      const remainder = pressure(rows.filter(c => c.strike !== strongest[0].strike));
      if (remainder !== null && remainder*sign > 0.05) breadth = result(8,15,'pressure survives strongest strike removal',sign===1?'long':'short');
    }
  }
  let downside = result(9,15,'no approach with bearish pressure');
  const opening = history[0];
  const below = opening.contracts.filter(c => c.type==='P' && c.strike < opening.spot);
  if (!opening.complete || !below.length || below.some(c => !(c.openOi >= 0) || c.openOi == null)) {
    downside = result(9,15,'opening put-OI reference unavailable');
  } else if (window(2)) {
    const wall = below.reduce((a,b) => a.openOi>b.openOi || (a.openOi===b.openOi && a.strike>b.strike)?a:b);
    const p = pressure(now.contracts);
    if (wall.openOi>0 && prior.spot>wall.strike+0.75 && now.spot<=wall.strike+0.75 &&
        now.spot>wall.strike && p!==null && p < -0.1) downside = result(9,15,'bearish approach crossing to opening observed OI '+wall.strike,'short');
  }
  let migration = result(10,5,'no rotation at nearly stationary spot');
  const ten = window(11);
  if (ten && Math.abs(now.spot-ten[0].spot)<=0.25) {
    const a=centroid(ten.slice(1,6)), b=centroid(ten.slice(6));
    if (a!==null && b!==null && Math.abs(b-a)>=0.4) migration = result(10,5,'fixed-ladder volume centroid migration', b>a?'long':'short');
  }
  let parity = result(11,5,'insufficient synchronized strike pairs');
  if (prior && now.snapshotTime !== prior.snapshotTime && window(2)) {
    const a = residuals(prior), b = residuals(now);
    const diffs = [...b].filter(([k]) => a.has(k)).map(([k,v]) => v-a.get(k));
    if (diffs.length>=2) {
      parity = result(11,5,'residual changes lack consensus');
      if (diffs.every(v => v>0.03)) parity = result(11,5,'two-strike rising parity residual','long');
      if (diffs.every(v => v< -0.03)) parity = result(11,5,'two-strike falling parity residual','short');
    }
  }
  return [absorb,breadth,downside,migration,parity];
};
