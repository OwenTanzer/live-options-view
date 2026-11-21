import { createClock,createSourceRegistry } from '../shared/one-clock.js';
import { archivedChainSource } from '../shared/one-clock-chain-source.js';
import { sessionAt,sessionInstant } from '../shared/one-clock-session.js';
// Read-only display boundary: never calls fetchLatest, ingestSnapshotQuotes,
// renderPaper, openTicket, or mutates lastData/session/quote interests.
const el=id=>document.getElementById(id);
const registry=createSourceRegistry(); let coverage=null;
registry.register(archivedChainSource({onCoverage:value=>{coverage=value;}}));
const today=sessionAt(Date.now());
el('history-clock-date').value=today;
const clock=createClock({registry,initial:{mode:'replay',origin:'recorded',session:today,instrument:'QQQ',cutoff:sessionInstant(today,10)}});
function clear(message) {
  el('history-clock-status').textContent=message; el('history-clock-rows').replaceChildren();
  el('history-clock-skew').textContent='';el('history-clock-adaptive').textContent='';el('history-clock-evidence').textContent='';
}
async function renderSelection() {
  const session=el('history-clock-date').value,instrument=el('history-clock-instrument').value;
  try {
    const [h,m,s=0]=el('history-clock-time').value.split(':').map(Number);
    const cutoff=sessionInstant(session,h,m,s);
    const selection=clock.select({mode:'replay',origin:'recorded',session,instrument,cutoff});
    coverage=null;clear('Loading evidence available at this cursor…');
    const result=await clock.read('chain'); if(result.discarded) return;
    const reading=result.reading,row=reading.row;
    if(!row) { clear('No qualified snapshot at this cursor. Missing publication or retained revision is unavailable.');el('history-clock-evidence').textContent=JSON.stringify(coverage,null,2);return; }
    const data=row.value;
    // Existing read-only renderer and calculations, same eligible parent record.
    buildHeatmapRows(el('history-clock-rows'),data,relativeOiRanges(data.rows));
    const raw=describeReferenceOiSkew(data,selection.cutoff);
    el('history-clock-status').textContent=`${instrument} · session ${session} · expiration ${data.expiration} · cursor ${el('history-clock-time').value} New York · snapshot ${new Date(row.eventAt).toISOString()} · ${reading.state} (${Math.round(reading.ageMs/1000)} s old). Read-only replay.`;
    el('history-clock-skew').textContent=`Published reference skew: ${raw.text} · ${raw.availability==='live'?'fresh at cursor':raw.availability}. ${raw.detail}`;
    if(instrument==='IBIT') {
      const adaptive=computeAdaptiveOiSkew(data.rows,data.underlying_price);
      el('history-clock-adaptive').textContent=adaptive.ok
        ? `Reconstructed IBIT adaptive skew: ${adaptive.imbalance.toFixed(3)} · ${adaptive.band_strikes} strikes · ±${(adaptive.half_width*100).toFixed(2)}%. Same archived snapshot; not a historically published reading.`
        : `Reconstructed IBIT adaptive skew unavailable: ${adaptive.reason}.`;
    }
    el('history-clock-evidence').textContent=JSON.stringify({source:row.source,id:row.id,eventAt:row.eventAt,availableAt:row.availableAt,evidence:row.evidence,details:row.details,coverage},null,2);
  } catch(error) { if(clock.snapshot().instrument===instrument && clock.snapshot().session===session) clear(error.message); }
}
el('history-clock-form').onsubmit=event=>{event.preventDefault();renderSelection();};
el('history-view').onchange=()=>{
  const active=el('history-view').value==='clock';
  el('history-clock').hidden=!active;el('history-daily').hidden=active;
  if(active) { if(el('hist-date').value) el('history-clock-date').value=el('hist-date').value;renderSelection(); }
  else clock.select({...clock.snapshot(),mode:'replay'}); // cancel a pending archive response
};
window.addEventListener('pagehide',()=>clock.dispose());
