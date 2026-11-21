const $ = id => document.getElementById(id);
const money = n => '$' + Number(n).toFixed(4);
let catalog = [], selected = null, current = null, pendingIntent = null, entries = false;
async function api(path, body) {
  const r = await fetch('/api/kalshi/' + path, { credentials: 'same-origin', cache: 'no-store',
    ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) });
  const d = await r.json();
  if (!r.ok) { if (r.status === 401 || r.status === 403) clearPrivate(); throw Error(d.error || 'Request failed'); }
  return d;
}
function clearPrivate() {
  catalog = []; selected = current = pendingIntent = null; $('workspace').hidden = true;
  for (const id of ['markets','positions','fills','replay','archive-list','quotes','rules','receipt','cash','health']) $(id).replaceChildren();
  $('submit').disabled = true;
}
async function run(action) { try { await action(); } catch(e) { $('status').textContent = e.message; } }
function card(text, description, action) {
  const c = document.createElement('article'); c.className = 'card';
  const h = document.createElement('strong'); h.textContent = text; c.append(h);
  const p = document.createElement('p'); p.textContent = description; c.append(p);
  if (action) { const b = document.createElement('button'); b.textContent = 'Inspect terms and fresh book'; b.onclick = () => run(action); c.append(b); }
  return c;
}
function list() {
  const term = $('search').value.toLowerCase(); $('markets').replaceChildren();
  for (const m of catalog.filter(x => JSON.stringify(x).toLowerCase().includes(term))) {
    $('markets').append(card(m.title + ' · ' + m.outcome,
      `${m.category} | ${m.series} · closes ${new Date(m.cutoff).toLocaleString()} · 24h volume ${m.volume_24h ?? 'unknown'} · open interest ${m.open_interest ?? 'unknown'}`,
      async () => { selected = m.ticker; await refresh(); $('detail').scrollIntoView({ behavior: 'smooth', block: 'start' }); }));
  }
}
function fresh() {
  return current?.quotes && Date.now() - Date.parse(current.provider_response_ts) <= 10000 &&
    Date.now() >= Date.parse(current.received_at) && Date.now() < Date.parse(current.rules.cutoff);
}
function paintQuote() {
  const ok = fresh();
  $('submit').disabled = !ok || ($('side').value === 'buy' && !entries);
  if (!current) return;
  $('quotes').textContent = ok ? ['YES','NO'].map(k => {
    const q = current.quotes[k]; return `${k}: bid ${money(q.bid)} × ${q.bid_size}, ask ${money(q.ask)} × ${q.ask_size}`;
  }).join(' / ') : current.reason || 'Book expired. Prices hidden; get a fresh book.';
}
async function refresh() {
  current = null; $('quotes').textContent = 'Fetching a fresh executable book…'; $('submit').disabled = true;
  const d = await api('market', { market: selected }); current = d;
  $('detail').hidden = false; $('title').textContent = d.rules.title;
  $('clock').textContent = `${d.clock_basis}. Provider ${d.provider_response_ts}; received ${d.received_at}.`;
  $('rules').textContent = JSON.stringify(d.rules, null, 2); $('comparison').textContent = d.comparison; paintQuote();
}
async function portfolio() {
  const d = await api('portfolio'); $('cash').textContent = `Paper cash ${money(d.balance_cash)} · separate from Options View and Kalshi balances`;
  $('positions').replaceChildren();
  for (const p of Object.values(d.positions)) if (p.pos) $('positions').append(card(p.prediction.rules.title + ' · ' + p.prediction.outcome,
    `${p.pos} held · cost ${money(p.avg)} each · no stale liquidation mark`, async () => { selected = p.prediction.rules.market; await refresh(); }));
  $('fills').textContent = JSON.stringify(d.trades, null, 2);
  $('health').textContent = d.health ? JSON.stringify(d.health) : 'Background observations begin after a market is selected.';
}
async function check() {
  clearPrivate();
  const s = await api('status'); $('status').textContent = s.status; entries = s.entries;
  if (!s.enabled) return;
  $('workspace').hidden = false; const d = await api('markets'); catalog = d.markets; $('scope').textContent = d.scope; list(); await portfolio();
}
$('check').onclick = () => run(check);
$('discover').onclick = () => run(async () => { const d = await api('discover', {}); catalog = d.markets; $('scope').textContent = d.scope; list(); });
$('search').oninput = list; $('refresh').onclick = () => run(refresh); $('side').onchange = paintQuote;
$('ticket').onsubmit = e => { e.preventDefault(); run(async () => {
  $('submit').disabled = true;
  const intent = { market: selected, outcome: $('outcome').value, side: $('side').value, qty: Number($('qty').value) };
  if (!pendingIntent || JSON.stringify(pendingIntent.intent) !== JSON.stringify(intent)) pendingIntent = { intent, id: crypto.randomUUID() };
  const d = await api('paper', { ...intent, execution_request_id: pendingIntent.id }); pendingIntent = null;
  $('receipt').textContent = `Paper fill: ${d.trade.qty} ${d.trade.prediction.outcome} at ${money(d.trade.price)}, fees ${money(d.trade.fee_units / 10000)}. Receipt ${d.trade.execution_id}.`;
  current = null; $('quotes').textContent = 'Order recorded. Get a fresh book for the next order.'; await portfolio();
}); };
$('archives').onclick = () => run(async () => {
  const d = await api('archive'); $('archive-list').replaceChildren();
  for (const a of d.archives) { const b = document.createElement('button'); b.textContent = `${a.received_at} · ${a.market}`;
    b.onclick = () => run(async () => { const r = await api('archive?id=' + encodeURIComponent(a.id));
      $('replay').textContent = 'ARCHIVED OBSERVATION — not executable\n' + JSON.stringify(r.evidence, null, 2); }); $('archive-list').append(b); }
});
$('logout').onclick = () => run(async () => {
  clearPrivate(); await fetch('/credit-tape/auth/logout', { method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'manual' });
  const check = await fetch('/api/kalshi/status', { credentials: 'same-origin', cache: 'no-store' });
  if (check.status !== 401) throw Error('Sign-out was not confirmed. Close this page and sign out from Credit Tape.');
  $('status').textContent = 'Signed out. Private records cleared from this page.';
});
setInterval(paintQuote, 500);
document.addEventListener('visibilitychange', () => { if (document.hidden) { current = null; $('quotes').textContent = 'Refresh the book after returning to this page.'; $('submit').disabled = true; } });
window.addEventListener('pageshow', e => { if (e.persisted) run(check); });
run(check);
