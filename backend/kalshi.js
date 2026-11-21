import { ACCOUNT, OWNER_ID, SITE, configured, ownerIdentity, privateJSON } from './kalshi-access.js';
import { archive, archiveIndex, getArchive, budget, readState, writeState, pruneArchive } from './kalshi-store.js';
import { discover, fetchMarket, displayMarket, publicRead, KALSHI_API } from './kalshi-source.js';
import { computeBookFromTrades, readJsonBody, withUserRecord } from './common.js';
import { createStorage } from './storage.js';
import { quoteEvent, marketKey, positionKey } from './prediction-model.js';
import { resolvePredictionRecord } from './prediction.js';
import { cutoverFenceActive } from './fence.js';

async function account(env) {
  const store = createStorage(env);
  await store.users.insert(ACCOUNT, { username: ACCOUNT, principal: { provider: 'github', id: OWNER_ID },
    account_scope: ACCOUNT, auth_kind: 'credit_identity', starting_balance: 10000, balance_cash: 10000,
    trades: [], version: 0, createdAt: new Date().toISOString() });
  const record = await store.users.get(ACCOUNT);
  if (record?.principal?.id !== OWNER_ID || record.account_scope !== ACCOUNT || record.auth_kind !== 'credit_identity') throw Error('Account identity conflict');
  return record;
}
function sameIntent(t, b) {
  return t.prediction?.rules.market === b.market && t.prediction.outcome === b.outcome && t.side === b.side && t.qty === b.qty;
}
async function paperOrder(request, env) {
  const input = await readJsonBody(request);
  if (input.error) return privateJSON({ error: 'Invalid order body' }, 400);
  const b = input.body;
  if (!b || !/^[a-f0-9-]{36}$/.test(b.execution_request_id || '') || !['buy', 'sell'].includes(b.side) ||
      !['YES', 'NO'].includes(b.outcome) || !Number.isSafeInteger(b.qty) || b.qty < 1 || b.qty > 10000 ||
      Object.keys(b).some(k => !['execution_request_id', 'market', 'outcome', 'side', 'qty'].includes(k))) return privateJSON({ error: 'Invalid paper intent' }, 400);
  let record = await account(env);
  const previous = record.trades.find(t => t.execution_request_id === b.execution_request_id);
  if (previous) return sameIntent(previous, b) ? privateJSON({ trade: previous, balance_cash: record.balance_cash }) : privateJSON({ error: 'Request ID conflict' }, 409);
  if (b.side === 'buy' && env.KALSHI_ENTRIES !== '1') return privateJSON({ error: 'New paper entries disabled' }, 409);
  await budget(env, 'orders-minute', 6, 60000);
  const evidence = await fetchMarket(env, b.market);
  const quote = quoteEvent(evidence.market, b.outcome, b.side, b.qty, Date.now());
  if (quote.error) return privateJSON({ error: quote.error }, 409);
  const receipt = await archive(env, b.market, evidence); // Persistence must succeed before any debit.
  const sym = positionKey(evidence.market.rules, b.outcome);
  const cashUnits = (b.side === 'buy' ? -1 : 1) * quote.price_units * b.qty - quote.fee_units;
  const trade = { execution_request_id: b.execution_request_id, execution_id: b.execution_request_id,
    sym, instrument_type: 'prediction', multiplier: 1, underlying: evidence.market.rules.underlying, side: b.side, qty: b.qty,
    price: quote.price_units / 10000, fee_units: quote.fee_units, cash_delta: cashUnits / 10000,
    prediction: quote.prediction, bid: quote.bid, ask: quote.ask, displayed_size: quote.size,
    quote_ts: quote.quoteTs, ts: new Date().toISOString(), status: 'filled', execution_mode: 'paper',
    principal: { provider: 'github', id: OWNER_ID }, account_scope: ACCOUNT,
    provenance: { archive: receipt, evidence } };
  const outcome = await withUserRecord(env, ACCOUNT, current => {
    const old = current.trades.find(t => t.execution_request_id === b.execution_request_id);
    if (old) return sameIntent(old, b) ? { result: { trade: old, balance_cash: current.balance_cash } } : { error: 'Request ID conflict' };
    if (current.account_closed || current.trades.length >= 1000) return { error: 'Paper account capacity reached' };
    if (quoteEvent(evidence.market, b.outcome, b.side, b.qty, Date.now()).error) return { error: 'Quote expired before ledger commit' };
    const nextChange = Date.parse(evidence.market.rules.fee.next_change);
    if (Number.isFinite(nextChange) && Date.now() >= nextChange) return { error: 'Fee convention changed; refresh' };
    const book = computeBookFromTrades(current.trades);
    if (b.side === 'sell' && (book[sym]?.pos || 0) < b.qty) return { error: 'Cannot sell unheld contracts' };
    if (b.side === 'buy' && new Set(current.trades.filter(t => t.prediction).map(t => t.prediction.rules.market)).size >= 20 &&
        !current.trades.some(t => t.prediction?.rules.market === b.market)) return { error: 'Twenty-market paper trial capacity reached' };
    const balanceUnits = Math.round(current.balance_cash * 10000) + cashUnits;
    if (balanceUnits < 0) return { error: 'Insufficient paper cash' };
    return { record: { ...current, trades: [...current.trades, trade], balance_cash: balanceUnits / 10000 },
      result: { trade, balance_cash: balanceUnits / 10000 } };
  });
  return outcome.error ? privateJSON({ error: outcome.error }, 409) : privateJSON(outcome.result, 201);
}

