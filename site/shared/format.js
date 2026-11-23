// format.js — panel status formatting + order validation, moved out of
// docs/shared.js (B3 slice 4, Nov 2026). Verbatim move: the code below is
// byte-identical to what lived in shared.js lines 26-105 (VWAP/RVOL +
// momentum formatters), 107-254 (short-squeeze scanner formatters), 256-264
// (STEO formatters), and 266-302 (order validation). Plain classic script
// (no module system, no build step), like the file it came from: the live
// page and diag.html load it with its own <script> tag (see docs/index.html,
// right after the quotes.js tag), where it defines formatVwapRvol /
// formatMomentum / formatSqueezeScanStatus / formatSqueezeScheduleStatus /
// describeSqueezeAcquisitionFailure / formatSqueezeFirstSeen / fmtSteoDelta /
// findRevision / normalizePaperOrder / normalizeShareOrder as globals; under
// Node it exports the same twelve names (see the guard at the bottom).
// #178 added the chain-snapshot header formatters below those
// (chainSessionFromEtTime / formatSnapshotAge / formatChainSnapshot and the
// CHAIN_SNAPSHOT_LIVE_MS bound). They are browser globals like the rest, and
// are NOT re-exported through site/shared.js, whose 26-name Node surface is
// pinned by tests/csv_ranges.test.js.
// Self-contained: every name it reads is defined in this file
// (formatSqueezeScheduleStatus reads formatSqueezeScanStatus and
// describeSqueezeAcquisitionFailure; normalizeShareOrder reads
// normalizePaperOrder) — the only cross-module reads in the old file were
// comments. No ESM conversion in this step.

// -- VWAP/RVOL display formatting --------------------------------------------
// Pure formatting for the `underlying_market` block published on
// intraday/latest.json (see crassus/crassus/market.py's UnderlyingMarket and
// docs/plans/2026-07-vwap-rvol.md) -- the same canonical record the
// momentum_qqq strategy reads, so the UI and the strategy never disagree on
// what "VWAP" or "RVOL" mean here. No DOM access, kept testable the same
// simple way as tickerSessionState/TickerStateStore in docs/shared/quotes.js.
//
// `state` is one of the same four values docs/index.html already uses for
// ticker tiles (live/stale/fallback/closed, see TickerStateStore._decorate
// in docs/shared/quotes.js and the `.ticker-btn[data-state=...]` CSS) so a stale VWAP looks exactly
// as visually "stale" as a stale ticker quote already does -- no new state
// vocabulary. "closed" is inferred here from the observation's own age
// (clearly a leftover reading from a prior session), not from session-hour
// math, because the collector can only ever write "live"/"stale" itself
// (see collector.py's `_compute_underlying_market` -- it has no way to know
// it has stopped running).
const VWAP_RVOL_CLOSED_AFTER_MS = 4 * 60 * 60 * 1000;

function formatVwapRvol(underlyingMarket, nowMs = Date.now()) {
  if (!underlyingMarket) {
    return { text: 'VWAP/RVOL: unavailable', state: 'fallback' };
  }
  const um = underlyingMarket;
  const observedMs = Date.parse(um.spot_ts || um.vwap_ts || '');
  const ageMs = Number.isFinite(observedMs) ? nowMs - observedMs : null;
  if (ageMs != null && ageMs > VWAP_RVOL_CLOSED_AFTER_MS) {
    return { text: 'VWAP/RVOL: session closed', state: 'closed' };
  }

  const vwapPart = um.vwap != null
    ? (um.price_vs_vwap_pct != null
      ? `VWAP ${um.vwap.toFixed(2)} (${um.price_vs_vwap_pct >= 0 ? '+' : ''}${um.price_vs_vwap_pct.toFixed(2)}%)`
      : `VWAP ${um.vwap.toFixed(2)}`)
    : 'VWAP —';
  const rvol = um.rvol || {};
  const rvolPart = rvol.status === 'ok' && rvol.multiple != null
    ? `RVOL ${rvol.multiple.toFixed(2)}×`
    : rvol.status === 'insufficient_history' ? 'RVOL warming up'
    : 'RVOL —';

  return { text: `${vwapPart} | ${rvolPart}`, state: um.freshness === 'live' ? 'live' : 'stale' };
}

