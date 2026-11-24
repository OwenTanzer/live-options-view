// price strip — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// ── price strip ───────────────────────────────────────────────────────────────

const MAIN_TICKERS = ['QQQ', 'SPY', 'VIX', 'OVX', 'SMH', 'IGV', 'JPY/USD', 'KOSPI', 'USO', '10Y'];
const SMALL_TICKERS = ['AAPL', 'META', 'GOOGL', 'AMZN', 'TSLA', 'MU', 'SPCX', 'BTC/USD'];
const TICKER_CLASSES = {
  QQQ:'equity', SPY:'equity', VIX:'index', OVX:'index', SMH:'equity', IGV:'equity', 'JPY/USD':'futures',
  KOSPI:'international', USO:'equity', '10Y':'yield', AAPL:'equity', META:'equity',
  GOOGL:'equity', AMZN:'equity', TSLA:'equity', MU:'equity', SPCX:'equity',
  'BTC/USD':'crypto',
};
const tickerState = new TickerStateStore(TICKER_CLASSES, { staleAfterMs: SHARE_QUOTE_MAX_AGE_MS });
let tickerPoller = null;
let prevPrices = {};

// #179: one shared quote store, one panel and one refresh controller. The
// legacy tile lists stay for the compact compatibility tests; the real page
// renders the core/pins/movers panel from the same store.
let quoteStoreRef = null;
let quotePanelRef = null;
let quotePanelController = null;

function fmtPrice(v, sym, source) {
  if (v == null) return '—';
  if (sym === 'JPY/USD') return v > 0 ? '¥' + (1 / v).toFixed(2) : '—';
  if (sym === 'BTC/USD') return v > 1000 ? v.toLocaleString('en-US', {maximumFractionDigits: 0}) : v.toFixed(2);
  if (sym === 'VIX') return v.toFixed(2);
  // 10Y's raw scale is declared per source in market_data/overview/universe.json
  // (`scale`): Cboe TNX arrives x10 over DXLink and already in percent from
  // Yahoo. Normalizing by the source, not by magnitude, means a source switch
  // cannot be mistaken for a 10x yield move.
  if (sym === '10Y') return (source === 'dxlink' ? v / 10 : v).toFixed(3) + '%';
  return v.toFixed(2);
}

function buildTickerEl(sym, isSmall) {
  const el = document.createElement('div');
  el.className = 'ticker-btn' + (isSmall ? ' sm' : '');
  el.id = `tk-${sym.replace('/', '-')}`;
  // VWAP/RVOL/momentum only exist for QQQ (the collector's only underlying,
  // see collector.py's TICKER constant) -- VIX/SMH/etc. never carry this
  // data, so only QQQ's tile gets the extra lines.
  const vwapLine = sym === 'QQQ' ? '<span class="vwap-line"></span><span class="momentum-line"></span>' : '';
  el.innerHTML = `<span class="sym">${sym}</span><span class="px">—</span><span class="chg flat"></span><span class="market-state">${sym === 'SPY' ? 'unavailable' : 'fallback'}</span>${vwapLine}`;
  if (isTradeableShareSymbol(sym)) {
    el.classList.add('tradable');
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', `Trade ${sym} shares`);
    el.onclick = event => openShareTicket(event, sym);
    el.onkeydown = event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openShareTicket(event, sym);
      }
    };
  }
  return el;
}

function initPriceStrip() {
  // The accepted panel (#179): core markets, personal pins and session-aware
  // movers, all fed from the one prices.json store below.
  if (typeof initQuotePanel === 'function') {
    quoteStoreRef = new QuoteStore();
    quotePanelRef = initQuotePanel({
      documentRef: document,
      store: quoteStoreRef,
      pins: new QuotePins({ storage: safeStorage() }),
    });
  }
  // Legacy containers are optional: an older page can still build its tiles.
  const main = document.getElementById('px-main');
  const sec = document.getElementById('px-secondary');
  if (main) MAIN_TICKERS.forEach(s => main.appendChild(buildTickerEl(s, false)));
  if (sec) SMALL_TICKERS.forEach(s => sec.appendChild(buildTickerEl(s, true)));
}

