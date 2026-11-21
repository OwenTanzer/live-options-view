// quotes.js — live-quote transport + ticker state, moved out of docs/shared.js
// (B3 slice 3, Nov 2026). Verbatim move: the code below is byte-identical to
// what lived in shared.js lines 20-165, 407-422 and 424-675. Plain classic
// script (no module system, no build step), like the file it came from: the
// live page and diag.html load it with its own <script> tag (see
// docs/index.html, right after the heatmap.js tag), where it defines
// TRADEABLE_SHARE_SYMBOLS / isTradeableShareSymbol / LiveQuoteService /
// tickerSessionState / SHARE_QUOTE_MAX_AGE_MS / freshShareQuote /
// TickerStateStore / sameSet / LiveQuotePoller / parseRetryAfter as globals;
// under Node it exports the same ten names (see the guard at the bottom).
// Self-contained: LiveQuoteService reads sameSet, TickerStateStore reads
// tickerSessionState, LiveQuotePoller reads parseRetryAfter — all in this
// file. No load-time dependencies on any other shared concern. The three
// pieces stay in their original file order; the format.js and chain.js
// sections that used to sit between them stay in shared.js. No ESM
// conversion in this step.

// Ephemeral quote state shared by the live table, trade ticket, and open
// positions. Durable snapshots are one producer today; a faster poller or
// stream can publish through the same interface later without coupling display
// consumers to R2.
const TRADEABLE_SHARE_SYMBOLS = new Set([
  'QQQ', 'USO', 'SMH', 'IGV', 'META', 'GOOGL', 'AMZN', 'TSLA', 'MU', 'SPCX', 'AAPL',
]);

function isTradeableShareSymbol(symbol) {
  return typeof symbol === 'string' && TRADEABLE_SHARE_SYMBOLS.has(symbol);
}

class LiveQuoteService {
  constructor() {
    this.quotes = new Map();
    this.visibleContracts = new Set();
    this.openPositions = new Set();
    // Contracts held by the automated accounts on the Automated tab. Tracked
    // separately from openPositions so the two never overwrite each other --
    // the viewer's own book and the bots' books are independent interests that
    // happen to share one quote transport.
    this.botPositions = new Set();
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

  setBotPositions(symbols) {
    const next = new Set(symbols || []);
    const changed = !sameSet(this.botPositions, next);
    this.botPositions = next;
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
        bid_ts: quote.bid_ts ?? null,
        ask_ts: quote.ask_ts ?? null,
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
    return new Set([...this.visibleContracts, ...this.openPositions, ...this.botPositions]);
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

function tickerSessionState(instrumentClass, nowMs = Date.now()) {
  if (instrumentClass === 'crypto') return 'live';
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', weekday: 'short', hour: '2-digit',
    minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(nowMs)).filter(p => p.type !== 'literal').map(p => [p.type, p.value]));
  const weekday = !['Sat', 'Sun'].includes(parts.weekday);
  const minutes = Number(parts.hour) * 60 + Number(parts.minute);
  if (instrumentClass === 'futures') {
    if (parts.weekday === 'Sat') return 'closed';
    if (parts.weekday === 'Sun') return minutes >= 18 * 60 ? 'live' : 'closed';
    if (parts.weekday === 'Fri') return minutes >= 17 * 60 ? 'closed' : 'live';
    return minutes >= 17 * 60 && minutes < 18 * 60 ? 'closed' : 'live';
  }
  if (!weekday) return 'closed';
  if (instrumentClass === 'international') return 'live'; // source freshness is the session authority
  if (minutes >= 570 && minutes < 960) return 'live';
  if (instrumentClass === 'equity' && minutes >= 240 && minutes < 1200) return 'extended';
  return 'closed';
}

const SHARE_QUOTE_MAX_AGE_MS = 15_000;

function freshShareQuote(quote, nowMs = Date.now(), maxAgeMs = SHARE_QUOTE_MAX_AGE_MS) {
  if (!quote) return null;
  const { bid, ask } = quote;
  if (typeof bid !== 'number' || typeof ask !== 'number' ||
      !Number.isFinite(bid) || !Number.isFinite(ask) || bid < 0 || ask <= 0 || bid > ask) {
    return null;
  }
  const bidMs = Date.parse(quote.bid_ts);
  const askMs = Date.parse(quote.ask_ts);
  if (!Number.isFinite(bidMs) || !Number.isFinite(askMs)) return null;
  const sideAges = [nowMs - bidMs, nowMs - askMs];
  if (sideAges.some(age => age > maxAgeMs || age < -30_000)) return null;
  return { bid, ask, mid: (bid + ask) / 2 };
}

class TickerStateStore {
  constructor(symbolClasses, { nowFn = Date.now, staleAfterMs = 30_000 } = {}) {
    this.symbolClasses = { ...(symbolClasses || {}) };
    this.nowFn = nowFn;
    this.staleAfterMs = staleAfterMs;
    this.quotes = new Map();
    this.listeners = new Set();
    this.interestListeners = new Set();
  }

