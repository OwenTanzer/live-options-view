import { requireSession, jsonResponse, computeBookFromTrades, withUserRecord } from './common.js';
import { createStorage } from './storage.js';
import { marketKey, positionKey, quoteEvent, validateRules, UNIT } from './prediction-model.js';

export function predictionNamespace(env) {
  const mode = env.PREDICTION_ENVIRONMENT;
  if (mode === 'production') throw Error('Production research requires the private Kalshi principal route');
  if (!['fixtures', 'demo', 'production'].includes(mode)) throw Error('Prediction environment unavailable');
  if (mode === 'fixtures' && !(env.ENVIRONMENT === 'preview' && env.ISOLATION === 'disposable')) throw Error('Fixtures require disposable preview');
  if (mode !== 'fixtures' && !env.PREDICTION_RIGHTS_RECEIPT) throw Error('Source rights unavailable');
  if (!env.PREDICTION_DATA) throw Error('Private prediction storage unavailable');
  return `prediction/v1/${mode}/`;
}
export async function predictionCatalog(env) {
  const prefix = predictionNamespace(env);
  const object = await env.PREDICTION_DATA.get(`${prefix}current.json`);
  if (!object) throw Error('Prediction evidence unavailable');
  const catalog = await object.json();
  if (catalog.schema !== 'prediction.catalog.v1' || !Array.isArray(catalog.markets) || catalog.markets.length > 200) throw Error('Invalid catalog');
  for (const m of catalog.markets) {
    validateRules(m.rules);
    if (m.rules.environment !== env.PREDICTION_ENVIRONMENT) throw Error('Environment mismatch');
  }
  return catalog;
}
export async function predictionQuote(env, intent, now = Date.now()) {
  if (intent.side === 'buy' && env.PREDICTION_ENTRIES !== 'enabled') return { error: 'Prediction entries disabled', status: 409 };
  const catalog = await predictionCatalog(env);
  for (const m of catalog.markets) for (const outcome of ['YES', 'NO']) {
    if (positionKey(m.rules, outcome) === intent.sym) return quoteEvent(m, outcome, intent.side, intent.qty, now);
  }
  return { error: 'Unknown prediction instrument', status: 409 };
}
export async function handlePrediction(request, env, url) {
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Sign in to view prediction markets' }, 401);
  try {
    if (url.pathname === '/api/prediction/portfolio') {
      const trades = session.record.trades.filter(t => t.instrument_type === 'prediction');
      let markets = [];
      try { markets = (await predictionCatalog(env)).markets; } catch { /* Holdings survive feed outages. */ }
      const positions = Object.entries(computeBookFromTrades(trades)).map(([sym, b]) => {
        const entry = trades.find(t => t.sym === sym && t.prediction);
        const m = markets.find(m => marketKey(m.rules) === marketKey(entry.prediction.rules));
        const q = m ? quoteEvent(m, entry.prediction.outcome, 'sell', Math.max(1, b.pos), Date.now()) : { error: 'Feed unavailable' };
        return { sym, ...b, liquidation_value: b.pos && !q.error ? q.bid * b.pos : null, mark_reason: q.error ?? null };
      });
      return jsonResponse({ positions, trades, balance_cash: session.record.balance_cash }, 200);
    }
    if (env.PREDICTION_DISPLAY !== 'enabled') return jsonResponse({ error: 'Prediction display disabled' }, 404);
    const prefix = predictionNamespace(env);
    if (url.pathname === '/api/prediction/archive') {
      const batch = url.searchParams.get('batch');
      if (!/^[a-f0-9]{64}$/.test(batch || '')) return jsonResponse({ error: 'Invalid batch digest' }, 400);
      const obj = await env.PREDICTION_DATA.get(`${prefix}batches/${batch}.json`);
      return obj ? jsonResponse(await obj.json(), 200) : jsonResponse({ error: 'Batch unavailable' }, 404);
    }
    if (url.pathname !== '/api/prediction/markets') return jsonResponse({ error: 'Not found' }, 404);
    return jsonResponse(await predictionCatalog(env), 200);
  } catch { return jsonResponse({ error: 'Prediction evidence unavailable' }, 503); }
}

