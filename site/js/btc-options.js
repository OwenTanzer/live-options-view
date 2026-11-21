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
  visibleSymbols() {
    const rows = [...document.querySelectorAll('.btc-contract')];
    const visible = rows.filter(el => {
      const rect = el.getBoundingClientRect();
      return rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
    }).map(el => el.dataset.symbol);
    if (ticketEl?.dataset.btc) visible.unshift(ticketEl.dataset.btc);
    return [...new Set(visible)].slice(0, 64).join(',');
  },
  requestVisible() {
    if (!this.active || !this.data || this.access) return;
    clearTimeout(this.scrollTimer);
    this.scrollTimer = setTimeout(() => {
      if (this.active && this.data && this.visibleSymbols() !== this.requestedWindow) this.refresh();
    }, 200);
  },
  async refresh() {
    this.syncSession();
    // Choosing BTC / submitting ordinary login deliberately binds only this
    // paper session when the existing server-verified GitHub owner is present.
    if (this.active && sessionVerified && session && typeof btcOwnerLink !== 'undefined') {
      await btcOwnerLink.github(true);
      this.syncSession();
    }
    const hasHoldings = session && Object.values(computeBook()).some(b => b.instrument_type === 'futures_option' && b.pos > 0);
    if (!sessionVerified || !session || (!this.active && !hasHoldings)) { this.quotes.clear(); this.render(); return; }
    const generation = this.generation, request = ++this.request, username = session.username;
    try {
      const query = new URLSearchParams();
      if (this.active) query.set('expiry', this.expiry || 'front');
      const visible = this.visibleSymbols();
      this.requestedWindow = visible;
      if (visible) query.set('symbols', visible);
      const search = query.toString();
      const response = await fetch('/api/btc-options' + (search ? '?' + search : ''), { credentials: 'same-origin', cache: 'no-store' });
      const data = response.ok ? await response.json() : await response.json().catch(() => ({}));
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
      this.requestVisible();
      this.refreshTicket();
      renderPaper();
    } catch (error) {
      if (generation !== this.generation || request !== this.request) return;
      this.quotes.clear();
      if (this.access) this.data = null;
      else if (this.data) this.data = { ...this.data, quotes: [], subscribed_keys: [], session: 'unavailable' };
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
    if (wasActive !== active && typeof tickerPoller !== 'undefined') tickerPoller?.kick();
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
  reason(symbol, action, book) {
    const side = action === 'buy' ? 'ask' : 'bid', q = this.quotes.get(symbol);
    if (!sessionVerified || !session) return 'Sign in to trade';
    if (this.access) return 'No authorized data source for this session';
    if (pendingExecutions.has(symbol)) return 'Order pending';
    if (action === 'sell' && !((book || computeBook())[symbol]?.pos >= 1)) return 'No position to close';
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
    const dismiss = document.createElement('button');
    dismiss.type = 'button'; dismiss.className = 'btc-dismiss'; dismiss.textContent = 'Close ticket';
    dismiss.onclick = () => closeTicket(); ticketEl.appendChild(dismiss);
    this.refreshTicket();
    if (action) ticketEl.querySelector('.btn-' + action).focus();
  },
  refreshTicket() {
    const sym = ticketEl?.dataset?.btc;
    if (!sym) return;
    // Dismissing the review surface does not cancel an in-flight request.
    const dismiss = ticketEl.querySelector('.btc-dismiss');
    if (dismiss) { dismiss.disabled = false; dismiss.title = pendingExecutions.has(sym) ? 'Close ticket; submitted request continues' : 'Close ticket'; }
    let details = ticketEl.querySelector('.btc-ticket-details');
    if (!details) { details = document.createElement('p'); details.className = 'btc-ticket-details'; ticketEl.appendChild(details); }
    const q = this.quotes.get(sym);
    details.textContent = `1 contract = 5 BTC. Premium ×5 + $2.50 fee per side. Owned: ${computeBook()[sym]?.pos || 0}. Bid size ${q?.bid_size ?? '—'} · ask size ${q?.ask_size ?? '—'}. Bid: ${this.reason(sym, 'sell') || this.clockLabel(sym, 'bid')}. Ask: ${this.reason(sym, 'buy') || this.clockLabel(sym, 'ask')}.`;
    const snapshot = this.data?.display_prices?.[sym];
    if (snapshot) {
      const values = ['bid', 'ask', 'mark'].filter(side => Number.isFinite(snapshot[side]))
        .map(side => side + ' ' + snapshot[side].toLocaleString('en-US', {maximumFractionDigits: 2})).join(' · ');
      if (values) details.textContent += ` Tastytrade snapshot at ${snapshot.as_of}: ${values}.`;
    }
    for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
      const px = this.side(sym, side), button = ticketEl.querySelector('.btn-' + action);
      const reason = this.reason(sym, action);
      button.disabled = !!reason || px == null;
      button.textContent = `${action === 'buy' ? 'Buy' : 'Sell'} 1 @ ${px == null ? 'unavailable' : '$' + px.toFixed(2) + '/BTC ($' + (px * 5).toFixed(2) + ')'}`;
      button.title = reason || this.clockLabel(sym, side);
    }
  },
  centerRows(root, data) {
    const rows = [...root.querySelectorAll('.btc-strike-row')];
    const nearest = rows.reduce((a, b) => Math.abs(a.dataset.strike - data.center) <= Math.abs(b.dataset.strike - data.center) ? a : b);
    nearest.scrollIntoView({ block: 'center' });
    this.pendingCenterY = data.center_from_future === false ? window.scrollY : null;
  },
  updateCoverage(listed) {
    const label = document.querySelector('.btc-coverage');
    if (!label) return;
    const status = document.querySelector('.btc-status p');
    status.textContent = this.error || `${this.data.session} · Paper · USD/BTC · Ask buys / bid closes`;
    const subscribed = new Set(this.data.subscribed_keys);
    const count = listed.filter(c => subscribed.has(c.contract_key)).length;
    const fresh = listed.filter(c => ['ask', 'bid'].some(side => this.side(c.contract_key, side) != null)).length;
    label.textContent = ` ${listed.length} listed · ${count} subscribed · ${fresh} fresh`;
    if (Object.keys(this.data.display_prices || {}).length) label.textContent += ' · * snapshot';
  },
  updateActions() {
    const book = computeBook();
    for (const row of document.querySelectorAll('.btc-contract')) {
      const sym = row.dataset.symbol;
      const snapshot = this.data?.display_prices?.[sym];
      const quote = this.quotes.get(sym);
      const mark = row.querySelector('.btc-reference-mark');
      const hasBook = ['bid', 'ask'].some(side => (Number.isFinite(quote?.[side]) && quote[side] > 0) || Number.isFinite(snapshot?.[side]));
      mark.hidden = hasBook || !Number.isFinite(snapshot?.mark);
      if (!mark.hidden) {
        mark.textContent = 'Mark ' + snapshot.mark.toLocaleString('en-US', {maximumFractionDigits: 2}) + '*';
        mark.title = 'Tastytrade snapshot ' + snapshot.as_of;
      }
      for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
        const price = this.side(sym, side), reason = this.reason(sym, action, book);
        const button = row.querySelector('.btc-' + action);
        button.disabled = !!reason || price == null;
        const q = quote;
        const streamed = Number.isFinite(q?.[side]) && q[side] > 0;
        const useSnapshot = !streamed && Number.isFinite(snapshot?.[side]);
        const raw = streamed ? q[side] : snapshot?.[side];
        button.textContent = Number.isFinite(raw) ? raw.toLocaleString('en-US', { maximumFractionDigits: 2 }) + (useSnapshot ? '*' : '') : '—';
        button.setAttribute('aria-label', `${action === 'buy' ? 'Buy at ask' : 'Sell / close at bid'} ${q?.contract.type || ''} ${q?.contract.strike || ''}: ${button.textContent}${reason ? ' · ' + reason : ''}`);
        button.title = useSnapshot ? 'Tastytrade snapshot ' + snapshot.as_of : reason || this.clockLabel(sym, side);
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
    const listed = (data?.contracts || []).filter(c => !data.expiry || c.exp === data.expiry);
    const book = computeBook();
    const layout = JSON.stringify([this.owner, data?.expiry, data?.expiries, listed.map(c => [c.contract_key, book[c.contract_key]?.pos])]);
    const sameExpiry = data?.expiry && root.dataset.expiry === data.expiry && root.querySelector('.btc-series');
    if (data && root.dataset.layout === layout && root.querySelector('.btc-series')) {
      this.updateActions();
      this.updateCoverage(listed);
      if (this.pendingCenterY != null && window.scrollY !== this.pendingCenterY) this.pendingCenterY = null;
      if (this.pendingCenterY != null && data.center_from_future) this.centerRows(root, data);
      return;
    }
    const anchor = sameExpiry && [...root.querySelectorAll('.btc-strike-row')].find(row => {
      const rect = row.getBoundingClientRect();
      return rect.bottom > 0 && rect.top < window.innerHeight;
    });
    const anchorTop = anchor && anchor.getBoundingClientRect().top;
    root.dataset.expiry = data?.expiry || '';
    root.dataset.layout = layout;
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
          const github = element('a', 'btc-github-login', 'Sign in with GitHub');
          github.href = '/credit-tape/auth/login?return=btc'; status.appendChild(github);
          const check = element('button', 'btc-github-check', 'Use verified GitHub for this paper session');
          check.type = 'button'; check.disabled = btcOwnerLink.busy;
          check.onclick = async () => { await btcOwnerLink.github(); await this.refresh(); }; status.appendChild(check);
          status.appendChild(element('p', '', `GitHub verification applies only to your current paper account “${session.username}”. Your balances and sessions stay separate.`));
          status.appendChild(element('p', '', `If you own this subscription, link your existing enrolled owner passkey to paper account “${session.username}” for this session only. This does not sign you into the journal.`));
          const link = element('button', 'btc-owner-link', 'Link this session with owner passkey');
          link.type = 'button'; link.disabled = btcOwnerLink.busy; link.onclick = () => btcOwnerLink.link(); status.appendChild(link);
          status.appendChild(element('p', 'btc-link-status', btcOwnerLink.message));
        } else status.appendChild(element('p', '', 'Owner linking is not available for this source. No authorized cross-user source is configured.'));
      } else {
        const login = element('button', 'btc-login', 'Sign in');
        login.type = 'button'; login.onclick = () => openPaperLogin(); status.appendChild(login);
        const github = element('a', 'btc-github-login', 'Sign in with GitHub');
        github.href = '/credit-tape/auth/login?return=btc'; status.appendChild(github);
        status.appendChild(element('p', '', 'Then sign in to your normal paper account. An already verified owner needs no extra passkey step.'));
      }
      return;
    }
    if (!data) {
      status.appendChild(element('p', '', this.error || 'Loading standard monthly contracts…'));
      if (this.error) { const retry = element('button', '', 'Retry quotes'); retry.onclick = () => this.refresh(); status.appendChild(retry); }
      return;
    }
    status.appendChild(element('p', '', `${data.session} · Paper · USD/BTC · Ask buys / bid closes`));
    if (typeof btcOwnerLink !== 'undefined') {
      const unlink = element('button', 'btc-owner-unlink', 'Unlink data');
      unlink.type = 'button'; unlink.disabled = btcOwnerLink.busy; unlink.onclick = () => btcOwnerLink.unlink(); status.appendChild(unlink);
      if (btcOwnerLink.message) status.appendChild(element('p', 'btc-link-status', btcOwnerLink.message));
    }
    if (data.expiries?.length) {
      const picker = element('select', '', '');
      picker.setAttribute('aria-label', 'Standard monthly expiry');
      for (const expiry of data.expiries) picker.add(new Option(expiry, expiry));
      picker.value = data.expiry;
      picker.onchange = () => { this.expiry = picker.value; this.refresh(); };
      status.appendChild(picker);
      status.appendChild(element('span', 'btc-coverage', ''));
    }
    const groups = new Map();
    for (const c of [...listed].sort((a, b) => a.exp.localeCompare(b.exp) || a.future_month.localeCompare(b.future_month) || a.future_symbol.localeCompare(b.future_symbol) || a.strike - b.strike || a.type.localeCompare(b.type))) {
      const key = JSON.stringify([c.exp, c.future_month, c.future_symbol]);
      if (!groups.has(key)) groups.set(key, { contract: c, strikes: new Map() });
      const strikes = groups.get(key).strikes;
      if (!strikes.has(c.strike)) strikes.set(c.strike, {});
      strikes.get(c.strike)[c.type] = c;
    }
    const leg = (c, type) => {
      if (!c) { const empty = element('div', 'btc-empty', '—'); empty.setAttribute('aria-label', `No ${type} contract at this strike`); return empty; }
      const q = this.quotes.get(c.contract_key), row = element('article', 'btc-contract', '');
      row.dataset.symbol = c.contract_key;
      row.dataset.type = c.type;
      const held = book[c.contract_key]?.pos || 0;
      const position = element('span', 'btc-position', held > 0 ? `Owned: ${held}` : 'No open position');
      if (!held) position.className += ' btc-sr';
      row.appendChild(position);
      const sides = element('div', 'btc-sides', '');
      for (const [action, side] of [['sell', 'bid'], ['buy', 'ask']]) {
        const cell = element('div', 'btc-side', ''), button = element('button', 'btc-' + action, '');
        button.dataset.btcAction = action; button.dataset.symbol = c.contract_key;
        button.onclick = e => { if (!this.reason(c.contract_key, action) && this.side(c.contract_key, side) != null) this.openContract(e, c, action); };
        cell.appendChild(button);
        cell.appendChild(element('span', 'btc-sr btc-freshness btc-' + side + '-time', ''));
        const reason = element('span', 'btc-sr btc-reason btc-' + action + '-reason', '');
        reason.id = `btc-${action}-${c.contract_key}`;
        button.setAttribute('aria-describedby', reason.id);
        cell.appendChild(reason);
        sides.appendChild(cell);
      }
      row.appendChild(sides);
      row.appendChild(element('span', 'btc-reference-mark', ''));
      const ticket = element('button', 'btc-ticket', '⋯');
      ticket.setAttribute('aria-label', `Open ${c.type} ${c.strike} ticket and quote details`);
      ticket.dataset.btcAction = 'ticket'; ticket.dataset.symbol = c.contract_key;
      ticket.onclick = e => this.openContract(e, c);
      row.appendChild(ticket); return row;
    };
    for (const { contract: c, strikes } of groups.values()) {
      const group = element('section', 'btc-series', '');
      group.appendChild(element('h3', '', `${c.exp} · ${c.future_symbol} · ${c.future_month}`));
      const heading = element('div', 'btc-chain-head', '');
      for (const text of ['Calls', 'Strike', 'Puts']) heading.appendChild(element('span', '', text));
      group.appendChild(heading);
      const columns = element('div', 'btc-chain-columns', '');
      for (const text of ['Bid', 'Ask', '', 'Bid', 'Ask']) columns.appendChild(element('span', '', text));
      group.appendChild(columns);
      for (const [strike, pair] of strikes) {
        const row = element('div', 'btc-strike-row', ''); row.dataset.strike = strike;
        row.appendChild(leg(pair.call, 'call'));
        row.appendChild(element('strong', 'btc-strike', strike.toLocaleString('en-US')));
        row.appendChild(leg(pair.put, 'put')); group.appendChild(row);
      }
      root.appendChild(group);
    }
    if (!data.contracts?.length) root.append('No verified standard monthly contracts available.');
    this.updateActions();
    this.updateCoverage(listed);
    if (!sameExpiry) {
      this.pendingCenterY = null;
      if (data.center && listed.length) this.centerRows(root, data);
    } else if (anchor) {
      const replacement = root.querySelector(`.btc-strike-row[data-strike="${anchor.dataset.strike}"]`);
      if (replacement) window.scrollBy(0, replacement.getBoundingClientRect().top - anchorTop);
    }
    if (focused) [...root.querySelectorAll('[data-btc-action]')].find(el => el.dataset.symbol === focused.symbol && el.dataset.btcAction === focused.btcAction)?.focus({ preventScroll: true });
  },
};
document.addEventListener?.('scroll', () => btcOptions.requestVisible(), { capture: true, passive: true });
function refreshVisibleBtcQuotes() {
  if (!document.hidden) btcOptions.refresh();
}
document.addEventListener?.('visibilitychange', refreshVisibleBtcQuotes);
setInterval(refreshVisibleBtcQuotes, 5000);
setInterval(() => btcOptions.updateActions(), 1000);
