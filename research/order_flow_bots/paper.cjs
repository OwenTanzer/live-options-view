'use strict';

const MINUTE = 60_000;
const validSpot = s => Number.isFinite(s?.spot) && s.spot > 0;
const validTime = Number.isFinite;

const { gaps } = require('./outcomes.cjs');

function paper(rows, replay, { costBpsPerSide = 1, maxAgeMs = 120_000, initialCash = 10_000 } = {}) {
  if (!Number.isFinite(costBpsPerSide) || costBpsPerSide < 0 || costBpsPerSide >= 10000 ||
      !Number.isFinite(maxAgeMs) || maxAgeMs <= 0 || !Number.isFinite(initialCash) || initialCash <= 0)
    throw new RangeError('Invalid paper execution assumptions');
  const { coverage } = replay, { open, close } = coverage;
  if (!validTime(open) || !validTime(close) || close <= open) throw Error('Invalid session bounds');
  const snaps = replay.snapshots.filter(s => s.t >= open && s.t <= close).slice().sort((a, b) => a.t - b.t);
  // A timestamp with multiple records lacks an unambiguous ordering and fill price.
  const observations = [];
  for (const snap of snaps) {
    const prev = observations.at(-1);
    if (prev && snap.t === prev.t) prev.spot = null;
    else observations.push({ t: snap.t, spot: snap.spot });
  }
  const outages = gaps(replay);
  const clear = (start, end) => !outages.some(g => (g.start === null || g.start <= end) && (g.end === null || g.end >= start));
  const first = (time, inclusive) => {
    let lo = 0, hi = observations.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (inclusive ? observations[mid].t < time : observations[mid].t <= time) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  };
  const nextUsableSpot = (index, deadline) => {
    for (; index < observations.length && observations[index].t <= deadline; index++) {
      if (validSpot(observations[index])) return observations[index];
    }
    return null;
  };
  const price = (spot, orderSide) => spot * (1 + orderSide * costBpsPerSide / 10000);
  const accounts = Array.from({length:11}, (_, i) => ({ id:i+1, initialCash, realizedPnl:0,
    closed:0, skipped:0, unresolved:0, endingCash:initialCash, openPosition:null }));
  const lockedUntil = Array(11).fill(-Infinity), seen = new Set(), trades = [];
  for (const row of rows.filter(r => r.episode).slice().sort((a,b) => a.t - b.t || a.id - b.id)) {
    if (!Number.isInteger(row.id) || row.id < 1 || row.id > 11) throw Error('Invalid paper slot');
    const i = row.id - 1, account = accounts[i], due = row.t + row.horizon * MINUTE;
    const tr = { id:row.id, session:replay.session, signal:row.signal, decisionAt:row.t, due,
      status:'skipped', reason:null, breakoutAt:null, direction:null, entry:null, exit:null, pnl:null };
    trades.push(tr);
    const skip = reason => { tr.reason=reason; account.skipped++; };
    const key = row.id + '/' + row.t;
    if (seen.has(key)) { skip('duplicate slot decision'); continue; }
    seen.add(key);
    if (row.session !== replay.session || !row.complete || !validTime(row.t) || row.t < open ||
        !Number.isInteger(row.horizon) || row.horizon <= 0 || due > close ||
        !['long','short','range'].includes(row.signal) || (row.signal === 'range') !== (row.id === 6)) {
      skip('ineligible session, signal or horizon'); continue;
    }
    if (row.t < lockedUntil[i]) { skip('previous order or position outstanding'); continue; }
    let start = row.t, direction = row.signal, index = first(start, false);
    if (direction === 'range') {
      const from = start - 5 * MINUTE, before = observations.slice(first(from, true),index);
      if (from < open || before.length < 2 || before[0].t - from > maxAgeMs ||
          start - before.at(-1).t > maxAgeMs || !clear(from,start) ||
          before.some((s,j) => !validSpot(s) || (j && s.t-before[j-1].t > maxAgeMs))) {
        skip('incomplete pre-signal range'); continue;
      }
      const high = Math.max(...before.map(s => s.spot)), low = Math.min(...before.map(s => s.spot));
      tr.range = { from, to:start, high, low };
      let prev = start, failedAt = null;
      for (; index < observations.length && observations[index].t < due; index++) {
        const obs = observations[index];
        if (!validSpot(obs) || obs.t - prev > maxAgeMs || !clear(prev, obs.t)) {
          failedAt = Math.min(obs.t, prev + maxAgeMs); break;
        }
        if (obs.spot > high || obs.spot < low) {
          tr.breakoutAt = obs.t; direction = obs.spot > high ? 'long' : 'short';
          start = obs.t; index++; break;
        }
        prev = obs.t;
      }
      if (tr.breakoutAt == null) {
        lockedUntil[i] = failedAt ?? due;
        skip(failedAt != null || due-prev>maxAgeMs || !clear(prev,due)
          ? 'incomplete breakout observation' : 'no sampled breakout before expiry'); continue;
      }
    }
    const entry = nextUsableSpot(index, Math.min(due, start + maxAgeMs));
    if (!validSpot(entry) || entry.t >= due || entry.t-start > maxAgeMs || !clear(start,entry.t)) {
      lockedUntil[i] = Math.min(due, start + maxAgeMs, entry?.t ?? Infinity);
      skip('next fresh pre-expiry entry unavailable'); continue;
    }
    const side = direction === 'long' ? 1 : -1, entryPrice = price(entry.spot, side);
    lockedUntil[i] = entry.t;
    if (entryPrice > account.endingCash) { skip('insufficient one-share collateral'); continue; }
    tr.direction = direction; tr.entry = { t:entry.t, spot:entry.spot, price:entryPrice };
    account.endingCash -= side * entryPrice;
    const exit = nextUsableSpot(first(due, true), due + maxAgeMs);
    if (!validSpot(exit) || exit.t-due > maxAgeMs || !clear(due,exit.t)) {
      tr.status='unresolved'; tr.reason='fixed-horizon exit unavailable';
      account.unresolved++; account.openPosition={ direction, shares:1, entry:tr.entry };
      lockedUntil[i]=Infinity; continue;
    }
    const exitPrice=price(exit.spot,-side);
    tr.exit={ t:exit.t, spot:exit.spot, price:exitPrice };
    tr.pnl=side*(exitPrice-entryPrice); tr.status='closed'; tr.reason='fixed-horizon exit';
    account.endingCash+=side*exitPrice; account.realizedPnl+=tr.pnl; account.closed++;
    lockedUntil[i]=exit.t;
  }
  return { assumptions:{instrument:'QQQ', sharesPerFill:1, costBpsPerSide,maxAgeMs,initialCash,
    pricing:'Synthetic one-share fills at sampled spot adjusted adversely on each side. Horizon begins at decision time. Short sales reserve one-share collateral. No quote, queue, borrow or fill evidence.'},
    accounts,trades };
}
module.exports = { paper };
