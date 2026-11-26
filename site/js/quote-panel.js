// quote-panel DOM + one refresh controller (#179).
//
// Everything here reads from the shared QuoteStore in site/shared/quote-panel.js
// and the generated instrument projection. There is one timer: QuotePanelController.
// Card rendering never fetches; a failed refresh leaves the last values in place
// with their original ages and a visible status.

const QUOTE_PANEL_VISIBLE_MS = 15_000;
const QUOTE_PANEL_HIDDEN_MS = 60_000;
const QUOTE_PANEL_BACKOFF_MS = 15_000;
const QUOTE_PANEL_MAX_BACKOFF_MS = 120_000;

function quotePanelElement(doc, tag, className, text) {
  const el = doc.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function quotePanelClear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function quoteStateText(row) {
  if (!row || row.state === 'missing') return 'unavailable';
  if (row.state === 'daily') return 'daily';
  if (row.state === 'closed' || row.state === 'maintenance') return row.session === 'maintenance' ? 'break' : 'closed';
  if (row.state === 'stale') return 'stale';
  if (row.state === 'unknown') return 'unknown';
  return QUOTE_SESSION_LABELS[row.session] || 'live';
}

function quoteCardTitle(row) {
  const parts = [row.name, row.source ? `source ${row.source}` : 'source unknown'];
  parts.push(row.observedAt ? `observed ${row.observedAt}` : row.daily ? 'daily observation' : 'observation time unavailable');
  if (row.stale) parts.push('retained after a failed refresh');
  return parts.join(' · ');
}

function quoteCard(doc, row, { section, index = null, movable = false, onPinToggle = null, onMove = null } = {}) {
  const el = quotePanelElement(doc, 'article', 'ticker-btn quote-card');
  el.setAttribute('data-symbol', row.label);
  el.dataset.symbol = row.label;
  el.setAttribute('data-section', section);
  el.setAttribute('tabindex', '0');
  el.setAttribute('aria-label', `${row.name}, ${formatQuotePrice(row)}, ${formatQuotePercent(row.changePct)}, ${quoteStateText(row)}`);
  const symbol = quotePanelElement(doc, 'span', 'sym', row.label);
  const price = quotePanelElement(doc, 'span', 'px', '—');
  const change = quotePanelElement(doc, 'span', 'chg flat');
  const state = quotePanelElement(doc, 'span', 'market-state', 'unavailable');
  const age = quotePanelElement(doc, 'span', 'quote-age', '');
  el.appendChild(symbol);
  el.appendChild(price);
  el.appendChild(change);
  el.appendChild(state);
  el.appendChild(age);
  if (row.label === 'QQQ' && section === 'core') {
    const momentum = quotePanelElement(doc, 'span', 'momentum-strip');
    momentum.id = 'qqq-card-momentum';
    el.appendChild(momentum);
  }
  const card = { el, row, section, index, priceEl: price, changeEl: change, stateEl: state, ageEl: age, moveLeft: null, moveRight: null, unpin: null };
  if (movable) {
    const controls = quotePanelElement(doc, 'span', 'pin-controls');
    const left = quotePanelElement(doc, 'button', 'pin-move', '◀');
    left.setAttribute('type', 'button');
    left.setAttribute('aria-label', `Move ${row.label} left`);
    left.addEventListener('click', event => { if (event?.stopPropagation) event.stopPropagation(); onMove?.(-1); });
    const right = quotePanelElement(doc, 'button', 'pin-move', '▶');
    right.setAttribute('type', 'button');
    right.setAttribute('aria-label', `Move ${row.label} right`);
    right.addEventListener('click', event => { if (event?.stopPropagation) event.stopPropagation(); onMove?.(1); });
    const unpin = quotePanelElement(doc, 'button', 'pin-remove', '✕');
    unpin.setAttribute('type', 'button');
    unpin.setAttribute('aria-label', `Unpin ${row.label}`);
    unpin.addEventListener('click', event => { if (event?.stopPropagation) event.stopPropagation(); onPinToggle?.(row.label); });
    controls.appendChild(left); controls.appendChild(right); controls.appendChild(unpin);
    el.appendChild(controls);
    card.moveLeft = left; card.moveRight = right; card.unpin = unpin;
  }
  if (typeof isTradeableShareSymbol === 'function' && isTradeableShareSymbol(row.label)
      && typeof openShareTicket === 'function') {
    el.classList.add('tradable');
    el.setAttribute('role', 'button');
    el.addEventListener('click', event => openShareTicket(event, row.label));
    el.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openShareTicket(event, row.label);
      }
    });
  }
  paintQuoteCard(card, row);
  el._quoteCard = card;
  return card;
}