export async function handleKalshi(request, env) {
  const url = new URL(request.url);
  if (url.origin !== SITE || env.ENVIRONMENT === 'preview' || env.ISOLATION !== undefined) return privateJSON({ error: 'Not found' }, 404);
  if (!['GET', 'POST'].includes(request.method)) return privateJSON({ error: 'Method not allowed' }, 405);
  if (request.headers.get('Origin') && request.headers.get('Origin') !== SITE ||
      request.method === 'POST' && request.headers.get('Origin') !== SITE) return privateJSON({ error: 'Forbidden origin' }, 403);
  const routes = new Map([['/api/kalshi/status','GET'], ['/api/kalshi/markets','GET'], ['/api/kalshi/market','POST'],
    ['/api/kalshi/portfolio','GET'], ['/api/kalshi/paper','POST'], ['/api/kalshi/archive','GET'], ['/api/kalshi/discover','POST']]);
  if (!routes.has(url.pathname)) return privateJSON({ error: 'Not found' }, 404);
  if (routes.get(url.pathname) !== request.method) return privateJSON({ error: 'Method not allowed' }, 405);
  try {
    const principal = await ownerIdentity(request);
    if (!principal) return privateJSON({ error: 'Sign in with Owen’s authorized GitHub identity. All other accounts are denied.' }, 401);
    if (url.pathname === '/api/kalshi/status') return privateJSON({ authorized: true, enabled: configured(env),
      entries: env.KALSHI_ENTRIES === '1', principal: 'Owen', account: 'Separate personal Kalshi paper account',
      status: configured(env) ? 'Private personal research enabled' : 'Private personal research is awaiting operator activation' });
    if (!configured(env)) return privateJSON({ error: 'Private personal research is awaiting operator activation' }, 503);
    // GET portfolio can initialize a private account; every data operation honors the write fence.
    if (await cutoverFenceActive(env)) return privateJSON({ error: 'Account maintenance; retry later' }, 503);
    if (url.pathname === '/api/kalshi/markets') return privateJSON(await readState(env, 'discovery') || { markets: [], scope: 'Use Discover to collect the supported event markets.' });
    if (url.pathname === '/api/kalshi/discover') {
      await budget(env, 'discover', 1, 60000);
      const result = await discover(env);
      await writeState(env, 'discovery', result);
      return privateJSON(result);
    }
    if (url.pathname === '/api/kalshi/market') {
      const body = await readJsonBody(request);
      if (body.error) return privateJSON({ error: 'Invalid request' }, 400);
      await budget(env, 'detail', 6, 60000);
      const evidence = await fetchMarket(env, body.body?.market), receipt = await archive(env, evidence.market.rules.market, evidence);
      await writeState(env, 'selected', evidence.market.rules.market);
      return privateJSON({ ...displayMarket(evidence), archive: receipt });
    }
    if (url.pathname === '/api/kalshi/paper') return await paperOrder(request, env);
    if (url.pathname === '/api/kalshi/archive') {
      const id = url.searchParams.get('id');
      return privateJSON(id ? { mode: 'archived', evidence: await getArchive(env, id) } : { archives: await archiveIndex(env) });
    }
    const record = await account(env);
    return privateJSON({ balance_cash: record.balance_cash, positions: computeBookFromTrades(record.trades),
      trades: record.trades, health: await readState(env, 'health'), mode: 'paper', account_scope: ACCOUNT });
  } catch {
    // No provider bodies, cookies, account records or thrown request URLs in logs.
    return privateJSON({ error: 'Private research unavailable; source, identity, storage or request budget needs attention.' }, 503);
  }
}

