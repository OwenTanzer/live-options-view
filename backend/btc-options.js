// Private BTC monthly paper product. No broker orders or public archive fallback.
import { computeBookFromTrades, jsonResponse, requireSession, withUserRecord } from './common.js';
import { createStorage } from './storage.js';
import { linearKey, linearPayload, linearFinal } from './btc-usdc.js';
import { btcAllowed, ownerLinkAvailable } from './btc-owner-link.js';

export const BTC_KEY = /^(?:BTCOPT:[0-9a-f]{40}|USDC:BTC_USDC-\d{1,2}[A-Z]{3}\d{2}-\d+(?:\.\d+)?-[CP])$/;
export const BTC_FEE = 2.50; // Explicit simulation convention, not a broker fee quote.
export function btcSession(now = Date.now()) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago',
    weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(now).map(x => [x.type, x.value]));
  const minute = +p.hour * 60 + +p.minute;
  return (['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].includes(p.weekday) && minute >= 960 && minute < 962) ||
    (p.weekday === 'Sat' && minute >= 120 && minute < 240) ? 'maintenance' : 'open';
}
export function validBtcContract(c, key = c?.contract_key) {
  if (linearKey(key)) return c?.contract_key === key && key === 'USDC:' + c.provider_symbol &&
    c.instrument_type === 'futures_option' && c.product === 'BTC_USDC' && c.exchange === 'Deribit' && c.series === 'linear' && Number.isFinite(c.multiplier) && c.multiplier > 0 && c.multiplier <= 1 &&
    ['call','put'].includes(c.type) && Number.isFinite(c.strike) && c.strike > 0 && Number.isFinite(Date.parse(c.expires_at));
  return c && BTC_KEY.test(key) && c.contract_key === key && c.instrument_type === 'futures_option' &&
    ((c.product === '/BTC' && c.multiplier === 5) || (c.product === '/MBT' && c.multiplier === 0.1)) && c.series === 'monthly' && c.exchange === 'CME' &&
    ['call', 'put'].includes(c.type) && Number.isFinite(c.strike) && c.strike > 0 &&
    typeof c.provider_symbol === 'string' && c.provider_symbol.length <= 128 &&
    typeof c.streamer_symbol === 'string' && c.streamer_symbol.endsWith(':XCME') &&
    typeof c.future_symbol === 'string' && c.future_symbol.startsWith(c.product) && /^\/(?:BTC|MBT)[A-Z][0-9]{1,4}$/.test(c.future_symbol) && c.future_expiry === c.exp &&
    /^\d{4}-\d{2}-\d{2}$/.test(c.exp) && Number.isFinite(Date.parse(c.expires_at)) &&
    Number.isFinite(Date.parse(c.stops_trading_at));
}
export function btcExecutionClock(row, side, now = Date.now()) {
  const receipt = Date.parse(row[side + '_received_ts']);
  const fresh = t => Number.isFinite(t) && now - t >= -1000 && now - t <= 15000;
  if (!fresh(receipt)) return null;
  const source = row[side + '_ts'];
  // A present stale/future source time cannot be bypassed via the fallback.
  if (source != null) {
    const ts = Date.parse(source);
    return fresh(ts) ? { at: source, source_at: source, basis: 'provider' } : null;
  }
  const c = row[side + '_change'];
  if (!c || c.schema !== 'observed_side_change.v1' || !/^[0-9a-f]{32}$/.test(row.stream_epoch || '') ||
      c.stream_epoch !== row.stream_epoch) return null;
  const ts = Date.parse(c.at), baseline = Date.parse(c.baseline_at), prior = Date.parse(c.previous_received_at);
  if (!fresh(ts) || !Number.isFinite(baseline) || !Number.isFinite(prior) ||
      !(baseline <= prior && prior < ts && ts <= receipt)) return null;
  if (![c.baseline_sequence, c.previous_sequence, c.sequence].every(Number.isSafeInteger) ||
      !(0 < c.baseline_sequence && c.baseline_sequence <= c.previous_sequence && c.previous_sequence < c.sequence)) return null;
  if (![c.previous_price, c.previous_size, c.price, c.size].every(v => Number.isFinite(v) && v >= 0) ||
      c.price !== row[side] || c.size !== row[side + '_size'] ||
      (c.previous_price === c.price && c.previous_size === c.size)) return null;
  return { at: c.at, source_at: null, basis: 'observed_side_change' };
}
export function exactBtcQuote(payload, intent, now = Date.now()) {
  const row = payload?.quotes?.find(q => q.symbol === intent.sym);
  const c = row?.contract;
  const fail = error => ({ error, status: 409 });
  if (!validBtcContract(c, intent.sym)) return fail('Exact BTC monthly identity unavailable');
  if (payload.session !== 'open' || (!linearKey(intent.sym) && btcSession(now) !== 'open')) return fail('BTC session closed');
  if (now >= Date.parse(c.stops_trading_at)) return fail('Contract expired');
  if (intent.side === 'buy' && !payload.entry_keys?.includes(intent.sym)) return fail('Contract is not in the active monthly series');
  const side = intent.side === 'buy' ? 'ask' : 'bid';
  const clock = btcExecutionClock(row, side, now);
  if (!clock) return fail('Executable side is stale or untimed');
  if (!(Number.isFinite(row[side]) && row[side] > 0 && Number.isFinite(row[side + '_size']) && row[side + '_size'] >= 1)) return fail('Executable side has no price or size');
  if (Number.isFinite(row.bid) && Number.isFinite(row.ask) && row.bid > row.ask) return fail('Crossed book');
  if (row.available !== true) return fail('Quote unavailable after disconnect or maintenance');
  const linear = linearKey(intent.sym);
  if (linear && !(Number.isFinite(row.index_price) && row.index_price > 0)) return fail('Missing Bitcoin fee index');
  return { bid: row.bid, ask: row.ask, strike: c.strike, type: c.type, exp: c.exp, multiplier: c.multiplier,
    contract: structuredClone(c), quoteTs: clock.at, quote_clock_basis: clock.basis,
    quote_source_ts: clock.source_at, bidTs: row.bid_ts, askTs: row.ask_ts,
    source_quote: structuredClone(row), fees: linear ? c.multiplier * Math.min(0.0003 * row.index_price, 0.125 * row[side]) : BTC_FEE, fee_model: linear ? 'linear-usdc-standard-taker-at-par' : 'btc-paper-flat-2.50-per-side-v1' };
}
export function heldBtc(record) {
  return Object.values(computeBookFromTrades(record.trades || [])).filter(b => b.instrument_type === 'futures_option' && b.pos > 0).map(b => b.contract);
}
export async function fetchBtc(env, record, symbols = [], expiry = null, product = null) {
  if (product === 'usdc' || (product == null && linearKey(symbols[0]))) {
    const keys = product == null ? symbols : [...new Set([...symbols, ...heldBtc(record).map(c => c.contract_key)])];
    const payload = await linearPayload(env, keys.filter(linearKey), expiry, product == null);
    return Response.json(payload);
  }
  if (!env.LIVE_QUOTE_ORIGIN || !env.LIVE_QUOTE_KEY) throw new Error('Quote service unavailable');
  const url = new URL('/btc-options', env.LIVE_QUOTE_ORIGIN);
  if (product === 'micro') url.searchParams.set('product','micro');
  if (expiry) url.searchParams.set('expiry', expiry);
  url.searchParams.set('symbols', symbols.filter(k => !linearKey(k)).join(','));
  url.searchParams.set('held', JSON.stringify(heldBtc(record).filter(c => !linearKey(c.contract_key)).map(c => c.provider_symbol)));
  return fetch(url, { headers: { 'X-Live-Quote-Key': env.LIVE_QUOTE_KEY },
    redirect: 'manual', signal: AbortSignal.timeout(10000), cf: { cacheTtl: 0, cacheEverything: false } });
}
export async function handleBtcOptions(request, env) {
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Sign in required' }, 401);
  // Holdings are not a market-data entitlement. Entries may be disabled
  // independently while an entitled owner continues to manage positions.
  if (!await btcAllowed(env, session, request)) return jsonResponse({ error: 'This paper session has no authorized Bitcoin data source',
    owner_link_available: ownerLinkAvailable(env) }, 403);
  const symbols = (new URL(request.url).searchParams.get('symbols') || '').split(',').filter(Boolean);
  if (symbols.length > 64 || symbols.some(s => !BTC_KEY.test(s))) return jsonResponse({ error: 'Invalid contract keys' }, 400);
  const params = new URL(request.url).searchParams;
  const product = params.get('product') || 'cme';
  if (!['cme','micro','usdc'].includes(product)) return jsonResponse({error:'Unknown Bitcoin product'},400);
  if (product === 'usdc' && !env.BTC_USDC_DATA_ORIGIN) return jsonResponse({error:'USDC data source not configured'},503);
  try {
    const response = await fetchBtc(env, session.record, symbols, params.get('expiry'), product);
    if (!response.ok) return jsonResponse({ error: 'BTC source unavailable', provider_status: response.status }, 503);
    const data = await response.json();
    const held = heldBtc(session.record);
    const other = held.filter(c => linearKey(c.contract_key) !== (product === 'usdc'));
    if (other.length) {
      const reads = product === 'usdc'
        ? [fetchBtc(env,session.record,other.map(c=>c.contract_key)).then(r=>r.json())]
        : other.slice(0,4).map(c=>linearPayload(env,[c.contract_key],null,true));
      for (const result of await Promise.allSettled(reads)) {
        if (result.status === 'fulfilled') data.quotes.push(...(result.value.quotes || []));
      }
    }
    if (!await btcAllowed(env, session, request)) return jsonResponse({ error: 'Bitcoin data access revoked' }, 403);
    const entries = env.BTC_OPTIONS_ENTRIES === '1';
    return jsonResponse({ ...data, entries_enabled: entries, fee_per_side: product === 'usdc' ? null : BTC_FEE,
      fee_model: product === 'usdc' ? 'Standard linear taker fee; simulated USDC at par; zero expiry fee' : 'Simulated $2.50 per contract per side; $0 expiration fee' }, 200);
  } catch { return jsonResponse({ error: 'BTC source unavailable' }, 503); }
}

// Licensed final marks must be provisioned separately in this exact private
// namespace. No daily close, browser-supplied mark or live spot can substitute.
export async function btcFinalMark(env, c, now = Date.now()) {
  if (!validBtcContract(c) || now < Date.parse(c.expires_at)) return null;
  if (linearKey(c.contract_key)) return linearFinal(env,c,now);
  const obj = await env.PAPER_TRADES?.get(`paper-trades/btc-final/${c.contract_key.slice(7)}/${c.exp}.json`);
  if (!obj) return null;
  const m = await obj.json();
  return m.contract_key === c.contract_key && m.future_symbol === c.future_symbol && m.exp === c.exp &&
    m.kind === 'official_final' && m.source === c.exchange && typeof m.source_ref === 'string' && m.source_ref.length > 0 &&
    Number.isFinite(m.value) && m.value >= 0 && Date.parse(m.published_at) >= Date.parse(c.expires_at) &&
    Date.parse(m.published_at) <= now ? m : null;
}
export async function settleBtcAccount(env, username, now = Date.now()) {
  const record = await createStorage(env).users.get(username);
  if (!record) return { error: 'not_found' };
  const marks = new Map();
  for (const c of heldBtc(record)) {
    try { const m = await btcFinalMark(env, c, now); if (m) marks.set(c.contract_key, m); } catch { /* retry later */ }
  }
  return withUserRecord(env, username, current => {
    const trades = [...current.trades], settled = [], pending = [];
    let balance_cash = current.balance_cash;
    for (const [sym, b] of Object.entries(computeBookFromTrades(trades))) {
      if (b.instrument_type !== 'futures_option' || b.pos <= 0 || now < Date.parse(b.contract?.expires_at)) continue;
      const id = `settle:${sym}:${b.exp}`;
      if (trades.some(t => t.execution_request_id === id)) continue;
      const mark = marks.get(sym);
      if (!mark) { pending.push(sym); continue; }
      const price = Math.max(b.type === 'call' ? mark.value - b.strike : b.strike - mark.value, 0);
      const cash_delta = price * b.pos * b.contract.multiplier;
      const trade = { execution_id: id, execution_request_id: id, sym, underlying: b.contract.product,
        instrument_type: 'futures_option', contract: b.contract, multiplier: b.contract.multiplier, strike: b.strike,
        type: b.type, exp: b.exp, side: 'sell', qty: b.pos, price, fees: 0, cash_delta,
        fee_model: 'paper-expiration-zero-fee', settlement_mark: mark, status: 'filled',
        ts: new Date(now).toISOString(), username, note: 'Simulated exercise at the official final settlement price' };
      trades.push(trade); settled.push(trade); balance_cash += cash_delta;
    }
    return { ...(settled.length ? { record: { ...current, trades, balance_cash } } : {}),
      result: { settled, pending, balance_cash } };
  });
}
