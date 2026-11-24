import {model,receiptMinute,flowResponse,alkalusFlow} from '../shared/intraminute.js';

const $=id=>document.getElementById(id);
const sample=1794580560000, api='/api/one-clock/intraminute';
const time=at=>new Date(at).toISOString().slice(11,23);
const params=new URLSearchParams(location.search);
let minute=sample, rows=[], generation=0, timer, playback, live=false, data=null;
let detailOffset=0;
let selected=params.has('strike')?Number(params.get('strike')):null;
const isoMinute=at=>new Date(at).toISOString().slice(0,16);

function reset() {
  generation++; clearTimeout(timer); clearInterval(playback); playback=null; live=false;
  rows=[]; $('play').textContent='Play replay'; $('live').disabled=false;
}
function strikes() {
  const cutoff=minute+Number($('cursor').value);
  const available=new Set(rows.filter(r=>r.kind==='option' && r.at<=cutoff && r.at>=minute).map(r=>r.strike));
  if(selected!==null && Number.isFinite(selected)) available.add(selected);
  const values=[...available].sort((a,b)=>a-b);
  if($('strike').dataset.values===values.join(','))return;
  $('strike').dataset.values=values.join(',');
  $('strike').replaceChildren(new Option('All strikes',''),...values.map(v=>new Option('$'+v,String(v))));
  $('strike').value=selected===null?'':String(selected);
}
function paint() {
  const width=Number($('width').value), cutoff=minute+Number($('cursor').value);
  const result=model(rows,{minute,cutoff,width,strike:selected});
  data=result;strikes();
  const {bins,gaps}=result;
  const canvas=$('plot'), w=Math.max(320,canvas.clientWidth), h=390, scale=devicePixelRatio||1;
  canvas.width=w*scale; canvas.height=h*scale;
  const c=canvas.getContext('2d'); c.scale(scale,scale);
  const left=60,right=w-15,span=right-left;
  const x=i=>left+span*i/bins.length;
  c.font='12px system-ui';
  function label(s,xx,yy){c.fillStyle='#d4dee4';c.fillText(s,xx,yy);}
  function line(x1,y1,x2,y2,color) {c.strokeStyle=color;c.beginPath();c.moveTo(x1,y1);c.lineTo(x2,y2);c.stroke();}
  for(const [start,end] of gaps) {
    const a=Math.max(left,x((start-minute)/width)), b=Math.min(right,x((end-minute)/width));
    if(b<=a)continue;
    c.fillStyle='#48404a';c.fillRect(a,0,b-a,365);
    c.save();c.beginPath();c.rect(a,0,b-a,365);c.clip();
    for(let i=a-360;i<b;i+=14)line(i,365,i+365,0,'#625261');c.restore();
  }
  const max=Math.max(1,...bins.flatMap(b=>[...b.C,...b.P]));
  for(const [kind,title,base] of [['C','Calls',55],['P','Puts',139]]) {
    label(title,3,base-12);label(String(max),3,base+5);
    line(left,base,right,base,'#63717c');
    bins.forEach((b,i)=>{
      const [buy,sell,unknown]=b[kind], bw=Math.max(.7,span/bins.length);
      c.fillStyle='#46bea0'; c.fillRect(x(i),base-buy/max*32,bw,buy/max*32);
      c.fillStyle='#f39785'; c.fillRect(x(i),base,bw,sell/max*32);
      if(unknown){c.fillStyle='#a1aaba';c.fillRect(x(i),base+40-unknown/max*8,bw,Math.max(1,unknown/max*9));}
    });
  }
  label('Pressure',3,205); line(left,204,right,204,'#63717c');
  bins.forEach((b,i)=>{
    const flow=flowResponse(result,i,width,minute,cutoff);
    if(flow?.imbalance==null)return;
    const dy=flow.imbalance*19;
    c.fillStyle=dy>=0?'#46bea0':'#f39785';
    c.fillRect(x(i),Math.min(204,204-dy),Math.max(.7,span/bins.length),Math.abs(dy));
  });
  const prices=bins.flatMap(b=>[b.bid,b.ask,...($('trades').checked?[b.low,b.high]:[])]).filter(Number.isFinite);
  if(prices.length) {
    const hi=Math.max(...prices)+.005,lo=Math.min(...prices)-.005,y=v=>348-(v-lo)/(hi-lo)*103;
    label('QQQ',3,232);label(hi.toFixed(2),3,249);label(lo.toFixed(2),3,349);
    let prev=null;
    bins.forEach((b,i)=>{
      if(b.bid!==undefined){
        const xx=x(i+.5);line(xx,y(b.bid),xx,y(b.ask),'#63acdb');
        const gap=gaps.some(([a,z])=>prev && a<=minute+(i+1)*width && z>=minute+prev.i*width);
        if(prev && i===prev.i+1 && !gap)line(x(prev.i+.5),y(prev.mid),xx,y(b.mid),'#ffca63');
        c.fillStyle='#ffca63';c.fillRect(xx-1,y(b.mid)-1,2,2);prev={i,mid:b.mid};
      }
      if($('trades').checked && b.low!==undefined)line(x(i+.5),y(b.low),x(i+.5),y(b.high)+1,'#c4b7eb');
    });
  }
  for(let s=0;s<=60;s+=10)label(s+'s',Math.min(right-16,x(s*1000/width)),380);
  const sec=Number($('detail').value);
  if(Number.isInteger(sec)&&sec>=0&&sec<60)line(x(sec*1000/width),17,x(sec*1000/width),358,'#eee');
  $('clock').textContent=time(cutoff);
  $('summary').textContent=result.calls.toLocaleString()+' call contracts · '+result.puts.toLocaleString()+' put contracts · '+result.signed.toLocaleString()+' quote-signed · '+result.unknown.toLocaleString()+' unclassified';
  inspect(sec);
}
function inspect(second) {
  const slice=data.inspected.filter(row=>Math.floor((row.at-minute)/1000)===second);
  $('detail-summary').textContent='Second '+second+' · '+slice.length+' observed events through the cutoff';
  const width=Number($('width').value), index=Math.floor((second*1000+detailOffset)/width);
  const flow=flowResponse(data,index,width,minute,minute+Number($('cursor').value));
  if(!flow){$('response').textContent='Selected flow interval is beyond the cutoff.';$('next-second').textContent='';}
  else {
    // The Alkalus classification and the one-second-later response are two
    // separate items. The classification uses only this interval's own
    // first-to-last midpoint move and signed flow; the later response is a
    // next-interval observation and never changes the classification.
    const cls=alkalusFlow(data,index,width,minute);
    const label=cls===null?null:cls.kind==='golden'?'golden time':cls.kind==='copper'?'working copper':'lead time';
    $('response').textContent='At '+(index*width/1000).toFixed(1)+' s · net '+flow.pressure+' directional contracts · imbalance '+(flow.imbalance===null?'unclassified':flow.imbalance.toFixed(2))+' · Alkalus (preliminary, flow only) · '+(cls===null
      ?'unclassified — no midpoint, no option flow, or interrupted coverage in this interval.'
      :label+' · midpoint moved '+(cls.displacement>=0?'+':'')+cls.displacement.toFixed(3)+' ('+cls.displacementSpreads.toFixed(1)+' spreads)');
    $('next-second').textContent='Next-second observation — not part of this interval’s classification: midpoint change one second later '+(flow.response===null?'unavailable':(flow.response>=0?'+':'')+flow.response.toFixed(3));
  }
  const bucket=data.bins[index];
  if(bucket?.first) $('response').textContent+=' · QQQ trades O '+bucket.first.price+' / H '+bucket.high+' / L '+bucket.low+' / C '+bucket.last.price+' · '+bucket.shares+' shares';
  const body=$('rows');body.replaceChildren();
  for(const r of slice){
    const tr=document.createElement('tr');
    const quote=r.kind==='quote', option=r.kind==='option';
    for(const value of [time(r.at),quote?time(r.bidAt)+' / '+time(r.askAt):time(r.t),
      option?r.symbol:'QQQ',quote?r.bid+' × '+r.ask:r.price,quote?'—':r.size,
      option?r.label+' · '+r.basis+' · seq '+r.seq:quote?'bid / ask':'flag '+r.flag+' · seq '+r.seq+(r.cancel?' · cancel':'')+(r.correction?' · correction':'')]){
      const td=document.createElement('td');td.textContent=value;tr.append(td);
    }
    body.append(tr);
  }
}
async function replay(at,offset=59999) {
  reset(); const id=generation;
  minute=receiptMinute(at);$('minute').value=isoMinute(minute);$('cursor').value=offset;
  $('state').textContent='Reading archived minute…';paint();
  const bundled=[sample-60000,sample,sample+60000].includes(minute);
  try {
    const res=await fetch(bundled?'/intraminute/'+minute+'.json':api+'?minute='+minute);
    if(!res.ok) throw Error(res.status===404?'No published archive for this minute.':'Archive unavailable ('+res.status+').');
    const body=await res.json();
    if(body.minute!==minute || body.version!==1 || !Array.isArray(body.rows)) throw Error('Invalid archive minute.');
    if(id!==generation)return;
    rows=body.rows;strikes();paint();
    $('state').textContent=(body.source==='session-provisional'?'Provisional session tape · ':'Archived QQQ tape · ')+isoMinute(minute).replace('T',' ')+' UTC';
  } catch(e){if(id===generation)$('state').textContent=e.message;}
}
function follow(){
  reset();live=true;$('live').disabled=true;
  const id=generation;let cursor=0,epoch='',delay=250,partial=false,started=null,disconnectedAt=null,lastReceipt=null;
  minute=receiptMinute(Date.now());$('minute').value=isoMinute(minute);paint();
  async function poll(){
    if(generation!==id)return;
    if(!document.hidden){
      try {
        const res=await fetch(api+'?'+new URLSearchParams({cursor,epoch}),{cache:'no-store',signal:AbortSignal.timeout(4500)});
        if(!res.ok)throw Error('Live tape unavailable ('+res.status+').');
        const body=await res.json();
        if(id!==generation)return;
        if(body.reset){rows=[];partial=body.truncated;disconnectedAt=null;lastReceipt=null;}
        epoch=body.epoch;cursor=body.cursor;started=body.started??started;
        for(const r of body.rows) {
          if(r.kind==='gap')disconnectedAt=r.reason==='stream_disconnect'?r.at:null;
          else lastReceipt=r.at;
        }
        rows=rows.concat(body.rows);
        minute=receiptMinute(Date.now()); rows=rows.filter(r=>r.at>=minute-60000).slice(-100000);
        if(disconnectedAt!==null && disconnectedAt<minute && !rows.some(r=>r.kind==='gap' && r.reason==='stream_disconnect' && r.at>=disconnectedAt)){
          rows.unshift({kind:'gap',at:minute,start:minute,reason:'stream_disconnect'});
        }
        $('minute').value=isoMinute(minute);$('cursor').value=Math.min(59999,Date.now()-minute);
        const connection=disconnectedAt===null?'Live':'Disconnected since '+time(disconnectedAt)+' UTC';
        const freshness=lastReceipt===null?'awaiting QQQ receipt':'last receipt '+time(lastReceipt)+' UTC · age '+Math.max(0,(Date.now()-lastReceipt)/1000).toFixed(1)+' s';
        $('state').textContent=connection+' · '+freshness+(partial || (started && started>minute)?' · partial minute coverage':'');
        strikes();paint();delay=body.more?0:250;
      } catch(e){if(id!==generation)return;$('state').textContent=e.message;delay=Math.min(8000,Math.max(1000,delay*2));}
    }
    if(generation===id)timer=setTimeout(poll,document.hidden?2000:delay);
  }
  poll();
}
$('load').onclick=()=>{const v=Date.parse($('minute').value+'Z');if(Number.isFinite(v))replay(v);};
$('sample').onclick=()=>replay(sample);$('prev').onclick=()=>replay(minute-60000);
$('next').onclick=()=>replay(minute+60000);$('live').onclick=follow;
$('cursor').oninput=()=>{if(live){generation++;live=false;clearTimeout(timer);$('live').disabled=false;$('state').textContent='Paused live tape';}paint();};
$('width').onchange=paint;$('trades').onchange=paint;$('detail').oninput=paint;
$('strike').onchange=()=>{selected=$('strike').value===''?null:Number($('strike').value);paint();};
$('plot').onclick=e=>{const area=e.target.getBoundingClientRect();$('detail').value=Math.max(0,Math.min(59,Math.floor((e.clientX-area.left-60)/(area.width-75)*60)));detailOffset=Math.min(59999,Math.max(0,Math.floor((e.clientX-area.left-60)/(area.width-75)*60000)))%1000;paint();};
$('play').onclick=()=>{
  if(live)$('cursor').oninput();
  if(playback){clearInterval(playback);playback=null;$('play').textContent='Play replay';return;}
  if(Number($('cursor').value)>=59999)$('cursor').value=0;
  const start=performance.now()-Number($('cursor').value);
  $('play').textContent='Pause replay';
  playback=setInterval(()=>{$('cursor').value=Math.min(59999,performance.now()-start);paint();
    if(Number($('cursor').value)>=59999)$('play').click();},100);
};
window.addEventListener('resize',()=>{if(data)paint();});
const at=Number(params.get('at'));
if(params.has('at') && Number.isSafeInteger(at) && at>0 && at<4102444800000){
  detailOffset=at%1000;
  $('detail').value=Math.floor(at%60000/1000);replay(at,at%60000);
}else if(params.get('live')==='1')follow();
else replay(sample);
