// Deribit v2-compatible, operator-configured market-data origin for the private paper chain.
const NAME = /^BTC_USDC-(\d{1,2}[A-Z]{3}\d{2})-(\d+(?:\.\d+)?)-([CP])$/;
export const linearKey = key => typeof key === 'string' && key.startsWith('USDC:') && NAME.test(key.slice(5));

async function read(env, method, params) {
  if (!env.BTC_USDC_DATA_ORIGIN) throw new Error('USDC market data source is not configured');
  const url = new URL('/api/v2/public/' + method, env.BTC_USDC_DATA_ORIGIN);
  if (url.protocol !== 'https:') throw new Error('Expected secure market data origin');
  url.search = new URLSearchParams(params);
  const response = await fetch(url, {redirect: 'manual', signal: AbortSignal.timeout(8000),
    headers: env.BTC_USDC_DATA_TOKEN ? {Authorization: 'Bearer ' + env.BTC_USDC_DATA_TOKEN} : {}});
  if (!response.ok) {
    const error = new Error('USDC data provider unavailable');
    error.status = response.status;
    throw error;
  }
  const data = await response.json();
  if (data.error || data.result == null) throw new Error('USDC data provider unavailable');
  return data.result;
}

const catalogs = new Map();
async function catalog(env, method, params, lifetime) {
  const key = env.BTC_USDC_DATA_ORIGIN + '|' + method;
  const cached = catalogs.get(key);
  if (cached && Date.now() < cached.until) return cached.result;
  const result = await read(env, method, params);
  catalogs.set(key, {result, until:Date.now()+lifetime});
  return result;
}

export function linearContract(row) {
  const name = row?.instrument_name, match = NAME.exec(name);
  if (!match || row.kind !== 'option' || row.is_active !== true || row.settlement_currency !== 'USDC' ||
      row.quote_currency !== 'USDC' || row.contract_size !== 1 || !Number.isFinite(row.min_trade_amount) || row.min_trade_amount <= 0 || row.min_trade_amount > 1 || row.strike !== Number(match[2]) ||
      row.option_type !== (match[3] === 'C' ? 'call' : 'put') || !Number.isFinite(row.expiration_timestamp)) return null;
  const at = new Date(row.expiration_timestamp).toISOString(), exp = at.slice(0, 10);
  return {contract_key:'USDC:' + name, provider_symbol:name, instrument_type:'futures_option',
    exchange:'Deribit', product:'BTC_USDC', series:'linear', multiplier:row.min_trade_amount, exp, expires_at:at, stops_trading_at:at,
    strike:row.strike, type:row.option_type, future_symbol:'BTC_USDC-' + match[1], future_expiry:exp, future_month:exp.slice(0,7)};
}

async function quote(env, contract) {
  const params = {instrument_name:contract.provider_symbol};
  let b;
  try {
    b = await read(env, 'get_order_book', {...params, depth:1});
  } catch (error) {
    if (error.status !== 429) throw error;
    // The ticker supplies the same timestamped best prices and displayed sizes.
    await new Promise(resolve => setTimeout(resolve, 1200));
    b = await read(env, 'ticker', params);
  }
  if (b.instrument_name !== contract.provider_symbol || !Number.isFinite(b.timestamp)) throw new Error('Unverified book');
  const ts = new Date(b.timestamp).toISOString(), received = new Date().toISOString();
  return {symbol:contract.contract_key, contract, available:b.state === 'open', source:'Deribit linear',
    bid:b.best_bid_price, ask:b.best_ask_price, bid_size:b.best_bid_amount / contract.multiplier, ask_size:b.best_ask_amount / contract.multiplier,
    index_price:b.index_price, bid_ts:ts, ask_ts:ts, bid_received_ts:received, ask_received_ts:received};
}

export async function linearFinal(env, c, now) {
  const result = await read(env, 'get_delivery_prices', {index_name:'btc_usdc',count:1000});
  const row = result?.data?.find(r=>r.date === c.exp && Number.isFinite(r.delivery_price) && r.delivery_price > 0);
  if (!row) return null;
  return {contract_key:c.contract_key, future_symbol:c.future_symbol, exp:c.exp, kind:'official_final',
    source:'Deribit',source_ref:'get_delivery_prices/btc_usdc/'+c.exp,value:row.delivery_price,
    published_at:new Date(now).toISOString(), time_basis:'observed'};
}

// Keep each refresh bounded while giving every held lot a turn at a fresh book.
export function linearBookWindow(keys, pinned = []) {
  const selected = [...new Set(pinned)].slice(0, 4);
  const remaining = [...new Set(keys)].filter(key => !selected.includes(key));
  const capacity = 4 - selected.length;
  if (remaining.length <= capacity) return selected.concat(remaining);
  const start = (Math.floor(Date.now() / 5000) * capacity) % remaining.length;
  return selected.concat([...remaining, ...remaining].slice(start, start + capacity));
}

export async function linearPayload(env, symbols, requestedExpiry, exact = false, pinned = []) {
  if (exact) {
    const c = linearContract(await read(env, 'get_instrument', {instrument_name:symbols[0].slice(5)}));
    if (!c || c.contract_key !== symbols[0]) throw new Error('Unknown linear option');
    return {quotes:[await quote(env,c)], entry_keys:[c.contract_key], session:'open'};
  }
  const args = {currency:'USDC', kind:'option'};
  const [rows, summaries] = await Promise.all([catalog(env,'get_instruments',{...args,expired:false},30000),catalog(env,'get_book_summary_by_currency',args,5000)]);
  const all = rows.map(linearContract).filter(c=>c && Date.parse(c.expires_at)>Date.now());
  const expiries = [...new Set(all.map(c=>c.exp))].sort();
  const expiry = expiries.includes(requestedExpiry) ? requestedExpiry : expiries[0];
  const contracts = all.filter(c=>c.exp===expiry);
  const directory = new Map(all.map(c=>[c.contract_key,c]));
  const prices = {}, listed = new Set(contracts.map(c=>c.provider_symbol));
  let center = null;
  for (const s of summaries) {
    if (!listed.has(s.instrument_name) || !Number.isFinite(s.creation_timestamp)) continue;
    prices['USDC:'+s.instrument_name] = {bid:s.bid_price,ask:s.ask_price,mark:s.mark_price,as_of:new Date(s.creation_timestamp).toISOString()};
    if (Number.isFinite(s.underlying_price)) center = s.underlying_price;
  }
  const targets = linearBookWindow(symbols.filter(k=>directory.has(k)), pinned.filter(k=>directory.has(k)));
  const quotes = (await Promise.allSettled(targets.map(k=>quote(env,directory.get(k))))).filter(q=>q.status==='fulfilled').map(q=>q.value);
  return {contracts, quotes, expiries, expiry, center, center_from_future:center!=null, display_prices:prices,
    subscribed_keys:quotes.map(q=>q.symbol), entry_keys:all.map(c=>c.contract_key), session:'open'};
}
