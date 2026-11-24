// crypto panel — read-only display of the collector's published
// `crypto/latest.json` (market_data/crypto_collector/). Classic script,
// loaded by index.html in the same position the inline section occupied,
// so the page's script order — and the shared global scope — is
// unchanged. No ESM conversion.
//
// This page never computes or re-derives a price: it only formats
// what the collector already published, the same producer/display
// boundary formatVwapRvol/formatMomentum draw for the collector's own
// signals (shared.js). A row whose source could not be read is shown
// as "source unavailable" with a null price; a Coinbase price is
// never replaced by a CoinGecko price or vice versa, and no number is
// shown without its as-of time and source.
const CRYPTO_POLL_MS = 300_000; // Collector publishes hourly; poll every 5 minutes.
const CRYPTO_STALE_AFTER_MS = 2 * 60 * 60 * 1000; // Hourly cadence: older than 2h is stale.
let cryptoSnapshot = null;
let cryptoMinute = null;
let cryptoError = null;
let cryptoFetchVersion = 0;
let cryptoPollHandle = null;

// Injectable clock (same pattern as squeeze-panel.js): renderCryptoPanel
// calling Date.now() directly would make fixed-date fixtures assert
// fresh/stale against whatever day the suite happened to run on.
function cryptoNow() { return Date.now(); }

function formatUsd(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  if (value >= 1000) return '$' + value.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (value >= 1) return '$' + value.toFixed(2);
  return '$' + value.toFixed(4);
}

function formatPct(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  return (value > 0 ? '+' : '') + value.toFixed(2) + '%';
}

function formatMarketCap(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  if (value >= 1e12) return '$' + (value / 1e12).toFixed(2) + 'T';
  if (value >= 1e9) return '$' + (value / 1e9).toFixed(2) + 'B';
  if (value >= 1e6) return '$' + (value / 1e6).toFixed(1) + 'M';
  return formatUsd(value);
}

// HH:MM UTC of an as-of timestamp, or '—' when there is none.
function formatAsOf(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return '—';
  return new Date(ms).toISOString().slice(11, 16) + ' UTC';
}

// fresh / stale / unavailable for one provider observation, judged
// against the provider's own time, never an equity session clock.
function cryptoAgeState(observedIso, nowMs) {
  if (!observedIso) return 'unavailable';
  const ms = Date.parse(observedIso);
  if (!Number.isFinite(ms)) return 'unavailable';
  if (ms > nowMs + 30000) return 'stale';
  return nowMs - ms > CRYPTO_STALE_AFTER_MS ? 'stale' : 'fresh';
}

function cryptoSeverityLabel(severity) {
  return { extreme: 'Extreme', notable: 'Notable', info: 'Info' }[severity] || 'Info';
}

let cryptoLiveRequest = null;
async function fetchCryptoLatest() {
  cryptoLiveRequest?.abort();
  const controller = new AbortController();
  cryptoLiveRequest = controller;
  const version = ++cryptoFetchVersion;
  try {
    const url = `${BROWSER_DATA}/crypto/latest.json?_=${cryptoNow()}`;
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
    if (version !== cryptoFetchVersion) return;
    if (response.status === 404) {
      cryptoSnapshot = null;
      cryptoError = 'no snapshot published yet';
    } else {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const value = await response.json();
      if (version !== cryptoFetchVersion) return;
      if (!value || typeof value !== 'object' || Array.isArray(value) ||
          !Array.isArray(value.coins)) throw new Error('Invalid crypto snapshot');
      cryptoSnapshot = value;
      cryptoError = null;
    }
  } catch (e) {
    if (version !== cryptoFetchVersion) return;
    cryptoError = e.message || 'fetch failed';
  }
  if (version !== cryptoFetchVersion) return;
  renderCryptoPanel();
}

