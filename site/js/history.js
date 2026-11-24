const el = id => document.getElementById(id);
const origin = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const market = el('market'), symbol = el('symbol'), series = el('series'), date = el('date');
const pageSize = 100;
const labels = {t:'Start (UTC)', event:'Available bar end (UTC)',open:'Open',high:'High',low:'Low',close:'Close',
  volume:'Volume',adj_close:'Adjusted close',adj_volume:'Adjusted volume',price:'Price',bars:'Source bars'};
let generation = 0, active = null, offset = 0, fileURL = null;
let cryptoCatalog, stockSymbols, entries = [];
const options = (select, pairs) => select.replaceChildren(...pairs.map(([title,value]) => new Option(title,value)));
function reset() {
  active = null;
  offset = 0;
  if (fileURL) URL.revokeObjectURL(fileURL);
  fileURL = null;
  el('download').hidden = true;
  el('auth').hidden = true;
  el('newer').disabled = el('older').disabled = true;
  el('head').replaceChildren();
  el('rows').replaceChildren();
  el('status').textContent = 'Loading archive…';
}
async function get(url) {
  const r = await fetch(url, {cache:'no-store'});
  if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), {status:r.status});
  return r;
}
function timestamp(row) { return typeof row.t === 'number' ? row.t : Date.parse(row.t); }
function render() {
  if (!active) return;
  const {rows, receipt} = active;
  const end = date.value ? Date.parse(date.value + 'T00:00:00Z') + 86400000 : Infinity;
  let lo = 0, hi = rows.length;
  while (lo < hi) {
    const mid = Math.floor((lo + hi)/2);
    if (timestamp(rows[mid]) < end) lo = mid+1; else hi = mid;
  }
  const last = Math.max(0,lo-offset), first = Math.max(0,last-pageSize);
  const page = rows.slice(first,last).reverse();
  const columns = Object.keys(labels).filter(k => page.some(row => row[k] !== undefined));
  function line(tag,values) {
    const tr=document.createElement('tr');
    for(const value of values){const cell=document.createElement(tag);cell.textContent=value;tr.append(cell);}
    return tr;
  }
  el('head').replaceChildren(line('th',columns.map(k=>labels[k])));
  el('rows').replaceChildren(...page.map(row=>line('td',columns.map(k=> {
    if (row[k] == null) return '—';
    return k==='t'||k==='event' ? new Date(row[k]).toISOString().replace('T',' ').slice(0,19) : String(row[k]);
  }))));
  el('newer').disabled=offset===0;
  el('older').disabled=first===0;
  const shown=last ? 'Showing '+(first+1).toLocaleString()+'–'+last.toLocaleString() : 'No records through this date';
  el('status').textContent=shown+' of '+rows.length.toLocaleString()+' records. Archive received '+receipt+'.';
}
async function load(level) {
  const id=++generation;
  reset();
  if(level==='market'){symbol.replaceChildren(); series.replaceChildren();}
  if(level==='symbol')series.replaceChildren();
  try {
    const cryptoMarket=market.value==='crypto';
    if(level==='market'){
      if(cryptoMarket) cryptoCatalog ??= await (await get('/data/crypto-history.json')).json();
      else stockSymbols ??= await (await get('/data/stock-history-symbols.json')).json();
      if(id!==generation)return;
      const names=cryptoMarket ? [...new Set(cryptoCatalog.coverage.map(x=>x.symbol))].sort() : stockSymbols;
      options(symbol,names.map(x=>[x,x]));
      if(cryptoMarket && names.includes('BTC'))symbol.value='BTC';
    }
    if(level!=='series'){
      if(cryptoMarket){
        entries=cryptoCatalog.coverage.filter(x=>x.symbol===symbol.value).map(x=>({
          label:x.interval+' · '+x.provider+' · '+(x.kind==='price_sample'?'price samples':'candles')+(x.primary?'':' (alternate)'),
          url:'/api/archive-history?'+new URLSearchParams({snapshot:cryptoCatalog.archive_sha256,symbol:x.symbol,interval:x.interval,provider:x.provider}),
          count:x.count,receipt:cryptoCatalog.available_at
        }));
      } else {
        const ticker=symbol.value;
        const manifest=await (await get(origin+'/nasdaq/candles/'+ticker+'/provenance.json')).json();
        if(id!==generation)return;
        if(manifest.symbol!==ticker || manifest.publication?.status!=='published')throw Error('Invalid stock receipt');
        entries=Object.entries(manifest.files).filter(([iv,f])=>f.available_at && /^[a-f0-9]{64}$/.test(f.sha256) &&
          f.version_key==='nasdaq/candles/'+ticker+'/versions/'+f.sha256+'.json').map(([iv,f])=>({
            label:iv,url:origin+'/'+f.version_key,count:f.bars,digest:f.sha256,receipt:f.available_at
          }));
      }
      options(series,entries.map((x,i)=>[x.label,String(i)]));
    }
    const entry=entries[Number(series.value)];
    if(!entry)throw Error('No published interval');
    const response=await get(entry.url);
    const bytes=await response.arrayBuffer();
    if(id!==generation)return;
    if(entry.digest){
      const hash=new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));
      if(Array.from(hash,b=>b.toString(16).padStart(2,'0')).join('')!==entry.digest)throw Error('Stock digest mismatch');
    }
    const payload=JSON.parse(new TextDecoder().decode(bytes)), rows=cryptoMarket?payload.rows:payload.bars;
    if(!Array.isArray(rows)||rows.length!==entry.count)throw Error('Archive count mismatch');
    if(id!==generation)return;
    active={rows,receipt:entry.receipt};
    fileURL=URL.createObjectURL(new Blob([bytes],{type:'application/json'}));
    const link=el('download');link.href=fileURL;link.download=symbol.value+'-'+entry.label.split(' · ').slice(0,2).join('-')+'.json';link.hidden=false;
    render();
  } catch(error){
    if(id!==generation)return;
    el('auth').hidden=error.status!==401;
    el('status').textContent=error.status===401?'Sign in to read this private series.':'Archive unavailable. '+error.message;
  }
}
market.onchange=()=>load('market');symbol.onchange=()=>load('symbol');series.onchange=()=>load('series');
date.oninput=()=>{offset=0;render();};
el('newer').onclick=()=>{offset=Math.max(0,offset-pageSize);render();};
el('older').onclick=()=>{offset+=pageSize;render();};
market.value=new URLSearchParams(location.search).get('market')==='crypto'?'crypto':'stocks';
load('market');
