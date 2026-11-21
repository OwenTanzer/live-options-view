import { createClock, createSourceRegistry, selectAsOf,observation } from '../shared/one-clock.js';
import { eventRule, saveRule, evaluateEvents, aggregateEvents } from '../shared/one-clock-events.js';
import { demoRules, demoRows, demoStart, demoClose, demoSelection, demoSources } from '../shared/one-clock-demo.js';

const $ = id => document.getElementById(id);
const storageKey = 'one-clock.rules.v1';
let versions = structuredClone(demoRules);
let storageError = '';
try {
  const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
  if (Array.isArray(saved) && saved.length) versions = saved.map(eventRule);
} catch { storageError = 'Saved definitions could not be read. Your stored copy was left untouched; export any new definition before leaving.'; }
let editing = versions[0];
let currentRule = versions[0];
let timer = null;
let selectedId = null;
let previewing = false;
let observedMode=false,accountState=null,observedBody=null;
let observedGeneration=0,observedSelection=null;
const registry = createSourceRegistry();
for (const source of demoSources) registry.register({ ...source, version: 'fixture-1', owner: 'offline demo',
  maxAgeMs: 150000, read: async () => demoRows.filter(r => r.source === source.id) });
const clock = createClock({ registry, initial: { ...demoSelection, mode: 'replay', cutoff: demoStart + 301000 } });