// ── next-hour estimates (One Clock synthetic cohort) ─────────────
// Estimates only: read through site/js/crypto-predictions.js when
// it is loaded (window.CryptoPredictions). A prediction is a
// computed guess about the next hour — rendered in its own section
// as ESTIMATE ONLY / RESEARCH ONLY, never in the price table and
// never as a live price. A passed prediction is not shown as the
// live estimate — it is retained in the file as historical
// evidence, and the read path reports "no live estimate" once
// its target hour has passed. BTC only in this version: it is the coin with
// a checked-in prediction seed; per-coin expansion is future scope.
let cryptoPrediction = null;
let cryptoPredictionError = null;
let cryptoPredictionVersion = 0;
let cryptoPredictionExpiry = null;

async function fetchCryptoPredictions() {
  const version = ++cryptoPredictionVersion;
  const predictionsModule = typeof window !== 'undefined' ? window.CryptoPredictions : null;
  if (!predictionsModule) {
    cryptoPrediction = null;
    cryptoPredictionError = 'predictions module not loaded';
    renderCryptoPredictions();
    return;
  }
  try {
    const result = await predictionsModule.readPredictionsAsOf({ coin: 'BTC', cutoff: cryptoNow() });
    if (version !== cryptoPredictionVersion) return;
    cryptoPrediction = result;
    cryptoPredictionError = null;
  } catch (e) {
    if (version !== cryptoPredictionVersion) return;
    cryptoPrediction = null;
    cryptoPredictionError = e.message || 'fetch failed';
  }
  clearTimeout(cryptoPredictionExpiry);
  const end = Date.parse(cryptoPrediction?.reading?.row?.value?.target_end);
  if (Number.isFinite(end) && end > cryptoNow()) {
    cryptoPredictionExpiry = setTimeout(() => {
      renderCryptoPredictions();
      fetchCryptoPredictions();
    }, end - cryptoNow());
  }
  renderCryptoPredictions();
}

function renderCryptoPredictions() {
  const predictionsEl = document.getElementById('crypto-predictions');
  if (!predictionsEl) return;
  if (cryptoPredictionError) {
    predictionsEl.innerHTML = '<p class="crypto-empty">Next-hour estimates unavailable — ' + escapeHtml(cryptoPredictionError) + '.</p>';
    return;
  }
  const result = cryptoPrediction;
  if (!result || result.state === 'unavailable' || !result.reading || !result.reading.row || result.reading.row.value == null) {
    predictionsEl.innerHTML = '<p class="crypto-empty">No live next-hour estimate' + (result && result.reason ? ' — ' + escapeHtml(result.reason) : '') + '.</p>';
    return;
  }
  const value = result.reading.row.value;
  const targetEnd = Date.parse(value.target_end);
  if (!Number.isFinite(targetEnd) || targetEnd <= cryptoNow()) {
    predictionsEl.innerHTML = '<p class="crypto-empty">No live next-hour estimate — target hour has passed; retained as historical evidence.</p>';
    return;
  }
  const window = value.data_window || {};
  const basis = Array.isArray(value.basis) ? value.basis.join(', ') : '';
  predictionsEl.innerHTML =
    '<p class="crypto-estimate-label">ESTIMATE ONLY · RESEARCH ONLY — not a live price, not a forecast to trade on.</p>' +
    '<p><strong>BTC</strong> next-hour estimate ' +
    '<span class="crypto-hero-price">' + formatUsd(value.estimated_price) + '</span> ' +
    '<span>' + escapeHtml(value.direction || '') + ' ' + formatPct(value.pct_move) + '</span></p>' +
    '<p>Range ' + formatUsd(value.range_low) + ' – ' + formatUsd(value.range_high) +
    ' · confidence ' + (value.confidence == null ? '—' : Math.round(value.confidence * 100) + '%') +
    ' · data: ' + (window.bars_1h == null ? '—' : window.bars_1h + ' 1h bars') +
    (window.first_bar ? ' (' + formatAsOf(window.first_bar) + ' – ' + formatAsOf(window.last_bar) + ')' : '') +
    (basis ? ' · basis: ' + escapeHtml(basis) : '') + '</p>' +
    '<p>Estimate available ' + formatAsOf(new Date(result.reading.row.availableAt).toISOString()) +
    ' · target hour ' + formatAsOf(value.target_start) + ' – ' + formatAsOf(value.target_end) +
    ' · reading ' + escapeHtml(result.state) + '</p>';
}

