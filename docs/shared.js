// shared.js — data parsing + heatmap rendering shared between index.html (the
// live app) and diag.html (the R2 fetch diagnostic). Plain classic script (no
// module system, no build step) so both pages just <script src="shared.js">
// this before their own inline script. Kept dependency-free of paper-trading
// state (positions/click handling are passed in via opts) so diag.html can
// exercise the exact same parse/threshold/render code the live app runs,
// without dragging in unrelated app state.

// Ephemeral quote state shared by the live table, trade ticket, and open
// positions. Durable snapshots are one producer today; a faster poller or
// stream can publish through the same interface later without coupling display
// consumers to R2.
class LiveQuoteService {
  constructor() {
    this.quotes = new Map();
    this.visibleContracts = new Set();
    this.openPositions = new Set();
    this.listeners = new Set();
    this.interestListeners = new Set();
  }

  setVisibleContracts(symbols) {
    const next = new Set(symbols || []);
    const changed = !sameSet(this.visibleContracts, next);
    this.visibleContracts = next;
    this._prune();
    if (changed) this._notifyInterests();
  }

  setOpenPositions(symbols) {
    const next = new Set(symbols || []);
    const changed = !sameSet(this.openPositions, next);
    this.openPositions = next;
    this._prune();
    if (changed) this._notifyInterests();
  }

  publish(quotes, { source = 'unknown', observedAt = new Date().toISOString() } = {}) {
    const wanted = this._wanted();
    let changed = false;
    for (const quote of (quotes || [])) {
      const symbol = quote.symbol;
      if (!symbol || !wanted.has(symbol)) continue;
      const bid = quote.bid ?? null;
      const ask = quote.ask ?? null;
      const mid = bid != null && ask != null ? (bid + ask) / 2 : (quote.mid ?? null);
      const quoteObservedAt = quote.quote_ts || observedAt;
      const currentObservedMs = Date.parse(this.quotes.get(symbol)?.observedAt);
      const nextObservedMs = Date.parse(quoteObservedAt);
      if (Number.isFinite(currentObservedMs) && Number.isFinite(nextObservedMs) &&
          nextObservedMs < currentObservedMs) {
        continue;
      }
      this.quotes.set(symbol, {
        bid, ask, mid,
        strike: +quote.strike,
        type: quote.type,
        exp: quote.exp,
        observedAt: quoteObservedAt,
        source,
      });
      changed = true;
    }
    if (changed) this._notify();
  }

  get(symbol) {
    return this.quotes.get(symbol) || null;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  subscribeInterests(listener) {
    this.interestListeners.add(listener);
    return () => this.interestListeners.delete(listener);
  }

  interestSymbols() {
    return [...this._wanted()].sort();
  }

  _wanted() {
    return new Set([...this.visibleContracts, ...this.openPositions]);
  }

  // Interest-set changes (a contract scrolling out of view, a position
  // closing) drop quotes just like a stale publish would, so subscribers need
  // the same notification either way.
  _prune() {
    const wanted = this._wanted();
    let changed = false;
    for (const symbol of this.quotes.keys()) {
      if (!wanted.has(symbol)) {
        this.quotes.delete(symbol);
        changed = true;
      }
    }
    if (changed) this._notify();
  }

  _notify() {
    this.listeners.forEach(listener => listener());
  }

  _notifyInterests() {
    const symbols = this.interestSymbols();
    this.interestListeners.forEach(listener => listener(symbols));
  }
}

function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const value of a) if (!b.has(value)) return false;
  return true;
}

class LiveQuotePoller {
  constructor(service, {
    fetchFn = (...args) => fetch(...args),
    endpoint = '/api/live-quotes',
    foregroundIntervalMs = 5_000,
    hiddenIntervalMs = 60_000,
    maxQuoteAgeMs = 15_000,
    requestTimeoutMs = 8_000,
    maxBackoffMs = 120_000,
    randomFn = Math.random,
    nowFn = Date.now,
    isHidden = () => typeof document !== 'undefined' && document.visibilityState !== 'visible',
  } = {}) {
    this.service = service;
    this.fetchFn = fetchFn;
    this.endpoint = endpoint;
    this.foregroundIntervalMs = foregroundIntervalMs;
    this.hiddenIntervalMs = hiddenIntervalMs;
    this.maxQuoteAgeMs = maxQuoteAgeMs;
    this.requestTimeoutMs = requestTimeoutMs;
    this.maxBackoffMs = maxBackoffMs;
    this.randomFn = randomFn;
    this.nowFn = nowFn;
    this.isHidden = isHidden;
    this.timer = null;
    this.inFlight = null;
    this.failureCount = 0;
    this.running = false;
    this.healthListeners = new Set();
    this.health = {
      state: 'idle', lastAttemptAt: null, lastSuccessAt: null,
      retryAt: null, quoteAgeMs: null, requested: 0, returned: 0,
      httpStatus: null, message: 'Waiting for quote interests',
    };
    this.unsubscribeInterests = service.subscribeInterests(() => this.kick());
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.kick();
  }