export async function settleKalshiMarket(env, evidence) {
  const raw = evidence.receipts[0].data.market;
  if (raw.market_type !== 'binary' || raw.notional_value_dollars !== '1.0000' || raw.status !== 'finalized' || !['yes', 'no'].includes(raw.result) ||
      raw.is_provisional === true || !Number.isFinite(Date.parse(raw.settlement_ts)) ||
      Date.parse(raw.settlement_ts) > Date.now() ||
      typeof raw.settlement_value_dollars !== 'string' ||
      Number(raw.settlement_value_dollars) !== (raw.result === 'yes' ? 1 : 0)) return { pending: true };
  const record = await createStorage(env).users.get(ACCOUNT);
  if (!record) return { pending: true };
  const prior = await readState(env, `resolution:${raw.ticker}`);
  const signature = JSON.stringify([raw.result, raw.settlement_value_dollars, raw.settlement_ts]);
  const revision = prior?.signature === signature ? prior.revision : (prior?.revision || 0) + 1;
  // Record revision before application: a retry after a crash uses the same revision.
  await writeState(env, `resolution:${raw.ticker}`, { signature, revision });
  const entries = record.trades.filter(t => t.side === 'buy' && t.prediction?.rules.market === raw.ticker);
  const resolutions = entries.map(t => ({ id: `kalshi:${raw.ticker}:${revision}`, revision, official: true,
    result: raw.result.toUpperCase(), source: KALSHI_API + `/markets/${raw.ticker}`,
    market_key: marketKey(t.prediction.rules), rule_version: t.prediction.rules.version,
    exchange_ts: raw.settlement_ts, receive_ts: evidence.receipts[0].receive_ts,
    official_evidence: evidence.receipts[0] }));
  const outcome = await withUserRecord(env, ACCOUNT, r => {
    const resolved = resolvePredictionRecord(r, resolutions);
    return resolved.result.settled.length ? resolved : { result: resolved.result };
  });
  if (outcome.error) throw Error('Settlement retry required');
  return { adjustments: outcome.result.settled.length };
}

export async function kalshiTick(env) {
  if (!configured(env)) return;
  await budget(env, 'cron', 1, 900000);
  if (await cutoverFenceActive(env)) return;
  await pruneArchive(env);
  const record = await createStorage(env).users.get(ACCOUNT);
  const markets = [...new Set([await readState(env, 'selected'), ...(record?.trades || []).map(t => t.prediction?.rules.market)].filter(Boolean))];
  const offset = (await readState(env, 'sweep-offset')) || 0;
  let captured = 0, failures = 0;
  // At most two markets per 15-minute tick, including settled holdings for corrections.
  for (let i = 0; i < Math.min(2, markets.length); i++) {
    const market = markets[(offset + i) % markets.length];
    // Settlement must survive absent/unsupported books, fees and discovery metadata.
    try {
      const official = await publicRead(env, `/markets/${encodeURIComponent(market)}`);
      if (official.data.market?.ticker !== market) throw Error('Resolution identity mismatch');
      await settleKalshiMarket(env, { receipts: [official] });
    } catch { failures++; }
    try {
      const evidence = await fetchMarket(env, market);
      await archive(env, market, evidence);
      captured++;
    } catch { failures++; }
  }
  await writeState(env, 'sweep-offset', markets.length ? (offset + 2) % markets.length : 0);
  await writeState(env, 'health', { checked_at: new Date().toISOString(), captured, failures, tracked: markets.length,
    coverage: 'Periodic REST samples only; no continuous book history between observations.' });
}
