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
  side(symbol, side) {
    const q = this.quotes.get(symbol), now = Date.now();
    if (!q?.available || this.data?.session !== 'open') return null;
    const stamps = [q[side + '_ts'], q[side + '_received_ts']].map(Date.parse);
    if (stamps.some(t => !Number.isFinite(t) || now - t > 15000 || now - t < -1000)) return null;
    if (!(q[side] > 0 && q[side + '_size'] >= 1) || (q.bid != null && q.ask != null && q.bid > q.ask)) return null;
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
    }
  },
  render() {
    if (!this.active || !this.data) return;
    const data = this.data, root = document.getElementById('btc-chain');
    root.replaceChildren();
    const status = document.createElement('p');
    status.textContent = `${data.session} · ${data.source_status} · Simulated fills. ${data.fee_model}. ${data.entries_enabled ? '' : 'New entries disabled.'}`;
    root.appendChild(status);
    for (const c of (data.contracts || []).sort((a, b) => a.strike - b.strike || a.type.localeCompare(b.type))) {
      const q = this.quotes.get(c.contract_key), button = document.createElement('button');
      button.className = 'btc-contract';
      const label = side => {
        const price = this.side(c.contract_key, side);
        const stamp = Date.parse(q?.[side + '_ts']);
        return `${side} ${price == null ? 'unavailable' : '$' + price.toFixed(2) + '/BTC · $' + (price * 5).toFixed(2) + '/contract'} · size ${q?.[side + '_size'] ?? '—'} · ${Number.isFinite(stamp) ? Math.max(0, Math.floor((Date.now() - stamp) / 1000)) + 's' : 'untimed'}`;
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