  interestSymbols() { return Object.keys(this.symbolClasses).sort(); }
  subscribeInterests(listener) { this.interestListeners.add(listener); return () => this.interestListeners.delete(listener); }
  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  get(symbol) { return this._decorate(symbol, this.quotes.get(symbol)); }
  snapshot() { return Object.fromEntries(this.interestSymbols().map(s => [s, this.get(s)]).filter(([, v]) => v)); }

  publish(quotes, { observedAt = new Date(this.nowFn()).toISOString() } = {}) {
    let changed = false;
    for (const quote of (quotes || [])) {
      const symbol = quote.symbol;
      if (!this.symbolClasses[symbol] || quote.price == null) continue;
      const fallbackSource = ['r2-snapshot', 'last-known', 'yfinance'].includes(quote.source);
      const quoteAt = quote.quote_ts ?? (fallbackSource ? null : observedAt);
      const current = this.quotes.get(symbol);
      const currentIsFallback = current &&
        ['r2-snapshot', 'last-known', 'yfinance'].includes(current.source);
      if (current && fallbackSource && !currentIsFallback) {
        const currentAge = this.nowFn() - Date.parse(current.observedAt);
        if (Number.isFinite(currentAge) && currentAge <= this.staleAfterMs) continue;
      }
      // With no provider observation time there is no evidence that one
      // fallback is newer than another, so retain the value already shown.
      if (current && fallbackSource && currentIsFallback && !quoteAt) continue;
      if (current && quoteAt && Date.parse(quoteAt) < Date.parse(current.observedAt)) continue;
      this.quotes.set(symbol, {
        price: quote.price, bid: quote.bid ?? null, ask: quote.ask ?? null,
        bid_ts: quote.bid_ts ?? null, ask_ts: quote.ask_ts ?? null,
        prev_close: quote.prev_close ?? null, chg_pct: quote.chg_pct ?? null,
        source: quote.source || 'unknown', observedAt: quoteAt,
        instrumentClass: quote.instrument_class || this.symbolClasses[symbol],
      });
      changed = true;
    }
    if (changed) this.listeners.forEach(listener => listener());
  }

  publishFallback(prices, observedAt) {
    const eligible = Object.entries(prices || {}).filter(([symbol]) => {
      const current = this.quotes.get(symbol);
      return !current || this.nowFn() - Date.parse(current.observedAt) > this.staleAfterMs;
    });
    this.publish(eligible.map(([symbol, value]) => ({
      symbol, ...value, source: 'r2-snapshot', quote_ts: observedAt,
    })), { observedAt });
  }