function cryptoRowHtml(row) {
  const unavailable = row.status === 'source_unavailable' || row.price == null;
  const price = unavailable ? 'source unavailable' : formatUsd(row.price);
  const change = row.change_24h_pct == null ? '—' : formatPct(row.change_24h_pct);
  const changeClass = row.change_24h_pct == null ? ''
    : row.change_24h_pct > 0 ? 'crypto-up' : row.change_24h_pct < 0 ? 'crypto-down' : '';
  const asOf = row.minuteLabel ? escapeHtml(row.minuteLabel) : formatAsOf(row.as_of);
  const source = row.source === 'coinbase' ? 'Coinbase' : row.source === 'coingecko' ? 'CoinGecko' : '—';
  return `<tr${unavailable ? ' class="crypto-unavailable"' : ''}>
      <td>${row.rank ?? '—'}</td>
      <td><strong>${escapeHtml(row.symbol ?? '—')}</strong> <span class="crypto-name">${escapeHtml(row.name ?? '')}</span></td>
      <td>${price}</td>
      <td class="${changeClass}">${change}</td>
      <td>${formatMarketCap(row.market_cap)}</td>
      <td>${source}</td>
      <td>${asOf}</td>
    </tr>`;
}

function renderCryptoPanel() {
  const statusEl = document.getElementById('crypto-status');
  const warningEl = document.getElementById('crypto-warning');
  const heroEl = document.getElementById('crypto-hero');
  const marketEl = document.getElementById('crypto-market');
  const tbody = document.getElementById('crypto-tbody');
  const patternsEl = document.getElementById('crypto-patterns');
  const captionEl = document.getElementById('crypto-caption');

  if (cryptoError) {
    // Keep the last-rendered table rather than blanking it on a
    // transient fetch error -- the same "don't paint over good data"
    // rule renderBots' roster-error handling follows.
    statusEl.textContent = `crypto snapshot unavailable — ${cryptoError}` + (cryptoSnapshot ? ' · showing last known' : ' · no snapshot available');
    statusEl.dataset.state = cryptoSnapshot ? 'stale' : 'unavailable';
    if (!cryptoSnapshot) {
      tbody.innerHTML = '<tr><td colspan="7" class="crypto-empty">No crypto snapshot published yet.</td></tr>';
      patternsEl.innerHTML = '<li class="crypto-empty">No patterns available.</li>';
    }
    return;
  }
  if (!cryptoSnapshot) {
    statusEl.textContent = 'loading…';
    statusEl.dataset.state = 'fallback';
    return;
  }

  const nowMs = cryptoNow();
  const constructed = cryptoSnapshot.constructed_at;
  const ageState = cryptoAgeState(constructed, nowMs);
  const rows = typeof PanelMinute === 'undefined' ? cryptoSnapshot.coins || [] :
    PanelMinute.rows('crypto', cryptoSnapshot, cryptoMinute, nowMs);
  const unavailable = rows.filter(r => r.status === 'source_unavailable' || r.price == null);
  const state = ageState === 'fresh' && !unavailable.length ? 'live'
    : ageState === 'fresh' ? 'stale' : ageState;
  statusEl.textContent = `snapshot ${formatAsOf(constructed)}` +
    (ageState === 'stale' ? ' · stale (older than 2h)' : ' · fresh') +
    (unavailable.length ? ` · ${unavailable.length} source${unavailable.length === 1 ? '' : 's'} unavailable` : '');
  statusEl.dataset.state = state;

  const warning = unavailable.length
    ? `${unavailable.map(r => r.symbol).join(', ')}: source unavailable — no price shown, never substituted from the other source.`
    : '';
  warningEl.textContent = warning;
  warningEl.classList.toggle('hidden', !warning);

  const btc = rows.find(r => r.symbol === 'BTC');
  if (btc) {
    heroEl.innerHTML = `<span class="crypto-hero-price">${btc.price == null ? 'source unavailable' : formatUsd(btc.price)}</span>
      <span class="crypto-hero-change ${btc.change_24h_pct > 0 ? 'crypto-up' : btc.change_24h_pct < 0 ? 'crypto-down' : ''}">${formatPct(btc.change_24h_pct)} (24h)</span>
      <span>24h high ${formatUsd(btc.high_24h)} · 24h low ${formatUsd(btc.low_24h)}</span>
      <span>24h volume ${btc.volume_24h == null ? '—' : '$' + Number(btc.volume_24h).toLocaleString('en-US', { maximumFractionDigits: 0 })}</span>
      <span>${btc.minuteLabel ? escapeHtml(btc.minuteLabel) : "Hourly price as of " + formatAsOf(btc.as_of)} · ${btc.source === 'coinbase' ? 'Coinbase' : 'CoinGecko'}</span>`;
  } else {
    heroEl.textContent = 'Bitcoin row missing from snapshot.';
  }

  const market = cryptoSnapshot.market || {};
  const fearGreed = market.fear_greed || {};
  marketEl.innerHTML = `<span>Total market cap ${formatMarketCap(market.total_market_cap_usd)}</span>
    <span>BTC dominance ${market.btc_dominance_pct == null ? '—' : market.btc_dominance_pct.toFixed(1) + '%'}</span>
    <span>Fear &amp; Greed ${fearGreed.status === 'ok' && fearGreed.value != null
      ? `${fearGreed.value} (${escapeHtml(fearGreed.classification || '')}) · as of ${formatAsOf(fearGreed.as_of)}`
      : 'unavailable'}</span>`;

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="crypto-empty">Snapshot has no coin rows.</td></tr>';
  } else {
    tbody.innerHTML = rows.map(cryptoRowHtml).join('');
  }
  if (captionEl) {
    const membership = cryptoSnapshot.universe?.membership_source;
    const label = membership === 'live' ? 'live top 50' : membership === 'embedded_fallback' ? 'embedded fallback' : 'membership source unknown';
    captionEl.textContent = `${rows.length} coins by market-cap rank · ${label} · snapshot ${formatAsOf(constructed)}` +
      (ageState === 'stale' ? ' · stale' : '');
  }

  const patterns = cryptoSnapshot.patterns || [];
  if (!patterns.length) {
    patternsEl.innerHTML = '<li class="crypto-empty">No patterns detected in the latest update.</li>';
  } else {
    patternsEl.innerHTML = patterns.map(p =>
      `<li><span class="crypto-severity crypto-severity-${escapeHtml(p.severity || 'info')}">${escapeHtml(cryptoSeverityLabel(p.severity))}</span> ` +
      `<strong>${escapeHtml(p.title || p.kind || 'Pattern')}</strong> ` +
      `<span class="crypto-pattern-detail">${escapeHtml(p.detail || '')}</span> ` +
      `<span class="crypto-pattern-time">${formatAsOf(p.detected_at)}</span></li>`).join('');
  }
}

// Mounted once, on first open, like the News panel.
function loadCryptoPanel() {
  if (typeof PanelMinute !== 'undefined') PanelMinute.watch('crypto', BROWSER_DATA, value => {
    cryptoMinute = value;
    if (cryptoSnapshot && Date.parse(value?.baseline_at) > Date.parse(cryptoSnapshot.constructed_at)) fetchCryptoLatest();
    else if (cryptoSnapshot) renderCryptoPanel();
  });
  fetchCryptoLatest();
  fetchCryptoPredictions();
  if (!cryptoPollHandle) cryptoPollHandle = setInterval(() => {
    fetchCryptoLatest();
    fetchCryptoPredictions();
  }, CRYPTO_POLL_MS);
}