// Official records only; revisions adjust prior payouts rather than rewriting fills.
function remainingPremiumUnits(trades, sym) {
  let units = 0, held = 0;
  for (const t of trades) {
    if (t.sym !== sym || t.adjustment_units !== undefined) continue;
    if (t.side === 'buy') { units += Math.round(t.price * UNIT) * t.qty; held += t.qty; }
    else if (held > 0) { units -= Math.round(units * t.qty / held); held -= t.qty; }
  }
  return units;
}
export function resolvePredictionRecord(record, resolutions, now = Date.now()) {
  let balance = Math.round(record.balance_cash * UNIT);
  const trades = [...record.trades], applied = [];
  const entries = record.trades.filter(t => t.instrument_type === 'prediction' && t.prediction && t.side === 'buy');
  for (const sym of new Set(entries.map(t => t.sym))) {
    const entry = entries.find(t => t.sym === sym), meta = entry.prediction;
    const r = resolutions.filter(r => r.market_key === marketKey(meta.rules)).sort((a, b) => b.revision - a.revision)[0];
    if (!r || r.official !== true || !r.source || !r.id || !Number.isSafeInteger(r.revision) || r.revision < 1 ||
        !['YES', 'NO', 'VOID'].includes(r.result) || r.rule_version !== meta.rules.version ||
        !Number.isFinite(Date.parse(r.receive_ts)) || !Number.isFinite(Date.parse(r.exchange_ts)) ||
        Date.parse(r.receive_ts) > now || Date.parse(r.exchange_ts) > now ||
        Date.parse(r.exchange_ts) < Date.parse(meta.rules.cutoff) || now < Date.parse(meta.rules.cutoff)) continue;
    const previous = trades.filter(t => t.sym === sym && t.resolution);
    if (previous.some(t => t.resolution.revision >= r.revision)) continue;
    const initial = previous.find(t => !t.adjustment_units);
    const b = computeBookFromTrades(record.trades)[sym];
    const qty = initial?.qty ?? b.pos;
    if (qty <= 0) continue;
    const cost = initial?.resolution_cost_units ?? remainingPremiumUnits(record.trades, sym);
    const target = r.result === 'VOID' ? cost : r.result === meta.outcome ? UNIT * qty : 0;
    const paid = previous.reduce((s, t) => s + (t.payout_units ?? 0), 0);
    const delta = target - paid;
    const trade = { execution_request_id: `resolve:${sym}:${r.revision}`, execution_id: `resolve:${sym}:${r.revision}`,
      sym, instrument_type: 'prediction', multiplier: 1, underlying: meta.rules.underlying, prediction: meta,
      side: 'sell', qty: previous.length ? 0 : qty, price: previous.length ? 0 : target / qty / UNIT,
      status: 'filled', ts: new Date(now).toISOString(), resolution: r,
      resolution_cost_units: cost, payout_units: delta, ...(previous.length ? { adjustment_units: delta } : {}) };
    balance += delta; trades.push(trade); applied.push(trade);
  }
  return { record: { ...record, balance_cash: balance / UNIT, trades }, result: { settled: applied } };
}
export async function settlePredictions(env, now = new Date()) {
  if (!env.PREDICTION_ENVIRONMENT) return { accounts: 0 };
  const prefix = predictionNamespace(env);
  const obj = await env.PREDICTION_DATA.get(`${prefix}resolutions.json`);
  if (!obj) return { accounts: 0, pending: true };
  const resolutions = await obj.json();
  if (!Array.isArray(resolutions) || resolutions.length > 10000) throw Error('Invalid resolution index');
  const store = createStorage(env);
  let count = 0;
  for (const username of await store.users.listUsernames()) {
    const outcome = await withUserRecord(env, username, record => {
      const resolved = resolvePredictionRecord(record, resolutions, now.getTime());
      return resolved.result.settled.length ? resolved : { result: resolved.result };
    });
    if (outcome.error) throw Error('Prediction settlement retry required');
    count += outcome.result.settled.length;
  }
  return { adjustments: count };
}
