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
  }

  setVisibleContracts(symbols) {
    this.visibleContracts = new Set(symbols || []);
    this._prune();
  }

  setOpenPositions(symbols) {
    this.openPositions = new Set(symbols || []);
    this._prune();
  }

  publish(quotes, { source = 'unknown', observedAt = new Date().toISOString() } = {}) {
    const wanted = this._wanted();
    for (const quote of (quotes || [])) {
      const symbol = quote.symbol;
      if (!symbol || !wanted.has(symbol)) continue;
      const bid = quote.bid ?? null;
      const ask = quote.ask ?? null;
      const mid = bid != null && ask != null ? (bid + ask) / 2 : (quote.mid ?? null);
      this.quotes.set(symbol, {
        bid, ask, mid,
        strike: +quote.strike,
        type: quote.type,
        exp: quote.exp,
        observedAt,
        source,
      });
    }
    this._prune();
    this.listeners.forEach(listener => listener());
  }

  get(symbol) {
    return this.quotes.get(symbol) || null;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  _wanted() {
    return new Set([...this.visibleContracts, ...this.openPositions]);
  }

  _prune() {
    const wanted = this._wanted();
    for (const symbol of this.quotes.keys()) {
      if (!wanted.has(symbol)) this.quotes.delete(symbol);
    }
  }
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

  // ATM
  const strikes = [...new Set(rows.map(r => r.Strike))].sort((a, b) => a - b);
  if (!strikes.length) return false;
  const atm = strikes.reduce((best, s) => Math.abs(s - spot) < Math.abs(best - spot) ? s : best, strikes[0]);

  // Build display range ±DISPLAY around ATM
  const offsets = [];
  for (let o = DISPLAY; o >= -DISPLAY; o--) offsets.push(o);

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

if (typeof module !== 'undefined') module.exports = { LiveQuoteService };