// -- time-series momentum display formatting ---------------------------------
// Pure formatting for `underlying_market.momentum` (see
// crassus/crassus/market.py's UnderlyingMarket.momentum_* fields and
// docs/plans/2026-07-momentum-indicator.md). Display/log-only reference
// signal -- computed by the collector the same way momentum_qqq computes its
// own live trading signal, but independently, not a shared value (each
// momentum_qqq account runs its own separately-configured lookback). Same
// state vocabulary and "closed"-by-age inference as formatVwapRvol above.
function formatMomentum(underlyingMarket, nowMs = Date.now()) {
  if (!underlyingMarket) {
    return { text: 'Momentum: unavailable', state: 'fallback' };
  }
  const um = underlyingMarket;
  const observedMs = Date.parse(um.spot_ts || '');
  const ageMs = Number.isFinite(observedMs) ? nowMs - observedMs : null;
  if (ageMs != null && ageMs > VWAP_RVOL_CLOSED_AFTER_MS) {
    return { text: 'Momentum: session closed', state: 'closed' };
  }

  const momentum = um.momentum || {};
  let text;
  if (momentum.status === 'ok' && momentum.return_pct != null) {
    const sign = momentum.return_pct >= 0 ? '+' : '';
    const arrow = momentum.direction === 'up' ? '▲' : momentum.direction === 'down' ? '▼' : '→';
    const lookback = momentum.lookback_minutes != null ? Math.round(momentum.lookback_minutes) : '?';
    text = `Momentum ${arrow} ${sign}${momentum.return_pct.toFixed(2)}% (${lookback}m)`;
  } else if (momentum.status === 'warming_up') {
    text = 'Momentum warming up';
  } else if (momentum.status === 'stale_anchor') {
    text = 'Momentum stale (gap in data)';
  } else {
    text = 'Momentum —';
  }

  return { text, state: um.freshness === 'live' ? 'live' : 'stale' };
}

// -- short-squeeze scanner panel formatting (OA-191) -------------------------
// Pure formatting/tracking for docs/index.html's squeeze panel, which
// displays the independently-maintained scanner's published `latest.json`
// pointer (OwenTanzer/short-squeeze-scanner's squeeze_scanner/storage.py
// `upload()`). Same producer/display split as formatVwapRvol/formatMomentum
// above: this page never computes a score, only formats what was published.
// Legacy manual formatter retained for compatibility and acquisition-time labels.
// The live panel uses formatSqueezeScheduleStatus for freshness decisions.
const SQUEEZE_SCAN_STALE_AFTER_MS = 6 * 60 * 60 * 1000;

// PR #105 review, finding 2: freshness must be judged against ACQUISITION
// time (when the scan actually ran), not `published_at` (when the pointer
// file happened to be written to R2) -- those can differ by a day if an
// upload is delayed, and `published_at` alone would render a September 22
// scan uploaded September 23 as fresh ("live"). `finished_at` (falling back
// to `started_at`) is the acquisition-time field; `published_at` is never
// used for the age/staleness judgment here anymore.
function formatSqueezeScanStatus(pointer, nowMs = Date.now()) {
  if (!pointer) return { text: 'No scan published yet', state: 'fallback' };
  const acquiredMs = Date.parse(pointer.finished_at || pointer.started_at || '');
  const dateText = Number.isFinite(acquiredMs)
    ? new Date(acquiredMs).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'America/New_York' })
    : 'unknown date';
  const timeText = Number.isFinite(acquiredMs)
    ? new Date(acquiredMs).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
    : 'unknown time';
  // A prior-day scan is explicitly stale regardless of raw age in ms -- a
  // 9pm run is under SQUEEZE_SCAN_STALE_AFTER_MS old at 11pm the same
  // night, but is still yesterday's data once a new session has started.
  const todayKey = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(nowMs));
  const scanDateKey = Number.isFinite(acquiredMs)
    ? new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(acquiredMs))
    : null;
  const priorDay = scanDateKey != null && scanDateKey !== todayKey;
  const label = priorDay ? `${dateText}, ${timeText}` : timeText;

  if (pointer.status === 'empty') {
    return { text: `Scan complete ${label} ET — no eligible candidates`, state: 'stale' };
  }
  const ageMs = Number.isFinite(acquiredMs) ? nowMs - acquiredMs : null;
  const stale = priorDay || ageMs == null || ageMs > SQUEEZE_SCAN_STALE_AFTER_MS;
  return { text: `Scan ${label} ET${stale ? ' (stale)' : ''}`, state: stale ? 'stale' : 'live' };
}

