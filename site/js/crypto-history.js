import { instant, selectAsOf } from '../shared/one-clock.js';

const HOUR = 3600000;
const utc = value => new Date(value).toISOString().replace('T', ' ').slice(0, 19);

export function archiveAsOf(archive, cutoff) {
  const at = instant(cutoff);
  const available = instant(archive.available_at);
  if (at < available) return { available: false, coverage: [], candles: [] };
  if (!Array.isArray(archive.coverage) || !Array.isArray(archive.btc_hourly)) {
    throw new Error('Archive coverage or candle history is missing.');
  }
  for (const bar of archive.btc_hourly) {
    if (bar.source !== 'crypto_candle_cache' || bar.instrument !== 'BTC' ||
        bar.provider !== 'coinbase' || bar.origin !== 'recorded' ||
        bar.session !== 'continuous' || bar.interval !== '1h' ||
        instant(bar.availableAt) < available || !Number.isFinite(bar.value?.close)) {
      throw new Error('The BTC archive contains an invalid hourly observation.');
    }
  }
  const candles = selectAsOf(archive.btc_hourly, {
    session: 'continuous', instrument: 'BTC', origin: 'recorded', cutoff: at,
  });
  return { available: true, coverage: archive.coverage, candles };
}

export function coverageCells(row) {
  const status = row.source_batch_done === true ? 'Batch marked done'
    : row.source_batch_done === false ? 'Batch in progress' : 'Batch status unavailable';
  const source = row.primary === false ? row.provider + ' (alternate)' : row.primary === true
    ? row.provider + ' (primary)' : row.provider;
  return [row.symbol, row.interval, source, row.kind === 'price_sample' ? 'Price samples' : 'Candles',
    row.count, status, row.first == null ? '—' : utc(row.first), row.last == null ? '—' : utc(row.last)];
}

function replaceRows(body, rows) {
  body.replaceChildren(...rows.map(values => {
    const tr = document.createElement('tr');
    for (const value of values) {
      const td = document.createElement('td');
      td.textContent = String(value);
      tr.append(td);
    }
    return tr;
  }));
}

export function mountArchive(panel) {
  const find = id => panel.querySelector('#crypto-history-' + id);
  const cutoff = find('cutoff');
  const status = find('status');
  const coverage = find('coverage');
  const candles = find('candles');
  let archive;
  let loading = false;

  function render() {
    if (!archive) return;
    try {
      const selected = archiveAsOf(archive, cutoff.value ? cutoff.value + 'Z' : Date.now());
      replaceRows(coverage, selected.coverage.map(coverageCells));
      replaceRows(candles, [...selected.candles].reverse().map(row => [
        utc(row.eventAt), row.value.close.toLocaleString('en-US', {maximumFractionDigits: 8}),
      ]));
      if (!selected.available) {
        status.textContent = 'Archive unavailable at this cutoff. First received ' + utc(archive.available_at) + ' UTC.';
        return;
      }
      const bars = selected.candles;
      const span = bars.length ? 1 + (bars.at(-1).eventAt - bars[0].eventAt) / HOUR : 0;
      const gaps = Math.max(0, span - bars.length);
      status.textContent = 'Archive received ' + utc(archive.available_at) + ' UTC. ' +
        new Set(selected.coverage.map(row => row.symbol)).size + ' coins. ' +
        bars.length + ' BTC hourly closes available in the published sample' +
        (gaps ? ' · ' + gaps + ' missing hours between its endpoints.' : '.');
    } catch (error) {
      coverage.replaceChildren();
      candles.replaceChildren();
      status.textContent = 'Archive unavailable. ' + error.message;
    }
  }

  async function open() {
    if (!panel.open || loading) return;
    if (archive) { render(); return; }
    loading = true;
    status.textContent = 'Loading crypto history…';
    try {
      const response = await fetch('/data/crypto-history.json', {cache: 'no-cache'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      archive = await response.json();
      render();
    } catch (error) {
      status.textContent = 'Archive unavailable. ' + error.message;
    } finally {
      loading = false;
    }
  }
  cutoff.addEventListener('input', render);
  panel.addEventListener('toggle', open);
  if (panel.open) open();
}

if (typeof document !== 'undefined') {
  const panel = document.getElementById('crypto-history');
  if (panel) mountArchive(panel);
}
