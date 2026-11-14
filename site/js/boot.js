// boot — extracted from docs/index.html (B2, readability quest, Nov 2026).
// Classic script, loaded by index.html in the same position the inline
// section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion. This is the last inline
// section: after this slice, the only inline script left in the page is
// the stale-page protection (BUILD_ID must stay in index.html itself —
// stamp-build.mjs requires exactly one unstamped declaration there).
// Boot is an async IIFE that runs at load; everything it calls is defined
// by the earlier sections (paper trading declares the shared scope it
// reads: session, liveQuotes, lastData, chainSelection, ticketEl), so the
// move is position-preserving and load-order neutral.
// ── boot ──────────────────────────────────────────────────────────────────────
(async function boot() {
  initPriceStrip();
  renderChainTitle(chainSelection.symbol);
  document.getElementById('underlying-select').onchange = (e) => selectUnderlying(e.target.value);

  // Show a cached last-known session instantly (display-only), then
  // hydrate the authoritative state from the server below.
  session = readSessionDisplayCache();
  renderAuthGate();
  if (session) renderPaper();

  document.getElementById('auth-form').onsubmit = async (e) => {
    e.preventDefault();
    const username = document.getElementById('auth-username').value.trim();
    const password = document.getElementById('auth-password').value;
    const errorEl = document.getElementById('auth-error');
    const submitBtn = document.getElementById('auth-submit');
    errorEl.textContent = '';
    submitBtn.disabled = true;
    try {
      session = await submitAuth(username, password);
      cacheSessionDisplay();
      document.getElementById('auth-form').reset();
      renderAuthGate();
      updateQuoteInterests();
      renderPaper();
      renderSelectedChain();
    } catch (error) {
      errorEl.textContent = error.message;
    } finally {
      submitBtn.disabled = false;
    }
  };
  document.getElementById('auth-toggle').onclick = () => {
    authMode = authMode === 'login' ? 'register' : 'login';
    renderAuthGate();
  };
  document.getElementById('auth-logout').onclick = async () => {
    try { await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); } catch (e) {}
    session = null;
    try { localStorage.removeItem(SESSION_CACHE_KEY); } catch (e) {}
    renderAuthGate();
    renderSelectedChain();
  };
  document.getElementById('margin-call-continue').onclick = hideMarginCallScreen;

  document.getElementById('tab-live').onclick    = () => showTab('live');
  document.getElementById('tab-history').onclick = () => showTab('history');
  document.getElementById('tab-bots').onclick    = () => showTab('bots');
  document.getElementById('tab-squeeze').onclick = () => showTab('squeeze');
  document.getElementById('tab-news').onclick    = () => showTab('news');
  document.getElementById('hist-date').onchange  = (e) => loadHistoryDate(e.target.value);

  setStatus('', 'Loading calibration data…');
  await loadRanges();

  setStatus('', 'Fetching live snapshot…');
  await Promise.all([fetchLatest(), fetchPrices(), fetchSpyHealth(), refreshSessionFromServer()]);
  renderAuthGate();
  updateQuoteInterests();
  if (session) renderPaper();
  renderSelectedChain();

  liveQuotePoller = new LiveQuotePoller(liveQuotes);
  liveQuotePoller.subscribeHealth(renderQuoteHealth);
  liveQuotePoller.start();
  tickerState.subscribe(() => {
    updatePriceStrip(tickerState.snapshot());
    if (session) renderPaper();
  });
  updatePriceStrip(tickerState.snapshot());
  tickerPoller = new LiveQuotePoller(tickerState, { maxQuoteAgeMs: SHARE_QUOTE_MAX_AGE_MS });
  tickerPoller.subscribeHealth(health => renderTransportHealth('ticker-health', 'Tickers', health));
  tickerPoller.start();

  // OI heatmap: 60s refresh cycle
  startCountdown();

  // prices.json is owned by the quote panel's one refresh controller (#179):
  // it refreshes on a bounded visible/hidden cadence, immediately on foreground
  // return, and backs off after failures without dropping last values.
  if (typeof startQuotePanelRefresh === 'function') startQuotePanelRefresh();
  setInterval(fetchSpyHealth, 15_000);

  // eia_steo.json updates server-side every few hours at most (see
  // collector.py's EIA_STEO_POLL_SECS) -- polling once a minute is just
  // "pick up a change reasonably soon," not matched to its real cadence.
  fetchSteoCalibration();
  setInterval(fetchSteoCalibration, 60_000);

  // Short-squeeze scanner panel (OA-191) -- an independent producer, polled
  // the same "notice it eventually" way as eia_steo.json above.
  fetchSqueezeLatest();
  setInterval(fetchSqueezeLatest, SQUEEZE_POLL_MS);

  // Calibration data has no periodic retry of its own — safety net in case the
  // visibility handler above never fires (e.g. the tab was never backgrounded,
  // just had one bad fetch at boot).
  setInterval(() => { if (!ranges) loadRanges(); }, 60_000);
})();
