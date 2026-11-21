// short-squeeze scanner panel (OA-191) — extracted from docs/index.html
// (B2, readability quest, Nov 2026). Classic script, loaded by index.html
// in the same position the inline section occupied, so the page's script
// order — and the shared global scope — is unchanged. No ESM conversion.
// ── short-squeeze scanner panel (OA-191) ────────────────────────────────────
// Renders the independently-maintained scanner's published `latest.json`
// pointer (OwenTanzer/short-squeeze-scanner's squeeze_scanner/storage.py
// `upload()`). This page never computes or re-derives a score -- it only
// formats what the scanner already published, the same producer/display
// boundary formatVwapRvol/formatMomentum draw for the collector's own
// signals (shared.js). `pointer.candidates` is already the top 10
// comparison-eligible (options-valid) rows, sorted by combined rank
// ascending -- storage.py does that filtering/sorting/truncation before
// publishing, not this page.
const SQUEEZE_POLL_MS = 60_000; // Poll published state; never trigger acquisition.
let squeezeSchedulePointer = null;
let squeezeCalendar = null;
let squeezeFetchVersion = 0;
let squeezePointer = null; // parsed latest.json (last SUCCESSFUL run only)
let squeezeAttemptPointer = null; // parsed latest-attempt.json (every attempt, success or not)
let squeezeError = null;

// Injectable clock (PR #105 review, finding 4): renderSqueezePanel calling
// Date.now() directly made tests/squeeze_panel.test.js's fixed-date
// fixtures assert fresh/stale against whatever day the suite happened to
// run on, rather than the fixture's own intended "now." Same pattern this
// file's LiveQuotePoller/TickerStateStore already use for nowFn, just as a
// free variable instead of a constructor option since this panel has no
// class of its own.
function squeezeNow() { return Date.now(); }

async function fetchSqueezeLatest() {
  const version = ++squeezeFetchVersion;
  try {
    const base = `${BROWSER_DATA}/squeeze-scanner/v1/scheduled`;
    const cacheBust = squeezeNow();
    const urls = ['latest.json', 'latest-attempt.json', 'latest-schedule.json']
      .map(name => `${base}/${name}?_=${cacheBust}`);
    urls.push(`/squeeze-calendar.json?_=${cacheBust}`);
    const values = await Promise.all(urls.map(async url => {
      const response = await fetch(url, { cache: 'no-store' });
      if (response.status === 404) return null;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const value = await response.json();
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid scanner response');
      return value;
    }));
    const published = values[0];
    if (published && (!['complete', 'empty'].includes(published.status) ||
        !Array.isArray(published.candidates) || published.candidates.length > 10 ||
        published.candidates.some(row => !row || typeof row.ticker !== 'string' || !row.scores ||
          ['combined', 'factor', 'options'].some(key => row.scores[key] != null && !Number.isFinite(row.scores[key]))))) {
      throw new Error('Invalid published shortlist');
    }
    if (version !== squeezeFetchVersion) return;
    // Apply the complete poll together; JSON/network failures retain the last
    // consistent set, and an older poll cannot replace a newer one.
    [squeezePointer, squeezeAttemptPointer, squeezeSchedulePointer, squeezeCalendar] = values;
    squeezeError = null;
  } catch (e) {
    if (version !== squeezeFetchVersion) return;
    squeezeError = e.message || 'fetch failed';
  }
  renderSqueezePanel();
}

// Scores are drawn as bars on a fixed 0-1 scale. The scanner clamps every
// component to [0, 1] and each score is a weighted mean of components
// (short-squeeze-scanner squeeze_scanner/scoring.py), so the scale is the
// producer's own, not one this page invents. The number stays beside the
// bar; a missing score is a word, never an empty bar that reads as zero.
function squeezeScoreCell(value, missingText) {
  if (value == null || !Number.isFinite(value)) {
    return `<td class="squeeze-score squeeze-score-missing">${missingText}</td>`;
  }
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return `<td class="squeeze-score"><span class="squeeze-bar" aria-hidden="true"><span style="width:${pct}%"></span></span><span class="squeeze-num">${value.toFixed(3)}</span></td>`;
}