function startQuotePanelRefresh() {
  if (!quotePanelRef || typeof QuotePanelController !== 'function') return null;
  quotePanelController = new QuotePanelController({
    refresh: fetchPrices,
    store: quoteStoreRef,
    panel: quotePanelRef,
    documentRef: document,
    windowRef: typeof window !== 'undefined' ? window : null,
  });
  quotePanelController.start({ immediate: false });
  return quotePanelController;
}

function updatePriceStrip(pricesData) {
  const all = [...MAIN_TICKERS, ...SMALL_TICKERS];
  for (const sym of all) {
    const d   = pricesData[sym];
    const el  = document.getElementById(`tk-${sym.replace('/', '-')}`);
    if (!el || !d) continue;

    const px = d.price;
    const pxEl  = el.querySelector('.px');
    const chgEl = el.querySelector('.chg');
    const stateEl = el.querySelector('.market-state');

    const prev = prevPrices[sym];
    if (px != null && prev != null && px !== prev) {
      el.classList.remove('flash-up', 'flash-down');
      void el.offsetWidth; // reflow to restart animation
      el.classList.add(px > prev ? 'flash-up' : 'flash-down');
    }
    prevPrices[sym] = px;

    pxEl.textContent = fmtPrice(px, sym, d.source);

    const chg = d.chg_pct;
    if (chg != null) {
      const sign = chg >= 0 ? '+' : '';
      chgEl.textContent = `${sign}${chg.toFixed(2)}%`;
      chgEl.className = `chg ${chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat'}`;
    } else {
      chgEl.textContent = '';
      chgEl.className = 'chg flat';
    }
    const state = d.state || 'fallback';
    el.dataset.state = state;
    stateEl.textContent = state;
    el.title = `${state} · ${d.source || 'unknown'} · ${d.observedAt || 'timestamp unavailable'}`;
  }
}

// Updates the QQQ tile's second line from renderHeatmap()'s 60s-cadence
// underlying_market read -- deliberately not driven by updatePriceStrip's
// 10s prices.json/live-quotes cadence, see docs/plans/2026-07-vwap-rvol.md.
function updateQqqVwapRvolLine(underlyingMarket) {
  const el = document.getElementById('tk-QQQ');
  if (!el) return;
  const lineEl = el.querySelector('.vwap-line');
  if (!lineEl) return;
  const vr = formatVwapRvol(underlyingMarket);
  lineEl.textContent = vr.text;
  lineEl.dataset.state = vr.state;
}

// Same 60s cadence and rationale as updateQqqVwapRvolLine above -- see
// docs/plans/2026-07-momentum-indicator.md.
function updateQqqMomentumLine(underlyingMarket) {
  const el = document.getElementById('tk-QQQ');
  if (!el) return;
  const lineEl = el.querySelector('.momentum-line');
  if (!lineEl) return;
  const mo = formatMomentum(underlyingMarket);
  lineEl.textContent = mo.text;
  lineEl.dataset.state = mo.state;
}

async function fetchPrices() {
  try {
    const resp = await fetch(`${BROWSER_DATA}/intraday/prices.json?_=${Date.now()}`);
    if (!resp.ok) return null;
    const data = await resp.json();
    const { SPY: spy, ...legacyPrices } = data.prices || {};
    tickerState.publishFallback(legacyPrices, data.timestamp || new Date().toISOString());
    // SPY has no yfinance or last-known substitute. Preserve its own provider
    // observation time so a cached price ages to stale instead of looking live.
    if (Number.isFinite(spy?.price) && spy.price > 0 && spy.quote_ts && spy.source === 'dxlink' &&
        (spy.symbol == null || spy.symbol === 'SPY')) {
      tickerState.publish([{ ...spy, symbol: 'SPY' }], { observedAt: spy.quote_ts });
    }
    updatePriceStrip(tickerState.snapshot());
    if (quoteStoreRef) {
      quoteStoreRef.mergePayload(data);
      if (quotePanelRef) quotePanelRef.render(quoteStoreRef.snapshot());
    }
    return data;
  } catch (e) {
    console.warn('prices.json fetch failed:', e);
    return null;
  }
}
