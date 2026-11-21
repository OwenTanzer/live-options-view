// paper trading — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// This module declares the page's shared scope nearly every other module
// touches: `session`, `spotMarks`, `SESSION_CACHE_KEY`, `liveQuotes`,
// `lastData`/`lastDataDate`, `chainSelection`, `chainState`, `ticketEl`,
// `pendingExecutions` and `positionMarks`. `lastTs` stays declared in the
// stale-page section; `botsTimer`/`renderBots` come from the bots panel and
// `showMarginCallScreen` from the auth module — all runtime-only, after all
// scripts load. `ticketEl` is assigned by the trade-ticket module, `session`
// by the auth module, `lastData` by the data-fetch-cycle module.
// ── paper trading ─────────────────────────────────────────────────────────────
// Trades: {ts, sym, strike, type, exp, side, qty, price, note?}. Positions and
// PnL is derived from the trade list (average-cost basis, per-instrument multiplier).
//
// Balances are server-authoritative, tied to a logged-in username, so they
// follow a trader across devices instead of resetting per-browser. `session`
// is the hydrated server state ({username, balance_cash, trades}) or null
// when logged out. `spotMarks` (underlying price per expiration date) stays
// client-local display state, uploaded to /api/settle only when needed to
// price a settlement.
let session = null;
let spotMarks = {};

const SESSION_CACHE_KEY = 'paperbook_session_cache';

// Display-only cache so a reload shows something instantly before /api/me
// resolves. Never treated as authoritative -- always overwritten by the next
// server response.
function cacheSessionDisplay() {
  try { localStorage.setItem(SESSION_CACHE_KEY, JSON.stringify(session)); } catch (e) {}
}
function readSessionDisplayCache() {
  try { return JSON.parse(localStorage.getItem(SESSION_CACHE_KEY)); } catch (e) { return null; }
}

function newExecutionRequestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, char => {
    const value = Math.floor(Math.random() * 16);
    return (char === 'x' ? value : (value & 0x3) | 0x8).toString(16);
  });
}

// The Worker is the execution authority: it fetches and validates a fresh
// exact-contract quote, persists the canonical fill and the balance
// mutation, then returns both for the local display. Auth rides the
// same-origin session cookie automatically -- no manual header needed.
async function requestExecution(info, order, executionRequestId) {
  const options = {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      execution_request_id: executionRequestId,
      sym: info.sym,
      instrument_type: info.instrument_type || 'option',
      ...order,
    }),
  };
  let response;
  try {
    response = await fetch('/api/paper-trade', options);
  } catch (error) {
    // A lost response may happen after the Worker durably wrote the execution.
    // Retry once with the same durable identity; the Worker returns the
    // existing record instead of creating a second fill.
    response = await fetch('/api/paper-trade', options);
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.error || `Execution failed (HTTP ${response.status})`);
    error.orderId = result.order_id || executionRequestId;
    error.orderStatus = result.status || 'rejected';
    throw error;
  }
  return Object.freeze(result);
}

const liveQuotes = new LiveQuoteService();
let lastData = null;          // QQQ snapshot: paper trading/settlement source
let lastDataDate = null;
const UNDERLYING_KEY = 'chain_underlying';
const NO_DATA_TEXT = document.getElementById('no-data')?.textContent || 'No live data yet.';
function readSavedUnderlying() {
  try { return localStorage.getItem(UNDERLYING_KEY) || 'QQQ'; } catch (e) { return 'QQQ'; }
}
const chainSelection = new ChainSelection(readSavedUnderlying());
const chainState = {};         // symbol -> { data, error }, each validated for that symbol
let ticketEl = null;
const pendingExecutions = new Set();
let positionMarks = {};

function openPositionSymbols() {
  const symbols = new Set();
  if (!session) return symbols;
  for (const [sym, position] of Object.entries(computeBook(session.trades))) {
    if (position.pos && position.instrument_type === 'option') symbols.add(sym);
  }
  return symbols;
}

