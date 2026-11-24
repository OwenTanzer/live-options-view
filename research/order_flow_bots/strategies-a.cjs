'use strict';

// Frames are immutable and describe one regular session in event-time order.
// All spot-dependent baskets are fixed at the start of each observation window.
// The replay has one expiry, one-dollar strikes, and at most one C/P per strike.
const MINUTE = 60000;
const valid = Number.isFinite;
const windowCache = new WeakMap();

function closeWindow(history, end, minutes, incrementOnly=false) {
  if (end < minutes) return null;
  const frames = history.slice(end - minutes, end + 1);
  if (frames.some((f, j) => !(incrementOnly ? (j===0 || f.minuteComplete) : f.complete) || !valid(f.spot) || f.spot <= 0 ||
      (j && f.t !== frames[j - 1].t + MINUTE))) return null;
  return frames;
}

function basket(frame, width) {
  const selected = frame.contracts.filter(c => Math.abs(c.strike - frame.spot) <= width);
  const keys = new Set(selected.map(c => c.strike + '/' + c.type));
  for (let strike = Math.ceil(frame.spot - width); strike <= Math.floor(frame.spot + width); strike++) {
    if (!keys.has(strike + '/C') || !keys.has(strike + '/P')) return null;
  }
  if (keys.size !== selected.length || !selected.length) return null;
  return selected;
}

function fiveMinuteFlow(history, end) {
  if (end < 5) return null;
  const last = history[end];
  if (windowCache.has(last)) return windowCache.get(last);
  const frames = closeWindow(history, end, 5, true);
  const initial = frames && basket(frames[0], 1.5);
  if (!initial) return null;
  const wanted = new Set(initial.map(c => c.symbol));
  let gross = 0, signed = 0, quantity = 0, unknownQuantity = 0, unknownPremium = 0, prints = 0, unknownPrints = 0;
  for (const f of frames.slice(1)) {
    const seen = new Set();
    for (const c of f.contracts) {
      if (!wanted.has(c.symbol)) continue;
      if (seen.has(c.symbol) || ![c.buy, c.sell, c.unknown, c.buyPremium, c.sellPremium, c.unknownPremium]
        .every(n => valid(n) && n >= 0)) return null;
      seen.add(c.symbol);
      prints += c.printCount; unknownPrints += c.unknownCount;
      gross += c.buyPremium + c.sellPremium + c.unknownPremium;
      signed += (c.type === 'C' ? 1 : -1) * (c.buyPremium - c.sellPremium);
      quantity += c.buy + c.sell + c.unknown;
      unknownQuantity += c.unknown;
      unknownPremium += c.unknownPremium;
    }
    if (seen.size !== wanted.size) return null;
  }
  const value = { gross, signed, pressure: gross > 0 ? signed / gross : null,
    unknownQuantity, unknownPremium, quantity, prints, unknownPrints,
    classified: prints > 0 && gross > 0 && unknownPrints / prints <= 0.3 };
  windowCache.set(last, value);
  return value;
}

function quantile(values, fraction) {
  const sorted = values.slice().sort((a, b) => a - b);
  const at = (sorted.length - 1) * fraction, lower = Math.floor(at);
  return sorted[lower] + (sorted[Math.ceil(at)] - sorted[lower]) * (at - lower);
}

function result(id, horizon, reason, signal = null) {
  return { id, horizon, reason, signal };
}

function bounded(strike) {
  const b = strike.flow?.[1];
  return b && b.covered === b.of && b.of > 0 && valid(b.low) && valid(b.high) && b.low <= b.high;
}