  stop() {
    this.running = false;
    clearTimeout(this.timer);
    this.timer = null;
  }

  dispose() {
    this.stop();
    this.unsubscribeInterests();
    this.healthListeners.clear();
  }

  subscribeHealth(listener) {
    this.healthListeners.add(listener);
    listener({ ...this.health });
    return () => this.healthListeners.delete(listener);
  }

  kick() {
    if (!this.running || this.inFlight) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.poll(), 0);
  }

  async poll() {
    if (!this.running || this.inFlight) return this.inFlight;
    const symbols = this.service.interestSymbols();
    if (!symbols.length) {
      this._setHealth({
        state: 'idle', requested: 0, returned: 0, retryAt: null,
        message: 'Waiting for quote interests',
      });
      this._schedule(this._normalInterval());
      return null;
    }

    const startedAt = this.nowFn();
    this._setHealth({
      state: this.failureCount ? 'backing-off' : 'connecting',
      lastAttemptAt: new Date(startedAt).toISOString(),
      requested: symbols.length,
      retryAt: null,
      message: 'Fetching live quotes',
    });
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), this.requestTimeoutMs) : null;
    const url = `${this.endpoint}?symbols=${encodeURIComponent(symbols.join(','))}`;
    this.inFlight = this.fetchFn(url, {
      cache: 'no-store',
      ...(controller ? { signal: controller.signal } : {}),
    });

    try {
      const response = await this.inFlight;
      if (!response.ok) {
        const error = new Error(`HTTP ${response.status}`);
        error.status = response.status;
        error.retryAfter = response.headers?.get?.('Retry-After');
        throw error;
      }
      const payload = await response.json();
      const quotes = Array.isArray(payload.quotes) ? payload.quotes : [];
      if (!quotes.length) throw new Error('No live quotes returned');

      const now = this.nowFn();
      const ages = quotes.map(quote => now - Date.parse(quote.quote_ts)).filter(Number.isFinite);
      const oldestAge = ages.length ? Math.max(...ages) : Infinity;
      this.service.publish(quotes, {
        source: 'dxlink-live',
        observedAt: payload.server_ts || new Date(now).toISOString(),
      });
      this.failureCount = 0;
      const partial = quotes.length < symbols.length;
      const stale = oldestAge > this.maxQuoteAgeMs || oldestAge < -30_000;
      const state = stale || partial ? 'stale' : 'live';
      this._setHealth({
        state,
        lastSuccessAt: new Date(now).toISOString(),
        quoteAgeMs: Number.isFinite(oldestAge) ? oldestAge : null,
        requested: symbols.length,
        returned: quotes.length,
        httpStatus: 200,
        message: stale ? 'Live quotes are stale' : partial ? 'Some live quotes are unavailable' : 'Live quotes healthy',
      });
      this._schedule(this._normalInterval());
    } catch (error) {
      this.failureCount += 1;
      const retryMs = this._retryDelay(error);
      const rateLimited = error.status === 429;
      const unavailable = !rateLimited && this.failureCount === 1;
      this._setHealth({
        state: rateLimited ? 'rate-limited' : unavailable ? 'unavailable' : 'backing-off',
        returned: 0,
        httpStatus: error.status || null,
        retryAt: new Date(this.nowFn() + retryMs).toISOString(),
        message: rateLimited ? 'Live quotes rate limited' : 'Live quotes unavailable',
      });
      this._schedule(retryMs);
    } finally {
      if (timeout) clearTimeout(timeout);
      this.inFlight = null;
    }
    return null;
  }

  _normalInterval() {
    return this.isHidden() ? this.hiddenIntervalMs : this.foregroundIntervalMs;
  }

  _retryDelay(error) {
    const retryAfterMs = parseRetryAfter(error.retryAfter, this.nowFn());
    if (retryAfterMs != null) return Math.min(retryAfterMs, this.maxBackoffMs);
    const base = Math.min(this.foregroundIntervalMs * (2 ** (this.failureCount - 1)), this.maxBackoffMs);
    return Math.round(base * (0.75 + this.randomFn() * 0.5));
  }

  _schedule(delayMs) {
    if (!this.running) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.poll(), delayMs);
  }

  _setHealth(patch) {
    this.health = { ...this.health, ...patch };
    const snapshot = { ...this.health };
    this.healthListeners.forEach(listener => listener(snapshot));
  }
}

function parseRetryAfter(value, nowMs = Date.now()) {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const dateMs = Date.parse(value);
  return Number.isFinite(dateMs) ? Math.max(0, dateMs - nowMs) : null;
}

const DISPLAY = 20;   // ±N strikes shown

