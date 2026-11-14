// data fetch cycle — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// ── data fetch cycle ──────────────────────────────────────────────────────────
// One fetch of one underlying's latest snapshot, validated against the
// requested symbol before anything can render it.
async function fetchChain(symbol) {
  const path = chainLatestPath(symbol);
  if (!path) return { error: `${symbol} is not a supported underlying` };
  try {
    const resp = await fetch(`${BROWSER_DATA}/${path}?_=${Date.now()}`);
    if (resp.status === 404) return { error: `No ${symbol} snapshot has been published yet.` };
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const check = validateChainPayload(data, symbol);
    if (!check.ok) return { error: `${symbol} snapshot rejected: ${check.reason}.` };
    return { data };
  } catch (e) {
    console.error(e);
    return { error: `${symbol} snapshot could not be loaded.`, transient: true };
  }
}

let latestQqqRequest = 0;
let latestSelectedRequest = 0;
async function fetchLatest() {
  const token = chainSelection.token();
  // QQQ is always refreshed: paper trading, expiry settlement marks and the
  // QQQ price tile depend on it regardless of which chain is on screen.
  const qqqRequest = ++latestQqqRequest;
  // Same-selection overlap guard: a second fetchLatest (timer, re-select)
  // supersedes the first for the selected chain too, so a slow reply for
  // the same selection cannot overwrite fresher data that already landed.
  const selectedRequest = ++latestSelectedRequest;
  const qqq = await fetchChain('QQQ');
  // QQQ refreshes independently of the selected chain, but an older refresh
  // must not replace newer paper-trading, indicator or cached chain state.
  if (qqqRequest !== latestQqqRequest) return;
  if (qqq.data && qqq.data.rows.length) {
    lastTs = qqq.data.timestamp;
    lastData = qqq.data;
    chainState.QQQ = { data: qqq.data, error: null };
    ingestSnapshotQuotes(qqq.data);
    updateQqqVwapRvolLine(qqq.data.underlying_market);
    updateQqqMomentumLine(qqq.data.underlying_market);
    // OI skew meter (js/oi-skew-meter.js): same 60s QQQ payload, no extra request.
    if (typeof updateOiSkewMeter === 'function') updateOiSkewMeter(qqq.data);
  } else {
    chainState.QQQ = { data: chainState.QQQ?.data ?? null, error: qqq.error || 'No QQQ rows in the latest snapshot.' };
    // A failed/empty refresh must not leave the OI skew meter labelled live.
    if (typeof noteOiSkewRefreshFailure === 'function') noteOiSkewRefreshFailure(chainState.QQQ.error);
  }

  if (token.symbol !== 'QQQ') {
    const other = await fetchChain(token.symbol);
    // The selection changed while this was in flight: drop the reply rather
    // than render (or cache) it under whatever is selected now. A newer
    // fetchLatest for the same selection supersedes this one too.
    if (selectedRequest !== latestSelectedRequest) return;
    if (!chainSelection.accepts(token)) return;
    if (other.data) {
      chainState[token.symbol] = { data: other.data, error: null };
      // Interests first: the quote cache only keeps wanted (visible) symbols.
      updateQuoteInterests();
      ingestSnapshotQuotes(other.data);
    } else {
      chainState[token.symbol] = {
        data: other.transient ? chainState[token.symbol]?.data ?? null : null,
        error: other.error,
      };
    }
  }
  if (!chainSelection.accepts(token)) return;
  updateQuoteInterests();
  renderSelectedChain();
  renderPaper();
}

function selectedChainData() {
  return chainState[chainSelection.symbol]?.data ?? null;
}

function clearChain(message) {
  document.getElementById('heatmap-body').innerHTML = '';
  document.getElementById('heatmap-wrap').classList.add('hidden');
  const noData = document.getElementById('no-data');
  noData.textContent = message;
  noData.classList.remove('hidden');
  for (const id of ['vwap-rvol', 'momentum', 'chain-note']) document.getElementById(id).textContent = '';
  const price = document.getElementById('price');
  price.textContent = '—';
  price.title = '';
  price.dataset.state = '';
  // The snapshot line belongs to the snapshot that just went away; leaving it
  // behind would label the NEXT underlying's empty chain with the old one's
  // time and age.
  const priceAge = document.getElementById('price-age');
  priceAge.textContent = '';
  priceAge.title = '';
  priceAge.dataset.state = '';
  document.getElementById('tier-badge').style.display = 'none';
}

function renderChainTitle(symbol) {
  const data = chainState[symbol]?.data;
  const expiry = data?.expiration;
  document.getElementById('title').textContent = `${symbol} ${expiry ? (expiry === data.date ? '0DTE' : `exp ${expiry}`) : 'Options'} Live OI`;
  document.title = `${symbol} Live OI — moopertonic`;
  const select = document.getElementById('underlying-select');
  if (select.value !== symbol) select.value = symbol;
}

// Renders whichever underlying is selected, from that underlying's own
// validated snapshot only. Each symbol's stale/unavailable state is its own.
function renderSelectedChain() {
  const symbol = chainSelection.symbol;
  renderChainTitle(symbol);
  const state = chainState[symbol];
  const data = state?.data;
  if (!data) {
    clearChain(state?.error || (symbol === 'QQQ' ? NO_DATA_TEXT : `Loading ${symbol}…`));
    setStatus(state?.error ? 'stale' : '', state?.error ? 'unavailable' : 'Loading…');
    return;
  }
  if (!data.rows.length) {
    clearChain(`No ${symbol} contracts in the latest snapshot.`);
    setStatus('stale', data.snapshot_time || '—');
    return;
  }
  renderHeatmap(data, symbol);
  if (!data.underlying_price) return;  // renderHeatmap already marked it stale
  const hasBids = data.rows.some(r => r.Bid != null);
  // Dot color carries live/stale; text is just a small timestamp note —
  // deeper diagnosis lives in diag.html, not the main view. The freshness
  // judgment is the same call the header's snapshot line uses (#178), so the
  // dot and the age text can never disagree about a chain that has stopped.
  const freshness = formatChainSnapshot(data);
  setStatus(freshness.state === 'live' && hasBids && !state.error ? 'live' : 'stale', data.snapshot_time);
}

function selectUnderlying(symbol) {
  if (!chainSelection.select(symbol)) return;
  if (typeof closeTicket === 'function') closeTicket();
  try { localStorage.setItem(UNDERLYING_KEY, chainSelection.symbol); } catch (e) {}
  // Clear immediately so nothing from the previous underlying stays on screen
  // under the new heading, then fetch the new one right away.
  clearChain(`Loading ${chainSelection.symbol}…`);
  renderSelectedChain();
  updateQuoteInterests();
  fetchLatest();
  startCountdown();
}

function setStatus(type, text) {
  const dot  = document.getElementById('status-dot');
  const span = document.getElementById('status-text');
  dot.className  = type;  // 'live' | 'stale' | ''
  span.textContent = text;
}
