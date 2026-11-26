import { createClock,createSourceRegistry } from '../shared/one-clock.js';
import { archivedChainSource } from '../shared/one-clock-chain-source.js';
import { botLedgerSource,botInventory } from '../shared/one-clock-bot-source.js';
import {botDecisionSource} from '../shared/one-clock-bot-decisions.js';
import { squeezeSource } from '../shared/one-clock-squeeze-source.js';
import { sessionAt,sessionInstant } from '../shared/one-clock-session.js';
import {replaySelection} from '../shared/one-clock-replay-link.js';
// Read-only display boundary: never calls fetchLatest, ingestSnapshotQuotes,
// renderPaper, openTicket, or mutates lastData/session/quote interests.
const el=id=>document.getElementById(id);
const registry=createSourceRegistry(); let coverage=null;
registry.register(archivedChainSource({onCoverage:value=>{coverage=value;}}));
let botCoverage=null;
registry.register(botLedgerSource({getBot:()=>el('history-clock-bot').value,onCoverage:value=>{botCoverage=value;}}));
let decisionCoverage=null;
registry.register(botDecisionSource({getBot:()=>el('history-clock-bot').value,onCoverage:value=>{decisionCoverage=value;}}));
let shortlistCoverage=null;
registry.register(squeezeSource({onCoverage:value=>{shortlistCoverage=value;}}));
const today=sessionAt(Date.now());
el('history-clock-date').value=today;
const clock=createClock({registry,initial:{mode:'live',origin:'recorded',session:today,instrument:'QQQ',cutoff:sessionInstant(today,10)}});
function clearBot(message) {
  el('history-clock-bot-status').textContent=message;
  el('history-clock-bot-rows').replaceChildren();el('history-clock-bot-inventory').textContent='';
}
async function renderDecisions(selection) {
  const status=el('clock-decisions-status'), body=el('clock-decisions');
  body.replaceChildren();
  if(!el('history-clock-bot').value){status.textContent='Select a bot to see recorded decisions.';return;}
  status.textContent='Loading decision receipts…';
  try {
    const result=await clock.read('bot-decision');
    if(result.discarded)return;
    status.textContent=result.rows.length+' recorded decisions. '+(decisionCoverage?.reason || 'Coverage is limited to published receipts.');
    body.replaceChildren();
    for(const row of result.rows) {
      const tr=document.createElement('tr');
      for(const value of [new Date(row.eventAt).toISOString(),new Date(row.availableAt).toISOString(),
        row.value.outcome,row.value.action,row.value.inputAgeMs,row.details?.anchor?.anchor]) {
        const td=document.createElement('td');
        td.textContent=value==null?'—':String(value);
        tr.append(td);
      }
      body.append(tr);
    }
  } catch(error){if(clock.snapshot().generation===selection.generation)status.textContent=error.message;}
}
async function renderBot() {
  const selection=clock.snapshot();
  renderDecisions(selection);
  clearBot(el('history-clock-bot').value?'Loading retained receipt pages…':'Select a public bot.');
  if(!el('history-clock-bot').value) return;
  try {
    const result=await clock.read('bot-ledger');if(result.discarded)return;
    const rows=result.rows,inventory=botInventory(rows,{session:selection.session,complete:botCoverage?.complete===true});
    const fills=rows.filter(row=>row.details.eventSession===selection.session);
    el('history-clock-bot-status').textContent=`${fills.length} visible session fills · ${rows.length-fills.length} carried ledger entries · ${botCoverage?.pages??0} pages · ${botCoverage?.complete?'Retained publication receipts complete.':'Publication gaps: opening and current inventory unavailable.'}`;
    el('history-clock-bot-inventory').textContent=JSON.stringify(inventory,null,2);
    el('history-clock-bot-rows').replaceChildren();
    for(const row of fills.sort((a,b)=>a.eventAt-b.eventAt || a.id.localeCompare(b.id))) {
      const tr=document.createElement('tr');
      for(const value of [new Date(row.eventAt).toISOString(),new Date(row.availableAt).toISOString(),row.value.sym,row.value.side,row.value.qty,row.value.price]) {
        const td=document.createElement('td');td.textContent=String(value);tr.append(td);
      }
      el('history-clock-bot-rows').append(tr);
    }
  } catch(error) {if(clock.snapshot().generation===selection.generation)clearBot(error.message);}
}
el('history-clock-load-bots').onclick=async()=>{
  const button=el('history-clock-load-bots');button.disabled=true;
  try {
    const response=await fetch('/api/one-clock/bots/roster',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok)throw new Error('Public roster unavailable');
    const body=await response.json();
    for(const bot of body.bots??[]) {
      if(typeof bot!=='string' || [...el('history-clock-bot').options].some(o=>o.value===bot))continue;
      const option=document.createElement('option');option.value=bot;option.textContent=bot;el('history-clock-bot').append(option);
    }
  } catch(error) {clearBot(error.message);} finally {button.disabled=false;}
};
function clearShortlist(message) {el('history-clock-shortlist-status').textContent=message;el('history-clock-shortlist-evidence').textContent='';}
async function renderShortlist(selection) {
  clearShortlist('Reading retained publication receipts…');
  try {
    const result=await clock.read('shortlist');if(result.discarded)return;
    const row=result.reading.row;
    el('history-clock-shortlist-status').textContent=row
      ? `${selection.instrument}: ${row.value.listed?'on the published top-ten shortlist, rank '+row.value.rank:'not in this published top-ten shortlist'} · run finished ${new Date(row.details.finishedAt).toISOString()} · publication proven ${new Date(row.availableAt).toISOString()}.`
      : 'No qualified publication at this cursor. Missing coverage is not an observed absence.';
    el('history-clock-shortlist-evidence').textContent=JSON.stringify({...(row?{value:row.value,evidence:row.evidence,details:row.details}:{}),coverage:shortlistCoverage},null,2);
  } catch(error) {if(clock.snapshot().generation===selection.generation)clearShortlist(error.message);}
}
let tab='live';
let replay=false;
const replayTabs=new Set(['live','bots','history','squeeze']);
function updateView() {
  const visible=replay && replayTabs.has(tab);
  el('clock-navigation').hidden=!replayTabs.has(tab);
  el('history-clock').hidden=!visible;
  el('clock-chain-view').hidden=!['live','history'].includes(tab);
  el('clock-journal-nav').hidden=true;
  el('clock-floor-view').hidden=!['bots','history'].includes(tab);
  el('clock-shortlist-view').hidden=!['history','squeeze'].includes(tab);
  for(const name of ['live','bots','squeeze']) {
    el(name+'-panel').classList.toggle('hidden',tab!==name || visible);
  }
  el('history-daily').hidden=replay;
  el('history-view').value=replay?'clock':'daily';
  el('clock-mode').value=replay?'replay':'live';
}
function renderCommitted(selection) {
  const bitcoin = el('history-clock-btc');
  bitcoin.href = '/api/one-clock/btc-replay?one_clock_at=' + selection.cutoff;
  bitcoin.hidden = tab !== 'history';
  const credit = el('history-clock-credit');
  credit.hidden = tab !== 'history';
  const kalshi = el('history-clock-kalshi');
  kalshi.hidden = credit.hidden;
  kalshi.href = '/kalshi.html?one_clock_at=' + selection.cutoff;
  credit.href = '/credit-tape/replay?one_clock_at=' + selection.cutoff;
  const flow=el('history-order-flow-link');
  flow.hidden=tab!=='history' || selection.instrument!=='QQQ';
  if(flow.hidden) flow.removeAttribute('href');
  if(tab==='history' && ['QQQ','SPY','IBIT'].includes(selection.instrument)){
    const params=new URLSearchParams({one_clock_session:selection.session,one_clock_instrument:selection.instrument,one_clock_at:String(selection.cutoff)});
    el('clock-journal').href='/trade-journal.html?'+params;
    if(!flow.hidden) flow.href='/order-flow.html?'+params;
    el('clock-journal-nav').hidden=false;
  }else el('clock-journal-nav').hidden=true;
  el('clock-scope').textContent=selection.instrument+' · '+new Date(selection.cutoff).toISOString();
  if(tab==='squeeze')return renderShortlist(selection);
  if(tab==='bots')return renderBot();
  if(tab==='history'){renderBot();renderShortlist(selection);}
  if(tab==='live' || tab==='history')return renderChain(selection);
}
clock.subscribe(selection=>{
  coverage=null;botCoverage=null;decisionCoverage=null;shortlistCoverage=null;
  window.dispatchEvent(new CustomEvent('one-clock-selection',{detail:selection}));
});
window.oneClockDashboard={
  snapshot:clock.snapshot,
  isLive() {return !replay;},
  showTab(name) {
    tab=name;updateView();
    if(replay && replayTabs.has(tab))renderCommitted(clock.snapshot());
    return replay;
  },
  liveTime() {
    if(replay)return clock.snapshot().cutoff;
    const now=Date.now();
    return clock.select({mode:'live',origin:'recorded',session:sessionAt(now),
      instrument:typeof chainSelection!=='undefined'?chainSelection.symbol:'QQQ',cutoff:now}).cutoff;
  },
};
updateView();
el('history-clock-bot').onchange=()=>renderCommitted(clock.select(clock.snapshot()));
function clear(message) {
  el('history-clock-status').textContent=message; el('history-clock-rows').replaceChildren();
  el('history-clock-momentum').textContent='';
  el('history-clock-skew').textContent='';el('history-clock-adaptive').textContent='';el('history-clock-evidence').textContent='';
}
async function renderChain(selection) {
  const {session,instrument}=selection;
  if(instrument==='BTC'){clear('CME BTC chain archive unavailable at this cursor. IBIT is a separate instrument.');return;}
  if(!['QQQ','SPY','IBIT'].includes(instrument)){clear('Chain archive is available for QQQ, SPY and IBIT. Other sources keep their own coverage.');return;}
  try {
    clear('Loading evidence available at this cursor…');
    const result=await clock.read('chain'); if(result.discarded) return;
    const reading=result.reading,row=reading.row;
    if(!row) { clear('No qualified snapshot at this cursor. Missing publication or retained revision is unavailable.');el('history-clock-evidence').textContent=JSON.stringify(coverage,null,2);return; }
    const data=row.value;
    // Existing read-only renderer and calculations, same eligible parent record.
    const spot=reading.state==='fresh' && Number.isFinite(data.underlying_price) && data.underlying_price>0 ? data.underlying_price : null;
    const chain={...data,underlying_price:spot};
    const opening=instrument==='QQQ' && typeof loadOpeningOi==='function'?await loadOpeningOi(session,selection.cutoff):null;
    const current=clock.snapshot();
    if(!replay || current.session!==session || current.instrument!==instrument || current.cutoff!==selection.cutoff)return;
    buildHeatmapRows(el('history-clock-rows'),chain,instrument==='QQQ'?null:relativeOiRanges(data.rows),{
      showUnanchored:true,...(instrument==='QQQ'?{openingOi:opening}:{})});
    const spotText=spot===null?'Underlying spot unavailable':'Underlying spot $'+spot;
    const raw=describeReferenceOiSkew(data,selection.cutoff);
    const cursorLabel=new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(selection.cutoff);
    el('history-clock-status').textContent=`${instrument} · session ${session} · expiration ${data.expiration} · cursor ${cursorLabel} New York · snapshot ${new Date(row.eventAt).toISOString()} · ${reading.state} (${Math.round(reading.ageMs/1000)} s old). ${spotText}. Read-only replay.${instrument==='QQQ' && typeof openingOiNote==='function'?' '+openingOiNote(opening):''}`;
    if (instrument === 'QQQ') {
      const node = el('history-clock-momentum');
      if (data.underlying_market?.momentum_windows) {
        paintMomentumStrip(node, data.underlying_market, {at:selection.cutoff, retained:reading.state !== 'fresh'});
      } else node.textContent = 'Momentum windows unavailable in this snapshot.';
    }
    el('history-clock-skew').textContent=`Published reference skew: ${raw.text} · ${raw.availability==='live'?'fresh at cursor':raw.availability}. ${raw.detail}`;
    if(instrument==='IBIT') {
      const adaptive=computeAdaptiveOiSkew(data.rows,spot);
      el('history-clock-adaptive').textContent=adaptive.ok
        ? `Reconstructed IBIT adaptive skew: ${adaptive.imbalance.toFixed(3)} · ${adaptive.band_strikes} strikes · ±${(adaptive.half_width*100).toFixed(2)}%. Same archived snapshot; not a historically published reading.`
        : `Reconstructed IBIT adaptive skew unavailable: ${adaptive.reason}.`;
    }
    el('history-clock-evidence').textContent=JSON.stringify({source:row.source,id:row.id,eventAt:row.eventAt,availableAt:row.availableAt,evidence:row.evidence,details:row.details,coverage},null,2);
  } catch(error) { if(clock.snapshot().generation===selection.generation) clear(error.message); }
}
function renderSelection() {
  try {
    const session=el('history-clock-date').value,instrument=el('history-clock-instrument').value;
    const [h,m,s=0]=el('history-clock-time').value.split(':').map(Number);
    const cutoff=sessionInstant(session,h,m,Math.floor(s),Math.round((s%1)*1000));
    return renderCommitted(clock.select({mode:'replay',origin:'recorded',session,instrument,cutoff}));
  } catch(error) {el('history-clock-btc').hidden=true;el('clock-journal-nav').hidden=true;el('history-clock-credit').hidden=true;el('history-clock-kalshi').hidden=true;clear(error.message);clearBot('Invalid cursor; choose a valid session and time.');clearShortlist('Invalid cursor.');}
}
el('history-clock-form').onsubmit=event=>{event.preventDefault();renderSelection();};
function changeMode(next) {
  if(next===replay)return;
  replay=next;
  if(replay && tab==='history' && el('hist-date').value)el('history-clock-date').value=el('hist-date').value;
  if(replay && tab==='live' && typeof chainSelection!=='undefined')el('history-clock-instrument').value=chainSelection.symbol;
  if(typeof closeTicket==='function')closeTicket();
  updateView();
  setBotsPolling(tab==='bots' && !replay);
  if(replay){
    cancelLiveSqueezeRead();
    renderSelection();
  }
  else {
    window.oneClockDashboard.liveTime();
    fetchSqueezeLatest();
    el('clock-scope').textContent='';
  }
}
el('history-view').onchange=()=>changeMode(el('history-view').value==='clock');
el('clock-mode').onchange=()=>changeMode(el('clock-mode').value==='replay');
window.addEventListener('pagehide',event=>{if(!event.persisted)clock.dispose();});
try {
  const linked=replaySelection(location.search);
  if(linked) {
    showTab('history');replay=true;cancelLiveSqueezeRead();updateView();
    el('history-clock-date').value=linked.session;el('history-clock-instrument').value=linked.instrument;
    const local=new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(linked.cutoff);
    el('history-clock-time').value=local+'.'+String(linked.cutoff%1000).padStart(3,'0');
    const note=document.createElement('p');note.textContent='Source replay at the event trigger. An observer receipt can be retained later; this source view does not move its recorded receipt into the past. Unavailable source evidence remains unavailable.';
    el('history-clock-form').after(note);renderCommitted(clock.select(linked));
  }
}catch(error){clear(error.message);}
