function renderHeatmap(data, symbol = 'QQQ') {
  const { underlying_price, tier, snapshot_time, underlying_market } = data;
  const spot = underlying_price;
  const isQqq = symbol === 'QQQ';

  if (!spot) {
    setStatus('stale', snapshot_time);
    // A non-QQQ chain without its own spot is cleared, not left showing rows
    // it can't center -- and never filled with another underlying's price.
    if (!isQqq) clearChain(`${symbol} spot price unavailable in the latest snapshot.`);
    return;
  }

  const book = computeBook();   // computed once per render, not per-cell
  // Paper tickets, positions and QQQ's OIranges calibration are QQQ-only:
  // other underlyings render view-only, colored relative to their own
  // snapshot (see relativeOiRanges in docs/shared/chain.js).
  buildHeatmapRows(document.getElementById('heatmap-body'), data, isQqq ? ranges : relativeOiRanges(data.rows), {
    ...(isQqq?{openingOi:data.openingOi}:{}),
    getPosition: (sym) => book[sym]?.pos || 0,
    getQuote: (sym) => liveQuotes.get(sym),
    onCellClick: (e, row) => openTicket(e, row),
  });
  document.getElementById('chain-note').textContent = isQqq ? (typeof openingOiNote==='function'?openingOiNote(data.openingOi):'Opening OI unavailable') : 'OI colors relative to this snapshot · execution requires a fresh contract quote';

  // Header
  const priceEl = document.getElementById('price');
  priceEl.textContent = `$${spot?.toFixed(2) ?? '—'}`;
  // #178: the header price is the CHAIN SNAPSHOT's spot, not a live quote, and
  // it used to carry no age at all -- a chain frozen at 16:14 ET read as
  // current next to advancing Core Markets cards. Say which snapshot this is,
  // which session it belongs to, how old it is, and mark it retained when
  // collection has stopped. formatChainSnapshot judges the session from the
  // producer's ET stamp and the age from the same instant the status dot uses.
  const freshness = formatChainSnapshot(data);
  priceEl.dataset.state = freshness.state;
  priceEl.title = freshness.detail;
  const priceAgeEl = document.getElementById('price-age');
  priceAgeEl.textContent = freshness.text;
  priceAgeEl.dataset.state = freshness.state;
  priceAgeEl.title = freshness.detail;

  const tierBadge = document.getElementById('tier-badge');
  // "Regular" alone is the 0DTE_Regular EXPIRY TIER, and beside a price it
  // reads as a market session -- precisely the #178 misreading. Keep the tier
  // name available, but label it as a tier; the session is the line above.
  const tierLabel = (tier || '').replace('0DTE_', '');
  tierBadge.textContent = tierLabel ? `${tierLabel} tier` : '';
  tierBadge.title = tierLabel ? `Expiration tier ${tier} — not a market session` : '';
  tierBadge.style.color  = TIER_COLORS[tier] || '#c9d1d9';
  tierBadge.style.display = '';

  // VWAP/RVOL -- same underlying_market block the QQQ tile's second line
  // reads (updateQqqVwapRvolLine below); both driven by this one 60s-cadence
  // render, not the 30s prices.json / live-quotes cadence (see
  // docs/plans/2026-07-vwap-rvol.md for why).
  const vr = formatVwapRvol(underlying_market);
  const vrEl = document.getElementById('vwap-rvol');
  vrEl.textContent = vr.text;
  vrEl.dataset.state = vr.state;

  const mo = formatMomentum(underlying_market);
  const moEl = document.getElementById('momentum');
  moEl.textContent = mo.text;
  moEl.dataset.state = mo.state;

  document.getElementById('no-data').classList.add('hidden');
  document.getElementById('heatmap-wrap').classList.remove('hidden');
}