  _decorate(symbol, quote) {
    if (!quote) return null;
    const age = this.nowFn() - Date.parse(quote.observedAt);
    let state;
    if (['r2-snapshot', 'last-known', 'yfinance'].includes(quote.source)) state = 'fallback';
    else if (!Number.isFinite(age) || age > this.staleAfterMs || age < -30_000) state = 'stale';
    else state = tickerSessionState(quote.instrumentClass, this.nowFn());
    return { ...quote, state, ageMs: age };
  }
}

function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const value of a) if (!b.has(value)) return false;
  return true;
}

// An error response's JSON body, without letting an empty or malformed one
// replace the HTTP failure with a parse failure. The producer's error
// documents are a few hundred bytes; nothing here buffers a quote payload.
async function readErrorBody(response) {
  try {
    if (typeof response?.json !== 'function') return null;
    const value = await response.json();
    return value && typeof value === 'object' ? value : null;
  } catch (error) {
    return null;
  }
}

class LiveQuotePoller {
  constructor(service, {
    fetchFn = (...args) => fetch(...args),
    endpoint = '/api/live-quotes',
    requestedSymbols = () => service.interestSymbols(),
    foregroundIntervalMs = 5_000,
    hiddenIntervalMs = 60_000,
    closedIntervalMs = 60_000,
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
    this.requestedSymbols = requestedSymbols;
    this.foregroundIntervalMs = foregroundIntervalMs;
    this.hiddenIntervalMs = hiddenIntervalMs;
    this.closedIntervalMs = closedIntervalMs;
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
      httpStatus: null, serviceState: null, sessionState: null, message: 'Waiting for quote interests',
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
    const symbols = this.requestedSymbols();
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
        // The collector answers an option request with 503 + a health block
        // when its quote feed has no session. That is a definite answer
        // ("closed"), not a transient failure, so the body has to survive
        // into the handler instead of being discarded with the error.
        error.body = await readErrorBody(response);
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
        serviceState: payload.health?.state ?? null,
        sessionState: payload.health?.session ?? null,
        message: stale ? 'Live quotes are stale' : partial ? 'Some live quotes are unavailable' : 'Live quotes healthy',
      });
      this._schedule(this._normalInterval());
    } catch (error) {
      const health = error.body?.health || {};
      const serviceState = typeof health.state === 'string' ? health.state : null;
      const sessionState = typeof health.session === 'string' ? health.session : null;
      // 'offline' alone is ambiguous: the producer reports it before the
      // collector has opened a session, while the scheduler deliberately waits
      // for the next one, after a session ended, and after one died. Only the
      // two deliberate outside-session values may be shown as a closed lane:
      // `session_closed` (a session ran and ended) and `waiting_for_session`
      // (a cold start or the scheduler waiting outside the option session).
      // Anything else offline is an outage while the lane should be up, and
      // must keep the ordinary failure and backoff behaviour; labelling a
      // trading-hours failure "session closed" would hide it.
      const closed = error.status === 503 &&
        (sessionState === 'session_closed' || sessionState === 'waiting_for_session');
      if (!closed) this.failureCount += 1;
      const retryMs = closed
        ? this.closedIntervalMs
        : this._retryDelay(error);
      const rateLimited = error.status === 429;
      const unavailable = !rateLimited && !closed && this.failureCount === 1;
      this._setHealth({
        state: closed ? 'closed' : rateLimited ? 'rate-limited' : unavailable ? 'unavailable' : 'backing-off',
        returned: 0,
        httpStatus: error.status || null,
        serviceState,
        sessionState,
        retryAt: new Date(this.nowFn() + retryMs).toISOString(),
        message: closed ? 'The option quote feed is closed for this session'
          : rateLimited ? 'Live quotes rate limited' : 'Live quotes unavailable',
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

if (typeof module !== 'undefined') {
  module.exports = { TRADEABLE_SHARE_SYMBOLS, isTradeableShareSymbol, LiveQuoteService, tickerSessionState, SHARE_QUOTE_MAX_AGE_MS, freshShareQuote, TickerStateStore, sameSet, LiveQuotePoller, parseRetryAfter };
}