function regime(history, end) {
  const frame = history[end];
  const r = (reason, signal) => result(1, 15, reason, signal);
  if (!basket(frame, 3)) return r('incomplete three-dollar band');
  const band = frame.strikes.filter(s => Math.abs(s.strike - frame.spot) <= 3);
  const expected = Math.floor(frame.spot + 3) - Math.ceil(frame.spot - 3) + 1;
  if (band.length !== expected || !band.every(bounded)) return r('incomplete gamma bounds');
  const low = band.reduce((s, k) => s + k.flow[1].low, 0);
  const high = band.reduce((s, k) => s + k.flow[1].high, 0);
  if (low <= 0 && high >= 0) return r('gamma interval includes zero');
  const frames = closeWindow(history, end, 15);
  if (!frames) return r('no complete preceding 15-minute range');
  const prices = frames.slice(0, -1).map(f => f.spot);
  if (high < 0) {
    if (frame.spot > Math.max(...prices)) return r('short gamma upward breakout', 'long');
    if (frame.spot < Math.min(...prices)) return r('short gamma downward breakout', 'short');
    return r('short gamma inside preceding range');
  }
  // Choose the anchor from the previous frame before observing current price.
  const prior = frames.at(-2);
  if (!basket(prior, 3)) return r('previous strike band incomplete');
  const priorBand = prior.strikes.filter(s => Math.abs(s.strike - prior.spot) <= 3);
  const count = Math.floor(prior.spot + 3) - Math.ceil(prior.spot - 3) + 1;
  if (priorBand.length !== count || !priorBand.every(bounded)) return r('previous strike bounds incomplete');
  const anchor = priorBand.filter(s => s.flow[1].low > 0)
    .sort((a,b) => b.flow[1].low - a.flow[1].low || a.strike - b.strike)[0];
  if (!anchor) return r('no previously determined positive anchor');
  const deviation = frame.spot / anchor.strike - 1;
  if (Math.abs(deviation) < 0.003) return r('inside anchor deviation threshold');
  return r('positive gamma fade versus prior anchor ' + anchor.strike, deviation > 0 ? 'short' : 'long');
}

function rollingSignals(history, end) {
  const now = fiveMinuteFlow(history, end);
  let imbalance = result(2, 15, 'missing five-minute basket');
  let expansion = result(6, 15, 'missing five-minute basket');
  if (!now) return [imbalance, expansion];
  const signed = [], gross = [];
  // Same-session exploratory thresholds, based on completed disjoint five-minute
  // windows ending no later than the start of the observation being tested.
  for (let i = end - 5; i >= 5; i -= 5) {
    const prior = fiveMinuteFlow(history, i);
    if (prior?.classified) { gross.push(prior.gross); signed.push(prior.signed); }
  }
  if (!now.classified) imbalance = result(2, 15, 'no classified pressure or unknown share above 30%');
  else if (signed.length < 6) imbalance = result(2, 15, 'fewer than six signed reference windows');
  else if (now.signed > Math.max(0, quantile(signed, 0.9))) imbalance = result(2, 15, 'positive premium imbalance', 'long');
  else if (now.signed < Math.min(0, quantile(signed, 0.1))) imbalance = result(2, 15, 'negative premium imbalance', 'short');
  else imbalance = result(2, 15, 'inside signed pressure thresholds');
  if (!now.classified) expansion = result(6, 15, 'incomplete shared print-side coverage');
  else if (gross.length < 6) expansion = result(6, 15, 'fewer than six activity reference windows');
  else if (now.gross > quantile(gross, 0.9)) expansion = result(6, 15, 'unusually high gross premium', 'range');
  else expansion = result(6, 15, 'gross premium below threshold');
  if (now.classified && signed.length >= 6) {
    const diagnostic = (value, refs) => ({value, lower:quantile(refs,0.1),upper:quantile(refs,0.9),
      references:refs.length,prints:now.prints,unknownPrints:now.unknownPrints});
    imbalance.comparison = diagnostic(now.signed,signed);
    expansion.comparison = diagnostic(now.gross,gross);
  }
  return [imbalance, expansion];
}

