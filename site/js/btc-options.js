// Authenticated, memory-only product view. Never ingested as an equity snapshot.
const btcOptions = {
  active: false, generation: 0, request: 0, quotes: new Map(), data: null,
  owner: null, error: null, access: null, ownerLinkAvailable: false,
  syncSession() {
    const username = sessionVerified ? session?.username || null : null;
    if (username === this.owner) return;
    this.owner = username; this.generation++; this.request++;
    this.quotes.clear(); this.data = null; this.error = null; this.access = null;
    this.ownerLinkAvailable = false;
    if (typeof btcOwnerLink !== 'undefined') btcOwnerLink.message = '';
    if (ticketEl?.dataset?.btc) closeTicket();
    document.getElementById('btc-chain').replaceChildren();
    this.render();
  },
  async refresh() {
    this.syncSession();
    const hasHoldings = session && Object.values(computeBook()).some(b => b.instrument_type === 'futures_option' && b.pos > 0);
    if (!sessionVerified || !session || (!this.active && !hasHoldings)) { this.quotes.clear(); this.render(); return; }
    const generation = this.generation, request = ++this.request, username = session.username;
    try {
      const response = await fetch('/api/btc-options', { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (generation !== this.generation || request !== this.request || session?.username !== username) return;
      this.quotes.clear();
      if (!response.ok) {
        this.ownerLinkAvailable = data.owner_link_available === true;
        this.access = response.status === 401 ? 'signin' : response.status === 403 ? 'unauthorized' : null;
        if (this.access) {
          session.trades = (session.trades || []).filter(t => t.instrument_type !== 'futures_option');
          if (ticketEl?.dataset?.btc) closeTicket();
        }
        throw new Error(data.error || 'BTC source unavailable');
      }
      this.error = null; this.access = null;
      this.data = data;
      for (const q of data.quotes || []) this.quotes.set(q.symbol, q);
      this.render();
      this.refreshTicket();
      renderPaper();
    } catch (error) {
      if (generation !== this.generation || request !== this.request) return;
      this.quotes.clear(); this.data = null;
      this.error = error.message;
      this.render();
      this.refreshTicket(); renderPaper();
    }
  },
  select(active) {
    const wasActive = this.active;
    this.active = active; this.generation++; this.quotes.clear(); this.data = null;
    closeTicket();
    this.error = null; this.access = null;
    document.getElementById('btc-chain').replaceChildren();
    document.body.dataset.chain = active ? 'BTC' : '';
    document.getElementById('btc-panel').hidden = !active;
    const hint = document.getElementById('paper-hint');
    if (active && !wasActive) {
      this.equityHint = hint.textContent;
      hint.textContent = 'Open a Bitcoin contract ticket to buy or close one contract. Premium ×5, plus the displayed fee.';
    } else if (!active && wasActive) hint.textContent = this.equityHint || '';
    if (active) {
      this.saveSelection('BTC');
      showTab('live');
      clearChain('');
      document.getElementById('no-data').classList.add('hidden');
      this.header();
      if (typeof qqqIv !== 'undefined') qqqIv.select('BTC');
      this.render();
      this.refresh();
    }
  },
  saveSelection(symbol) {
    try { localStorage.setItem(UNDERLYING_KEY, symbol); } catch {}
    const url = new URL(location.href);
    url.searchParams.set('chain', symbol);
    history.replaceState(null, '', url);
  },
  header() {
    document.getElementById('underlying-select').value = 'BTC';
    document.getElementById('title').textContent = 'Bitcoin futures options';
    document.title = 'Bitcoin futures options — moopertonic';
  },
  clock(q, side, now = Date.now()) {
    const receipt = Date.parse(q[side + '_received_ts']);
    const fresh = t => Number.isFinite(t) && now - t >= -1000 && now - t <= 15000;
    if (!fresh(receipt)) return null;
    const source = q[side + '_ts'];
    if (source != null) return fresh(Date.parse(source)) ? { at: source, basis: 'provider' } : null;
    const c = q[side + '_change'];
    if (!c || c.schema !== 'observed_side_change.v1' || !/^[0-9a-f]{32}$/.test(q.stream_epoch || '') ||
        c.stream_epoch !== q.stream_epoch) return null;
    const ts = Date.parse(c.at), baseline = Date.parse(c.baseline_at), prior = Date.parse(c.previous_received_at);
    if (!fresh(ts) || !Number.isFinite(baseline) || !Number.isFinite(prior) ||
        !(baseline <= prior && prior < ts && ts <= receipt)) return null;
    if (![c.baseline_sequence, c.previous_sequence, c.sequence].every(Number.isSafeInteger) ||
        !(0 < c.baseline_sequence && c.baseline_sequence <= c.previous_sequence && c.previous_sequence < c.sequence)) return null;
    if (![c.previous_price, c.previous_size, c.price, c.size].every(v => Number.isFinite(v) && v >= 0) ||
        c.price !== q[side] || c.size !== q[side + '_size'] ||
        (c.previous_price === c.price && c.previous_size === c.size)) return null;
    return { at: c.at, basis: 'observed_side_change' };
  },
  clockLabel(symbol, side) {
    const q = this.quotes.get(symbol), clock = q && this.clock(q, side);
    return clock ? `${clock.basis === 'provider' ? 'provider side time' : 'observed side change'} · ${Math.max(0, Math.floor((Date.now() - Date.parse(clock.at)) / 1000))}s`
      : 'waiting for a fresh timed side or observed side change';
  },
  side(symbol, side) {
    const q = this.quotes.get(symbol), now = Date.now();
    if (!q?.available || this.data?.session !== 'open') return null;
    if (!this.clock(q, side, now)) return null;
    if (!(Number.isFinite(q[side]) && q[side] > 0 && Number.isFinite(q[side + '_size']) && q[side + '_size'] >= 1) || (q.bid != null && q.ask != null && q.bid > q.ask)) return null;
    if (now >= Date.parse(q.contract.stops_trading_at)) return null;
    return q[side];
  },
  mark(symbol) { return this.side(symbol, 'bid'); },
  reason(symbol, action) {
    const side = action === 'buy' ? 'ask' : 'bid', q = this.quotes.get(symbol);
    if (!sessionVerified || !session) return 'Sign in to trade';
    if (this.access) return 'No authorized data source for this session';
    if (pendingExecutions.has(symbol)) return 'Order pending';
    if (action === 'sell' && !(computeBook()[symbol]?.pos >= 1)) return 'No position to close';
    if (action === 'buy' && !this.data?.entries_enabled) return 'New entries disabled';
    if (action === 'buy' && !this.data?.entry_keys?.includes(symbol)) return 'Not in active monthly series';
    if (!q || !q.available) return 'Quote unavailable';
    if (this.data?.session !== 'open') return 'Session closed / maintenance';
    if (Date.now() >= Date.parse(q.contract.stops_trading_at)) return 'Contract expired';
    if (!(q[side] > 0 && q[side + '_size'] >= 1)) return `No executable ${side} price or size`;
    if (q.bid != null && q.ask != null && q.bid > q.ask) return 'Crossed quote';
    if (!this.clock(q, side)) return q[side + '_ts'] || q[side + '_change'] ? `${side} quote stale or invalid` : `Waiting for a fresh ${side} change`;
    return '';
  },
  openContract(e, c, action) {
    openTicket(e, { OptionSymbol: c.contract_key, Strike: c.strike, Type: c.type,
      Expiration: c.exp, instrument_type: 'futures_option', contract: c });
    this.refreshTicket();
    if (action) ticketEl.querySelector('.btn-' + action).focus();
  },
  refreshTicket() {
    const sym = ticketEl?.dataset?.btc;
    if (!sym) return;
    for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
      const px = this.side(sym, side), button = ticketEl.querySelector('.btn-' + action);
      const reason = this.reason(sym, action);
      button.disabled = !!reason || px == null;
      button.textContent = `${action === 'buy' ? 'Buy' : 'Sell'} 1 @ ${px == null ? 'unavailable' : '$' + px.toFixed(2) + '/BTC ($' + (px * 5).toFixed(2) + ')'}`;
      button.title = reason || this.clockLabel(sym, side);
    }
  },
  updateActions() {
    for (const row of document.querySelectorAll('.btc-contract')) {
      const sym = row.dataset.symbol;
      for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
        const price = this.side(sym, side), reason = this.reason(sym, action);
        const button = row.querySelector('.btc-' + action);
        button.disabled = !!reason || price == null;
        button.textContent = `${action === 'buy' ? 'Buy at ask' : 'Sell / close at bid'}${price == null ? '' : ' $' + price.toFixed(2)}`;
        row.querySelector('.btc-' + action + '-reason').textContent = reason || 'Opens ticket · 1 contract';
        row.querySelector('.btc-' + side + '-time').textContent = this.clockLabel(sym, side);
      }
    }
    this.refreshTicket();
  },
  render() {
    if (!this.active) return;
    this.header();
    const data = this.data, root = document.getElementById('btc-chain');
    const focus = document.activeElement?.dataset;
    const focused = focus?.btcAction ? { ...focus } : null;
    root.replaceChildren();
    const element = (tag, cls, text) => { const el = document.createElement(tag); el.className = cls; el.textContent = text; return el; };
    const status = element('div', 'btc-status', '');
    status.setAttribute('role', 'status');
    root.appendChild(status);
    if (!sessionVerified || !session || this.access) {
      const signedIn = sessionVerified && session && this.access !== 'signin';
      status.appendChild(element('p', '', signedIn ? 'This paper session has no authorized Bitcoin data source.' : 'Sign in to your normal paper account to continue.'));
      if (signedIn) {
        status.appendChild(element('p', '', 'This subscriber feed cannot be shared with other users. A paper username or journal login alone does not grant access.'));
        if (this.ownerLinkAvailable) {
          status.appendChild(element('p', '', `If you own this subscription, link your existing enrolled owner passkey to paper account “${session.username}” for this session only. This does not sign you into the journal.`));
          const link = element('button', 'btc-owner-link', 'Link this session with owner passkey');
          link.type = 'button'; link.disabled = btcOwnerLink.busy; link.onclick = () => btcOwnerLink.link(); status.appendChild(link);
          status.appendChild(element('p', 'btc-link-status', btcOwnerLink.message));
        } else status.appendChild(element('p', '', 'Owner linking is not available for this source. No authorized cross-user source is configured.'));
      } else {
        const login = element('button', 'btc-login', 'Sign in');
        login.type = 'button'; login.onclick = () => openPaperLogin(); status.appendChild(login);
      }
      return;
    }
    if (!data) {
      status.appendChild(element('p', '', this.error || 'Loading standard monthly contracts…'));
      if (this.error) { const retry = element('button', '', 'Retry quotes'); retry.onclick = () => this.refresh(); status.appendChild(retry); }
      return;
    }
    status.appendChild(element('p', '', `${data.session} · Simulated trading · ${data.fee_model || '$2.50 per contract per side'}`));
    status.appendChild(element('p', '', 'Premiums are USD per bitcoin. One contract = 5 BTC. Buy at ask; close owned contracts at bid.'));
    if (typeof btcOwnerLink !== 'undefined') {
      const unlink = element('button', 'btc-owner-unlink', 'Unlink Bitcoin data from this session');
      unlink.type = 'button'; unlink.disabled = btcOwnerLink.busy; unlink.onclick = () => btcOwnerLink.unlink(); status.appendChild(unlink);
      if (btcOwnerLink.message) status.appendChild(element('p', 'btc-link-status', btcOwnerLink.message));
    }
    for (const c of (data.contracts || []).sort((a, b) => a.strike - b.strike || a.type.localeCompare(b.type))) {
      const q = this.quotes.get(c.contract_key), row = element('article', 'btc-contract', '');
      row.dataset.symbol = c.contract_key;
      row.appendChild(element('h3', '', `$${c.strike.toLocaleString()} ${c.type.toUpperCase()}`));
      row.appendChild(element('p', '', `${c.future_symbol} · expires ${c.exp} · 5 BTC / contract`));
      const held = computeBook()[c.contract_key]?.pos || 0;
      row.appendChild(element('p', 'btc-position', held > 0 ? `Owned: ${held} contract${held === 1 ? '' : 's'}` : 'No open position'));
      const sides = element('div', 'btc-sides', '');
      for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
        const cell = element('div', 'btc-side', ''), button = element('button', 'btc-' + action, '');
        button.dataset.btcAction = action; button.dataset.symbol = c.contract_key;
        button.onclick = e => this.openContract(e, c, action);
        cell.appendChild(button);
        cell.appendChild(element('p', '', `${side} size ${q?.[side + '_size'] ?? '—'}${this.side(c.contract_key, side) == null ? '' : ' · $' + (q[side] * 5).toFixed(2) + ' / contract'}`));
        cell.appendChild(element('p', 'btc-freshness btc-' + side + '-time', ''));
        const reason = element('p', 'btc-reason btc-' + action + '-reason', '');
        reason.id = `btc-${action}-${c.contract_key}`;
        button.setAttribute('aria-describedby', reason.id);
        cell.appendChild(reason);
        sides.appendChild(cell);
      }
      row.appendChild(sides);
      const ticket = element('button', 'btc-ticket', 'Open trade ticket');
      ticket.dataset.btcAction = 'ticket'; ticket.dataset.symbol = c.contract_key;
      ticket.onclick = e => this.openContract(e, c);
      row.appendChild(ticket); root.appendChild(row);
    }
    if (!data.contracts?.length) root.append('No verified standard monthly contracts available.');
    this.updateActions();
    if (focused) [...root.querySelectorAll('[data-btc-action]')].find(el => el.dataset.symbol === focused.symbol && el.dataset.btcAction === focused.btcAction)?.focus({ preventScroll: true });
  },
};
setInterval(() => btcOptions.refresh(), 5000);
setInterval(() => btcOptions.updateActions(), 1000);