const TIER_COLORS = {
  '0DTE_Regular': '#4a90d9',
  '0DTE_Weekly':  '#e8c84b',
  '0DTE_Monthly': '#e8604b',
};

// ── CSV parser ────────────────────────────────────────────────────────────────
function parseCsv(text) {
  const lines = text.trim().split('\n');
  const headers = lines[0].split(',').map(h => h.trim());
  return lines.slice(1).map(line => {
    const vals = line.split(',');
    const obj = {};
    headers.forEach((h, i) => obj[h] = vals[i]?.trim() ?? '');
    return obj;
  });
}

// ── OIranges.csv -> { tier: { offset: { call:[p25,p50,p75,p90], put:[...] } } } ──
function parseRangesCsv(text) {
  const rows = parseCsv(text);
  const out = {};
  for (const r of rows) {
    const tier   = r.Tier;
    const offset = parseInt(r.StrikeOffset);
    if (!out[tier]) out[tier] = {};
    out[tier][offset] = {
      call: [+r.Call_p25, +r.Call_p50, +r.Call_p75, +r.Call_p90],
      put:  [+r.Put_p25,  +r.Put_p50,  +r.Put_p75,  +r.Put_p90],
      call_adj: +r.Call_adj || 1.0,
      put_adj:  +r.Put_adj  || 1.0,
    };
  }
  return out;
}

// ── threshold lookup (mirrors oi_viewer.py effective_thresholds) ──────────────
// `ranges` is passed explicitly (not a closed-over global) so this is callable
// from any page that has fetched+parsed its own OIranges.csv.
function getThresh(ranges, tier, offset, side) {
  if (!ranges) return [0, 0, 0, 0];
  const reg = ranges['0DTE_Regular']?.[offset];
  if (!reg) return [0, 0, 0, 0];
  const base = reg[side];
  if (tier === '0DTE_Regular') return base;
  const adj_key = side === 'call' ? 'call_adj' : 'put_adj';
  const t = ranges[tier]?.[offset];
  const mult = t ? (t[adj_key] || 1.0) : 1.0;
  return base.map(v => v * mult);
}

function oiBucket(oi, thresh) {
  if (oi === 0) return 0;
  const [p25, p50, p75, p90] = thresh;
  if (oi < p25) return 1;
  if (oi < p50) return 2;
  if (oi < p75) return 3;
  if (oi < p90) return 4;
  return 5;
}

function fmtOI(v) {
  if (!v || v === 0) return '';
  if (v < 1000)  return String(v);
  if (v < 10000) return (v / 1000).toFixed(1) + 'K';
  return Math.floor(v / 1000) + 'K';
}

function fmtVol(v) {
  if (!v || v === 0) return '';
  if (v < 1000)  return String(v);
  if (v < 10000) return (v / 1000).toFixed(1) + 'k';
  return Math.floor(v / 1000) + 'k';
}

function fmtQ(v) {
  if (v == null) return '';
  return v < 10 ? v.toFixed(2) : v.toFixed(1);
}
function quoteHtml(bid, ask) {
  if (bid == null && ask == null) return '';
  const b = bid != null ? fmtQ(bid) : '—';
  const a = ask != null ? fmtQ(ask) : '—';
  return `<span class="quote">${b} × ${a}</span>`;
}

// flow level 0–4 based on contracts-per-minute delta
function flowLevel(d) {
  if (d <= 0)   return 0;
  if (d < 20)   return 1;
  if (d < 100)  return 2;
  if (d < 500)  return 3;
  return 4;
}

function flowHtml(delta) {
  const lvl = flowLevel(delta);
  if (lvl === 0) return '';
  const label = delta < 1000 ? `+${delta}` : `+${(delta/1000).toFixed(1)}k`;
  return `<span class="flow flow-${lvl}">${label}</span>`;
}

// ATM strike + the ±display window around it, for a given {rows, spot} shape.
// Shared by buildHeatmapRows (what's rendered) and the live app's quote
// interest tracking (what LiveQuoteService keeps around), so the two windows
// can't drift out of sync with each other.
function computeAtmWindow(rows, spot, display = DISPLAY) {
  const strikeValues = [...new Set((rows || []).map(r => +r.Strike).filter(Number.isFinite))].sort((a, b) => a - b);
  if (!strikeValues.length || spot == null) return { atm: null, offsets: [], strikes: new Set() };
  const atm = strikeValues.reduce((best, s) => Math.abs(s - spot) < Math.abs(best - spot) ? s : best, strikeValues[0]);
  const offsets = [];
  for (let o = display; o >= -display; o--) offsets.push(o);
  const strikes = new Set(offsets.map(o => atm + o));
  return { atm, offsets, strikes };
}

