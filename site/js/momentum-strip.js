// Render only the windows supplied by the selected snapshot. The clock's own
// cursor is passed for replay; this renderer performs no fetch or computation.
function paintMomentumStrip(el, market, {at = Date.now(), compact = false, retained = false} = {}) {
  if (!el) return;
  const windows = market?.momentum_windows;
  if (!windows) {
    const legacy = formatMomentum(market, at);
    el.textContent = legacy.text;
    el.dataset.state = legacy.state;
    el.removeAttribute('title');
    return;
  }
  const observed = Date.parse(windows.observed_at);
  const age = at - observed;
  const future = Number.isFinite(observed) && age < 0;
  const state = future || !Number.isFinite(observed) ? 'unavailable'
    : retained || age > 150000 || market.freshness !== 'live' ? 'retained' : 'live';
  const doc = el.ownerDocument;
  const caption = doc.createElement('span');
  caption.className = 'momentum-caption';
  caption.textContent = 'Momentum' + (state === 'live' ? '' : ' · ' + state);
  el.replaceChildren(caption);
  el.dataset.state = state;
  el.title = Number.isFinite(observed) && !future
    ? 'QQQ regular-session momentum at ' + new Date(observed).toISOString() : 'No eligible QQQ observation';
  for (const key of ['5m','9m','15m','30m','60m','session']) {
    const label = key === 'session' ? 'Open' : key;
    const r = state === 'unavailable' ? null : windows.readings?.[key];
    const cell = doc.createElement('span');
    cell.className = 'momentum-cell';
    const good = r?.status === 'ok' && Number.isFinite(r.return_pct)
      && ['up','down','flat'].includes(r.direction);
    const arrow = good ? {up:'▲', down:'▼', flat:'→'}[r.direction] : '';
    const number = good ? (r.return_pct >= 0 ? '+' : '') + r.return_pct.toFixed(2) + '%' : '';
    const unavailable = r?.status === 'warming_up' ? 'warming' : ['stale','stale_anchor'].includes(r?.status) ? 'stale' : '—';
    cell.dataset.direction = good ? r.direction : 'unknown';
    cell.textContent = label + ' ' + (good ? arrow + (compact ? '' : ' ' + number) : unavailable);
    const detail = label + ' ' + (good ? r.direction + ' ' + number : unavailable === '—' ? 'unavailable' : unavailable);
    cell.setAttribute('aria-label', detail);
    cell.title = detail;
    el.appendChild(cell);
  }
}