// Calendar slots are exported from the producer's XNYS calendar and activation
// configuration. An expired/unavailable calendar never implies a fresh scan.
function formatSqueezeScheduleStatus(pointer, attempt, schedule, calendar, nowMs = Date.now()) {
  const label = formatSqueezeScanStatus(pointer, nowMs).text.replace(' (stale)', '');
  const result = (warning, state = 'stale', suffix = '') => ({ text: label + suffix, state, warning });
  const validFrom = Date.parse(calendar?.valid_from || '');
  const validUntil = Date.parse(calendar?.valid_until || '');
  if (calendar?.schema_version !== 1 || calendar?.calendar !== 'XNYS' ||
      calendar?.timezone !== 'America/New_York' || !Array.isArray(calendar?.slots) ||
      !Number.isFinite(validFrom) || !Number.isFinite(validUntil) ||
      nowMs < validFrom || nowMs >= validUntil) {
    return result('Schedule calendar unavailable or outside its coverage. Freshness is unconfirmed.');
  }
  const slots = calendar.slots.map(value => Date.parse(value));
  if (slots.some((value, i) => !Number.isFinite(value) || value < validFrom ||
      value >= validUntil || (i && value <= slots[i - 1]))) {
    return result('Schedule calendar is invalid. Freshness is unconfirmed.');
  }
  const due = slots.filter(value => value <= nowMs).at(-1);
  if (due == null) return result('Waiting for the first scheduled scan.', 'fallback');
  const slotLabel = new Date(due).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York',
  }) + ' ET';
  const matching = Date.parse(schedule?.scheduled_for || '') === due;
  if (matching && ['missed', 'interrupted'].includes(schedule.status)) {
    return result(`Scheduled scan ${slotLabel}: ${schedule.status}. Showing the last successful result, if available.`);
  }
  if (matching && schedule.status === 'claimed') {
    return result(nowMs - due <= 40 * 60_000
      ? `Scheduled scan ${slotLabel} is acquiring or awaiting publication. Previous results remain below.`
      : `Scheduled scan ${slotLabel} has no confirmed completion. Previous results remain below.`);
  }
  const failure = describeSqueezeAcquisitionFailure(pointer, attempt);
  if (failure) return result(failure);
  if (matching && schedule.status === 'finished' && !['complete', 'empty'].includes(schedule.acquisition_status)) {
    return result(`Scheduled scan ${slotLabel} did not complete: ${schedule.acquisition_status || 'unknown'}. Showing the last successful result, if available.`);
  }
  const started = Date.parse(pointer?.started_at || '');
  const finished = Date.parse(pointer?.finished_at || '');
  const current = pointer?.sampling_mode === 'scheduled' && Date.parse(pointer?.scheduled_for || '') === due &&
    ['complete', 'empty'].includes(pointer?.status) && Number.isFinite(started) && Number.isFinite(finished) &&
    started >= due && started <= due + 10 * 60_000 && finished >= started && finished <= nowMs &&
    matching && schedule.status === 'finished' && schedule.run_id === pointer.run_id &&
    schedule.acquisition_status === pointer.status;
  if (current) {
    const nyDay = ms => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(ms));
    const priorSession = nyDay(due) !== nyDay(nowMs);
    return result(null, priorSession ? 'stale' : 'live', priorSession ? ' — previous session' : ' — latest scheduled scan');
  }
  if (matching && schedule.status === 'finished') {
    return result(`Scheduled scan ${slotLabel} finished, but its published result is not yet confirmed. Showing the last successful result, if available.`);
  }
  return result(nowMs - due <= 10 * 60_000
    ? `Scheduled refresh due ${slotLabel}; waiting for the scan.`
    : `No confirmed schedule status for ${slotLabel}. Results may be stale.`);
}

