import {oiSkewAuditRule} from '../shared/one-clock-decision-events.js';
import {renderReceipt} from './one-clock-encounter-summary.js';
import {encounterForm} from './one-clock-encounter-form.js';
import { createClock, createSourceRegistry, selectAsOf,observation } from '../shared/one-clock.js';
import { eventRule, saveRule, evaluateEvents } from '../shared/one-clock-events.js';
import { demoRules, demoRows, demoStart, demoClose, demoSelection, demoSources } from '../shared/one-clock-demo.js';
import {createResultsView} from './one-clock-results-view.js';
import {ruleKey} from '../shared/one-clock-results.js';
import {loadFlowStudy} from '../shared/one-clock-flow-study.js';

const $ = id => document.getElementById(id);
const levelForm=encounterForm(document);
const archiveMode = new URLSearchParams(location.search).get('dataset') === 'order-flow';
const dataset = $('offline-dataset');
dataset.value=archiveMode?'order-flow':'synthetic';
dataset.onchange=()=>{const url=new URL(location.href);if(dataset.value==='order-flow')url.searchParams.set('dataset','order-flow');else url.searchParams.delete('dataset');location.assign(url);};
let study={rules:demoRules,sources:demoSources,rows:demoRows,selection:demoSelection,start:demoStart,close:demoClose};
if(archiveMode){
  const notice=document.querySelector('.notice');
  notice.querySelector('strong').textContent='Reconstructed QQQ order flow · September 25, 2026';
  notice.querySelector('span').textContent='Loading the audited archive…';
  const loadingSections = [document.querySelector('nav'), $('builder'), document.querySelector('.replay')];
  for (const section of loadingSections) section.inert=true;
  try{study=await loadFlowStudy();}
  catch(error){
    notice.querySelector('span').textContent=error.message;
    $('builder').hidden=true;
    document.querySelector('.replay').hidden=true;
    document.querySelector('nav').hidden=true;
    throw error;
  }
  notice.querySelector('span').textContent='Minute samples of 16 contracts at strikes $740–747. Flow gamma assumes 100% passive dealer participation. Historical publication times are unavailable.';
  $('account-controls').hidden=true;$('account-help').hidden=true;
  $('instrument').replaceChildren(option('QQQ'));
  $('session-strip').hidden=true;
  for (const section of loadingSections) section.inert=false;
}
const {rules:initialRules,sources:studySources,rows:studyRows,selection:studySelection,start:studyStart,close:studyClose}=study;
const storageKey = archiveMode?'one-clock.order-flow.rules.v1':'one-clock.rules.v1';
$('cursor').max=String((studyClose-studyStart+1000)/1000);
let versions = structuredClone(initialRules);
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
for (const source of studySources) registry.register({ ...source, version: '1', owner: archiveMode?'audited order-flow replay':'offline fixture',
  maxAgeMs: 150000, read: async () => studyRows.filter(r => r.source === source.id) });
const clock = createClock({ registry, initial: { ...studySelection, mode: 'replay', cutoff: studyStart + 301000 } });
const fixtureResults=createResultsView({container:$('stats'),stats:$('stats'),ledger:$('ledger'),fixture:true,
  onFilter:()=>{selectedId=null;$('order-flow-evidence').hidden=true;$('event-evidence').hidden=true;$('selected-event').textContent='Choose an occurrence in the filtered table to inspect its evidence.';},
  onInspect:event=>{selectedId=event.id;render();},onReplay:event=>{selectedId=event.id;$('result-scope').value='cursor';setCursor(event.triggeredAt);}});
