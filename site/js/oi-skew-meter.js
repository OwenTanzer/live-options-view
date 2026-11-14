// OI skew meter renderer (Skew + Direction H1, Nov 2026). Classic script,
// loaded after data-fetch-cycle.js; data-fetch-cycle calls
// updateOiSkewMeter(qqq.data) on the existing 60s QQQ refresh, so the meter
// adds no request of its own. All state/formatting decisions live in the pure
// site/shared/oi-skew.js; this file only writes them into #oi-skew-meter.
//
// The bot-signal row stays "not published" until the runner publishes its
// read-only projection; the raw reference reading is never presented as the
// bot's state.

function _oiSkewEtTime(ms) {
  if (ms == null) return 'time unknown';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', second: '2-digit',
    }).format(new Date(ms)) + ' ET';
  } catch (_) {
    return new Date(ms).toISOString().slice(11, 19) + ' UTC';
  }
}

function _oiSkewSigned(value) {
  // Typographic minus so a negative reading cannot be misread as a dash.
  return value > 0 ? `+${value.toFixed(3)}` : value < 0 ? `−${Math.abs(value).toFixed(3)}` : '0.000';
}

const OI_SKEW_AVAILABILITY_LABEL = {
  live: 'live', stale: 'stale', closed: 'session closed', unavailable: 'unavailable', no_data: 'no data',
};

// Freshness (Chej review of 074639e): the meter must not stay "live" just
// because nothing re-rendered it. State kept here:
//  - the last good payload (and bot projection) the fetch cycle delivered;
//  - the error of the most recent QQQ refresh, if it failed or was empty.
// A failed/empty refresh re-renders the last good snapshot as at best stale
// (noteOiSkewRefreshFailure); a successful one clears the error; and a
// ticker re-evaluates age every OI_SKEW_AGE_TICK_MS so elapsed time alone
// moves live -> stale -> session closed without any fetch.
const OI_SKEW_AGE_TICK_MS = 15 * 1000;
const _oiSkewState = { payload: null, bot: null, botError: null, refreshError: null, timer: null, botSeq: 0 };

// Bot-state projection fetch (INACTIVE). Disabled until the approved release
// that also enables the projector writer and both allowlist flags
// (backend/browser-data.js, frontend/preview-policy.mjs). While false the
// meter makes no extra request and shows "Bot signal: not published".
const OI_SKEW_PROJECTION_ENABLED = false;
const OI_SKEW_PROJECTION_PATH = 'crassus/oi-skew/v1/latest.json';

function updateOiSkewMeter(payload, nowMs = Date.now(), botProjection = undefined) {
  _oiSkewState.payload = payload;
  _oiSkewState.refreshError = null;
  if (botProjection !== undefined) {         // explicit projection (tests / callers)
    _oiSkewState.bot = botProjection;
    _oiSkewState.botError = null;
  } else if (OI_SKEW_PROJECTION_ENABLED) {
    fetchOiSkewProjection();
  }
  _ensureOiSkewAgeTicker();
  renderOiSkewMeter(nowMs);
}

// Same 60 s cadence as the QQQ payload (called from updateOiSkewMeter). A
// failure, bad JSON or a non-v1 document leaves the bot signal unavailable;
// a slower reply never overwrites a newer one.
async function fetchOiSkewProjection(fetchImpl = (typeof fetch === 'function' ? fetch : null),
                                     base = (typeof BROWSER_DATA === 'string' ? BROWSER_DATA : '')) {
  if (!fetchImpl) return;
  const seq = ++_oiSkewState.botSeq;
  try {
    const resp = await fetchImpl(`${base}/${OI_SKEW_PROJECTION_PATH}?_=${Date.now()}`);
    if (!resp.ok) throw new Error(resp.status === 404 ? 'not published (404)' : `HTTP ${resp.status}`);
    const doc = await resp.json();
    if (seq !== _oiSkewState.botSeq) return;
    if (!doc || doc.schema_version !== 'oi_skew_meter.v1') throw new Error('unrecognised document');
    _oiSkewState.bot = doc;
    _oiSkewState.botError = null;
  } catch (e) {
    if (seq !== _oiSkewState.botSeq) return;
    _oiSkewState.bot = null;
    _oiSkewState.botError = String(e && e.message || e);
  }
  renderOiSkewMeter(Date.now());
}