function option(value, label = value) { const el = document.createElement('option'); el.value = value; el.textContent = label; return el; }
function selectControl(label, name, options, value) {
  const wrapper = document.createElement('label'); wrapper.textContent = label;
  const input = document.createElement('select'); input.dataset.field = name;
  for (const [key, title] of options) input.append(option(key, title));
  input.value = value; wrapper.append(input); return wrapper;
}
function inputControl(label, name, value, type = 'text') {
  const wrapper = document.createElement('label'); wrapper.textContent = label;
  const input = document.createElement('input'); input.dataset.field = name; input.type = type; input.value = value;
  if (type === 'number') input.step = 'any';
  wrapper.append(input); return wrapper;
}
function removeButton(row) { const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Remove'; button.onclick = () => row.remove(); return button; }
const sources = registry.list().map(id => [id, registry.get(id).label]);
const formFields=new Map(demoSources.map(source=>[source.id,source.fields]));
function sourceFields(source, field) {
  const sourceControl = selectControl('Input', 'source', sources, source);
  const fieldControl = selectControl('Field', 'field', Object.entries(formFields.get(source)??{}), field);
  sourceControl.querySelector('select').onchange = event => {
    const fields = Object.entries(formFields.get(event.target.value)??{});
    fieldControl.querySelector('select').replaceChildren(...fields.map(([id,label]) => option(id,label)));
  };
  return [sourceControl,fieldControl];
}
function condition(c = { source: 'skew', field: 'imbalance', op: '>=', value: 0.3, maxAgeMs: 150000 }) {
  const row = document.createElement('div'); row.className = 'condition';
  row.append(...sourceFields(c.source,c.field),
    selectControl('Compare', 'op', ['>=','>','<=','<','=='].map(v => [v,v]), c.op), inputControl('Value', 'value', String(c.value)),
    selectControl('Reference', 'relativeToAnchor', [['false','Absolute'],['true','Change from first reading']], String(!!c.relativeToAnchor)), removeButton(row),
    inputControl('Maximum age (seconds)', 'maxAgeMs', c.maxAgeMs / 1000, 'number'));
  $('conditions').append(row);
}
function outcome(o = { id: `outcome-${crypto.randomUUID()}`, source: 'price', field: 'spot', measure: 'return', horizonMs: 900000, toleranceMs: 150000, maxAgeMs: 150000 }) {
  const row = document.createElement('div'); row.className = 'outcome'; row.dataset.id = o.id;
  row.append(...sourceFields(o.source,o.field),
    selectControl('Measure', 'measure', [['return','Return (%)'],['change','Change'],['value','Endpoint value']], o.measure),
    inputControl('Horizon (minutes)', 'horizonMs', o.horizonMs / 60000, 'number'),
    inputControl('Tolerance (seconds)', 'toleranceMs', o.toleranceMs / 1000, 'number'), removeButton(row),
    inputControl('Baseline maximum age (seconds)', 'maxAgeMs', o.maxAgeMs / 1000, 'number'));
  $('outcomes').append(row);
}
function get(row, field) { return row.querySelector(`[data-field="${field}"]`).value; }
function comparisonValue(value) {
  if (value === 'true') return true;
  if (value === 'false') return false;
  return value.trim() !== '' && Number.isFinite(Number(value)) ? Number(value) : value;
}
function formRule() {
  return eventRule({ id: editing.id, version: editing.version, name: $('rule-name').value,
    instruments: [$('instrument').value], combine: $('combine').value, transition: $('transition').value, missingInput: $('missing').value,
    conditions: [...$('conditions').children].map(row => ({ source: get(row,'source'), field: get(row,'field'), op: get(row,'op'),
      value: comparisonValue(get(row,'value')), relativeToAnchor: get(row,'relativeToAnchor') === 'true', maxAgeMs: Number(get(row,'maxAgeMs')) * 1000 })),
    outcomes: [...$('outcomes').children].map(row => ({ id: row.dataset.id, source: get(row,'source'), field: get(row,'field'),
      measure: get(row,'measure'), horizonMs: Number(get(row,'horizonMs')) * 60000, toleranceMs: Number(get(row,'toleranceMs')) * 1000,
      maxAgeMs: Number(get(row,'maxAgeMs')) * 1000 })),
  });
}
const key = rule => JSON.stringify([rule.id, rule.version]);
function choose(value) { return versions.find(v => key(v) === value); }
function versionOptions() {
  for (const id of ['versions','result-version']) {
    $(id).replaceChildren(...versions.map(v => option(key(v), `${v.name} · v${v.version}`)));
    $(id).value = key(currentRule);
  }
}
function openRule(rule) {
  editing = rule;
  if (rule.conditions.length) { currentRule = rule; previewing = false; }
  $('rule-name').value = rule.name; $('instrument').value = rule.instruments[0];
  $('combine').value = rule.combine; $('transition').value = rule.transition; $('missing').value = rule.missingInput;
  $('conditions').replaceChildren(); rule.conditions.forEach(condition);
  $('outcomes').replaceChildren(); rule.outcomes.forEach(outcome);
  $('rule-json').textContent = JSON.stringify(rule,null,2);
  updateOutcomes(); render();
}
function updateOutcomes() {
  $('result-instrument').replaceChildren(...currentRule.instruments.map(v => option(v)));
  $('outcome-choice').replaceChildren(...currentRule.outcomes.map(o => option(o.id, `${o.field} · ${o.horizonMs / 60000} min · ${o.measure}`)));
  if (!currentRule.outcomes.length) $('outcome-choice').append(option('', 'Tracking only'));
}
function showResults(active) {
  observedGeneration++;observedSelection=null;
  if(observedMode){observedMode=false;clearInterval(timer);timer=null;clock.select({...demoSelection,mode:'replay',cutoff:demoStart+301000});}
  $('observed-results').hidden=true;document.querySelector('.replay').hidden=false;
  $('show-observed').setAttribute('aria-pressed','false');
  document.querySelector('.notice').hidden=false;
  $('builder').hidden = active; $('results').hidden = !active;
  $('show-builder').setAttribute('aria-pressed', String(!active)); $('show-results').setAttribute('aria-pressed', String(active));
}
function status(message, error = false) { $('form-status').textContent = message; $('form-status').classList.toggle('error', error); }
function time(ms) { return new Date(ms).toISOString().slice(11,19); }
function formatted(value) { return value == null ? '—' : value.toFixed(3); }
function render() {
  const at = clock.snapshot();
  $('cursor-time').textContent = time(at.cutoff) + ' UTC';
  const full = $('result-scope').value === 'full';
  const selection = { ...at, instrument: $('result-instrument').value, cutoff: full ? demoClose + 1000 : at.cutoff };
  const events = evaluateEvents({ rule: currentRule, observations: demoRows, selection, sessionClose: demoClose });
  const outcomeId = $('outcome-choice').value;
  const outcomeSpec = currentRule.outcomes.find(o => o.id === outcomeId);
  const formatOutcome = value => formatted(value) + (value != null && outcomeSpec?.measure === 'return' ? '%' : '');
  const stats = aggregateEvents(events, outcomeId);
  $('rule-description').textContent = `${currentRule.name} · ${selection.instrument} · ${previewing ? 'Unsaved preview' : 'v' + currentRule.version} · ${full ? 'Full session (retrospective)' : 'As of ' + time(at.cutoff) + ' UTC'} · synthetic data only`;
  $('replay-description').textContent = `SYN-1 · ${selection.instrument} · synthetic clock in UTC. Fixture coverage: QQQ only. Counts and outcomes follow this cursor.`;
  const sameSkew = currentRule.conditions.some(c => c.source === 'skew' && c.field === 'imbalance' && !c.relativeToAnchor && c.op === '>=' && c.value > 0);
  $('comparison-note').textContent = sameSkew && currentRule.combine === 'all' ? 'A positive same-input skew trigger necessarily agrees with a meter that labels every positive imbalance call-heavy. Disagreement is impossible for that pairing; these results do not compare independent signals.' : 'Descriptive observations only. No predictive conclusion is implied.';
  $('stats').replaceChildren(...[['Occurrences',stats.occurrences],['Sessions',stats.sessions],['Measured',stats.measured],['Pending',stats.pending],['Missing',stats.missing],['Mean',formatOutcome(stats.mean)]].map(([name,value]) => {
    const box = document.createElement('div'); box.className = 'stat'; const strong = document.createElement('strong'); strong.textContent = value;
    const label = document.createElement('span'); label.textContent = name; box.append(strong,label); return box;
  }));
  $('ledger').replaceChildren(...events.map(event => {
    const tr = document.createElement('tr'); const out = event.outcomes.find(o => o.id === outcomeId);
    for (const value of [time(event.triggeredAt),`v${event.ruleVersion}`,event.matchingObservations,out ? out.state === 'measured' ? formatOutcome(out.value) : out.state : 'Tracked']) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    const td = document.createElement('td'); const button = document.createElement('button'); button.textContent = 'Replay trigger';
    button.onclick = () => { selectedId = event.id; $('result-scope').value = 'cursor'; setCursor(event.triggeredAt); }; td.append(button); tr.append(td); return tr;
  }));
  $('empty-state').textContent = events.length ? '' : 'No occurrences at this cutoff for the selected class and instrument.';
  const selected = events.find(e => e.id === selectedId);
  $('event-evidence').hidden = !selected;
  $('event-evidence').textContent = selected ? selected.inputEvidence.map(input =>
    `${input.source} / ${input.id}\nObserved ${time(input.eventAt)} · available ${time(input.availableAt)} UTC\nValue: ${input.value}${input.anchor == null ? '' : ' · anchor: ' + input.anchor}\nSource: ${input.evidence}`).join('\n\n') : '';
  $('selected-event').textContent = selected ? `Trigger ${time(selected.triggeredAt)} · ${selected.origin} · v${selected.ruleVersion}. Evidence below follows the selected results view.` : 'Choose an occurrence to inspect its trigger evidence.';
  const visible = selectAsOf(demoRows.filter(r => r.source === 'skew'), { ...at, instrument: selection.instrument });
  const points = visible.map(r => `${(r.eventAt-demoStart)/3600000*900},${90-r.value.imbalance*180}`).join(' ');
  const line = document.createElementNS('http://www.w3.org/2000/svg','polyline');
  line.setAttribute('points',points); line.setAttribute('fill','none'); line.setAttribute('stroke','#c8e49a'); line.setAttribute('stroke-width','2');
  $('session-strip').replaceChildren(line);
}
function setCursor(ms) { $('cursor').value = String((ms-demoStart)/1000); clock.select({ ...demoSelection, mode:'replay', cutoff:ms }); render(); }
$('cursor').oninput = () => setCursor(demoStart + Number($('cursor').value)*1000);
$('show-builder').onclick = () => showResults(false); $('show-results').onclick = () => { showResults(true); render(); };
$('add-condition').onclick = () => condition(); $('add-outcome').onclick = () => outcome();
$('reopen').onclick = () => { openRule(choose($('versions').value)); $('result-version').value = key(currentRule); status('Saved definition reopened.'); };
$('new-rule').onclick = () => {
  openRule({ id: crypto.randomUUID(), version: 1, name:'', instruments:['QQQ'], combine:'all', transition:'rising', missingInput:'hold', conditions:[], outcomes:[] });
  status('New event class. Add conditions and optional outcomes, then preview or save.');
};
// Server-account controls reuse the Builder definition. Source metadata can add
// fields here without adding another timer, cursor or private ingestion route.
async function accountRequest(body) {
  const response=await fetch('/api/one-clock/research',{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',
    ...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  const result=await response.json();if(!response.ok)throw new Error(result.error??`Observer request failed (${response.status})`);return result;
}
async function loadAccount() {
  accountState=await accountRequest();
  for(const [source,fields] of Object.entries(accountState.sources)) {
    formFields.set(source,fields);if(!sources.some(([id])=>id===source))sources.push([source,source]);
  }
  for(const id of ['instrument','observed-instrument']) {
    const selected=$(id).value;$(id).replaceChildren(...accountState.instruments.map(value=>option(value)));
    if(accountState.instruments.includes(selected))$(id).value=selected;
  }
  if(!$('observed-day').value)$('observed-day').value=accountState.defaultSession;
  return accountState;
}
$('account-save').onclick=async()=>{
  try {
    const state=await loadAccount(),draft=formRule();
    const version=Math.max(0,...versions.filter(r=>r.id===draft.id).map(r=>r.version),...state.rules.filter(r=>r.rule.id===draft.id).map(r=>r.rule.version))+1;
    const {rule}=await accountRequest({action:'save',rule:{...draft,version}});
    versions.push(rule);currentRule=rule;versionOptions();openRule(rule);
    try{localStorage.setItem(storageKey,JSON.stringify(versions));}catch{/* account save remains durable */}
    status(`Saved ${rule.name} v${rule.version} to your account. Choose Observe saved version to activate it.`);
  }catch(error){status(error.message,true);}
};
for(const [id,action] of [['account-activate','activate'],['account-pause','deactivate']])$(id).onclick=async()=>{
  try{const result=await accountRequest({action,ruleId:editing.id,version:editing.version});status(result.active?'Observation activated for this immutable version. Collection follows the periodic service cadence.':'Observation paused. Already-triggered outcomes continue to mature.');}
  catch(error){status(error.message,true);}
};
$('account-reopen').onclick=async()=>{
  try {
    const state=await loadAccount(),saved=state.rules.map(r=>r.rule);
    versions=[...versions.filter(r=>!saved.some(s=>key(s)===key(r))),...saved];
    if(saved.length){currentRule=saved.at(-1);versionOptions();openRule(currentRule);}
    status(`Loaded ${saved.length} account definitions. Select a saved definition and Reopen.`);
  }catch(error){status(error.message,true);}
};
registry.register({id:'observed-events',version:'1',owner:'authenticated observer',maxAgeMs:86400000,
  async read({session,instrument,cutoff,signal}) {
    const response=await fetch('/api/one-clock/research?'+new URLSearchParams({session,instrument,cutoff:String(cutoff)}),{signal,credentials:'same-origin',cache:'no-store'});
    const body=await response.json();if(!response.ok)throw new Error(body.error??'Observer read failed');
    if(body.session!==session||body.instrument!==instrument||body.cutoff!==cutoff||!Array.isArray(body.events))throw new Error('Observer scope mismatch');
    const rows=body.events.map(event=>observation({source:'observed-events',id:event.id,revision:event.observerRevision,
      session,instrument,origin:'recorded',eventAt:event.triggeredAt,availableAt:event.recordedAt,evidence:event.id,value:event}));
    if(!signal.aborted)observedBody=body;return rows;
  }});
async function readObserved(selection,generation) {
  const live=selection.mode==='live';
  try {
    if(!observedMode||generation!==observedGeneration)return;
    const cutoff=live?Date.now():selection.cutoff;
    $('observed-cutoff').value=new Date(cutoff).toISOString().slice(0,19);
    clock.select({...selection,cutoff});
    $('observed-status').textContent='Reading retained evidence…';
    $('observed-ledger').replaceChildren();$('observed-stats').textContent='';$('observed-coverage').textContent='';$('observed-evidence').hidden=true;
    const result=await clock.read('observed-events');if(result.discarded||!observedMode||generation!==observedGeneration)return;
    const events=result.rows.map(r=>r.value),outcomes=events.flatMap(e=>e.outcomes);
    $('observed-status').textContent=`${result.selection.session} · ${result.selection.instrument} · ${events.length} retained occurrences as of ${new Date(result.selection.cutoff).toISOString()}. ${live?'Live cutoff; periodic source collection.':'Fixed cutoff.'}`;
    $('observed-coverage').textContent=observedBody.coverage.length?observedBody.coverage.map(c=>`${c.source}: ${c.reason}; ${c.pending} pending artifacts, ${c.gaps} known gaps. Coverage remains provisional.`).join(' '):'No source coverage receipt existed at this cutoff. An empty result does not establish that no market events occurred.';
    $('observed-coverage').textContent+=' '+(observedBody.evaluation??[]).map(r=>`${r.rule_id} v${r.version}: ${r.state}${r.attempted_at?' as of '+new Date(r.attempted_at).toISOString():''}. ${r.reason??''}`).join(' ');
    $('observed-stats').textContent=`${new Set(events.map(e=>e.session)).size} sessions · ${outcomes.filter(o=>o.state==='measured').length} measured outcomes · ${outcomes.filter(o=>o.state==='pending').length} pending · ${outcomes.filter(o=>o.state==='missing').length} missing`;
    $('observed-ledger').replaceChildren(...events.map(event=>{
      const tr=document.createElement('tr');
      for(const value of [`${event.ruleId} v${event.ruleVersion}`,new Date(event.triggeredAt).toISOString(),new Date(event.recordedAt).toISOString(),event.collection,
        event.outcomes.map(o=>`${o.id}: ${o.state==='measured'?formatted(o.value):o.state}`).join('; ')||'Tracking only']) {const td=document.createElement('td');td.textContent=value;tr.append(td);}
      const td=document.createElement('td'),button=document.createElement('button');button.textContent='Inspect receipt';button.onclick=()=>{
        $('observed-evidence').hidden=false;$('observed-evidence').textContent=JSON.stringify({triggeredAt:event.triggeredAt,recordedAt:event.recordedAt,inputs:event.inputEvidence,outcomes:event.outcomes,lineage:event.lineage},null,2);
      };td.append(button);tr.append(td);return tr;
    }));
    $('observed-evidence').hidden=true;
  }catch(error){if(observedMode&&generation===observedGeneration)$('observed-status').textContent=error.message;}
}
function commitObserved(live) {
  const generation=++observedGeneration;clearInterval(timer);timer=null;
  observedSelection=Object.freeze({session:$('observed-day').value,instrument:$('observed-instrument').value,
    mode:live?'live':'replay',origin:'recorded',cutoff:live?Date.now():Date.parse($('observed-cutoff').value+'Z')});
  const selection=observedSelection;
  readObserved(selection,generation);
  if(live)timer=setInterval(()=>readObserved(selection,generation),60000);
}
$('show-observed').onclick=async()=>{
  const generation=++observedGeneration;observedSelection=null;
  observedMode=true;clearInterval(timer);timer=null;$('builder').hidden=true;$('results').hidden=true;document.querySelector('.replay').hidden=true;$('observed-results').hidden=false;
  $('show-observed').setAttribute('aria-pressed','true');$('show-builder').setAttribute('aria-pressed','false');$('show-results').setAttribute('aria-pressed','false');
  document.querySelector('.notice').hidden=true;$('observed-ledger').replaceChildren();$('observed-stats').textContent='';$('observed-coverage').textContent='';$('observed-evidence').hidden=true;
  try{await loadAccount();if(!observedMode||generation!==observedGeneration)return;commitObserved(true);}catch(error){if(observedMode&&generation===observedGeneration)$('observed-status').textContent=error.message;}
};
$('observed-read').onclick=()=>commitObserved(false);
$('observed-live').onclick=()=>commitObserved(true);
$('preview-rule').onclick = () => { try { currentRule = formRule(); previewing = true; updateOutcomes(); render(); showResults(true); } catch(e) { status(e.message,true); } };
$('rule-form').onsubmit = event => {
  event.preventDefault();
  try {
    const saved = saveRule(versions,formRule()); const next = [...versions,saved];
    localStorage.setItem(storageKey,JSON.stringify(next)); versions = next; currentRule = saved;
    versionOptions(); openRule(saved); status(`Saved ${saved.name} as v${saved.version}. Earlier versions are unchanged.`);
  } catch(e) { status(`Not saved: ${e.message}. Export the rule to retain a copy.`,true); }
};
$('export-rule').onclick = () => {
  try { const rule = formRule(); const url = URL.createObjectURL(new Blob([JSON.stringify(rule,null,2)],{type:'application/json'}));
    const a = document.createElement('a'); a.href=url; a.download=`one-clock-${rule.id}-v${rule.version}.json`; a.click(); URL.revokeObjectURL(url);
  } catch(e) { status(e.message,true); }
};
$('result-version').onchange = () => { const rule=choose($('result-version').value); openRule(rule); $('versions').value=key(rule); };
$('outcome-choice').onchange=render; $('result-scope').onchange=render; $('result-instrument').onchange=render;
$('play').onclick = () => {
  if (timer) { clearInterval(timer); timer=null; } else timer=setInterval(() => {
    const next=Math.min(demoClose+1000,clock.snapshot().cutoff+60000); setCursor(next);
    if(next>=demoClose+1000) $('play').click();
  },700);
  $('play').textContent=timer?'Pause':'Play'; $('play').setAttribute('aria-pressed',String(!!timer));
};
window.addEventListener('pagehide',()=>{ clearInterval(timer); clock.dispose(); });
versionOptions(); openRule(currentRule); if(storageError) status(storageError,true);