const retainedResults=createResultsView({container:$('observed-stats'),stats:$('observed-stats'),ledger:$('observed-ledger'),
  onFilter:()=>{$('observed-evidence').hidden=true;},
  onInspect:event=>renderReceipt($('observed-evidence'),event),
  onReplay:event=>{location.href='/?'+new URLSearchParams({one_clock_session:event.session,one_clock_instrument:event.instrument,one_clock_at:String(event.triggeredAt)})+'#history-clock';}});

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
const formFields=new Map(studySources.map(source=>[source.id,source.fields]));
function setChoices(control, choices, selected) {
  const options = choices.map(([id, label]) => option(id, label));
  // Saved rules can use an account field before the account request returns.
  if (selected && !choices.some(([id]) => id === selected)) options.push(option(selected));
  control.replaceChildren(...options);
  if (selected) control.value = selected;
}
function sourceFields(source, field) {
  const sourceControl = selectControl('Input', 'source', [], source);
  const fieldControl = selectControl('Field', 'field', [], field);
  const sourceSelect = sourceControl.querySelector('select');
  const fieldSelect = fieldControl.querySelector('select');
  setChoices(sourceSelect, sources, source);
  setChoices(fieldSelect, Object.entries(formFields.get(source) ?? {}), field);
  sourceSelect.onchange = () => setChoices(fieldSelect, Object.entries(formFields.get(sourceSelect.value) ?? {}));
  return [sourceControl,fieldControl];
}
function condition(c = { source: archiveMode?'orderFlow':'skew', field: archiveMode?'k744.flow':'imbalance', op: '>=', value: archiveMode?0:0.3, maxAgeMs: 150000 }) {
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
    selectControl('Measure', 'measure', [['return','Return (%)'],['change','Change'],['value','Endpoint value'],['category','Endpoint category']], o.measure),
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
  const level=levelForm.definition();
  if(level)return eventRule({id:editing.id,version:editing.version,name:$('rule-name').value,...level});
  return eventRule({ ...(editing.archiveHandoff ? {archiveHandoff:editing.archiveHandoff} : {}), id: editing.id, version: editing.version, name: $('rule-name').value,
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
  levelForm.open(rule);
  $('archive-encounter-controls').hidden=!rule.levelEncounter || archiveMode;
  $('rule-origin').hidden=!rule.archiveHandoff;
  $('rule-origin').textContent=rule.archiveHandoff ? 'Strategy version '+rule.archiveHandoff.version+' · hypothesis '+rule.archiveHandoff.id : '';
  if (rule.conditions.length) { currentRule = rule; previewing = false; }
  $('rule-name').value = rule.name;
  const instrument = rule.instruments[0];
  if (![...$('instrument').options].some(o => o.value === instrument)) $('instrument').append(option(instrument));
  $('instrument').value = instrument;
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
  if(observedMode){observedMode=false;clearInterval(timer);timer=null;clock.select({...studySelection,mode:'replay',cutoff:studyStart+301000});}
  $('observed-results').hidden=true;document.querySelector('.replay').hidden=false;
  $('show-observed').setAttribute('aria-pressed','false');
  document.querySelector('.notice').hidden=false;
  $('builder').hidden = active; $('results').hidden = !active;
  $('show-builder').setAttribute('aria-pressed', String(!active)); $('show-results').setAttribute('aria-pressed', String(active));
}
function status(message, error = false) { $('form-status').textContent = message; $('form-status').classList.toggle('error', error); }
function time(ms) { return new Date(ms).toISOString().slice(11,19); }
function render() {
  const at = clock.snapshot();
  $('cursor-time').textContent = time(at.cutoff) + ' UTC';
  const full = $('result-scope').value === 'full';
  const selection = { ...at, instrument: $('result-instrument').value, cutoff: full ? studyClose + 1000 : at.cutoff };
  const events = evaluateEvents({ rule: currentRule, observations: studyRows, selection, sessionClose: studyClose });
  const outcomeId = $('outcome-choice').value;
  $('rule-description').textContent = `${currentRule.name} · ${selection.instrument} · ${previewing ? 'Unsaved preview' : 'v' + currentRule.version} · ${full ? 'Full session (retrospective)' : 'As of ' + time(at.cutoff) + ' UTC'} · ${studySelection.origin} data`;
  $('replay-description').textContent = archiveMode?'QQQ · minute-close reconstruction, UTC. Snapshot prices use their archive timestamps.':`SYN-1 · ${selection.instrument} · synthetic clock in UTC. Fixture coverage: QQQ only. Counts and outcomes follow this cursor.`;
  const sameSkew = currentRule.conditions.some(c => c.source === 'skew' && c.field === 'imbalance' && !c.relativeToAnchor && c.op === '>=' && c.value > 0);
  $('comparison-note').textContent = sameSkew && currentRule.combine === 'all' ? 'A positive same-input skew trigger necessarily agrees with a meter that labels every positive imbalance call-heavy. Disagreement is impossible for that pairing; these results do not compare independent signals.' : 'Descriptive observations only. No predictive conclusion is implied.';
  fixtureResults.update({events,rules:[currentRule],cutoff:selection.cutoff,origin:studySelection.origin,outcome:outcomeId?{ruleKey:ruleKey(currentRule),id:outcomeId}:null});
  $('empty-state').textContent = events.length ? '' : 'No occurrences at this cutoff for the selected class and instrument.';
  const selected = events.find(e => e.id === selectedId);
  $('event-evidence').hidden = !selected;
  $('event-evidence').textContent = selected ? selected.inputEvidence.map(input =>
    `${input.source} / ${input.id}\nObserved ${time(input.eventAt)} · available ${time(input.availableAt)} UTC\nValue: ${input.value}${input.anchor == null ? '' : ' · anchor: ' + input.anchor}\nSource: ${input.evidence}`).join('\n\n') : '';
  $('selected-event').textContent = selected ? `Trigger ${time(selected.triggeredAt)} · ${selected.origin} · v${selected.ruleVersion}. Evidence below follows the selected results view.` : 'Choose an occurrence to inspect its trigger evidence.';
  const flowLink=$('order-flow-evidence');
  flowLink.hidden=!archiveMode || !selected;
  if(archiveMode && selected) flowLink.href='/order-flow.html?'+new URLSearchParams({
    one_clock_session:selected.session,one_clock_instrument:selected.instrument,one_clock_at:String(selected.triggeredAt)});
  if(archiveMode)return;
  const visible = selectAsOf(studyRows.filter(r => r.source === 'skew'), { ...at, instrument: selection.instrument });
  const points = visible.map(r => `${(r.eventAt-studyStart)/3600000*900},${90-r.value.imbalance*180}`).join(' ');
  const line = document.createElementNS('http://www.w3.org/2000/svg','polyline');
  line.setAttribute('points',points); line.setAttribute('fill','none'); line.setAttribute('stroke','#c8e49a'); line.setAttribute('stroke-width','2');
  $('session-strip').replaceChildren(line);
}
function setCursor(ms) { $('cursor').value = String((ms-studyStart)/1000); clock.select({ ...studySelection, mode:'replay', cutoff:ms }); render(); }
$('cursor').oninput = () => setCursor(studyStart + Number($('cursor').value)*1000);
$('show-builder').onclick = () => showResults(false); $('show-results').onclick = () => { showResults(true); render(); };
$('add-condition').onclick = () => condition(); $('add-outcome').onclick = () => outcome();
$('reopen').onclick = () => { openRule(choose($('versions').value)); $('result-version').value = key(currentRule); status('Saved definition reopened.'); };
$('oi-skew-audit').hidden = archiveMode;
$('oi-skew-audit').onclick = async () => {
  try {
    await loadAccount();
    openRule(oiSkewAuditRule(crypto.randomUUID()));
    status('OI Skew audit draft. All regular-session decisions count, including passes. Save to your account to retain the definition.');
  } catch (error) { status(error.message, true); }
};
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
let accountRequestInFlight = null;
function loadAccount() {
  if (!accountRequestInFlight) {
    accountRequestInFlight = accountRequest().then(updateAccountControls).finally(() => {accountRequestInFlight = null;});
  }
  return accountRequestInFlight;
}
function refreshSourceChoices() {
  for(const [source,fields] of Object.entries(accountState.sources)) {
    formFields.set(source,fields);if(!sources.some(([id])=>id===source))sources.push([source,source]);
  }
  // Update only the choices. A slow account response must preserve unsaved
  // values, comparison operators, horizons and rows added since page load.
  for (const row of document.querySelectorAll('#conditions .condition, #outcomes .outcome')) {
    const source = row.querySelector('[data-field="source"]');
    const field = row.querySelector('[data-field="field"]');
    setChoices(source, sources, source.value);
    setChoices(field, Object.entries(formFields.get(source.value) ?? {}), field.value);
  }
}
function updateAccountControls(state) {
  accountState = state;
  if (!archiveMode) refreshSourceChoices();
  for (const id of archiveMode ? ['observed-instrument'] : ['instrument','observed-instrument']) {
    const choices = accountState.instruments.map(value => [value,value]);
    if (id === 'observed-instrument') choices.unshift(['ALL','All instruments']);
    setChoices($(id), choices, $(id).value);
  }
  if(!$('encounter-day').value)$('encounter-day').value=accountState.defaultSession;
  if(!$('observed-day').value)$('observed-day').value=accountState.defaultSession;
  if(!$('observed-end').value)$('observed-end').value=accountState.defaultSession;
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
    const [from,to]=session.split('/');
    const response=await fetch('/api/one-clock/results?'+new URLSearchParams({from,to,instrument,cutoff:String(cutoff)}),{signal,credentials:'same-origin',cache:'no-store'});
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
    retainedResults.clear();$('observed-coverage').textContent='';$('observed-evidence').hidden=true;
    const result=await clock.read('observed-events');if(result.discarded||!observedMode||generation!==observedGeneration)return;
    const events=result.rows.map(r=>r.value);
    $('observed-status').textContent=`${result.selection.session} · ${result.selection.instrument} · ${events.length} retained occurrences as of ${new Date(result.selection.cutoff).toISOString()}. ${live?'Live cutoff; periodic source collection.':'Fixed cutoff.'}`;
    $('observed-coverage').textContent=observedBody.coverage.length?observedBody.coverage.map(r=>`${r.session} ${r.instrument} ${r.coverage.source}: ${r.coverage.reason}; ${r.coverage.pending} pending artifacts, ${r.coverage.gaps} known gaps. Coverage remains provisional.`).join(' '):'No source coverage receipt existed at this cutoff. An empty result does not establish that no market events occurred.';
    $('observed-coverage').textContent+=' '+(observedBody.evaluation.length?observedBody.evaluation.map(r=>`${r.session} ${r.instrument} ${r.rule_id} v${r.version}: ${r.state}${r.attempted_at?' as of '+new Date(r.attempted_at).toISOString():''}. ${r.reason??''}`).join(' '):'No rule-evaluation receipts in the selected scope.');
    if(observedBody.observerNotice)$('observed-coverage').textContent+=' '+observedBody.observerNotice;
    retainedResults.update({events,rules:observedBody.rules,cutoff:result.selection.cutoff,origin:'recorded',owner:observedBody.owner});
    $('observed-evidence').hidden=true;
  }catch(error){if(observedMode&&generation===observedGeneration){retainedResults.clear({forget:true});$('observed-status').textContent=error.message;}}
}
function commitObserved(live) {
  const generation=++observedGeneration;clearInterval(timer);timer=null;
  observedSelection=Object.freeze({session:$('observed-day').value+'/'+$('observed-end').value,instrument:$('observed-instrument').value,
    mode:live?'live':'replay',origin:'recorded',cutoff:live?Date.now():Date.parse($('observed-cutoff').value+'Z')});
  const selection=observedSelection;
  readObserved(selection,generation);
  if(live)timer=setInterval(()=>readObserved(selection,generation),60000);
}
function showObservedPanel() {
  observedGeneration++;observedSelection=null;
  observedMode=true;clearInterval(timer);timer=null;$('builder').hidden=true;$('results').hidden=true;document.querySelector('.replay').hidden=true;$('observed-results').hidden=false;
  $('show-observed').setAttribute('aria-pressed','true');$('show-builder').setAttribute('aria-pressed','false');$('show-results').setAttribute('aria-pressed','false');
  document.querySelector('.notice').hidden=true;retainedResults.clear();$('observed-coverage').textContent='';$('observed-evidence').hidden=true;
  return observedGeneration;
}
$('encounter-run').onclick=async()=>{
  const button=$('encounter-run'), progress=$('encounter-progress');
  const session=$('encounter-day').value, rule=editing, generation=++observedGeneration;
  button.disabled=true;
  try {
    if(!session)throw new Error('Choose an archived session.');
    const draft=formRule();
    const fields=['name','instruments','levelEncounter','minimumBucket','groups','approach','contact','maxGapMs','outcomes','id','source','field','measure','horizonMs','toleranceMs','maxAgeMs'];
    if(JSON.stringify(draft,fields)!==JSON.stringify(rule,fields))throw new Error('Save your edited study to your account before replaying it.');
    progress.textContent='Opening the archived session…';
    for(let batch=0;batch<100;batch++) {
      const body=await accountRequest({action:'replay-encounters',session,ruleId:rule.id,version:rule.version});
      if(generation!==observedGeneration || editing!==rule)return;
      const c=body.coverage;
      progress.textContent=c.retainedVersions+' archived snapshots indexed · '+c.pending+' pending · '+c.gaps+' with gaps.';
      if(c.gapReasons)progress.textContent+=' '+Object.entries(c.gapReasons).map(([reason,count])=>count+' '+reason.replaceAll('-',' ')).join(', ');
      if(body.complete) {
        showObservedPanel();retainedResults.clear({forget:true});$('observed-status').textContent='Archived replay · '+session+' · '+body.events.length+' encounters';
        $('observed-coverage').textContent=progress.textContent+' Source availability gates every encounter. Replay may include evidence the observer retained later.';
        retainedResults.update({events:body.events,rules:[body.rule],origin:'recorded',cutoff:body.cutoff});
        return;
      }
      if(!c.indexComplete || !c.processed || !c.pending){progress.textContent+=' '+c.reason;return;}
    }
    progress.textContent+=' Choose Replay saved study to continue.';
  }catch(error){progress.textContent=error.message;}finally{button.disabled=false;}
};
$('show-observed').onclick=async()=>{
  const generation=showObservedPanel();
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
    const next=Math.min(studyClose+1000,clock.snapshot().cutoff+60000); setCursor(next);
    if(next>=studyClose+1000) $('play').click();
  },700);
  $('play').textContent=timer?'Pause':'Play'; $('play').setAttribute('aria-pressed',String(!!timer));
};
window.addEventListener('pagehide',()=>{ clearInterval(timer); clock.dispose(); });
versionOptions(); openRule(currentRule); if(storageError) status(storageError,true);