// Called by the fetch cycle when a QQQ refresh failed or returned no rows.
function noteOiSkewRefreshFailure(reason, nowMs = Date.now()) {
  _oiSkewState.refreshError = String(reason || 'refresh failed');
  _ensureOiSkewAgeTicker();
  renderOiSkewMeter(nowMs);
}

function _ensureOiSkewAgeTicker() {
  if (_oiSkewState.timer || typeof setInterval !== 'function') return;
  _oiSkewState.timer = setInterval(() => renderOiSkewMeter(Date.now()), OI_SKEW_AGE_TICK_MS);
}

function renderOiSkewMeter(nowMs = Date.now()) {
  const root = document.getElementById('oi-skew-meter');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const raw = degradeOiSkewForRefreshFailure(
    describeReferenceOiSkew(_oiSkewState.payload, nowMs), _oiSkewState.refreshError);
  const bot = _oiSkewState.botError
    ? describeBotOiSkewFetchFailure(_oiSkewState.botError)
    : describeBotOiSkew(_oiSkewState.bot, nowMs);

  root.dataset.state = raw.availability;
  root.dataset.lean = raw.lean || 'none';

  const track = $('oi-skew-track');
  const marker = $('oi-skew-marker');
  const pct = oiSkewMeterPercent(raw.value);
  if (pct == null) {
    marker.hidden = true;
    $('oi-skew-value').textContent = '—';
    $('oi-skew-lean').textContent = OI_SKEW_AVAILABILITY_LABEL[raw.availability] || 'unavailable';
    track.setAttribute('aria-valuenow', '0');
    track.setAttribute('aria-valuetext', `OI skew ${OI_SKEW_AVAILABILITY_LABEL[raw.availability] || 'unavailable'}`);
  } else {
    marker.hidden = false;
    marker.style.left = `${pct}%`;
    $('oi-skew-value').textContent = _oiSkewSigned(raw.value);
    const lean = raw.lean.charAt(0).toUpperCase() + raw.lean.slice(1);
    $('oi-skew-lean').textContent = lean;
    track.setAttribute('aria-valuenow', raw.value.toFixed(3));
    track.setAttribute('aria-valuetext',
      `OI skew ${raw.value.toFixed(3)}, ${raw.lean}, ${OI_SKEW_AVAILABILITY_LABEL[raw.availability]}`);
  }

  const status = (OI_SKEW_AVAILABILITY_LABEL[raw.availability] || raw.availability)
    + (raw.refresh_failed ? ' · refresh failed' : '');
  $('oi-skew-meta').textContent =
    `${raw.symbol || 'QQQ'} · reference ±${((raw.band_pct ?? 0.02) * 100).toFixed(0)}% band · `
    + `snapshot ${_oiSkewEtTime(raw.snapshot_ms)} · ${status}`;
  root.title = raw.detail;

  const botEl = $('oi-skew-bot');
  botEl.textContent = bot.text;
  botEl.dataset.state = bot.state;
  botEl.dataset.availability = bot.availability;
  $('oi-skew-bot-detail').textContent = bot.detail;

  const p = bot.params || OI_SKEW_DEFAULTS;
  $('oi-skew-thresholds').textContent =
    `Bot gates${bot.params ? '' : ' (strategy defaults; account overrides not published)'}: `
    + `|skew| ≥ ${p.imbalance_threshold}, moved ≥ ${p.min_change_from_session_start} since its session start, `
    + `≥ ${p.min_band_strikes} strikes with OI in a ±${(p.near_money_pct * 100).toFixed(0)}% band.`;
}
