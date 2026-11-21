// Authenticated, memory-only product view. Never ingested as an equity snapshot.
const btcOptions = {
  active: false, generation: 0, request: 0, quotes: new Map(), data: null,
  owner: null,
  syncSession() {
    const username = session?.username || null;
    if (username === this.owner) return;
    this.owner = username; this.generation++; this.request++;
    this.quotes.clear(); this.data = null;
    if (ticketEl?.dataset?.btc) closeTicket();
    document.getElementById('btc-chain').textContent = username ? 'Loading standard monthly contracts…' : 'Sign in to view Bitcoin futures options.';
  },
  async refresh() {
    this.syncSession();
    const hasHoldings = session && Object.values(computeBook()).some(b => b.instrument_type === 'futures_option' && b.pos > 0);
    if (!session || (!this.active && !hasHoldings)) { this.quotes.clear(); return; }
    const generation = this.generation, request = ++this.request, username = session.username;
    try {
      const response = await fetch('/api/btc-options', { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (generation !== this.generation || request !== this.request || session?.username !== username) return;
      this.quotes.clear();
      if (!response.ok) throw new Error(data.error || 'BTC source unavailable');
      this.data = data;
      for (const q of data.quotes || []) this.quotes.set(q.symbol, q);
      this.render();
      this.refreshTicket();
      renderPaper();
    } catch (error) {
      if (generation !== this.generation || request !== this.request) return;
      this.quotes.clear(); this.data = null;
      document.getElementById('btc-chain').textContent = error.message;
      this.refreshTicket(); renderPaper();
    }
  },
  select(active) {
    this.active = active; this.generation++; this.quotes.clear(); this.data = null;
    closeTicket();
    document.getElementById('btc-panel').hidden = !active;
    document.getElementById('btc-select').setAttribute('aria-pressed', String(active));
    if (active) {
      clearChain('');
      document.getElementById('no-data').classList.add('hidden');
      document.getElementById('title').textContent = 'Bitcoin futures options';
      document.getElementById('btc-chain').textContent = session ? 'Loading standard monthly contracts…' : 'Sign in to view Bitcoin futures options.';
      this.refresh();
    } else renderSelectedChain();
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
  refreshTicket() {
    const sym = ticketEl?.dataset?.btc;
    if (!sym) return;
    const held = computeBook()[sym]?.pos || 0;
    for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
      const px = this.side(sym, side), button = ticketEl.querySelector('.btn-' + action);
      button.disabled = pendingExecutions.has(sym) || px == null || (action === 'buy' ? !this.data?.entries_enabled : held < 1);
      button.textContent = `${action === 'buy' ? 'Buy' : 'Sell'} 1 @ ${px == null ? 'unavailable' : '$' + px.toFixed(2) + '/BTC ($' + (px * 5).toFixed(2) + ')'}`;
      button.title = this.clockLabel(sym, side);
    }
  },
  render() {
    if (!this.active || !this.data) return;
    const data = this.data, root = document.getElementById('btc-chain');
    root.replaceChildren();
    const status = document.createElement('p');
    status.textContent = `${data.session} · ${data.source_status} · Simulated fills. ${data.fee_model}. When provider side time is missing, execution requires an observed price or size change after the initial snapshot. Observation time is not exchange time. ${data.entries_enabled ? '' : 'New entries disabled.'}`;
    root.appendChild(status);
    for (const c of (data.contracts || []).sort((a, b) => a.strike - b.strike || a.type.localeCompare(b.type))) {
      const q = this.quotes.get(c.contract_key), button = document.createElement('button');
      button.className = 'btc-contract';
      const label = side => {
        const price = this.side(c.contract_key, side);
        return `${side} ${price == null ? 'unavailable' : '$' + price.toFixed(2) + '/BTC · $' + (price * 5).toFixed(2) + '/contract'} · size ${q?.[side + '_size'] ?? '—'} · ${this.clockLabel(c.contract_key, side)}`;
      };
      button.textContent = `${c.future_symbol} (${c.future_month}) · expires ${c.exp} · ${c.strike} ${c.type}\n${label('bid')}\n${label('ask')}`;
      button.onclick = e => { openTicket(e, { OptionSymbol: c.contract_key, Strike: c.strike,
        Type: c.type, Expiration: c.exp, instrument_type: 'futures_option', contract: c }); this.refreshTicket(); };
      root.appendChild(button);
    }
    if (!data.contracts?.length) root.append('No verified standard monthly contracts available.');
  },
};
document.getElementById('btc-select').onclick = () => btcOptions.select(!btcOptions.active);
setInterval(() => btcOptions.refresh(), 5000);
