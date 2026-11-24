// Automated tab: trading-floor views of the bot roster.
//
// Two alternatives to the account cards, both fed by the same /api/bots poll
// (no extra requests): "Floor" -- a wall board with a heat grid of bots,
// floor counters, an entry/exit log and a scrolling fill tape -- and
// "Session lanes" -- one lane per bot across today's 09:30-16:00 New York
// session, a bar for each stretch it held a position. renderBots() in
// bots-panel.js hands over its scored roster on every render (poll or quote
// tick); everything here is display only.
//
// Entries and exits come from each bot's `session_fills` (today's New York
// session, oldest first, capped server-side). A fill is classified by
// replaying the book: flat -> open is an entry, open -> flat an exit, a
// sign flip an exit plus entry, otherwise an add or a reduce. The quantity a
// bot held before its first listed fill is its current position minus the
// listed fills' net, so a position carried in from an earlier session (or
// from before a truncated window) still draws a lane.

const BF_SESSION_OPEN_MIN = 9 * 60 + 30;
const BF_SESSION_CLOSE_MIN = 16 * 60;
const BF_RECENT_MS = 15 * 60_000;
const BF_LOG_ROWS = 14;
const BF_TAPE_ITEMS = 12;
const BF_VIEWS = ['cards', 'floor', 'lanes'];
const BF_VIEW_KEY = 'bots-view';

const bfClock = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const bfClockSeconds = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

let bfView = 'cards';

// Minutes after New York midnight for an instant.
function bfEtMinutes(ms) {
  const parts = Object.fromEntries(bfClock.formatToParts(new Date(ms)).map(p => [p.type, p.value]));
  return Number(parts.hour) * 60 + Number(parts.minute);
}

function bfHm(ms) { return bfClock.format(new Date(ms)); }

// "QQQ 610C" for an OCC option symbol, the symbol itself otherwise.
function bfContract(sym, qty) {
  const m = /^([A-Z]+)(\d{6})([CP])(\d{8})$/.exec(String(sym || ''));
  const size = qty != null ? ` ×${Math.abs(qty)}` : '';
  if (!m) return `${String(sym || '—')}${size}`;
  const strike = Number(m[4]) / 1000;
  return `${m[1]} ${Number.isInteger(strike) ? strike : strike.toFixed(1)}${m[3]}${size}`;
}

function bfSide(bot) {
  const ps = bot.positions || [];
  if (!ps.length) return 'FLAT';
  if (ps.length > 1) return 'MULTI';
  const p = ps[0];
  const short = p.qty < 0 ? 'SHORT ' : '';
  if (p.instrument_type === 'share') return short + 'SHARES';
  const t = p.type || (/\d{6}([CP])\d{8}$/.exec(p.sym || '') || [])[1];
  return short + (t === 'put' || t === 'P' ? 'PUT' : 'CALL');
}

// Replays today's fills into entry/exit events and held segments.
function bfReplay(bot, nowMs) {
  if (!Array.isArray(bot.session_fills)) {
    return { events: [], segments: [], coverage: 'unavailable', truncatedFromMs: null, nowMs };
  }
  const fills = bot.session_fills;
  const net = new Map();
  for (const f of fills) net.set(f.sym, (net.get(f.sym) || 0) + (f.side === 'buy' ? f.qty : -f.qty));
  const held = new Map();
  for (const p of bot.positions || []) held.set(p.sym, (held.get(p.sym) || 0) + p.qty);
  const syms = new Set([...net.keys(), ...held.keys()]);
  const qty = new Map();
  const open = new Map();
  const segments = [];
  for (const sym of syms) {
    const start = (held.get(sym) || 0) - (net.get(sym) || 0);
    qty.set(sym, start);
    // Carried in: held before the first listed fill of the day.
    if (start !== 0) open.set(sym, { sym, startMs: null, carried: true, sign: Math.sign(start) });
  }
  const events = [];
  for (const f of fills) {
    const ms = Date.parse(f.ts);
    if (!Number.isFinite(ms)) continue;
    const before = qty.get(f.sym) || 0;
    const after = before + (f.side === 'buy' ? f.qty : -f.qty);
    qty.set(f.sym, after);
    const settlement = f.kind === 'settlement';
    const flip = before !== 0 && after !== 0 && Math.sign(before) !== Math.sign(after);
    if (before !== 0 && (after === 0 || flip)) {
      const seg = open.get(f.sym);
      if (seg) segments.push({ ...seg, endMs: ms, settled: settlement, reversalEnd: flip && !settlement });
      open.delete(f.sym);
    }
    if (after !== 0 && !open.has(f.sym)) {
      open.set(f.sym, { sym: f.sym, startMs: ms, carried: false, sign: Math.sign(after), reversalStart: flip && !settlement });
    }
    const event = (kind, qty = f.qty) => events.push({ ms, kind, fill: { ...f, qty }, bot });
    if (settlement) event('settlement');
    else if (flip) {
      event('exit', Math.abs(before));
      event('entry', Math.abs(after));
    } else if (before === 0 && after !== 0) event('entry');
    else if (before !== 0 && after === 0) event('exit');
    else event(Math.abs(after) > Math.abs(before) ? 'add' : 'reduce');
  }
  for (const seg of open.values()) segments.push({ ...seg, endMs: null });
  const truncated = bot.session_fills_truncated === true && fills.length
    ? Date.parse(fills[0].ts) : null;
  return { events, segments, coverage: bot.session_fills_truncated === true ? 'partial' : 'complete', truncatedFromMs: Number.isFinite(truncated) ? truncated : null, nowMs };
}