// PR #105 review, finding 1: `latest.json` only ever advances on a
// successful (complete/empty) run -- storage.py's `upload()` always
// advances `latest-attempt.json` regardless of outcome. Without checking
// the attempt pointer too, a failed or partial noon scan is invisible: the
// morning's successful result keeps showing as if nothing had gone wrong
// since. `successPointer`/`attemptPointer` are the parsed latest.json /
// latest-attempt.json bodies (either may be null if nothing was ever
// published to that path yet).
function describeSqueezeAcquisitionFailure(successPointer, attemptPointer) {
  if (!attemptPointer) return null;
  if (attemptPointer.status === 'complete' || attemptPointer.status === 'empty') return null; // the attempt succeeded
  const attemptIsNewer = !successPointer ||
    Date.parse(attemptPointer.started_at || '') > Date.parse(successPointer.started_at || '');
  if (!attemptIsNewer) return null; // the failure is older than the successful result already shown
  const startedMs = Date.parse(attemptPointer.started_at || '');
  const time = Number.isFinite(startedMs)
    ? new Date(startedMs).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
    : 'unknown time';
  return successPointer
    ? `Most recent scan (started ${time} ET) did not complete: ${attemptPointer.status}. Showing the last successful result below.`
    : `No successful scan yet. Most recent attempt (started ${time} ET): ${attemptPointer.status}.`;
}

// PR #105 review round 2: renders the "First seen today" column against
// the real producer contract (short-squeeze-scanner#2's
// docs/archive-contract.md, "Display contract v1: canonical first
// appearance") -- `first_seen_at` (UTC write-attempt timestamp, null if
// unknown) and `is_new` (true only for the run that first introduces the
// ticker; false for a known existing candidate, including a reappearance;
// null if unknown/legacy). The round-1 placeholder
// (`row.first_seen_at ? 'New' : 'unavailable'`) treated ANY non-null
// first_seen_at as New and never rendered the time itself -- both an
// incumbent (is_new=false) and a genuine newcomer (is_new=true) rendered
// identically as bare "New" text. `is_new` alone decides the New label;
// first_seen_at (New York time) is shown either way when known.
function formatSqueezeFirstSeen(row) {
  if (row.first_seen_at == null || row.is_new == null) return { text: 'unavailable', isNew: false };
  const seenMs = Date.parse(row.first_seen_at);
  const time = Number.isFinite(seenMs)
    ? new Date(seenMs).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
    : 'unknown time';
  return { text: time, isNew: row.is_new === true };
}

// EIA STEO crude calibration panel formatting (docs/index.html's
// fetchSteoCalibration). Pure, DOM-free -- same rationale as
// formatVwapRvol/formatMomentum above: testable directly, no fixture DOM
// needed.
function fmtSteoDelta(delta, unit = '') {
  if (delta == null) return { text: '', cls: '' };
  const sign = delta >= 0 ? '+' : '';
  return { text: `${sign}${delta.toFixed(2)}${unit}`, cls: delta > 0 ? 'up' : delta < 0 ? 'down' : '' };
}

function findRevision(revisions, period) {
  return (revisions || []).find(r => r.period === period) || null;
}

function normalizePaperOrder({ side, quantity, orderType = 'market', limitPrice = null } = {}) {
  const qty = Number(quantity);
  if (side !== 'buy' && side !== 'sell') {
    return { error: 'Choose buy or sell.' };
  }
  if (!Number.isInteger(qty) || qty < 1 || qty > 100_000) {
    return { error: 'Quantity must be a positive whole number.' };
  }
  if (orderType !== 'market' && orderType !== 'limit') {
    return { error: 'Choose market or limit.' };
  }

  if (orderType === 'market') {
    return { value: { side, qty, order_type: 'market' } };
  }

  const price = Number(limitPrice);
  if (!Number.isFinite(price) || price <= 0 || price > 1_000_000) {
    return { error: 'Limit price must be greater than zero.' };
  }
  const cents = Math.round(price * 100);
  if (Math.abs(price * 100 - cents) > 1e-7) {
    return { error: 'Limit price must use increments of $0.01.' };
  }
  return { value: { side, qty, order_type: 'limit', limit_price: cents / 100 } };
}

function normalizeShareOrder({
  side, quantity, heldQuantity = 0, orderType = 'market', limitPrice = null,
} = {}) {
  const normalized = normalizePaperOrder({ side, quantity, orderType, limitPrice });
  if (normalized.error) return normalized;
  if (side === 'sell' && normalized.value.qty > Math.max(0, Number(heldQuantity) || 0)) {
    return { error: 'You cannot sell more shares than you hold.' };
  }
  return normalized;
}

