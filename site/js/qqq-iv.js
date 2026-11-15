// QQQ same-day chain readings, in annualized volatility percentage points.
const qqqIv = (() => {
  const panel = document.getElementById('qqq-iv');
  const chart = panel.querySelector('canvas');
  const caption = panel.querySelector('small');
  const stamp = panel.querySelector('time');
  const et = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  const clock = new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  const day = new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'});
  const buttons = [...panel.querySelectorAll('button')];
  let points = [], active = 0, pinned = false;
  function median(values) {
    values.sort((a,b)=>a-b);
    const mid = Math.floor(values.length/2);
    return values.length ? (values[mid]+values[Math.floor((values.length-1)/2)])/2 : NaN;
  }
  function update(data) {
    const time = Date.parse(data.timestamp);
    if (!Number.isFinite(time) || data.date !== data.expiration || day.format(time) !== data.date ||
        clock.format(time) < '09:30' || clock.format(time) >= '16:00') return;
    const rows = data.rows.filter(r=>r.Expiration===data.date && Number.isFinite(r.IV) && r.IV>0 &&
      Number.isFinite(r.Delta) && Number.isFinite(r.Bid) && Number.isFinite(r.Ask) &&
      r.Bid>0 && r.Ask>=r.Bid && 2*(r.Ask-r.Bid)/(r.Ask+r.Bid)<=0.25);
    function leg(type, low, high) {
      return median(rows.filter(r=>r.Type===type && Math.abs(r.Delta)>=low && Math.abs(r.Delta)<=high).map(r=>100*r.IV));
    }
    const iv=(leg('call',.35,.65)+leg('put',.35,.65))/2, skew=leg('put',.15,.35)-leg('call',.15,.35);
    if (!Number.isFinite(iv) && !Number.isFinite(skew)) return;
    if (points.length && time<=points.at(-1)[0]) return;
    if (points.length && day.format(points.at(-1)[0])!==data.date) points=[];
    points.push([time, Number.isFinite(iv)?iv:null, Number.isFinite(skew)?skew:null]);
    points=points.slice(-400);
    try { localStorage.setItem('qqq-iv-series-v1',JSON.stringify(points)); } catch {}
    render();
  }
  function load(series) {
    if (!Array.isArray(series)) return;
    const valid=series.filter(p=>Array.isArray(p) && p.length===3 && Number.isFinite(p[0]) && Math.abs(p[0])<8.64e15 &&
      (Number.isFinite(p[1]) || Number.isFinite(p[2]))).slice(-400);
    if (valid.length && (!points.length || valid.at(-1)[0]>points.at(-1)[0])) { points=valid; render(); }
  }
  function render() {
    if (!points.length) return;
    const last=points.at(-1);
    stamp.textContent=et.format(last[0])+' ET';
    buttons.forEach((b,i)=>{
      const v=last[i+1];
      b.querySelector('b').textContent=Number.isFinite(v)?(i===1 && v>=0?'+':'')+v.toFixed(2)+(i===0?'%':' pts'):'\u2014';
      if (i===0) b.dataset.band=Number.isFinite(v)?(v<15?'low':v>=30?'elevated':'standard'):'';
    });
    const values=points.filter(p=>Number.isFinite(p[active+1]));
    caption.textContent=(active===0?'Near-the-money IV. Heuristic bands: low below 15%, elevated from 30%.':'Put IV minus call IV, matched at 15\u201335 absolute delta.')+' '+(values.length?et.format(values[0][0])+' to '+clock.format(values.at(-1)[0])+' ET.':'No qualifying pairs.');
    const ctx=chart.getContext('2d'); ctx.clearRect(0,0,chart.width,chart.height);
    if (!values.length) return;
    const ys=values.map(p=>p[active+1]), lo=Math.min(...ys),hi=Math.max(...ys);
    const first=points[0][0],span=Math.max(1,last[0]-first);
    ctx.font='12px sans-serif';ctx.fillStyle='#a3adb7';
    ctx.fillText(hi.toFixed(1),1,13); if(hi!==lo)ctx.fillText(lo.toFixed(1),1,74);
    ctx.strokeStyle=ctx.fillStyle='#79c0ff';ctx.lineWidth=2;ctx.beginPath();let connected=false;
    for (const p of points) {
      const v=p[active+1]; if (!Number.isFinite(v)) {connected=false;continue;}
      const x=40+238*(p[0]-first)/span, y=hi===lo?40:70-60*(v-lo)/(hi-lo);
      if (connected) ctx.lineTo(x,y); else ctx.moveTo(x,y);
      ctx.fillRect(x-1,y-1,2,2);connected=true;
    }
    ctx.stroke();
  }
  const pop=panel.querySelector('.iv-pop');
  function show(i) {active=i;pop.hidden=false; buttons.forEach((b,j)=>b.setAttribute('aria-expanded',String(j===i))); render();}
  function close() {pinned=false;pop.hidden=true;buttons.forEach(b=>b.setAttribute('aria-expanded','false'));}
  buttons.forEach((b,i)=>{
    b.onpointerenter=e=>{if(e.pointerType==='mouse' && !pinned)show(i);};
    b.onclick=()=>{if(pinned && active===i)close();else{pinned=true;show(i);}};
  });
  panel.onpointerleave=()=>{if(!pinned)close();};
  document.addEventListener('pointerdown',e=>{if(!panel.contains(e.target))close();});
  panel.onkeydown=e=>{if(e.key==='Escape')close();};
  try {load(JSON.parse(localStorage.getItem('qqq-iv-series-v1')));} catch {}
  fetch('iv-replay.json').then(r=>r.ok?r.json():[]).then(load).catch(()=>{});
  return {update, select(symbol){panel.hidden=symbol!=='QQQ';if(panel.hidden)close();}};
})();