function turnover(history, end) {
  const frame = history[end], r = (reason, signal) => result(3, 15, reason, signal);
  if (!frame.oiVerified) return r('opening OI clearing date unverified');
  if (!closeWindow(history, end, 1)) return r('no complete preceding observation');
  const prev = new Map(history[end-1].contracts.map(c => [c.symbol, c]));
  const signs = new Set();
  for (const c of frame.contracts) {
    const prior = prev.get(c.symbol);
    if (!prior || !valid(c.openOi) || c.openOi <= 0 || c.openOi !== prior.openOi) continue;
    if (![c.cumBuy, c.cumSell, c.cumUnknown, prior.cumBuy, prior.cumSell, prior.cumUnknown]
      .every(n => valid(n) && n >= 0) || c.cumBuy < prior.cumBuy ||
      c.cumSell < prior.cumSell || c.cumUnknown < prior.cumUnknown) continue;
    const classified = c.cumBuy + c.cumSell, total = classified + c.cumUnknown;
    const previous = prior.cumBuy + prior.cumSell + prior.cumUnknown;
    if (previous > c.openOi || total <= c.openOi || !classified || c.cumUnknown / total > 0.3) continue;
    if (Math.max(c.cumBuy, c.cumSell) / classified <= 0.7) continue;
    signs.add((c.cumBuy > c.cumSell) === (c.type === 'C') ? 'long' : 'short');
  }
  return signs.size === 1 ? r('directional turnover crossing', [...signs][0]) : r(signs.size ? 'conflicting turnover directions' : 'no qualified turnover crossing');
}

function wall(history, end) {
  const r = (reason, signal) => result(4, 5, reason, signal);
  const frames = closeWindow(history, end, 2);
  if (!frames) return r('missing anchor, touch or rejection');
  const [anchor, touch, current] = frames;
  // Snapshot spot observations can prove a crossing and reclaim, but cannot
  // resolve a hidden intraminute touch or fill. Use only visible transitions.
  const levels = anchor.strikes.filter(s => bounded(s) && s.flow[1].low > 0)
    .sort((a, b) => b.flow[1].low - a.flow[1].low || a.strike - b.strike);
  const above = levels.find(s => s.strike > anchor.spot);
  const below = levels.find(s => s.strike < anchor.spot);
  if (above && touch.spot >= above.strike && current.spot < above.strike) return r('rejected frozen upper wall ' + above.strike, 'short');
  if (below && touch.spot <= below.strike && current.spot > below.strike) return r('reclaimed frozen lower wall ' + below.strike, 'long');
  return r(levels.length ? 'no observed wall rejection' : 'no positive wall in captured ladder');
}

function divergence(history, end) {
  const r = (reason, signal) => result(5, 10, reason, signal);
  const window = closeWindow(history, end, 31);
  if (!window) return r('missing breakout history');
  const first = fiveMinuteFlow(history, end - 6), second = fiveMinuteFlow(history, end - 1);
  if (!first?.classified || !second?.classified) return r('incomplete or ambiguous pressure comparison');
  const frame = window.at(-1);
  const band = frame.strikes.filter(s => Math.abs(s.strike - frame.spot) <= 3);
  const count = Math.floor(frame.spot + 3) - Math.ceil(frame.spot - 3) + 1;
  let stratum = 'unavailable gamma';
  if (basket(frame, 3) && band.length === count && band.every(bounded)) {
    if (band.reduce((sum,k) => sum + k.flow[1].high, 0) < 0) return r('short gamma excludes fade');
    stratum = band.reduce((sum,k) => sum + k.flow[1].low, 0) > 0 ? 'positive gamma' : 'indeterminate gamma';
  }
  if (stratum === 'unavailable gamma') return r('missing gamma band excludes fade');
  const prices = window.slice(0, -2).map(f => f.spot);
  const max = Math.max(...prices), min = Math.min(...prices);
  const breakout = window.at(-2).spot, now = window.at(-1).spot;
  if (breakout > max && now < max && second.pressure < first.pressure) return r('failed high on weakening pressure / ' + stratum, 'short');
  if (breakout < min && now > min && second.pressure > first.pressure) return r('failed low on strengthening pressure / ' + stratum, 'long');
  return r('no pressure-divergent failed breakout');
}

module.exports = function evaluateA(history) {
  const end = history.length - 1, frame = history[end];
  if ((!frame?.complete && !frame?.minuteComplete) || !valid(frame.spot) || frame.spot <= 0) {
    return [15, 15, 15, 5, 10, 15].map((h, i) => result(i + 1, h, 'missing or incomplete decision frame'));
  }
  const [imbalance, expansion] = rollingSignals(history, end);
  if (!frame.complete) return [result(1,15,'incomplete session'),imbalance,result(3,15,'incomplete session'),
    result(4,5,'incomplete session'),result(5,10,'incomplete session'),expansion];
  return [regime(history, end), imbalance, turnover(history, end), wall(history, end), divergence(history, end), expansion];
};