// Same ±DISPLAY window buildHeatmapRows() renders, via the shared helper in
// shared/heatmap.js — computed separately here would risk silently drifting out of
// sync with what's actually on screen.
function visibleContractSymbols(data) {
  const rows = data?.rows || [];
  const spot = data?.underlying_price;
  if (!rows.length || spot == null) return [];
  const { atm, strikes } = computeAtmWindow(rows, spot);
  if (atm == null) return [];
  return rows.filter(row => strikes.has(+row.Strike) && row.OptionSymbol).map(row => row.OptionSymbol);
}

function updateQuoteInterests(data = selectedChainData()) {
  liveQuotes.setOpenPositions(openPositionSymbols());
  liveQuotes.setVisibleContracts(visibleContractSymbols(data));
}

function renderTransportHealth(elementId, label, health) {
  const el = document.getElementById(elementId);
  if (!el) return;
  let detail = '';
  if (health.state === 'live' && health.quoteAgeMs != null) {
    detail = `${Math.max(0, Math.round(health.quoteAgeMs / 1000))}s`;
  } else if (health.state === 'stale') {
    detail = `${health.returned}/${health.requested}`;
  } else if (health.retryAt) {
    const seconds = Math.max(0, Math.ceil((Date.parse(health.retryAt) - Date.now()) / 1000));
    detail = `retry ${seconds}s`;
  }
  const labels = {
    idle: 'waiting', connecting: 'connecting', live: 'live', stale: 'stale',
    'backing-off': 'backing off', 'rate-limited': 'rate limited', unavailable: 'unavailable',
    closed: 'session closed',
  };
  el.className = `transport-health ${health.state}`;
  el.textContent = `${label}: ${labels[health.state] || health.state}${detail ? ` · ${detail}` : ''}`;
  el.title = health.message || '';
}

function renderQuoteHealth(health) {
  renderTransportHealth('quote-health', 'Options', health);
}