function paintQuoteCard(card, row) {
  const { el, priceEl, changeEl, stateEl, ageEl } = card;
  card.row = row;
  priceEl.textContent = formatQuotePrice(row);
  changeEl.textContent = formatQuotePercent(row.changePct);
  changeEl.className = `chg ${row.changePct === null ? 'flat' : row.changePct > 0 ? 'up' : row.changePct < 0 ? 'down' : 'flat'}`;
  stateEl.textContent = quoteStateText(row);
  ageEl.textContent = row.ageMs === null
    ? (row.daily ? 'daily' : 'time unknown')
    : `updated ${formatQuoteAge(row.ageMs)} ago`;
  el.title = quoteCardTitle(row);
  el.dataset.state = row.state;
  el.setAttribute('aria-label', `${row.name}, ${formatQuotePrice(row)}, ${formatQuotePercent(row.changePct)}, ${quoteStateText(row)}`);
  return card;
}

function syncQuoteCards(container, labels, rowFor, build) {
  const existing = container.children;
  const labelOf = child => child.getAttribute
    ? child.getAttribute('data-symbol')
    : (child.dataset?.symbol ?? null);
  const same = existing.length === labels.length &&
    labels.every((label, index) => labelOf(existing[index]) === label);
  if (same) {
    for (let index = 0; index < labels.length; index += 1) {
      const card = existing[index]._quoteCard;
      card.index = index;
      paintQuoteCard(card, rowFor(labels[index]));
      if (card.moveLeft) card.moveLeft.disabled = index === 0;
      if (card.moveRight) card.moveRight.disabled = index === labels.length - 1;
    }
    return existing;
  }
  quotePanelClear(container);
  for (let index = 0; index < labels.length; index += 1) {
    const card = build(labels[index], index);
    if (card.moveLeft) card.moveLeft.disabled = index === 0;
    if (card.moveRight) card.moveRight.disabled = index === labels.length - 1;
    container.appendChild(card.el);
  }
  return container.children;
}

function renderQuotePanel(panel, snapshot) {
  const { documentRef: doc } = panel;
  const session = quoteSessionMode(snapshot);
  panel.els.session.textContent = session.label;
  panel.els.session.dataset.mode = session.mode;
  const newest = snapshot.rows.reduce((latest, row) => {
    const ms = row.observedMs ?? row.receiptMs;
    return ms !== null && Number.isFinite(ms) && ms > latest ? ms : latest;
  }, 0);
  const age = newest ? Math.max(0, snapshot.nowMs - newest) : null;
  panel.els.updated.textContent = newest ? `Quotes updated ${formatQuoteAge(age)} ago` : 'Observation time unavailable';
  if (panel.status) panel.els.updated.textContent = panel.status;

  // Core: always visible, even closed; missing rows stay visibly unavailable.
  // Cards are reconciled in place so a refresh cannot steal keyboard focus.
  const rowFor = label => snapshot.byLabel.get(label) || normalizeQuoteRow(label, null, null, snapshot.nowMs);
  syncQuoteCards(panel.els.core, [...QUOTE_CORE_LABELS], rowFor,
    label => quoteCard(doc, rowFor(label), { section: 'core' }));

  // Pins: user order, independent of the active session.
  const pinned = panel.pins.list();
  panel.els.pinCount.textContent = pinned.length ? `${pinned.length}/${panel.pins.limit}` : '';
  panel.els.pinsEmpty.hidden = pinned.length > 0;
  syncQuoteCards(panel.els.pins, pinned, rowFor, (label, index) => quoteCard(doc, rowFor(label), {
    section: 'pins', index, movable: true,
    onPinToggle: value => panel.removePin(value),
    onMove: delta => panel.movePin(label, delta),
  }));
  panel.els.pinScope.textContent = panel.pins.persistent
    ? 'Pins are saved in this browser only, not to your account.'
    : 'Pins are kept for this visit only: browser storage is unavailable.';
  if (panel.pins.dropped.length) {
    panel.els.pinMessage.textContent = `${panel.pins.dropped.length} pinned instrument(s) are no longer in the tracked universe and were removed.`;
  }

  // Movers: only the active, comparable group; fewer results instead of zeros.
  const movers = computeMovers(snapshot, { mode: session.mode === 'none' ? null : session.mode });
  panel.els.moversScope.textContent = movers.label
    ? `${movers.label} · ${movers.eligible} eligible of our tracked universe`
    : 'No active group';
  renderMoverList(doc, panel.els.moversUp, movers.up, 'up');
  renderMoverList(doc, panel.els.moversDown, movers.down, 'down');
  panel.els.moversNote.textContent = movers.eligible === 0
    ? 'No instrument in the active group has a fresh, valid comparison right now.'
    : `Showing ${movers.up.length} up and ${movers.down.length} down. Ranked within our tracked universe, not the whole market.`;
}