// -- options-chain snapshot header: spot, session and age (#178) -------------
// The legacy header above the chain shows the spot the chain was captured at
// plus a tier badge. Neither said WHEN the snapshot was taken, so a chain that
// stopped collecting at 16:14 ET sat next to Core Markets cards that were
// still advancing after hours: 675.34 / "Regular" / 16:14 ET above 675.51 /
// AFTER HOURS / updated 6s. The tier badge made that worse, because "Regular"
// is the 0DTE_Regular EXPIRY TIER and reads like a market-session label.
//
// This formatter reports the snapshot's own session, its age, and whether the
// quoted spot is a retained historical reading, so a stopped chain is labeled
// as stopped instead of being read as a current price. The session comes from
// the ET clock the producer wrote into `snapshot_time` ("16:14 ET") rather
// than from the reader's clock or time zone: a browser in another zone must
// not relabel 16:14 ET as the regular session. Age uses `timestamp`, the same
// instant the status dot already judges, so header and dot cannot disagree
// about freshness.
const CHAIN_SNAPSHOT_LIVE_MS = 2 * 60 * 1000;   // same bound as the status dot

const CHAIN_SESSION_LABELS = {
  premarket: 'Premarket',
  regular: 'Regular session',
  afterhours: 'After hours',
  overnight: 'Closed',
};

// "16:14 ET" -> 'afterhours'. Unparseable or absent -> null (unknown, never
// guessed). 09:30–16:00 is the regular session; 04:00–09:30 premarket;
// 16:00–20:00 after hours; anything else is outside the equity session day.
function chainSessionFromEtTime(snapshotTime) {
  const match = /^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*ET\s*$/i.exec(String(snapshotTime ?? ''));
  if (!match) return null;
  let hour = Number(match[1]);
  const minute = Number(match[2]);
  if (!Number.isFinite(hour) || !Number.isFinite(minute) || hour > 24 || minute > 59) return null;
  const meridiem = (match[3] || '').toUpperCase();
  if (meridiem === 'PM' && hour < 12) hour += 12;
  if (meridiem === 'AM' && hour === 12) hour = 0;
  const minutes = hour * 60 + minute;
  if (minutes >= 570 && minutes < 960) return 'regular';
  if (minutes >= 240 && minutes < 570) return 'premarket';
  if (minutes >= 960 && minutes < 1200) return 'afterhours';
  return 'overnight';
}