let latestSpyHealthRequest = 0;
async function fetchSpyHealth() {
  const request = ++latestSpyHealthRequest;
  let summary;
  try {
    const response = await fetch(`${BROWSER_DATA}/intraday/health.json?_=${Date.now()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    summary = summarizeSpyHealth(await response.json());
  } catch (e) {
    summary = { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY health could not be loaded' };
  }
  if (request !== latestSpyHealthRequest) return;
  const el = document.getElementById('spy-health');
  el.className = `transport-health ${summary.state}`;
  el.textContent = summary.text;
  el.title = summary.detail;
}

liveQuotes.subscribe(() => {
  // Bot marks are independent of lastData/session -- the Automated tab is
  // readable while logged out, so this re-render must come before the guard
  // below rather than after it.
  if (botsTimer) renderBots();
  if (!session) return;
  const book = computeBook(session.trades);
  for (const [symbol, position] of Object.entries(book)) {
    if (!position.pos) continue;
    const quote = liveQuotes.get(symbol);
    if (quote?.mid != null) positionMarks[symbol] = quote.mid;
  }
  renderSelectedChain();
  renderPaper();
});

function ingestSnapshotQuotes(data) {
  if (data.date && (!lastDataDate || data.date > lastDataDate)) lastDataDate = data.date;
  // Last known underlying price for this date, kept so settleExpired() can
  // determine ITM/OTM for short positions once the option itself is no longer
  // in the live quote feed (see settlementPrice()).
  const underlying = data.symbol ?? 'QQQ';
  // A nearest-expiry snapshot's spot belongs to its observation date, not
  // its future expiration. Never carry today's spot forward as an expiry mark.
  if (data.date && data.underlying_price != null && (data.expiration ?? data.date) === data.date) {
    spotMarks[`${underlying}:${data.date}`] = data.underlying_price;
  }
  // Settle before computing open positions for interest-tracking, so a
  // position that expired as of this snapshot's date doesn't count as "open"
  // and keep its now-stale quote alive for an extra cycle.
  settleExpired();
  updateQuoteInterests();
  publishSnapshotQuotes(data);
  if (session) {
    const book = computeBook(session.trades);
    for (const r of (data.rows || [])) {
      const quote = liveQuotes.get(r.OptionSymbol);
      // remember last mark for a held symbol, as a display/unrealized-PnL
      // fallback once it drops out of the live quote feed (e.g. after hours).
      // Not used for expiry settlement -- see settlementPrice().
      if (quote?.mid != null && book[r.OptionSymbol]?.pos) positionMarks[r.OptionSymbol] = quote.mid;
    }
  }
}

// Seeds the quote cache from a snapshot's own rows (any underlying). Quotes
// are keyed by OCC symbol, so SPY and QQQ contracts can't collide.
function publishSnapshotQuotes(data) {
  const snapshotQuotes = (data.rows || []).map(row => ({
    symbol: row.OptionSymbol,
    bid: row.Bid,
    ask: row.Ask,
    mid: row.Mid,
    strike: row.Strike,
    type: row.Type,
    exp: row.Expiration,
  }));
  liveQuotes.publish(snapshotQuotes, { source: 'r2-snapshot', observedAt: data.timestamp });
}

// Average-cost position book: sym -> {pos, avg, realized, strike, type, exp}
function computeBook(trades = session?.trades || []) {
  const book = {};
  for (const t of trades) {
    if (t.adjustment_units !== undefined) {
      if (book[t.sym]) book[t.sym].realized += t.adjustment_units / 10000;
      continue;
    }
    const instrumentType = t.instrument_type ?? 'option';
    const multiplier = t.multiplier ?? (instrumentType === 'share' ? 1 : 100);
    const b = book[t.sym] ?? (book[t.sym] = {
      pos: 0, avg: 0, realized: 0,
      underlying: occRoot(t.sym) || t.underlying || 'QQQ',
      instrument_type: instrumentType, multiplier,
      strike: t.strike, type: t.type, exp: t.exp,
      ...(instrumentType === 'futures_option' ? { contract: t.contract, fees: 0, open_fees: 0, realized_gross: 0 } : {}),
    });
    b.realized -= (t.fee_units ?? 0) / 10000;
    const q = t.side === 'buy' ? t.qty : -t.qty;
    if (instrumentType === 'futures_option') {
      const fee = t.fees ?? 0; b.fees += fee;
      if (q > 0) b.open_fees += fee;
      else {
        const allocated = b.pos > 0 ? b.open_fees * Math.min(-q, b.pos) / b.pos : 0;
        b.open_fees -= allocated; b.realized -= allocated + fee;
        b.realized_gross += (t.price - b.avg) * Math.min(-q, b.pos) * b.multiplier;
      }
    }
    if (b.pos === 0 || Math.sign(b.pos) === Math.sign(q)) {
      b.avg = (b.avg * Math.abs(b.pos) + t.price * Math.abs(q)) / (Math.abs(b.pos) + Math.abs(q));
      b.pos += q;
    } else {
      const closing = Math.min(Math.abs(q), Math.abs(b.pos));
      b.realized += (t.price - b.avg) * closing * b.multiplier * Math.sign(b.pos);
      b.pos += q;
      if (b.pos === 0) b.avg = 0;
      else if (Math.sign(b.pos) === Math.sign(q)) b.avg = t.price;  // flipped through zero
    }
  }
  return book;
}

async function executeTrade(info, side, qty, orderType = 'market', limitPrice = null) {
  info = Object.freeze({ ...info });
  const submittedTicket = ticketEl;
  const normalized = normalizePaperOrder({ side, quantity: qty, orderType, limitPrice });
  if (normalized.error) {
    setTradePending(false, normalized.error);
    announceOrderStatus(normalized.error, 'error');
    return;
  }
  if (!info?.sym) {
    setTradePending(false, 'Select an instrument.');
    announceOrderStatus('Select an instrument.', 'error');
    return;
  }
  if (pendingExecutions.has(info.sym) || !session) return;
  pendingExecutions.add(info.sym);
  setTradePending(true, `Submitting ${normalized.value.order_type} order...`);
  let trade;
  const executionRequestId = newExecutionRequestId();
  announceOrderStatus(`Submitting order ${executionRequestId}...`);
  try {
    trade = await requestExecution(info, normalized.value, executionRequestId);
  } catch (error) {
    // Only the ticket that submitted this order gets its status touched. If
    // the user switched to a different ticket while the order was in flight,
    // or closed the ticket, leave other tickets alone and surface the
    // rejection in the hint instead.
    if (ticketEl && ticketEl === submittedTicket) {
      setTradePending(false, error.message);
    } else {
      showExecutionError(error.message, submittedTicket);
    }
    announceOrderStatus(`${error.orderStatus || 'rejected'} order ${error.orderId || executionRequestId}: ${error.message}`, 'error');
    return;
  } finally {
    pendingExecutions.delete(info.sym);
  }
  session.trades.push(trade);
  session.balance_cash = trade.balance_cash;
  announceOrderStatus(`Filled ${trade.order_type || 'market'} order ${trade.order_id || executionRequestId}.`, 'success');
  cacheSessionDisplay();
  updateQuoteInterests();
  if (ticketEl === submittedTicket) closeTicket();
  renderPaper();
  renderSelectedChain();  // refresh position badges
}

function announceOrderStatus(message, state = '') {
  const status = document.getElementById('paper-order-status');
  if (!status) return;
  status.className = state;
  status.textContent = message;
}

// The hint is the rejection's home only when the ticket that submitted the
// order is no longer open: while it is still open the error already shows
// in that ticket's status line, and writing it into a *different* ticket
// (opened after a switch) would blame the wrong order.
function showExecutionError(message, submittedTicket = ticketEl) {
  if (ticketEl && ticketEl === submittedTicket) return;
  const hint = document.getElementById('paper-hint');
  hint.textContent = `Execution rejected: ${message}`;
  hint.style.display = '';
}

function setTradePending(pending, message = '') {
  if (!ticketEl) return;
  ticketEl.querySelectorAll('button').forEach(button => button.disabled = pending);
  let status = ticketEl.querySelector('.t-status');
  if (!status) {
    status = document.createElement('div');
    status.className = 't-status';
    ticketEl.appendChild(status);
  }
  status.textContent = message;
}

// Settlement for positions held past expiration is now a server round trip
// (POST /api/settle) rather than a local mutation: balances are
// server-authoritative, and an under-collateralized short assignment can
// wipe the account (see showMarginCallScreen below), which has to happen
// server-side where the balance actually lives. This function still decides
// *whether* there's anything to settle (using the same no-exercise-capacity
// long / intrinsic-value short logic the server ports), so a normal render
// cycle with nothing expired never makes a network call.
let settling = false;

function hasExpiredPositions() {
  if (Object.values(computeBook()).some(b => b.instrument_type === 'futures_option' && b.pos > 0 && Date.now() >= Date.parse(b.contract?.expires_at))) return true;
  if (!session || !lastDataDate) return false;
  return Object.values(computeBook(session.trades)).some(b =>
    b.instrument_type === 'option' && b.pos !== 0 && b.exp && b.exp < lastDataDate);
}

async function settleExpired() {
  if (!session || settling || !hasExpiredPositions()) return;
  settling = true;
  try {
    const response = await fetch('/api/settle', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ as_of: lastDataDate || new Date().toISOString().slice(0, 10), spot_marks: spotMarks }),
    });
    const result = await response.json().catch(() => ({}));
    if (response.status === 410) {
      showMarginCallScreen(result.reason || 'A settlement obligation exceeded the account balance');
      return;
    }
    if (!response.ok) return; // transient failure -- retried on the next render cycle
    if (result.settled?.length) {
      session.trades.push(...result.settled);
      session.balance_cash = result.balance_cash;
      cacheSessionDisplay();
      updateQuoteInterests();
      renderPaper();
      renderSelectedChain();
    }
  } catch (error) {
    // network hiccup -- retried on the next render cycle
  } finally {
    settling = false;
  }
}

function closePosition(sym) {
  const b = computeBook()[sym];
  if (!b || b.pos === 0) return;
  executeTrade({
    sym, instrument_type: b.instrument_type,
    strike: b.strike, type: b.type, exp: b.exp,
  }, b.pos > 0 ? 'sell' : 'buy', b.instrument_type === 'futures_option' ? 1 : Math.abs(b.pos));
}

function instrumentLabel(value, symbol = value.sym) {
  if (value.instrument_type === 'prediction') return escapeHtml(symbol);
  if (value.instrument_type === 'futures_option') return escapeHtml(`${value.contract?.future_symbol || '/BTC'} ${value.strike} ${value.type} · ${value.exp}`);
  if ((value.instrument_type ?? 'option') === 'share') return symbol;
  return `${occRoot(symbol) || value.underlying || 'QQQ'} ${value.strike} ${value.type === 'call' ? 'C' : 'P'}${value.exp ? ` · ${value.exp}` : ''}`;
}

function fmtMoney(v) {
  const sign = v < 0 ? '−' : v > 0 ? '+' : '';
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}
function fmtPriceMoney(v) { return `$${Number(v).toFixed(2)}`; }
function pnlClass(v) { return v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : ''; }

// Bot aliases are operator-supplied free text and land in innerHTML on the
// Automated tab. Usernames are already constrained to [A-Za-z0-9_] server-side,
// but aliases are not, so escape both rather than rely on that asymmetry
// holding after some later change to the roster payload.
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function renderPaper() {
  if (!session) return;
  settleExpired();
  updateQuoteInterests();
  const book = computeBook(session.trades);
  const open = Object.entries(book).filter(([, b]) => b.pos !== 0);
  const realized = Object.values(book).reduce((s, b) => s + b.realized, 0);
  let unreal = 0;
  let unavailableBtcMark = false;

  const posDiv = document.getElementById('paper-positions');
  if (open.length === 0) {
    posDiv.innerHTML = '';
  } else {
    const optionRows = open.filter(([, b]) => ['option', 'futures_option'].includes(b.instrument_type));
    const shareRows = open.filter(([, b]) => b.instrument_type === 'share');
    let html = '';
    const eventRows = open.filter(([, b]) => b.instrument_type === 'prediction');
    if (eventRows.length) {
      html += '<h4>Prediction markets</h4><p><a href="/prediction.html">Quotes, paper exits and official results</a></p>';
      for (const [sym, b] of eventRows) html += `<p>${escapeHtml(sym)} · ${b.pos} held · entry ${fmtPriceMoney(b.avg)} · liquidation unavailable here</p>`;
    }
    if (optionRows.length) {
      html += '<h4>Options</h4><table><tr><th>Contract</th><th>Pos</th><th>Avg</th><th>Mark</th><th>PnL</th><th></th></tr>';
      for (const [sym, b] of optionRows) {
        const btc = b.instrument_type === 'futures_option';
        const mark = btc ? (typeof btcOptions !== 'undefined' ? btcOptions.mark(sym) : null) : (liveQuotes.get(sym)?.mid ?? positionMarks[sym] ?? null);
        const pnl = mark != null ? (mark - b.avg) * b.pos * b.multiplier - (b.open_fees || 0) : null;
        if (btc && mark == null) unavailableBtcMark = true;
        if (pnl != null) unreal += pnl;
        html += `<tr>
          <td>${instrumentLabel(b, sym)}</td>
          <td>${b.pos > 0 ? '+' : ''}${b.pos}</td>
          <td>${fmtQ(b.avg)}${btc ? `/BTC · $${(b.avg * 5 * b.pos + b.open_fees).toFixed(2)} basis` : ''}</td>
          <td>${mark != null ? fmtQ(mark) + (btc ? `/BTC · $${(mark * 5 * b.pos).toFixed(2)} liquidation before exit fee` : '') : (btc ? 'Exit unavailable / settlement pending' : '—')}</td>
          <td class="${pnl != null ? pnlClass(pnl) : ''}">${pnl != null ? fmtMoney(pnl) : '—'}</td>
          <td><button class="paper-btn" data-sym="${sym}">close</button></td>
        </tr>`;
      }
      html += '</table>';
    }
    if (shareRows.length) {
      html += '<h4>Shares</h4><table><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Live</th><th>Market value</th><th>PnL</th><th></th></tr>';
      for (const [sym, b] of shareRows) {
        const mark = freshShareQuote(tickerState.get(sym))?.mid ?? null;
        const pnl = mark != null ? (mark - b.avg) * b.pos : null;
        const marketValue = mark != null ? mark * b.pos : null;
        if (pnl != null) unreal += pnl;
        html += `<tr>
          <td>${sym}</td>
          <td>${b.pos > 0 ? '+' : ''}${b.pos}</td>
          <td>${fmtPriceMoney(b.avg)}</td>
          <td>${mark != null ? fmtPriceMoney(mark) : '—'}</td>
          <td>${marketValue != null ? fmtPriceMoney(marketValue) : '—'}</td>
          <td class="${pnl != null ? pnlClass(pnl) : ''}">${pnl != null ? fmtMoney(pnl) : '—'}</td>
          <td><button class="paper-btn" data-sym="${sym}">close</button></td>
        </tr>`;
      }
      html += '</table>';
    }
    posDiv.innerHTML = html;
    posDiv.querySelectorAll('.paper-btn').forEach(btn => btn.onclick = () => closePosition(btn.dataset.sym));
  }

  const total = realized + unreal;
  document.getElementById('paper-summary').innerHTML =
    `Cash <span>${fmtMoney(session.balance_cash)}</span>` +
    (session.trades.length
      ? ` · Realized <span class="${pnlClass(realized)}">${fmtMoney(realized)}</span>
         · Open <span class="${pnlClass(unreal)}">${unavailableBtcMark ? 'unavailable' : fmtMoney(unreal)}</span>
         · Total <span class="${pnlClass(total)}">${unavailableBtcMark ? 'unavailable' : fmtMoney(total)}</span>`
      : '');
  document.getElementById('paper-hint').style.display = session.trades.length ? 'none' : '';

  document.getElementById('paper-log-body').innerHTML =
    session.trades.slice().reverse().map(t => {
      const time = new Date(t.ts).toLocaleTimeString('en-US', { hour12: false, timeZone: 'America/New_York' });
      const market = t.bid != null && t.ask != null ? ` [${fmtQ(t.bid)} × ${fmtQ(t.ask)}]` : '';
      const order = t.order_type === 'limit' ? ` limit ${fmtQ(t.limit_price)}` : ' market';
      const orderId = t.order_id ? ` · order ${t.order_id}` : '';
      const btcClock = t.settlement_mark ? 'official final published' : t.quote_clock_basis === 'observed_side_change'
        ? 'observed side change (not exchange time)' : t.quote_clock_basis === 'provider' ? 'provider side time' : 'legacy source time';
      return `<div>${time} ET · ${t.side.toUpperCase()} ${t.qty} ${instrumentLabel(t)} @ ${fmtQ(t.price)}${market} ·${order}${orderId}${t.note ? ` (${t.note})` : ''}${t.instrument_type === 'futures_option' ? ` · simulated · cash ${fmtMoney(t.cash_delta)} · fee $${t.fees.toFixed(2)} · ${btcClock} ${escapeHtml(t.quote_ts || t.settlement_mark?.published_at || '—')} · sizes ${t.source_quote?.bid_size ?? '—'} × ${t.source_quote?.ask_size ?? '—'}` : ''}</div>`;
    }).join('') || '<div>No trades yet.</div>';

  document.getElementById('user-name').textContent = session.username;
}
