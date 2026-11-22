const $ = id => document.getElementById(id);
const money = n => '$' + Number(n).toFixed(4);
const cents = n => n == null ? '—' : Number((n * 100).toFixed(2)) + '¢';
const PAGE = 12;
let catalog = [], positions = [], rows = [], page = 0, entries = false, enabled = false;
let generation = 0, book = null, quoteBusy = false, orderBusy = false, auto = true, lastActivity = Date.now();
const intents = new Map(), quantities = new Map();
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
async function api(path, body) {
  const r = await fetch('/api/kalshi/' + path, { credentials:'same-origin', cache:'no-store',
    ...(body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {}) });
  const d = await r.json();
  if (!r.ok) { if ([401,403].includes(r.status)) clearPrivate(); throw Error(d.error || 'Request failed'); }
  return d;
}
function clearPrivate() {
  generation++; enabled = false; catalog = []; positions = []; rows = []; book = null; intents.clear();
  $('workspace').hidden = true; $('login').hidden = false; $('detail').hidden = true;
  for (const id of ['markets','positions','fills','replay','archive-list','rules','receipt','cash','health']) $(id).replaceChildren();
}
async function run(fn) { try { await fn(); } catch (e) { $('status').textContent = e.message; } }
function held(market, outcome) {
  return positions.find(p => p.prediction.rules.market === market && p.prediction.outcome === outcome)?.pos || 0;
}
function filtered() {
  const term = $('search').value.toLowerCase();
  return catalog.filter(m => (!$('category').value || m.category === $('category').value) &&
    [m.title,m.outcome,m.category,m.ticker].join(' ').toLowerCase().includes(term));
}
function fresh() {
  const t = Date.parse(book?.provider_response_ts), received = Date.parse(book?.received_at);
  return !document.hidden && Number.isFinite(t) && Date.now() >= received && Date.now() - t <= 10000;
}
function paint() {
  const ok = fresh();
  for(const id of ['search','category','check','discover']) $(id).disabled=orderBusy;
  $('refresh').disabled=orderBusy || quoteBusy;
  $('prev').disabled=orderBusy || page===0;
  $('next').disabled=orderBusy || (page+1)*PAGE>=filtered().length;

  for (const row of rows) {
    const qty = Number(row.input.value), validQty = Number.isSafeInteger(qty) && qty > 0 && qty <= 10000;
    const open = Date.now() < Date.parse(row.market.cutoff);
    for (const control of row.controls) {
      const q = ok ? book.quotes[row.market.ticker]?.[control.outcome] : null;
      const price = q?.[control.side === 'buy' ? 'ask' : 'bid'];
      const size = q?.[control.side === 'buy' ? 'ask_size' : 'bid_size'] || 0;
      control.price.textContent = cents(price);
      control.size.textContent = size ? size.toLocaleString() + ' available' : 'No quote';
      control.button.disabled = orderBusy || !open || !validQty || price == null || size < qty ||
        (control.side === 'buy' ? !entries : held(row.market.ticker,control.outcome) < qty);
      control.button.setAttribute('aria-label', (control.side === 'buy' ? 'Buy ' : 'Sell ') + control.outcome + ' ' + row.market.title);
      if (control.side === 'sell' && !held(row.market.ticker,control.outcome)) control.size.textContent = 'None held';
    }
  }
  if (quoteBusy) $('clock').textContent = 'Updating prices…';
  else if (ok) $('clock').textContent = 'Book ' + new Date(book.provider_response_ts).toLocaleTimeString() + ' · auto refresh';
  else $('clock').textContent = auto ? 'Prices expired · refreshing…' : 'Prices paused · Refresh prices to resume';
}
function list() {
  generation++; book = null; quoteBusy = false;
  const all = filtered(); page = Math.max(0,Math.min(page,Math.ceil(all.length/PAGE)-1));
  const visible = all.slice(page*PAGE,(page+1)*PAGE); rows = []; $('markets').replaceChildren();
  $('count').textContent = all.length + ' contracts'; $('page').textContent = all.length ? (page*PAGE+1) + '–' + (page*PAGE+visible.length) + ' of ' + all.length : '0';
  $('prev').disabled = page === 0; $('next').disabled = (page+1)*PAGE >= all.length;
  if (!visible.length) $('markets').append(el('p','empty','No matching contracts. Try another search or update markets.'));
  for (const m of visible) {
    const node = el('article','contract'); node.dataset.market = m.ticker;
    const identity = el('div','identity'); identity.append(el('h3','',m.title+(m.outcome&&!m.title.includes(m.outcome)?' · '+m.outcome:'')));
    const meta = el('div','meta',m.category + ' · ' + new Date(m.cutoff).toLocaleDateString());
    const terms = el('button','terms','Terms'); terms.onclick = () => run(() => details(m)); meta.append(terms); identity.append(meta); node.append(identity);
    const label = el('label','quantity','Qty'); const input = el('input'); input.type='number'; input.min='1'; input.max='10000'; input.step='1';
    input.value=quantities.get(m.ticker) || '1'; input.setAttribute('aria-label','Quantity for '+m.title);
    input.oninput=()=>{ quantities.set(m.ticker,input.value); paint(); }; label.append(input);
    const row = {market:m,input,controls:[]};
    for (const outcome of ['YES','NO']) {
      const side = el('div','side '+outcome.toLowerCase()); const title = el('div','side-title');
      title.append(el('span','',outcome),el('span','',held(m.ticker,outcome) ? held(m.ticker,outcome)+' held' : '')); side.append(title);
      for (const action of ['buy','sell']) {
        const button=el('button','quote '+action), price=el('strong','','—'), size=el('small','','Loading');
        button.append(el('small','',action==='buy'?'BUY · ASK':'SELL · BID'),price,size); button.disabled=true;
        button.onclick=()=>run(()=>trade(row,outcome,action)); side.append(button);
        row.controls.push({button,price,size,outcome,side:action});
      }
      node.append(side);
    }
    node.append(label); rows.push(row); $('markets').append(node);
  }
  paint();
}
async function loadQuotes() {
  if (!enabled || document.hidden || quoteBusy || !rows.length || orderBusy) return;
  const g=generation, markets=rows.map(r=>r.market.ticker); quoteBusy=true; paint();
  try {
    const result=await api('quotes',{markets});
    if (g!==generation) return;
    book=result; auto=true; $('status').textContent='Private personal research enabled';
  } catch(e) {
    if(g===generation){ book=null; auto=false; $('status').textContent='Prices unavailable. '+e.message; }
  } finally { if(g===generation){ quoteBusy=false; paint(); } }
}
async function portfolio() {
  const g=generation, d=await api('portfolio'); if(g!==generation) return;
  positions=Object.values(d.positions).filter(p=>p.pos>0);
  $('cash').textContent=Number(d.balance_cash).toLocaleString('en-US',{style:'currency',currency:'USD'}); $('held').textContent=positions.reduce((sum,p)=>sum+p.pos,0);
  $('position-count').textContent='('+positions.length+')'; $('positions').replaceChildren();
  for(const p of positions) {
    const node=el('div','position'), text=el('div'); text.append(el('strong','',p.prediction.rules.title),
      el('p','',p.pos+' '+p.prediction.outcome+' held · average '+cents(p.avg)));
    const button=el('button','','Show in chain'); button.onclick=()=>run(async()=>{
      $('category').value=''; $('search').value=p.prediction.rules.market; page=0; list(); await loadQuotes(); $('markets').scrollIntoView({block:'start'});
    }); node.append(text,button); $('positions').append(node);
    if(!catalog.some(m=>m.ticker===p.prediction.rules.market)) catalog.push({
      ticker:p.prediction.rules.market,title:p.prediction.rules.title,category:p.prediction.rules.category,
      cutoff:p.prediction.rules.cutoff,outcome:p.prediction.outcome
    });
  }
  if(!positions.length) $('positions').append(el('p','empty','No open positions.'));
  $('fills').textContent=JSON.stringify(d.trades,null,2);
  $('health').textContent=d.health ? JSON.stringify(d.health) : '';
}
async function trade(row,outcome,side) {
  if(orderBusy) return;
  const control=row.controls.find(c=>c.outcome===outcome&&c.side===side); paint();
  if(control.button.disabled) return;
  const intent={market:row.market.ticker,outcome,side,qty:Number(row.input.value)}, key=JSON.stringify(intent);
  if(!intents.has(key)) intents.set(key,crypto.randomUUID());
  const g=generation; orderBusy=true; paint(); $('receipt').textContent='Placing paper '+side+'…';
  try {
    const d=await api('paper',{...intent,execution_request_id:intents.get(key)});
    if(g!==generation) return;
    intents.delete(key);
    $('receipt').textContent='Paper '+side+': '+d.trade.qty+' '+outcome+' at '+cents(d.trade.price)+
      ' · fees '+money(d.trade.fee_units/10000)+' · Receipt '+d.trade.execution_id;
    await portfolio();
    if(g===generation) list();
  } catch(e) {
    if(g===generation) $('receipt').textContent=e.message+' Retry the same action to reuse its receipt ID.';
    throw e;
  } finally { orderBusy=false; paint(); }
  await loadQuotes();
}
async function details(m) {
  const g=generation, d=await api('market',{market:m.ticker}); if(g!==generation) return;
  $('title').textContent=m.title+' · terms'; $('rules').textContent=JSON.stringify(d.rules,null,2);
  $('comparison').textContent=d.comparison; $('detail').hidden=false; $('detail').open=true;
  $('detail').scrollIntoView({behavior:'smooth',block:'start'});
}
function categories() {
  $('category').replaceChildren(new Option('All categories',''));
  for(const c of [...new Set(catalog.map(m=>m.category))].sort()) $('category').add(new Option(c,c));
}
async function check() {
  clearPrivate(); const g=generation,s=await api('status'); if(g!==generation) return;
  $('status').textContent=s.status; entries=s.entries; if(!s.enabled) return;
  enabled=true; auto=true; lastActivity=Date.now(); $('workspace').hidden=false; $('login').hidden=true;
  let d=await api('markets'); if(g!==generation) return;
  if(!d.markets.length) { d=await api('discover',{}); if(g!==generation) return; }
  catalog=d.markets; $('scope').textContent=d.scope; await portfolio(); if(g!==generation) return;
  categories(); page=0; list(); await loadQuotes();
}
function startLive() {
$('check').onclick=()=>run(check);
$('refresh').onclick=()=>{lastActivity=Date.now();auto=true;run(loadQuotes);};
$('discover').onclick=()=>run(async()=>{
  if(orderBusy) return;
  const g=generation,d=await api('discover',{}); if(g!==generation) return;
  catalog=d.markets; $('scope').textContent=d.scope; await portfolio(); if(g!==generation) return;
  categories(); page=0; list(); await loadQuotes();
});
let searchTimer;
function changePage() { page=0; list(); clearTimeout(searchTimer); searchTimer=setTimeout(()=>run(loadQuotes),300); }
$('search').oninput=changePage; $('category').onchange=changePage;
$('prev').onclick=()=>run(async()=>{page--;list();await loadQuotes();});
$('next').onclick=()=>run(async()=>{page++;list();await loadQuotes();});
$('archives').onclick=()=>run(async()=>{
  const g=generation,d=await api('archive'); if(g!==generation)return; $('archive-list').replaceChildren();
  for(const a of d.archives){const b=el('button','',a.received_at+' · '+a.market); b.onclick=()=>run(async()=>{
    const g=generation,r=await api('archive?id='+encodeURIComponent(a.id)); if(g===generation)$('replay').textContent='ARCHIVED OBSERVATION — not executable\n'+JSON.stringify(r.evidence,null,2);
  });$('archive-list').append(b);}
});
$('logout').onclick=()=>run(async()=>{
  clearPrivate(); await fetch('/credit-tape/auth/logout',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'manual'});
  const r=await fetch('/api/kalshi/status',{credentials:'same-origin',cache:'no-store'});
  if(r.status!==401)throw Error('Sign-out was not confirmed. Sign out from Credit Tape.');
  $('status').textContent='Signed out. Private records cleared from this page.';
});
for(const event of ['pointerdown','keydown'])document.addEventListener(event,()=>{lastActivity=Date.now();});
setInterval(()=>{if(Date.now()-lastActivity>300000)auto=false;paint();},500);
setInterval(()=>{if(auto&&enabled&&!document.hidden)run(loadQuotes);},10000);
document.addEventListener('visibilitychange',()=>{
  book=null; paint(); if(!document.hidden&&enabled){lastActivity=Date.now();auto=true;run(loadQuotes);}
});
window.addEventListener('pageshow',e=>{if(e.persisted)run(check);});
run(check);

}