function formatSnapshotAge(ageMs) {
  if (!Number.isFinite(ageMs)) return 'age unknown';
  if (ageMs < -30_000) return 'clock skew';
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) return restMinutes ? `${hours}h ${restMinutes}m ago` : `${hours}h ago`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}d ${restHours}h ago` : `${days}d ago`;
}

// A wall-clock reading in `timeZone` as a UTC instant. Two passes: the first
// uses the offset at the naive guess, the second re-reads the offset at that
// corrected instant, which settles the one-hour discontinuity of a DST change.
// Built on Intl rather than a date library because this is a classic script
// with no build step and no dependencies.
function zonedWallClockToUtcMs(year, month, day, hour, minute, second = 0, timeZone = 'America/New_York') {
  const naive = Date.UTC(year, month - 1, day, hour, minute, second);
  const offsetAt = utcMs => {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date(utcMs)).filter(p => p.type !== 'literal');
    const f = Object.fromEntries(parts.map(p => [p.type, p.value]));
    return Date.UTC(Number(f.year), Number(f.month) - 1, Number(f.day),
      Number(f.hour), Number(f.minute), Number(f.second)) - utcMs;
  };
  const first = naive - offsetAt(naive);
  return naive - offsetAt(first);
}

// The instant a chain snapshot was captured, independent of the reader's time
// zone. `timestamp` is the producer's own wall clock, written on the Pacific
// machine that collects the chain ("11/10/2026 13:14:51" beside "16:14 ET"),
// so reading it as browser-local time would report a different age in every
// zone. Order: an offset-bearing spot timestamp if the payload has one (IBIT
// publishes one), then the producer's PT clock, then any parseable timestamp,
// then the ET pair (date + snapshot_time), and null rather than a guess.
function chainSnapshotInstant(data) {
  const spotTs = String(data?.underlying_market?.spot_ts ?? '').trim();
  if (/T.*(?:Z|[+-]\d{2}:?\d{2})$/.test(spotTs)) {
    const parsed = Date.parse(spotTs);
    if (Number.isFinite(parsed)) return parsed;
  }
  const localMatch = /^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(String(data?.timestamp ?? '').trim());
  if (localMatch) {
    return zonedWallClockToUtcMs(Number(localMatch[3]), Number(localMatch[1]), Number(localMatch[2]),
      Number(localMatch[4]), Number(localMatch[5]), localMatch[6] ? Number(localMatch[6]) : 0, 'America/Los_Angeles');
  }
  const parsed = Date.parse(data?.timestamp || '');
  if (Number.isFinite(parsed)) return parsed;
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(data?.date ?? '').trim());
  const timeMatch = /^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\s*ET$/i.exec(String(data?.snapshot_time ?? '').trim());
  if (dateMatch && timeMatch) {
    let hour = Number(timeMatch[1]);
    const minute = Number(timeMatch[2]);
    const second = timeMatch[3] ? Number(timeMatch[3]) : 0;
    const meridiem = (timeMatch[4] || '').toUpperCase();
    if (meridiem === 'PM' && hour < 12) hour += 12;
    if (meridiem === 'AM' && hour === 12) hour = 0;
    if (hour <= 24 && minute <= 59 && second <= 59) {
      return zonedWallClockToUtcMs(Number(dateMatch[1]), Number(dateMatch[2]), Number(dateMatch[3]),
        hour, minute, second, 'America/New_York');
    }
  }
  return null;
}

// state: 'live' (the snapshot is current), 'retained' (a historical value kept
// on screen after collection stopped) or 'untimed' (no usable timestamp).
function formatChainSnapshot(data, nowMs = Date.now()) {
  const snapshotTime = String(data?.snapshot_time ?? '').trim();
  const session = chainSessionFromEtTime(snapshotTime);
  const sessionLabel = session ? CHAIN_SESSION_LABELS[session] : null;
  const snapshotMs = chainSnapshotInstant(data);
  const ageMs = snapshotMs == null ? null : nowMs - snapshotMs;
  const state = ageMs == null ? 'untimed' : ageMs <= CHAIN_SNAPSHOT_LIVE_MS ? 'live' : 'retained';
  const ageLabel = formatSnapshotAge(ageMs);
  const parts = [`snapshot ${snapshotTime || 'time unknown'}`];
  if (sessionLabel) parts.push(sessionLabel);
  parts.push(ageLabel);
  if (state === 'retained') parts.push('retained');
  const detail = state === 'retained'
    ? `This chain is a historical snapshot taken at ${snapshotTime || 'an unknown time'}`
      + `${sessionLabel ? ` during the ${sessionLabel.toLowerCase()}` : ''}, ${ageLabel}. `
      + 'The quoted spot and its cells are the values captured then, not current prices; '
      + 'Core markets shows live prices.'
    : state === 'untimed'
      ? 'This snapshot has no usable timestamp, so its age cannot be shown.'
      : `Chain snapshot taken at ${snapshotTime || 'an unknown time'}`
        + `${sessionLabel ? ` during the ${sessionLabel.toLowerCase()}` : ''}, ${ageLabel}.`;
  return { state, session, sessionLabel, snapshotTime, snapshotMs, ageMs, text: parts.join(' · '), detail };
}

if (typeof module !== 'undefined') {
  module.exports = { VWAP_RVOL_CLOSED_AFTER_MS, formatVwapRvol, formatMomentum, CHAIN_SNAPSHOT_LIVE_MS, chainSessionFromEtTime, formatSnapshotAge, zonedWallClockToUtcMs, chainSnapshotInstant, formatChainSnapshot, SQUEEZE_SCAN_STALE_AFTER_MS, formatSqueezeScanStatus, formatSqueezeScheduleStatus, describeSqueezeAcquisitionFailure, formatSqueezeFirstSeen, fmtSteoDelta, findRevision, normalizePaperOrder, normalizeShareOrder };
}
