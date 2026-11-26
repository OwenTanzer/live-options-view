import {readingAsOf} from '../shared/one-clock.js';
import {archivedChainSource} from '../shared/one-clock-chain-source.js';
import {regimeComponents,regimeFlowSource} from '../shared/one-clock-regime.js';
import {sessionAt} from '../shared/one-clock-session.js';

const liveChain = archivedChainSource();
const time = new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hourCycle:'h23',
  hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3});
export function mountRegime(clock) {
  const panel = document.getElementById('clock-regime');
  const status = panel.querySelector('[role=status]');
  const table = panel.querySelector('tbody');
  let pending;
  let displayed;
  let lastLiveMinute = null;
  const symbol = () => typeof btcOptions !== 'undefined' && btcOptions.active ? 'BTC' : chainSelection.symbol;
  function clear() {
    pending?.abort();
    table.replaceChildren();
    status.textContent='';
  }
  async function render() {
    clear();
    if (!panel.open) return;
    const controller = new AbortController();
    pending=controller;
    const now=Date.now(), selected=clock.snapshot();
    const live=selected.mode==='live';
    const scope = live ? {session:sessionAt(now),instrument:symbol(),cutoff:now,origin:'recorded'} : selected;
    displayed={mode:selected.mode,instrument:scope.instrument,session:scope.session};
    if (live) lastLiveMinute = Math.floor(now / 60000);
    const description=scope.instrument+' · '+scope.session+' '+time.format(scope.cutoff)+' ET';
    status.textContent=description+' · loading';
    async function read(source) {
      if (!['QQQ','SPY','IBIT'].includes(scope.instrument)) return null;
      if (!live) return (await clock.read(source.id)).reading;
      const rows=await source.read({...scope,signal:controller.signal});
      return readingAsOf(rows,scope,source);
    }
    const [chain,flow]=await Promise.allSettled([read(liveChain),read(regimeFlowSource)]);
    if (controller.signal.aborted || !panel.open) return;
    const suffix=[chain.status==='rejected' && 'chain read failed',flow.status==='rejected' && 'flow read failed'].filter(Boolean);
    status.textContent=[description,...suffix].join(' · ');
    for (const r of regimeComponents(scope,chain.value,flow.value,maxPainStrike)) {
      const tr=document.createElement('tr');
      const th=document.createElement('th'); th.scope='row'; th.textContent=r.name; tr.append(th);
      for (const value of [[r.value,r.note].filter(Boolean).join(' · '),r.observed==null?'—':[r.observedLabel,time.format(r.observed)].filter(Boolean).join(' '),
        r.available==null?'—':time.format(r.available)]) {
        const td=document.createElement('td'); td.textContent=value; tr.append(td);
      }
      table.append(tr);
    }
  }
  // Follow the existing completed live chain refresh at most once per minute
  // while visible. Replay and closed panels create no periodic traffic.
  new MutationObserver(()=>{
    if (panel.open && panel.getClientRects().length && !document.hidden && clock.snapshot().mode==='live' &&
        Math.floor(Date.now()/60000)!==lastLiveMinute) render();
  }).observe(document.getElementById('price-age'),{childList:true});
  panel.addEventListener('toggle',render);
  if (location.hash==='#clock-regime') panel.open=true;
  panel.querySelector('button').addEventListener('click',render);
  clock.subscribe(s=>{
    if (panel.open && (s.mode==='replay' || displayed?.mode!==s.mode ||
        displayed?.session!==s.session || displayed?.instrument!==symbol())) render();
  });
  document.getElementById('underlying-select').addEventListener('change',()=>{
    // The classic dashboard's onchange may run after this module's listener.
    queueMicrotask(()=>{if(panel.open && clock.snapshot().mode==='live' && displayed?.instrument!==symbol()) render();});
  });
  // Switching a live underlying does not itself advance the replay controller.
  new MutationObserver(()=>{
    if(panel.open && clock.snapshot().mode==='live' && displayed?.instrument!==symbol()) render();
  }).observe(document.body,{attributes:true,attributeFilter:['data-chain']});
}
