// Render the selected producer snapshot; no additional polling, inference, or trading state.
function paintOptionSensitivities(data, retained = false) {
  const panel = document.getElementById('option-sensitivities');
  if (!panel) return;
  panel.hidden = !data;
  if (panel.hidden) return;
  const rows = (data.rows || []).filter(r => r.Expiration === data.expiration &&
    Number.isFinite(r.Strike) && r.Strike > 0 && ['call', 'put'].includes(r.Type));
  const strikes = [...new Set(rows.map(r => r.Strike))].sort((a, b) => a - b);
  const selector = panel.querySelector('select');
  const context = [data.symbol || 'QQQ', data.expiration].join('/');
  const previous = selector.dataset.context === context ? selector.value : '';
  selector.replaceChildren(new Option('Nearest to spot', ''), ...strikes.map(k => new Option(String(k), String(k))));
  selector.value = strikes.includes(Number(previous)) ? previous : '';
  selector.dataset.context = context;
  const spot = data.underlying_price;
  const nearest = Number.isFinite(spot) && spot > 0 ? strikes.reduce((best, k) =>
    best === null || Math.abs(spot - k) < Math.abs(spot - best) ? k : best, null) : null;

  function showStrike() {
    const strike = selector.value ? Number(selector.value) : nearest;
    const body = panel.querySelector('tbody');
    body.replaceChildren();
    for (const side of ['call', 'put']) {
      const row = rows.find(r => r.Strike === strike && r.Type === side);
      const tr = body.insertRow();
      const th = document.createElement('th');
      th.scope = 'row';
      th.textContent = side === 'call' ? 'Call' : 'Put';
      tr.append(th);
      for (const key of ['ModelGamma', 'ModelVanna', 'ModelVega', 'ModelCharm']) {
        const value = row?.[key];
        tr.insertCell().textContent = Number.isFinite(value) ? Number(value.toPrecision(4)).toString() : '—';
      }
    }
    panel.querySelector('output').textContent = [
      data.symbol || 'QQQ', data.expiration, strike === null ? 'Strike unavailable' : 'Strike ' + strike,
      data.snapshot_time, retained ? 'retained snapshot' : ''
    ].filter(Boolean).join(' · ');
  }
  selector.onchange = showStrike;
  showStrike();
}