// ── shared heatmap row builder ─────────────────────────────────────────────────
// Used by the live panel, the historical panel, and diag.html's full-pipeline
// test — takes the same {rows, underlying_price, tier} shape regardless of
// whether it came from live JSON or a parsed archive CSV. Paper-trading state
// (position badges, click-to-trade) is opt-in via `opts`, not baked in, so
// callers with no notion of positions (historical panel, diagnostics) don't
// need to fake any of that.
function buildHeatmapRows(tbody, data, ranges, opts = {}) {
  const { getPosition, getQuote, onCellClick } = opts;
  const { rows, underlying_price: spot, tier } = data;
  tbody.innerHTML = '';
  if (!spot || !rows || !rows.length) return false;

  // Index rows by strike+type
  const byStrike = {};
  for (const r of rows) {
    const key = `${r.Strike}`;
    if (!byStrike[key]) byStrike[key] = {};
    byStrike[key][r.Type] = r;
  }

  const { atm, offsets } = computeAtmWindow(rows, spot);
  if (atm == null) return false;

  for (const offset of offsets) {
    const strike = atm + offset;
    const strKey = `${strike}`;
    const callRow = byStrike[strKey]?.call;
    const putRow  = byStrike[strKey]?.put;
    const isAtm   = offset === 0;

    const callOI    = parseInt(callRow?.OpenInterest ?? 0) || 0;
    const putOI     = parseInt(putRow?.OpenInterest  ?? 0) || 0;
    const callVol   = parseInt(callRow?.Volume    ?? 0) || 0;
    const putVol    = parseInt(putRow?.Volume     ?? 0) || 0;
    const callDelta = parseInt(callRow?.VolDelta  ?? 0) || 0;
    const putDelta  = parseInt(putRow?.VolDelta   ?? 0) || 0;
    const callQuote = callRow?.OptionSymbol && getQuote ? getQuote(callRow.OptionSymbol) : null;
    const putQuote  = putRow?.OptionSymbol && getQuote ? getQuote(putRow.OptionSymbol) : null;
    const callBid   = getQuote ? (callQuote?.bid ?? null) : (callRow?.Bid ?? null);
    const callAsk   = getQuote ? (callQuote?.ask ?? null) : (callRow?.Ask ?? null);
    const putBid    = getQuote ? (putQuote?.bid ?? null) : (putRow?.Bid ?? null);
    const putAsk    = getQuote ? (putQuote?.ask ?? null) : (putRow?.Ask ?? null);

    const ct = getThresh(ranges, tier || '0DTE_Regular', offset, 'call');
    const pt = getThresh(ranges, tier || '0DTE_Regular', offset, 'put');
    const cb = oiBucket(callOI, ct);
    const pb = oiBucket(putOI,  pt);

    const tr = document.createElement('tr');
    if (isAtm) tr.classList.add('atm-row');

    // Call cell
    const callTd = document.createElement('td');
    callTd.className = `call-cell c${cb}`;
    const callPos = getPosition ? (getPosition(callRow?.OptionSymbol) || 0) : 0;
    callTd.innerHTML = fmtOI(callOI) +
      quoteHtml(callBid, callAsk) +
      (callVol ? `<span class="vol">${fmtVol(callVol)}</span>` : '') +
      flowHtml(callDelta) +
      (callPos ? `<span class="pos-badge">${callPos > 0 ? '+' : ''}${callPos}</span>` : '');
    if (onCellClick && callRow?.OptionSymbol) {
      callTd.classList.add('tradable');
      callTd.onclick = (e) => onCellClick(e, callRow);
    }
    tr.appendChild(callTd);

    // Strike cell
    const strikeTd = document.createElement('td');
    strikeTd.className = 'strike-cell' + (isAtm ? ' atm' : '');
    strikeTd.textContent = isAtm ? `${strike} ★` : `${strike}`;
    strikeTd.title = `${offset > 0 ? '+' : ''}${offset} from ATM`;
    tr.appendChild(strikeTd);

    // Put cell
    const putTd = document.createElement('td');
    putTd.className = `put-cell p${pb}`;
    const putPos = getPosition ? (getPosition(putRow?.OptionSymbol) || 0) : 0;
    putTd.innerHTML = fmtOI(putOI) +
      quoteHtml(putBid, putAsk) +
      (putVol ? `<span class="vol">${fmtVol(putVol)}</span>` : '') +
      flowHtml(putDelta) +
      (putPos ? `<span class="pos-badge">${putPos > 0 ? '+' : ''}${putPos}</span>` : '');
    if (onCellClick && putRow?.OptionSymbol) {
      putTd.classList.add('tradable');
      putTd.onclick = (e) => onCellClick(e, putRow);
    }
    tr.appendChild(putTd);

    tbody.appendChild(tr);
  }

  return true;
}

if (typeof module !== 'undefined') {
  module.exports = { LiveQuoteService, LiveQuotePoller, parseRetryAfter, computeAtmWindow };
}
