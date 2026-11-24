// The minute object carries only prices. Enrichment keeps its hourly clock.
(function (root) {
  'use strict';
  const readers = new Map();
  function rows(feed, baseline, overlay, now) {
    const original = baseline[feed === 'crypto' ? 'coins' : 'stocks'];
    const baselineAt = baseline.constructed_at || baseline.as_of;
    if (!overlay || overlay.schema_version !== 'panel_minute.v1' || overlay.feed !== feed ||
        overlay.baseline_at !== baselineAt || !overlay.prices ||
        !(now >= Date.parse(overlay.completed_at) && now - Date.parse(overlay.completed_at) < 120000)) {
      return original;
    }
    return original.map(row => {
      const mark = overlay.prices[row.symbol];
      if (!mark || (feed === 'crypto' && mark.source !== row.source)) return row;
      const time = Date.parse(mark.provider_observed_at);
      const hourlyTime = Date.parse(row.provider_observed_at);
      if (Number.isFinite(hourlyTime) && Number.isFinite(mark.price_usd) &&
          (!Number.isFinite(time) || time < hourlyTime)) return row;
      const price = Number.isFinite(mark.price_usd) && mark.price_usd > 0 ? mark.price_usd : null;
      const state = price === null ? 'missing' : Number.isFinite(time)
        ? (time <= now && now - time <= 60000 ? 'fresh' : 'stale')
        : (mark.freshness === 'stale' ? 'stale' : 'time unknown');
      const when = mark.provider_observed_at || mark.provider_time_label || 'no provider time';
      const age = feed === 'crypto' && state === 'stale' && time <= now
        ? ' (' + Math.floor((now - time) / 1000) + 's old)' : '';
      const timing = feed === 'crypto' ? {provider_observed_at: mark.provider_observed_at ?? null} : {};
      return {...row, ...timing, price, minuteLabel: 'Minute price · ' + state + age + ' · ' + when,
        status: price === null ? 'source_unavailable' : 'ok'};
    });
  }
  function watch(feed, base, callback) {
    if (readers.has(feed)) return;
    let busy = false;
    async function refresh() {
      const panel = document.getElementById(feed + '-panel');
      if (busy || document.hidden || !panel || panel.classList.contains('hidden')) return;
      busy = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(base + '/' + feed + '/prices.json?_=' + Date.now(),
          {cache: 'no-store', signal: controller.signal});
        await callback(response.ok ? await response.json() : null);
      } catch (_) { await callback(null); }
      finally { clearTimeout(timeout); busy = false; }
    }
    readers.set(feed, refresh);
    setInterval(refresh, 60000);
    document.addEventListener('visibilitychange', refresh);
    refresh();
  }
  const api = {rows, watch, refresh: feed => readers.get(feed)?.()};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PanelMinute = api;
})(typeof window !== 'undefined' ? window : this);
