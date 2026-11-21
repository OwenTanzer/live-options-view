function closeTicket() {
  if (ticketEl) { ticketEl.remove(); ticketEl = null; }
}
document.addEventListener('click', (e) => {
  if (ticketEl && !ticketEl.contains(e.target)) closeTicket();
});

function openTicket(e, row) {
  e.stopPropagation();
  closeTicket();
  const sym  = row.OptionSymbol;
  const btc = row.instrument_type === 'futures_option';
  const q = (btc ? btcOptions.quotes.get(sym) : liveQuotes.get(sym)) || {};
  const info = {
    sym, instrument_type: btc ? 'futures_option' : 'option', contract: row.contract,
    strike: +row.Strike, type: row.Type, exp: row.Expiration,
  };
  const fresh = stamp => Number.isFinite(Date.parse(stamp)) && Date.now() - Date.parse(stamp) <= 15000 && Date.now() - Date.parse(stamp) >= -30000;
  const buyPx  = fresh(q.ask_ts) ? q.ask : null;
  const sellPx = fresh(q.bid_ts) ? q.bid : null;

  const t = document.createElement('div');
  t.id = 'trade-ticket';
  if (btc) t.dataset.btc = sym;
  t.innerHTML = `
    <div class="t-head">${instrumentLabel(info)} · ${q.bid != null ? fmtQ(q.bid) : '—'} × ${q.ask != null ? fmtQ(q.ask) : '—'}</div>
    <div class="t-row t-order-row">
      <select id="t-order-type" aria-label="order type">
        <option value="market">Market</option>
        <option value="limit">Limit</option>
      </select>
      <label class="t-limit hidden">Limit $ <input type="number" min="0.01" max="1000000" step="0.01" id="t-limit" aria-label="limit price"></label>
    </div>
    <div class="t-row">
      <input type="number" min="1" step="1" value="1" id="t-qty" aria-label="quantity">
      <button class="btn-buy"  ${buyPx  == null ? 'disabled' : ''}>Buy @ ${buyPx  != null ? fmtQ(buyPx)  : '—'}</button>
      <button class="btn-sell" ${sellPx == null ? 'disabled' : ''}>Sell @ ${sellPx != null ? fmtQ(sellPx) : '—'}</button>
    </div>`;
  document.body.appendChild(t);

  const r = e.currentTarget.getBoundingClientRect();
  const maxLeft = window.scrollX + document.documentElement.clientWidth - t.offsetWidth - 12;
  t.style.left = Math.max(4, Math.min(r.left + window.scrollX, maxLeft)) + 'px';
  t.style.top  = (r.bottom + window.scrollY + 4) + 'px';

  const qtyEl = t.querySelector('#t-qty');
  if (btc) { qtyEl.value = '1'; qtyEl.disabled = true; t.querySelector('.t-head').textContent += ' · Simulated · $2.50 fee per side'; }
  const orderTypeEl = t.querySelector('#t-order-type');
  const limitWrap = t.querySelector('.t-limit');
  const limitEl = t.querySelector('#t-limit');
  orderTypeEl.onchange = () => {
    const isLimit = orderTypeEl.value === 'limit';
    limitWrap.classList.toggle('hidden', !isLimit);
    if (!isLimit) limitEl.value = '';
  };
  t.querySelector('.btn-buy').onclick  = () => executeTrade(info, 'buy', qtyEl.value, orderTypeEl.value, limitEl.value);
  t.querySelector('.btn-sell').onclick = () => executeTrade(info, 'sell', qtyEl.value, orderTypeEl.value, limitEl.value);
  ticketEl = t;
}

function openShareTicket(e, sym) {
  e.stopPropagation();
  closeTicket();
  const q = tickerState.get(sym) || {};
  const quote = freshShareQuote(q);
  const info = { sym, instrument_type: 'share' };
  const held = Math.max(0, computeBook()[sym]?.pos ?? 0);
  const buyPx = quote?.ask ?? null;
  const sellPx = quote?.bid ?? null;

  const t = document.createElement('div');
  t.id = 'trade-ticket';
  t.innerHTML = `
    <div class="t-head">${sym} shares · ${q.bid != null ? fmtQ(q.bid) : '—'} × ${q.ask != null ? fmtQ(q.ask) : '—'}</div>
    <div class="t-row t-order-row">
      <select id="t-order-type" aria-label="share order type">
        <option value="market">Market</option>
        <option value="limit">Limit</option>
      </select>
      <label class="t-limit hidden">Limit $ <input type="number" min="0.01" max="1000000" step="0.01" id="t-limit" aria-label="share limit price"></label>
    </div>
    <div class="t-row">
      <input type="number" min="1" step="1" value="1" id="t-qty" aria-label="share quantity">
      <button class="btn-buy" ${buyPx == null ? 'disabled' : ''}>Buy market</button>
      <button class="btn-sell" ${sellPx == null || held === 0 ? 'disabled' : ''}>Sell market</button>
    </div>`;
  document.body.appendChild(t);

  const r = e.currentTarget.getBoundingClientRect();
  const maxLeft = window.scrollX + document.documentElement.clientWidth - t.offsetWidth - 12;
  t.style.left = Math.max(4, Math.min(r.left + window.scrollX, maxLeft)) + 'px';
  t.style.top = (r.bottom + window.scrollY + 4) + 'px';

  const qtyEl = t.querySelector('#t-qty');
  const orderTypeEl = t.querySelector('#t-order-type');
  const limitWrap = t.querySelector('.t-limit');
  const limitEl = t.querySelector('#t-limit');
  const buyButton = t.querySelector('.btn-buy');
  const sellButton = t.querySelector('.btn-sell');
  orderTypeEl.onchange = () => {
    const isLimit = orderTypeEl.value === 'limit';
    limitWrap.classList.toggle('hidden', !isLimit);
    buyButton.textContent = isLimit ? 'Buy limit' : 'Buy market';
    sellButton.textContent = isLimit ? 'Sell limit' : 'Sell market';
    if (!isLimit) limitEl.value = '';
  };
  const submit = side => {
    const normalized = normalizeShareOrder({
      side,
      quantity: qtyEl.value,
      heldQuantity: held,
      orderType: orderTypeEl.value,
      limitPrice: limitEl.value,
    });
    if (normalized.error) {
      setTradePending(false, normalized.error);
      announceOrderStatus(normalized.error, 'error');
      return;
    }
    executeTrade(
      info, side, normalized.value.qty,
      normalized.value.order_type, normalized.value.limit_price,
    );
  };
  buyButton.onclick = () => submit('buy');
  sellButton.onclick = () => submit('sell');
  ticketEl = t;
}