function startReplay(params) {
  const cutoff=params.get('one_clock_at');
  if (params.getAll('one_clock_at').length!==1 || !/^[1-9][0-9]{12}$/.test(cutoff)) {
    $('status').textContent='Invalid replay cursor.';
    return;
  }
  const query='archive?one_clock_at='+cutoff;
  const login=$('login');
  login.target='_blank'; login.rel='noopener noreferrer';
  $('logout').hidden=true;
  let current=0, detail=0, next=null;
  function clear() {
    current++; detail++; next=null;
    $('frozen').hidden=true;
    $('frozen-list').replaceChildren();
    $('frozen-detail').replaceChildren();
    $('frozen-more').hidden=true;
  }
  async function page(more=false) {
    const continuation=next;
    if (!more) clear();
    const ticket=++current;
    $('frozen-more').hidden=true;
    try {
      const data=await api(query+(more&&continuation?'&'+new URLSearchParams(continuation):''));
      if(ticket!==current) return;
      login.hidden=true; $('frozen').hidden=false;
      $('frozen-time').textContent='As of '+new Date(Number(cutoff)).toISOString();
      $('status').textContent=data.archives.length?'Private archive':'No retained observations at this cursor.';
      for(const row of data.archives) {
        const button=el('button','',row.received_at+' · '+row.market);
        button.onclick=()=>run(async()=>{
          const read=++detail;
          $('frozen-detail').textContent='';
          try {
            const result=await api(query+'&id='+encodeURIComponent(row.id));
            if(read===detail) $('frozen-detail').textContent=JSON.stringify(result.evidence,null,2);
          } catch(error) { if(read===detail){clear();login.hidden=false;throw error;} }
        });
        $('frozen-list').append(button);
      }
      next=data.next; $('frozen-more').hidden=!next;
    } catch(error) {
      if(ticket===current) {clear(); login.hidden=false; throw error;}
    }
  }
  $('check').onclick=()=>run(()=>page());
  $('frozen-more').onclick=()=>run(()=>page(true));
  window.addEventListener('pagehide',clear);
  window.addEventListener('pageshow',event=>{if(event.persisted) run(()=>page());});
  document.addEventListener('visibilitychange',()=>{if(document.hidden) clear(); else run(()=>page());});
  run(()=>page());
}
const replayParams=new URLSearchParams(location.search);
if(replayParams.has('one_clock_at')) startReplay(replayParams); else startLive();
