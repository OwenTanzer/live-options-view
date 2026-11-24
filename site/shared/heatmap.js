// heatmap.js — heatmap cell rendering, moved out of docs/shared.js
// (B3 slice 2, Nov 2026). Verbatim move: the code below is byte-identical to
// what lived in shared.js lines 711-868. Plain classic script (no module
// system, no build step), like the file it came from: the live page and
// diag.html load it with its own <script> tag (see docs/index.html, right
// after the csv-ranges.js tag), where it defines DISPLAY / TIER_COLORS /
// fmtOI / fmtVol / fmtQ / quoteHtml / flowLevel / flowHtml / computeAtmWindow /
// buildHeatmapRows as globals; under Node it exports the same ten names (see
// the guard at the bottom). DISPLAY is the ±N strikes shown around the ATM
// strike; the cell formatters exist to render heatmap cells and quote chips
// in rows, which is why they live here and not in format.js (panel status
// text and order validation). No ESM conversion in this step.

// Under Node, buildHeatmapRows needs getThresh / oiBucket from the csv-ranges
// concern. The guarded `var` require below follows the B3 entry rule: `var`,
// never `let`/`const`, so the hoisted binding cannot clash with the
// same-named function declaration in the browser / vm global scope
// (csv-ranges.js defines both as globals; this file only reads them). In the
// browser the guard never runs — the names resolve to the globals the
// csv-ranges.js tag defined — and under Node they are module-scoped.
if (typeof module !== 'undefined' && typeof require === 'function') {
  var csvRanges = require('./csv-ranges.js');
  var getThresh = csvRanges.getThresh;
  var oiBucket = csvRanges.oiBucket;
}

const DISPLAY = 20;   // ±N strikes shown

const TIER_COLORS = {
  '0DTE_Regular': '#4a90d9',
  '0DTE_Weekly':  '#e8c84b',
  '0DTE_Monthly': '#e8604b',
};


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
//
// The window is selected along the snapshot's OWN strike ladder (#219). It
// used to be synthesized as `atm + offset`, which silently assumed a
// contiguous $1 ladder. QQQ and SPY happen to be $1 apart, so nothing showed;
// IBIT publishes whole-dollar strikes at 30–39 and 51–59 and half-dollar
// strikes at 39.5–50, so a synthetic grid invented strikes that were never
// published. Those cells rendered empty — a 19:38Z render centered on 41.5
// produced 30 phantom strikes out of 41 — and the quote request asked for the
// invented contracts while skipping real ones. Selecting by ladder position
// keeps `strikes` a subset of what the producer actually published, and
// `offset` becomes the distance in ladder steps from the ATM strike (identical
// to the old dollar offset on a $1 ladder, so OI calibration is unchanged).
function computeAtmWindow(rows, spot, display = DISPLAY) {
  const strikeValues = [...new Set((rows || []).map(r => +r.Strike).filter(Number.isFinite))].sort((a, b) => a - b);
  if (!strikeValues.length || spot == null) {
    return { atm: null, atmIndex: -1, offsets: [], strikes: new Set(), window: [] };
  }
  let atmIndex = 0;
  for (let i = 1; i < strikeValues.length; i++) {
    if (Math.abs(strikeValues[i] - spot) < Math.abs(strikeValues[atmIndex] - spot)) atmIndex = i;
  }
  const window = [];
  for (let o = display; o >= -display; o--) {
    const i = atmIndex + o;
    if (i < 0 || i >= strikeValues.length) continue;
    window.push({ strike: strikeValues[i], offset: o });
  }
  return {
    atm: strikeValues[atmIndex],
    atmIndex,
    offsets: window.map(entry => entry.offset),
    strikes: new Set(window.map(entry => entry.strike)),
    window,
  };
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
  if (!rows?.length || (!spot && !opts.showUnanchored)) return false;

  // Index rows by strike+type
  const byStrike = {};
  for (const r of rows) {
    const key = `${r.Strike}`;
    if (!byStrike[key]) byStrike[key] = {};
    byStrike[key][r.Type] = r;
  }

  // A replay publication can retain quotes and strikes after withdrawing its spot.
  const window = spot ? computeAtmWindow(rows, spot).window :
    Object.keys(byStrike).map(Number).filter(Number.isFinite).sort((a,b)=>b-a)
      .map(strike=>({strike,offset:null}));

  // A supplied opening reference controls OI only. Current strikes, ATM,
  // volume, quotes and option selection still use the displayed snapshot.
  const frozen=Object.hasOwn(opts,'openingOi');
  const opening=new Map((opts.openingOi?.levels??[]).map(level=>[level.strike,level]));
  // One row per PUBLISHED strike in the window, highest first. No row is
  // emitted for a strike the snapshot never carried (#219), so the table can
  // no longer show a strike with two empty cells beside it.
  for (const { strike, offset } of window) {
    const strKey = `${strike}`;
    const callRow = byStrike[strKey]?.call;
    const putRow  = byStrike[strKey]?.put;
    const isAtm   = offset === 0;

    const level=opening.get(strike);
    const callOI = callRow?.OpenInterest == null ? null : parseInt(callRow.OpenInterest);
    const putOI = putRow?.OpenInterest == null ? null : parseInt(putRow.OpenInterest);
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

    const ct = frozen ? null : getThresh(ranges, tier || '0DTE_Regular', offset, 'call');
    const pt = frozen ? null : getThresh(ranges, tier || '0DTE_Regular', offset, 'put');
    const cb = frozen ? (level?.call??0) : offset===null ? 0 : oiBucket(callOI??0, ct);
    const pb = frozen ? (level?.put??0) : offset===null ? 0 : oiBucket(putOI??0, pt);

    const tr = document.createElement('tr');
    if (isAtm) tr.classList.add('atm-row');

    // Call cell
    const callTd = document.createElement('td');
    if(frozen)callTd.title=level?.callOI!=null?'Snapshot OI; color uses opening reference OI '+level.callOI:'Snapshot OI; opening color unavailable';
    callTd.className = `call-cell c${cb}`;
    const callPos = getPosition ? (getPosition(callRow?.OptionSymbol) || 0) : 0;
    callTd.innerHTML = (frozen && !Number.isFinite(callOI)?'—':fmtOI(callOI)) +
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
    // Ladder steps, not dollars: on a mixed ladder (IBIT) neighbouring rows can
    // be $0.50 or $1 apart, so a dollar claim here would be a guess.
    strikeTd.title = offset===null ? 'Archived strike' : isAtm ? 'ATM strike'
      : `${Math.abs(offset)} ${Math.abs(offset) === 1 ? 'strike' : 'strikes'} ${offset > 0 ? 'above' : 'below'} ATM`;
    tr.appendChild(strikeTd);

    // Put cell
    const putTd = document.createElement('td');
    if(frozen)putTd.title=level?.putOI!=null?'Snapshot OI; color uses opening reference OI '+level.putOI:'Snapshot OI; opening color unavailable';
    putTd.className = `put-cell p${pb}`;
    const putPos = getPosition ? (getPosition(putRow?.OptionSymbol) || 0) : 0;
    putTd.innerHTML = (frozen && !Number.isFinite(putOI)?'—':fmtOI(putOI)) +
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
  module.exports = { DISPLAY, TIER_COLORS, fmtOI, fmtVol, fmtQ, quoteHtml, flowLevel, flowHtml, computeAtmWindow, buildHeatmapRows };
}