function renderMoverList(doc, list, rows, direction) {
  quotePanelClear(list);
  rows.forEach((row, index) => {
    const item = doc.createElement('li');
    item.className = `mover ${direction}`;
    const rank = quotePanelElement(doc, 'span', 'mover-rank', String(index + 1));
    const name = quotePanelElement(doc, 'span', 'mover-name', row.label);
    const pct = quotePanelElement(doc, 'span', `mover-change ${direction}`, formatQuotePercent(row.changePct));
    const price = quotePanelElement(doc, 'span', 'mover-price', formatQuotePrice(row));
    item.appendChild(rank); item.appendChild(name); item.appendChild(price); item.appendChild(pct);
    item.title = quoteCardTitle(row);
    list.appendChild(item);
  });
}

function initQuotePanel({ documentRef = document, store = null, pins = null } = {}) {
  const doc = documentRef;
  const els = {
    session: doc.getElementById('qp-session'),
    updated: doc.getElementById('qp-updated'),
    core: doc.getElementById('qp-core'),
    pins: doc.getElementById('qp-pins'),
    pinsEmpty: doc.getElementById('qp-pins-empty'),
    pinCount: doc.getElementById('qp-pin-count'),
    pinScope: doc.getElementById('qp-pin-scope'),
    pinAdd: doc.getElementById('qp-pin-add'),
    pinDialog: doc.getElementById('qp-pin-dialog'),
    pinSearch: doc.getElementById('qp-pin-search'),
    pinList: doc.getElementById('qp-pin-list'),
    pinMessage: doc.getElementById('qp-pin-message'),
    pinClose: doc.getElementById('qp-pin-close'),
    moversScope: doc.getElementById('qp-movers-scope'),
    moversUp: doc.getElementById('qp-movers-up'),
    moversDown: doc.getElementById('qp-movers-down'),
    moversNote: doc.getElementById('qp-movers-note'),
    announce: doc.getElementById('qp-announce'),
  };
  for (const [name, el] of Object.entries(els)) {
    if (!el) throw new Error(`quote panel is missing #${name} element`);
  }
  const panel = {
    documentRef: doc,
    store: store || (typeof quoteStore !== 'undefined' ? quoteStore : new QuoteStore()),
    pins: pins || (typeof quotePins !== 'undefined' ? quotePins : new QuotePins({
      storage: safeStorage(),
    })),
    els,
    status: null,
    render(snapshot) {
      const current = snapshot || this.store.snapshot();
      renderQuotePanel(this, current);
      renderPinCandidates(this, current);
    },
    announce(message) { els.announce.textContent = message; },
    setStatus(message) {
      this.status = message;
      if (message) els.updated.textContent = message;
    },
    openPicker() {
      els.pinDialog.hidden = false;
      if (typeof els.pinSearch.focus === 'function') els.pinSearch.focus();
      renderPinCandidates(this, this.store.snapshot());
    },
    closePicker() {
      els.pinDialog.hidden = true;
      if (typeof els.pinAdd.focus === 'function') els.pinAdd.focus();
    },
    addPin(label) {
      const result = this.pins.add(label);
      if (!result.ok) {
        els.pinMessage.textContent = result.reason === 'pin_limit'
          ? `Pin limit reached (${this.pins.limit}). Unpin one first.`
          : 'That instrument cannot be pinned.';
        return result;
      }
      this.announce(`Pinned ${label}.`);
      this.render();
      renderPinCandidates(this, this.store.snapshot());
      return result;
    },
    removePin(label) {
      const result = this.pins.remove(label);
      if (result.ok) this.announce(`Unpinned ${label}.`);
      this.render();
      return result;
    },
    movePin(label, delta) {
      const result = this.pins.move(label, delta);
      if (result.ok) this.announce(`${label} moved ${delta < 0 ? 'left' : 'right'}.`);
      this.render();
      return result;
    },
  };
  els.pinAdd.addEventListener('click', () => panel.openPicker());
  els.pinClose.addEventListener('click', () => panel.closePicker());
  els.pinDialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); panel.closePicker(); }
  });
  els.pinSearch.addEventListener('input', () => renderPinCandidates(panel, panel.store.snapshot()));
  panel.render();
  return panel;
}