function bfKindClass(kind) {
  return kind === 'settlement' ? 'bf-dim' : kind === 'entry' || kind === 'add' ? 'bf-in' : 'bf-out';
}

const BF_KIND_LABEL = { entry: 'IN', exit: 'OUT', add: 'ADD', reduce: 'TRIM', settlement: 'SETTLE' };

function bfCoverage(rows) {
  if (!rows.some(r => r.replay.coverage !== 'complete')) return 'complete';
  return rows.every(r => r.replay.coverage === 'unavailable') ? 'unavailable' : 'partial';
}

function bfCoverageNote(coverage) {
  if (coverage === 'complete') return '';
  return `<div class="bf-dim" role="status">${coverage === 'unavailable'
    ? 'Fill history unavailable. Current positions and P&amp;L can still be shown.'
    : 'Partial fill history. Counts and lanes cover only the fills available; earlier or unavailable activity is not shown.'}</div>`;
}

function bfPnl(s) {
  return s.complete ? s.equity - s.bot.starting_balance : null;
}

function bfMoney(v) {
  if (v == null) return 'pending';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}$${Math.abs(v).toFixed(Math.abs(v) >= 1000 ? 0 : 2)}`;
}

// Heat tint by P&L as a fraction of the starting stake; 5% saturates.
function bfHeat(pnl, stake, strength) {
  if (pnl == null) return 'rgba(120,120,120,0.10)';
  if (pnl === 0 || !stake) return 'rgba(120,120,120,0.16)';
  const a = Math.min(1, Math.abs(pnl / stake) / 0.05) * strength + 0.12;
  return pnl > 0 ? `rgba(31,138,60,${a.toFixed(3)})` : `rgba(192,53,43,${a.toFixed(3)})`;
}

function bfEventText(e) {
  const f = e.fill;
  const verb = f.side === 'buy' ? 'BUY' : 'SELL';
  const tag = e.kind === 'exit' ? ' EXIT' : e.kind === 'entry' ? ' ENTRY' : '';
  if (e.kind === 'settlement') return `${bfHm(e.ms)} ${String(e.bot.alias).toUpperCase()} SETTLE ${bfContract(f.sym, f.qty)} @${fmtQ(f.price)}`;
  return `${bfHm(e.ms)} ${String(e.bot.alias).toUpperCase()} ${verb} ${bfContract(f.sym, f.qty)} @${fmtQ(f.price)}${tag}`;
}

function bfHeader(title, data, nowMs) {
  const asOf = data && data.as_of ? bfClockSeconds.format(new Date(data.as_of)) : '—';
  return `<div class="bf-head">
    <span class="bf-title">${escapeHtml(title)}</span>
    <span class="bf-clock">${bfClockSeconds.format(new Date(nowMs))} ET</span>
    <span class="bf-dim">roster ${escapeHtml(asOf)} ET</span>
  </div>`;
}

function bfTape(events, coverage) {
  const items = events.slice(0, BF_TAPE_ITEMS).map(e =>
    `<span class="${bfKindClass(e.kind)}">${escapeHtml(bfEventText(e))}</span>`).join('');
  if (!items) return `<div class="bf-tape"><div class="bf-tape-empty">${coverage === 'complete'
    ? 'NO FILLS YET THIS SESSION' : 'FILL HISTORY UNAVAILABLE OR PARTIAL'}</div></div>`;
  // Two copies make the crawl seamless; the log below is the accessible copy.
  return `<div class="bf-tape" aria-hidden="true"><div class="bf-crawl">${items}${items}</div></div>`;
}

function bfCollect(scored, nowMs) {
  const rows = scored.map(s => ({ s, replay: bfReplay(s.bot, nowMs) }));
  const events = rows.flatMap(r => r.replay.events).sort((a, b) => b.ms - a.ms);
  return { rows, events };
}

function bfFloorHtml(scored, data, markFn, nowMs) {
  const { rows, events } = bfCollect(scored, nowMs);
  const coverage = bfCoverage(rows);
  const live = rows.filter(r => !r.s.bot.account_closed);
  const inPos = live.filter(r => (r.s.bot.positions || []).length).length;
  const entries = events.filter(e => e.kind === 'entry').length;
  const exits = events.filter(e => e.kind === 'exit').length;
  const marked = rows.filter(r => r.s.complete);
  const floorPnl = marked.reduce((t, r) => t + bfPnl(r.s), 0);
  const pending = rows.length - marked.length;

  const count = n => coverage === 'unavailable' ? '—' :
    coverage === 'partial' ? `${n}<small> · partial</small>` : String(n);
  const stats = [
    ['BOTS IN A POSITION', `${inPos} / ${live.length}`, 'bf-amber'],
    ['ENTRIES TODAY', count(entries), 'bf-red'],
    ['EXITS TODAY', count(exits), 'bf-blue'],
    ['FLOOR P&amp;L SINCE START', escapeHtml(bfMoney(floorPnl)) + (pending ? `<small> · ${pending} pending</small>` : ''),
      floorPnl >= 0 ? 'bf-green' : 'bf-red'],
  ].map(([label, value, cls]) => `<div class="bf-stat"><div class="bf-stat-label">${label}</div>
    <div class="bf-stat-value ${cls}">${value}</div></div>`).join('');

  const tiles = rows.map(({ s, replay }) => {
    const bot = s.bot;
    const pnl = bfPnl(s);
    const last = replay.events[replay.events.length - 1];
    const recent = last && nowMs - last.ms <= BF_RECENT_MS && nowMs >= last.ms;
    const edge = recent ? (last.kind === 'entry' ? 'bf-edge-in' : last.kind === 'exit' ? 'bf-edge-out' : '') : '';
    const ps = bot.positions || [];
    const p = ps.length === 1 ? ps[0] : null;
    const mark = p ? markFn(p.sym) : null;
    const contract = !ps.length ? '—' : p ? bfContract(p.sym, p.qty) : `${ps.length} legs`;
    const event = last
      ? `${BF_KIND_LABEL[last.kind]} ${bfHm(last.ms)}`
      : bot.account_closed ? 'CLOSED' : replay.coverage === 'complete' ? 'NO FILLS TODAY' : 'FILLS UNAVAILABLE';
    const side = bfSide(bot);
    const sideCls = side.endsWith('CALL') ? 'bf-tag-call' : side.endsWith('PUT') ? 'bf-tag-put' : side === 'FLAT' ? 'bf-tag-flat' : 'bf-tag-other';
    return `<div class="bf-tile ${edge}${bot.account_closed ? ' bf-closed' : ''}" style="background:${bfHeat(pnl, bot.starting_balance, 0.55)}">
      <div class="bf-tile-top"><span class="bf-alias">${escapeHtml(bot.alias)}</span><span class="bf-tag ${sideCls}">${side}</span></div>
      <div class="bf-tile-mid"><span class="bf-pnl">${escapeHtml(bfMoney(pnl))}</span><span>${escapeHtml(contract)}</span></div>
      <div class="bf-tile-low"><span>IN ${p && p.avg_price != null ? fmtQ(p.avg_price) : '—'} · MK ${mark != null ? fmtQ(mark) : '—'}</span>
        <span class="${recent ? 'bf-flash ' : ''}${last ? bfKindClass(last.kind) : ''}">${event}</span></div>
    </div>`;
  }).join('');

  const log = events.slice(0, BF_LOG_ROWS).map(e => {
    const f = e.fill;
    return `<li><span class="bf-dim">${bfHm(e.ms)}</span>
      <span class="${bfKindClass(e.kind)} bf-log-tag">${BF_KIND_LABEL[e.kind]}</span>
      <span class="bf-log-text">${escapeHtml(e.bot.alias)} ${escapeHtml(bfContract(f.sym))} @${fmtQ(f.price)}</span></li>`;
  }).join('') || (coverage === 'complete' ? '<li class="bf-dim">No fills yet this session.</li>'
    : '<li class="bf-dim">No fills in the available history.</li>');

  return `<div class="bf-wall">
    ${bfHeader('MOOPERTONIC · BOT DESK', data, nowMs)}
    ${bfTape(events, coverage)}
    ${bfCoverageNote(coverage)}
    <div class="bf-body">
      <div class="bf-main">
        <div class="bf-stats">${stats}</div>
        <div class="bf-grid">${tiles}</div>
      </div>
      <section class="bf-log" aria-label="Fill and settlement log, New York time">
        <h4>FILL &amp; SETTLEMENT LOG · ET</h4>
        <ol>${log}</ol>
      </section>
    </div>
    <div class="bf-foot"><span>Tile colour = P&amp;L since start (deeper = bigger) ·
      <span class="bf-in">amber border = entered</span> · <span class="bf-out">blue border = exited</span> in the last 15 min</span>
      <span>Sorted best → worst</span></div>
  </div>`;
}

function bfPct(min) {
  const span = BF_SESSION_CLOSE_MIN - BF_SESSION_OPEN_MIN;
  return Math.max(0, Math.min(100, ((min - BF_SESSION_OPEN_MIN) / span) * 100));
}

function bfLanesHtml(scored, data, nowMs) {
  const { rows, events } = bfCollect(scored, nowMs);
  const coverage = bfCoverage(rows);
  const nowPct = bfPct(bfEtMinutes(nowMs));
  const ticks = ['09:30', '10:30', '11:30', '12:30', '13:30', '14:30', '15:30'].map(t => {
    const [h, m] = t.split(':').map(Number);
    return `<span style="left:${bfPct(h * 60 + m).toFixed(3)}%">${t}</span>`;
  }).join('');
  const lanes = rows.map(({ s, replay }) => {
    const pnl = bfPnl(s);
    const segs = replay.coverage === 'unavailable' ? '<span class="bf-lane-unavailable bf-dim">History unavailable</span>' : replay.segments.map(seg => {
      const startMin = seg.carried
        ? (replay.truncatedFromMs != null ? bfEtMinutes(replay.truncatedFromMs) : BF_SESSION_OPEN_MIN)
        : bfEtMinutes(seg.startMs);
      const endMin = seg.endMs != null ? bfEtMinutes(seg.endMs) : bfEtMinutes(nowMs);
      // The tape records the full New York date; lanes only show regular hours.
      if (endMin < BF_SESSION_OPEN_MIN || startMin > BF_SESSION_CLOSE_MIN) return '';
      const left = bfPct(startMin);
      const right = bfPct(endMin);
      const entryInLane = !seg.carried && startMin >= BF_SESSION_OPEN_MIN;
      const endInLane = seg.endMs != null && endMin <= BF_SESSION_CLOSE_MIN;
      const m = /\d{6}([CP])\d{8}$/.exec(seg.sym);
      const cls = !m ? 'bf-seg-other' : m[1] === 'C' ? 'bf-seg-call' : 'bf-seg-put';
      return `<div class="bf-seg ${cls}${seg.endMs != null ? ' bf-seg-closed' : ''}" style="left:${left.toFixed(3)}%;width:${Math.max(0.4, right - left).toFixed(3)}%"
        title="${seg.sign < 0 ? 'Short' : 'Long'} ${escapeHtml(bfContract(seg.sym))}"><span>${escapeHtml(bfContract(seg.sym))}</span></div>
        ${!entryInLane ? '' : `<span class="bf-mk bf-in${seg.reversalStart ? ' bf-mk-reversal-in' : ''}" style="left:${left.toFixed(3)}%" title="Entry">▲</span>`}
        ${endInLane ? `<span class="bf-mk ${seg.settled ? 'bf-dim' : 'bf-out'}${seg.reversalEnd ? ' bf-mk-reversal-out' : ''}" style="left:${right.toFixed(3)}%" title="${seg.settled ? 'Settlement' : 'Exit'}">${seg.settled ? '◆' : '✕'}</span>` : ''}`;
    }).join('');
    return `<div class="bf-lane">
      <div class="bf-lane-label"><span class="bf-alias">${escapeHtml(s.bot.alias)}</span>
        <span class="${pnl == null ? 'bf-dim' : pnl >= 0 ? 'bf-green' : 'bf-red'}">${escapeHtml(bfMoney(pnl))}</span></div>
      <div class="bf-track" style="background:${bfHeat(pnl, s.bot.starting_balance, 0.22)}">${segs}
        <div class="bf-now" style="left:${nowPct.toFixed(3)}%"></div></div>
    </div>`;
  }).join('');
  return `<div class="bf-wall">
    ${bfHeader('MOOPERTONIC · SESSION LANES', data, nowMs)}
    ${bfTape(events, coverage)}
    ${bfCoverageNote(coverage)}
    <div class="bf-legend"><span><i class="bf-sw bf-seg-call"></i>call held (long or short)</span>
      <span><i class="bf-sw bf-seg-put"></i>put held (long or short)</span>
      <span><i class="bf-sw bf-seg-other"></i>shares / other</span>
      <span class="bf-in">▲ entry</span><span class="bf-out">✕ exit</span><span>◆ settlement</span><span>│ now</span>
      <span class="bf-dim">faded bar = closed · left-edge bar = carried in when history is complete</span></div>
    <div class="bf-lanes">
      <div class="bf-lane bf-axis"><div class="bf-lane-label"></div><div class="bf-ticks">${ticks}</div></div>
      ${lanes}
    </div>
    <div class="bf-foot"><span>Lane tint = P&amp;L since start (same scale as the floor) · each bar is time held, 09:30–16:00 ET</span></div>
  </div>`;
}

function renderBotFloor(scored, data, markFn) {
  const host = document.getElementById('bot-floor');
  if (!host || bfView === 'cards') return;
  const nowMs = typeof window !== 'undefined' && window.oneClockDashboard
    ? window.oneClockDashboard.liveTime() : Date.now();
  host.innerHTML = bfView === 'lanes'
    ? bfLanesHtml(scored || [], data, nowMs)
    : bfFloorHtml(scored || [], data, markFn || (() => null), nowMs);
}

function setBotsView(view, { persist = true } = {}) {
  if (!BF_VIEWS.includes(view)) view = 'cards';
  bfView = view;
  const cards = document.getElementById('bots-cards');
  const floor = document.getElementById('bot-floor');
  if (cards) cards.classList.toggle('hidden', view !== 'cards');
  if (floor) floor.classList.toggle('hidden', view === 'cards');
  for (const btn of document.querySelectorAll('.bots-view-btn')) {
    btn.setAttribute('aria-pressed', String(btn.dataset.view === view));
  }
  if (persist) {
    try { localStorage.setItem(BF_VIEW_KEY, view); } catch { /* per-viewer convenience only */ }
  }
  if (view !== 'cards' && typeof renderBots === 'function') renderBots();
}

(function initBotsView() {
  if (typeof document === 'undefined' || !document.querySelectorAll) return;
  for (const btn of document.querySelectorAll('.bots-view-btn')) {
    btn.addEventListener('click', () => setBotsView(btn.dataset.view));
  }
  let saved = null;
  try { saved = localStorage.getItem(BF_VIEW_KEY); } catch { /* ignore */ }
  // Initialization has never persisted a default: existing saved values are
  // explicit viewer choices. Keep them, including Cards on a phone.
  const phone = typeof matchMedia === 'function' && matchMedia('(max-width: 600px)').matches;
  setBotsView(BF_VIEWS.includes(saved) ? saved : phone ? 'floor' : 'cards', { persist: false });
})();
