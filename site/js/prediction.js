(() => {
  const el = id => document.getElementById(id);
  let catalog = { markets: [], observations: [] }, selected, pending;
  try { pending = JSON.parse(sessionStorage.getItem('prediction.pending') || 'null'); } catch {}
  const savePending = () => { if (pending) sessionStorage.setItem('prediction.pending', JSON.stringify(pending)); else sessionStorage.removeItem('prediction.pending'); };
  const money = n => !Number.isFinite(n) ? 'unavailable' : `$${n.toFixed(4)}`;
  const key = r => [r.provider, r.environment, r.series, r.market].map(encodeURIComponent).join(':');
  async function api(path, options) {
    const response = await fetch(path, { credentials: 'same-origin', ...options });
    const text = await response.text();
    let body; try { body = JSON.parse(text); } catch { body = { error: text }; }
    if (!response.ok) throw Object.assign(Error(body.error || `HTTP ${response.status}`), { terminal: response.status < 500 && body.status !== 'pending' });
    return body;
  }
  function cell(row, text) { const td = document.createElement('td'); td.textContent = text; row.appendChild(td); return td; }
  function render() {
    const table = document.createElement('table');
    const head = document.createElement('tr');
    for (const title of ['Event / deadline', 'YES bid / ask', 'NO bid / ask', 'Size / freshness', '']) { const th = document.createElement('th'); th.textContent = title; head.appendChild(th); }
    table.appendChild(head);
    for (const m of catalog.markets.filter(m => `${m.rules.event} ${m.rules.market} ${m.rules.threshold}`.toLowerCase().includes(el('search').value.toLowerCase()))) {
      const r = document.createElement('tr'), b = m.book, yes = b.yes?.[0], no = b.no?.[0];
      cell(r, `${m.rules.event} · ${m.rules.cutoff}`);
      cell(r, `${money(yes?.[0] / 10000)} / ${money(no ? 1 - no[0] / 10000 : null)}`);
      cell(r, `${money(no?.[0] / 10000)} / ${money(yes ? 1 - yes[0] / 10000 : null)}`);
      const age = Math.floor((Date.now() - Date.parse(b.exchange_ts)) / 1000);
      cell(r, `YES ask ${no?.[1] ?? 0}, NO ask ${yes?.[1] ?? 0} · ${b.synchronized && !b.gap ? `${age}s old` : 'gap / unavailable'} · ${m.status}`);
      const button = document.createElement('button'); button.textContent = 'Rules & ticket'; button.onclick = () => {
        selected = m; el('title').textContent = m.rules.event;
        const terms = m.rules;
        el('rules').textContent = `${terms.text}\n\nVenue: ${terms.provider} / ${terms.environment}\nSeries: ${terms.series} · Market: ${terms.market}\nRule version: ${terms.version} · Published: ${terms.published_at}\nSource: ${terms.source}\nIndex: ${terms.index}\nThreshold: ${terms.inclusive ? 'at or above' : 'strictly above'} $${terms.threshold}\nObservation: ${terms.window_start} to ${terms.window_end}\nTrading cutoff: ${terms.cutoff}\nPayout: ${money(terms.payout_units / 10000)} ${terms.currency}\nFee per contract per fill: ${money(terms.fee_units / 10000)}\nVoid: remaining entry premium refunded; fees retained.`;
        const observation = (catalog.observations || []).filter(o => o.market === key(m.rules)).at(-1);
        el('comparison').textContent = observation?.eligible
          ? `${observation.label}\nEvent interval: ${observation.event_interval.map(n => (n * 100).toFixed(2) + '%').join(' to ')}\nOptions interval: ${observation.option_interval.map(n => (n * 100).toFixed(2) + '%').join(' to ')}\nEvent minus options: ${observation.gap_interval.map(n => (n * 100).toFixed(2)).join(' to ')} percentage points\nTiming difference: ${observation.timing_uncertainty_ms} ms · available size ${observation.size}\n${observation.caveat}`
          : `Context only; no numerical probability gap. ${observation?.reasons?.join('; ') || 'Options comparison unavailable.'}`;
        el('evidence').textContent = JSON.stringify({ book: m.book, observation }, null, 2);
        el('receipt').textContent = ''; el('detail').showModal();
      }; cell(r, '').appendChild(button); table.appendChild(r);
    }
    el('markets').replaceChildren(table);
  }
  async function refresh() {
    try {
      const p = await api('/api/prediction/portfolio');
      el('portfolio').textContent = p.positions.map(p => `${p.sym}: ${p.pos} held · liquidation ${money(p.liquidation_value)}${p.mark_reason ? ` (${p.mark_reason})` : ''} · realized net ${money(p.realized)}`).join('\n') || 'No prediction positions.';
      el('history').textContent = JSON.stringify(p.trades, null, 2);
      catalog = await api('/api/prediction/markets'); render();
      el('status').textContent = `${catalog.markets.length} events · refreshed ${new Date().toLocaleTimeString()}. Quotes are checked again by the server.`;
    } catch (e) { el('status').textContent = e.message; }
  }
  el('ticket').onsubmit = async event => {
    event.preventDefault();
    if (!selected) { await sendPending(); return; }
    const intent = { sym: `event:${key(selected.rules)}:${el('outcome').value}`, instrument_type: 'prediction', side: el('side').value, qty: Number(el('qty').value), order_type: 'market' };
    if (pending && JSON.stringify(pending.intent) !== JSON.stringify(intent)) { el('receipt').textContent = 'Retry the pending order before changing the ticket.'; return; }
    pending ??= { intent, execution_request_id: crypto.randomUUID() };
    savePending();
    await sendPending();
  };
  async function sendPending() {
    if (!pending) return;
    el('submit').disabled = true;
    try {
      const fill = await api('/api/paper-trade', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...pending.intent, execution_request_id: pending.execution_request_id }) });
      el('receipt').textContent = `Filled ${fill.qty} at ${money(fill.price)} · fees ${money((fill.fee_units || 0) / 10000)} · ${fill.execution_request_id}`;
      pending = null; await refresh();
    } catch (e) { el('receipt').textContent = e.message; if (e.terminal) pending = null; }
    finally { savePending(); el('submit').disabled = false; }
  }
  if (pending) {
    const retry = document.createElement('button'); retry.textContent = 'Reconcile pending paper order';
    retry.onclick = async () => { el('detail').showModal(); await sendPending(); };
    el('status').after(retry);
  }
  el('search').oninput = render; el('refresh').onclick = refresh;
  el('dismiss').onclick = () => el('detail').close(); refresh();
})();
