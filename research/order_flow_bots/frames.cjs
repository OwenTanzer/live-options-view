'use strict';
const { decodeReplay, replayAt } = require('../../site/shared/order-flow-model.js');

function* minuteFrames(json, oiEvidence) {
  const {gaps} = require('./outcomes.cjs');
  const intervals = gaps(json);
  const openingTime = json.coverage.open;
  if (oiEvidence && (oiEvidence.session !== json.session || !/^\d{4}-\d{2}-\d{2}$/.test(oiEvidence.clearingDate) ||
      oiEvidence.clearingDate >= json.session || !Number.isFinite(oiEvidence.availableAt) ||
      oiEvidence.availableAt > openingTime || !oiEvidence.source || oiEvidence.manifestSha256 !== json.source.manifest_sha256)) {
    throw Error('Invalid opening OI provenance');
  }
  json = {...json,coverage:{...json.coverage,gaps:intervals}};
  const replay = decodeReplay(json);
  const { cols, contracts, multiplier, coverage } = replay;
  const symbols = new Set(contracts.map(c => c.symbol));
  if (symbols.size !== contracts.length || contracts.some(c => c.expiration !== json.session ||
      !/^QQQ/.test(c.symbol) || !['C','P'].includes(c.type))) {
    throw Error('Expected a single-session, same-day QQQ contract universe');
  }
  const last = contracts.map(() => ({ price: null, time: null }));
  const opening = replayAt(replay, coverage.open).contracts;
  let index = 0;
  for (let t = coverage.open; t <= coverage.close; t += 60000) {
    const view = replayAt(replay, t);
    const rows = view.contracts.map((c,i) => ({
      symbol: c.symbol, strike: c.strike, type: c.type, oi: c.oi, openOi: opening[i].oi,
      gamma: c.gamma, cumBuy: c.flow.B, cumSell: c.flow.S, cumUnknown: c.flow.U,
      printCount: 0, unknownCount: 0, buy: 0, sell: 0, unknown: 0, buyPremium: 0, sellPremium: 0, unknownPremium: 0
    }));
    while (index < cols.t.length && cols.t[index] <= t) {
      const i = cols.contract[index], time = cols.t[index], side = ['buy','sell','unknown'][cols.side[index]];
      const price = cols.cents[index] / 100, size = cols.size[index];
      if (!side || !rows[i] || (index && time < cols.t[index-1])) throw Error('Invalid print ordering or identity');
      if (time >= coverage.open) {
        rows[i].printCount++;
        if (side==='unknown') rows[i].unknownCount++;
        rows[i][side] += size;
        rows[i][side + 'Premium'] += size * price * multiplier;
        last[i] = { price, time };
      }
      index++;
    }
    for (let i = 0; i < rows.length; i++) Object.assign(rows[i], last[i]);
    const spot = view.contracts[0]?.spot;
    const complete = Number.isFinite(spot) && spot > 0 && !view.coverage.incomplete &&
      !view.snapshot.stale && t <= coverage.last_print + 60000;
    const minuteComplete = Number.isFinite(spot) && spot > 0 && !view.snapshot?.stale &&
      t <= coverage.last_print + 60000 && (t === openingTime || t-60000 >= coverage.first_print || coverage.first_print-openingTime <= 1000 && t-60000 === openingTime) &&
      !intervals.some(g => (g.start == null || g.start <= t) && (g.end == null || g.end > t-60000));
    yield { t, minuteComplete, session: json.session, spot, complete, snapshotTime: view.snapshot?.t,
      contracts: rows, strikes: view.strikes, oiVerified: Boolean(oiEvidence) };
  }
}
module.exports = { minuteFrames };