function renderPinCandidates(panel, snapshot) {
  const { documentRef: doc, els } = panel;
  if (!els.pinList) return;
  quotePanelClear(els.pinList);
  const query = els.pinSearch.value || '';
  const candidates = quoteCandidates(snapshot, panel.pins, query).slice(0, 40);
  if (!candidates.length) {
    const empty = quotePanelElement(doc, 'li', 'quote-note', 'No matching unpinned instruments.');
    els.pinList.appendChild(empty);
    return;
  }
  for (const row of candidates) {
    const item = doc.createElement('li');
    item.className = 'pin-candidate';
    const add = quotePanelElement(doc, 'button', 'pin-add', 'Pin');
    add.setAttribute('type', 'button');
    add.setAttribute('aria-label', `Pin ${row.label}, ${row.name}`);
    add.addEventListener('click', () => panel.addPin(row.label));
    const label = quotePanelElement(doc, 'span', 'pin-candidate-label', row.label);
    const name = quotePanelElement(doc, 'span', 'pin-candidate-name', row.name);
    const price = quotePanelElement(doc, 'span', 'pin-candidate-price', formatQuotePrice(row));
    item.appendChild(add); item.appendChild(label); item.appendChild(name); item.appendChild(price);
    els.pinList.appendChild(item);
  }
}

function safeStorage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null; } catch (e) { return null; }
}

class QuotePanelController {
  constructor({
    refresh, store, panel, documentRef = document, windowRef = null,
    nowFn = Date.now, visibleMs = QUOTE_PANEL_VISIBLE_MS,
    hiddenMs = QUOTE_PANEL_HIDDEN_MS, backoffMs = QUOTE_PANEL_BACKOFF_MS,
    maxBackoffMs = QUOTE_PANEL_MAX_BACKOFF_MS,
  } = {}) {
    this.refreshFn = refresh;
    this.store = store;
    this.panel = panel;
    this.documentRef = documentRef;
    this.windowRef = windowRef;
    this.nowFn = nowFn;
    this.visibleMs = visibleMs;
    this.hiddenMs = hiddenMs;
    this.backoffMs = backoffMs;
    this.maxBackoffMs = maxBackoffMs;
    this.timer = null;
    this.inFlight = null;
    this.failures = 0;
    this.running = false;
    this._onVisibility = this._onVisibility.bind(this);
    this._onFocus = () => this.kick();
  }

  start({ immediate = true } = {}) {
    if (this.running) return;
    this.running = true;
    this._listen();
    if (immediate) this.kick();
    else this._schedule(this._interval());
  }

  stop() {
    this.running = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this._unlisten();
  }

  dispose() { this.stop(); }

  _listen() {
    this.documentRef?.addEventListener?.('visibilitychange', this._onVisibility);
    this.windowRef?.addEventListener?.('focus', this._onFocus);
  }

  _unlisten() {
    this.documentRef?.removeEventListener?.('visibilitychange', this._onVisibility);
    this.windowRef?.removeEventListener?.('focus', this._onFocus);
  }

  _hidden() {
    return this.documentRef?.visibilityState === 'hidden';
  }

  _interval() { return this._hidden() ? this.hiddenMs : this.visibleMs; }

  _onVisibility() {
    if (!this.running) return;
    if (!this._hidden()) this.kick();
    else this._schedule(this.hiddenMs);
  }

  kick() {
    if (!this.running || this.inFlight) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refreshOnce(), 0);
  }

  _schedule(delay) {
    if (!this.running) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refreshOnce(), delay);
  }

  async refreshOnce() {
    if (!this.running || this.inFlight) return this.inFlight;
    const startedAt = this.nowFn();
    this.inFlight = (async () => {
      try {
        const payload = await this.refreshFn();
        if (payload === null || payload === undefined || payload === false) throw new Error('refresh_failed');
        this.failures = 0;
        this.store.mergePayload(payload);
        this.panel.status = null;
        this.panel?.render?.(this.store.snapshot());
        this._schedule(this._interval());
        return payload;
      } catch (error) {
        this.failures += 1;
        const delay = Math.min(this.maxBackoffMs, this.backoffMs * (2 ** (this.failures - 1)));
        const ago = formatQuoteAge(this.nowFn() - startedAt);
        // Re-decorate retained rows on the failure path too: ages and stale
        // states must advance even when no new payload arrives.
        this.panel?.render?.(this.store.snapshot());
        this.panel?.setStatus?.(`Quote refresh failed · showing last values (${ago} ago)`);
        this._schedule(delay);
        return null;
      } finally {
        this.inFlight = null;
      }
    })();
    return this.inFlight;
  }
}

if (typeof module !== 'undefined') {
  module.exports = {
    QUOTE_PANEL_VISIBLE_MS, QUOTE_PANEL_HIDDEN_MS, QUOTE_PANEL_BACKOFF_MS,
    QUOTE_PANEL_MAX_BACKOFF_MS, initQuotePanel, renderQuotePanel, quoteCard,
    paintQuoteCard, QuotePanelController, renderMoverList, renderPinCandidates,
  };
}