async function loadArchiveDesign(id) {
  if (!/^[a-z0-9][a-z0-9._-]{0,95}$/.test(id)) throw Error('Invalid hypothesis reference.');
  const response=await fetch('/api/strategy-archive?version='+encodeURIComponent(id));
  if (!response.ok) throw Error('Strategy Archive hypothesis is unavailable.');
  const record=(await response.json()).records.find(row=>row.id===id && row.kind==='handoff');
  if (!record) throw Error('The selected hypothesis is unavailable.');
  const design=record.data.design;
  for (const label of ['Population','Trigger','Comparator','Outcome','Horizon','Evaluation plan']) {
    const line=document.createElement('p');
    line.textContent=label+' — '+design[label];
    $('archive-plan').append(line);
  }
  $('archive-design').hidden=false;
  $('use-design').onclick=()=>{
    openRule({id:crypto.randomUUID(),version:1,name:record.version,instruments:['QQQ'],
      combine:'all',transition:'rising',missingInput:'hold',conditions:[],outcomes:[],
      archiveHandoff:{id:record.id,version:record.version}});
    status('Choose the instrument and encode the trigger and outcome from this design.');
  };
}
const archiveId=new URLSearchParams(location.search).get('strategy_handoff');
if (!archiveMode && archiveId) loadArchiveDesign(archiveId).catch(error=>status(error.message,true));
// Logged-out and offline visitors keep the local engineering fixture. Explicit
// account actions retry and present the server's authentication or availability message.
if (!archiveMode) loadAccount().catch(() => {});