// Truth layer chip (shared/truth-layer.js): the panel's existing state in the
// shared vocabulary, with the age of the last successful scan.
const SQUEEZE_TRUTH = { live: 'live', stale: 'stale', fallback: 'pending' };
function renderSqueezeTruth(state, note) {
  if (typeof describeTruth !== 'function') return;
  const finished = squeezePointer && Date.parse(squeezePointer.finished_at);
  renderTruthChip(document.getElementById('squeeze-truth'), describeTruth(SQUEEZE_TRUTH[state] || 'unavailable', {
    observedMs: Number.isFinite(finished) ? finished : (squeezePointer ? null : undefined),
    nowMs: squeezeNow(),
    note,
    why: 'Age is time since the last successful scan finished.',
  }));
}

function renderSqueezePanel() {
  const statusEl = document.getElementById('squeeze-status');
  const warningEl = document.getElementById('squeeze-warning');
  const tbody = document.getElementById('squeeze-tbody');

  if (squeezeError) {
    // Keep the last-rendered table rather than blanking it on a transient
    // fetch error -- the same "don't paint over good data" rule
    // renderBots' roster-error handling follows.
    statusEl.textContent = `scanner unavailable — ${squeezeError}` + (squeezePointer ? ' · showing last known' : ' · no scan available');
    statusEl.dataset.state = squeezePointer ? 'stale' : 'unavailable';
    renderSqueezeTruth(squeezePointer ? 'stale' : 'unavailable', 'scanner unreachable');
    if (!squeezePointer) tbody.innerHTML = '<tr><td colspan="6" class="squeeze-empty">Scan unavailable. Retry when the scanner can be reached.</td></tr>';
    return;
  }

  const status = formatSqueezeScheduleStatus(squeezePointer, squeezeAttemptPointer, squeezeSchedulePointer, squeezeCalendar, squeezeNow());
  statusEl.textContent = status.text;
  statusEl.dataset.state = status.state;

  // PR #105 review, finding 1: latest.json only advances on a successful
  // run, so a failed/partial attempt newer than the last success is
  // otherwise invisible -- the old successful table would keep looking
  // like nothing had gone wrong since.
  const failureNotice = status.warning;
  warningEl.textContent = failureNotice || '';
  warningEl.classList.toggle('hidden', !failureNotice);

  // Results from an earlier session must look old, not just be preceded by a
  // warning: the table is dimmed and captioned with its scan date.
  const currentDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(squeezeNow()));
  const scanDay = squeezePointer && squeezePointer.session_date;
  const staleResults = Boolean(scanDay && scanDay < currentDay);
  const tableEl = document.getElementById('squeeze-table');
  if (tableEl) tableEl.classList.toggle('squeeze-stale', staleResults);
  const captionEl = document.getElementById('squeeze-caption');
  if (captionEl) {
    captionEl.textContent = scanDay
      ? `${staleResults ? 'Earlier session — ' : ''}results from the ${scanDay} scan (New York)`
      : '';
  }

  renderSqueezeTruth(staleResults ? 'stale' : status.state, staleResults ? 'earlier session' : null);

  const rows = (squeezePointer && squeezePointer.candidates) || [];
  if (!rows.length) {
    tbody.innerHTML = squeezePointer ? '<tr><td colspan="6" class="squeeze-empty">No eligible candidates in the latest scan.</td></tr>' : '<tr><td colspan="6" class="squeeze-empty">No scan published yet.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(row => {
    const price = row.price != null ? `$${row.price.toFixed(2)}` : '—';
    // PR #105 review round 2: renders the producer's real first-seen
    // contract (short-squeeze-scanner#2) -- is_new alone decides the New
    // label; first_seen_at (NY time) is shown either way when known, null
    // renders "unavailable." formatSqueezeFirstSeen is a pure function of
    // the row's own data, not tracked state, so it stays stable across
    // any number of polls of the same run.
    const firstSeen = formatSqueezeFirstSeen(row);
    const firstSeenHtml = (firstSeen.isNew && squeezePointer.session_date === currentDay ? '<span class="squeeze-new-badge">New</span> ' : '') + escapeHtml(firstSeen.text);
    return `<tr>
      <td><a href="https://www.tradingview.com/symbols/${encodeURIComponent(row.ticker)}/" target="_blank" rel="noopener">${escapeHtml(row.ticker)}</a></td>
      <td>${price}</td>
      ${squeezeScoreCell(row.scores.factor, '—')}
      ${squeezeScoreCell(row.scores.options, 'unavailable')}
      ${squeezeScoreCell(row.scores.combined, '—')}
      <td class="squeeze-first-seen">${firstSeenHtml}</td>
    </tr>`;
  }).join('');
}
