import {renderEncounterSummary} from './one-clock-encounter-summary.js';
import {projectResults,compareResults,measureLabel,ruleKey} from '../shared/one-clock-results.js';
const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);return node;};
const number=value=>value==null?'—':typeof value==='number'?value.toLocaleString(undefined,{maximumFractionDigits:4}):JSON.stringify(value);
function options(select,items,keepMissing=false) {
  const previous=select.value;
  if(keepMissing&&previous&&!items.some(([value])=>value===previous))items.push([previous,'Selected filter unavailable at this cutoff']);
  select.replaceChildren(...items.map(([value,label])=>{const option=el('option',label);option.value=value;return option;}));
  if(items.some(([value])=>value===previous))select.value=previous;
}
export function createResultsView({container,stats,ledger,onInspect,onReplay,onFilter,fixture=false}) {
  const panel=el('div'),toolbar=el('div');toolbar.className='toolbar';panel.append(toolbar);container.before(panel);
  const controls={};
  function select(id,label,values) {
    const wrapper=el('label',label),input=el('select');input.setAttribute('aria-label',label);wrapper.append(input);toolbar.append(wrapper);controls[id]=input;options(input,values);return input;
  }
  select('class','Filter class / version',[['','All classes and versions']]);
  select('outcome','Measurement and horizon',[['','Tracking only']]);
  select('instrument','Filter instrument',[['','All loaded instruments']]);
  select('collection','Collection',[['','All collections'],['prospective','Prospective'],['backfill','Backfill'],['replay','Replay'],['retrospective','Retrospective'],['unknown','Unknown']]);
  select('state','Outcome state',[['','All occurrences'],['measured','Measured'],['pending','Pending'],['missing','Missing'],['unscored','No compatible outcome']]);
  select('timing','Headline endpoint timing',[['exact','On target'],['delayed','After target']]);
  controls.timing.parentElement.hidden=true;
  const searchLabel=el('label','Search occurrences'),search=el('input');search.type='search';search.setAttribute('aria-label','Search occurrences');searchLabel.append(search);toolbar.append(searchLabel);
  if(fixture){controls.class.parentElement.hidden=true;controls.outcome.parentElement.hidden=true;}
  const scope=el('p'),distribution=el('div'),comparison=el('details'),summary=el('summary','Compare measured outcomes');scope.className='muted';comparison.append(summary);
  const compareControls=el('div');compareControls.className='toolbar';comparison.append(compareControls);
  const compare={};
  function comparisonSelect(id,label,items) {const wrapper=el('label',label),input=el('select');input.setAttribute('aria-label',label);wrapper.append(input);compareControls.append(wrapper);options(input,items);compare[id]=input;}
  comparisonSelect('field','Compare groups by',[['rule','Class / version'],['instrument','Instrument'],['collection','Collection']]);
  comparisonSelect('a','Group A',[['','Choose A']]);comparisonSelect('b','Group B',[['','Choose B']]);comparisonSelect('category','Category rate',[['','Choose category']]);
  const comparisonText=el('p');comparisonText.className='muted';comparison.append(comparisonText);
  panel.append(scope);container.after(distribution,comparison);
  const encounterTable=el('div');encounterTable.className='table-wrap';distribution.before(encounterTable);
  let dataset=null,view=null,lastOwner=null,outcomeChosen=false;
  function drawComparison() {
    comparisonText.textContent='Choose two groups with the same measurement and horizon.';
    compare.category.parentElement.hidden=view?.summary.kind!=='category';
    if(!view||!compare.a.value||!compare.b.value||view.summary.kind==='category'&&!compare.category.value)return;
    try {
      const result=compareResults(view.rows,{field:compare.field.value,a:compare.a.value,b:compare.b.value,kind:view.summary.kind,
        ...(view.summary.kind==='category'?{category:JSON.parse(compare.category.value)}:{})});
      const unit=view.spec?.measure==='return'?'percentage points':result.unit;
      comparisonText.textContent=`A minus B: ${number(result.estimate)} ${unit}. Session-clustered standard error: ${number(result.standardError)}. ${result.aCount} measured in A (${result.aSessions} sessions), ${result.bCount} in B (${result.bSessions} sessions); ${result.sessions} distinct sessions. ${result.reason}`;
    }catch(error){comparisonText.textContent=error.message;}
  }
  function draw() {
    if(!dataset)return;
    try {
      const outcome=fixture?dataset.outcome:controls.outcome.value?JSON.parse(controls.outcome.value):null;
      const headline=dataset.rules.find(r=>ruleKey(r)===outcome?.ruleKey)?.transition==='per-headline';
      controls.timing.parentElement.hidden=!headline;
      view=projectResults({...dataset,outcome,filters:{ruleKeys:controls.class.value?[controls.class.value]:[],
        instruments:controls.instrument.value?[controls.instrument.value]:[],collections:controls.collection.value?[controls.collection.value]:[],
        endpointTiming:headline?controls.timing.value:null,states:controls.state.value?[controls.state.value]:[],query:search.value}});
      renderEncounterSummary(encounterTable,view.rows.map(r=>r.event),dataset.rules.filter(r=>!controls.class.value||ruleKey(r)===controls.class.value));
      const s=view.summary;
      scope.textContent=`${view.origin} evidence · ${s.occurrences} matching rows from ${dataset.events.length} loaded occurrences at ${new Date(view.cutoff).toISOString()}. Statistics use measured outcomes only; incompatible or absent measurements are counted separately.`;
      if(headline)scope.textContent+=' '+view.excludedTiming+' measured returns belong to the other timing group.';
      stats.replaceChildren(...[['Occurrences',s.occurrences],['Sessions',s.sessions],['Measured',s.measured],['Pending',s.pending],['Missing',s.missing],['Unscored',s.unscored],['Mean',number(s.mean)],['Median',number(s.median)]].map(([name,value])=>{
        const box=el('div');box.className='stat';box.append(el('strong',value),el('span',name));return box;
      }));
      ledger.replaceChildren(...view.rows.map(({event,outcome})=>{
        const tr=el('tr'),definition=dataset.rules.find(r=>ruleKey(r)===ruleKey(event));
        for(const value of [`${definition?.name??event.ruleId} · v${event.ruleVersion} · ${event.instrument}${event.headline?' · '+event.headline.title:event.breach?' · '+event.breach.direction+' · '+event.breach.closeClass.replaceAll('_',' '):''}${event.encounter?' · $'+event.encounter.strike+' · '+event.encounter.cohort+' · '+event.encounter.approach+' · '+event.encounter.contact:''}`,event.decisionAt !== undefined
            ? (event.breach?'Candle close ':'Decision ') + new Date(event.decisionAt).toISOString() + (event.breach && event.origin==='reconstructed'?'': ' · '+(event.breach?'received ':'published ')+new Date(event.triggeredAt).toISOString())
            : new Date(event.triggeredAt).toISOString(),
          event.recordedAt?new Date(event.recordedAt).toISOString():event.session,event.decisionAt === undefined || event.breach ? event.collection??'replay' :
            [event.collection??'replay', 'no sealed forecast', outcome?.baselineTiming === 'reconstructed' ? 'reconstructed baseline' : null].filter(Boolean).join(' · '),
          outcome?outcome.state==='measured'?number(outcome.value)+(view.spec?.measure==='return'?'%':'')+(event.headline?' · '+number(outcome.elapsedMs/60000)+' min elapsed'+(outcome.baseStale?' · stale base':''):''):outcome.state:'No compatible outcome'])tr.append(el('td',value));
        const cell=el('td'),inspect=el('button','Inspect receipt');inspect.onclick=()=>onInspect(event);cell.append(inspect);
        if(onReplay) {
          const replay = el('button', event.journalFill ? 'Open fill in journal replay' : event.decisionAt === undefined ? 'Replay trigger' : event.breach?'Replay evidence':'Replay publication');
          replay.onclick = () => onReplay(event, event.triggeredAt);
          cell.append(replay);
          if(event.headline && outcome?.availableAt){
            const endpoint=el('button','Replay quote receipt');endpoint.onclick=()=>onReplay(event,outcome.availableAt);cell.append(endpoint);
          }
          if (event.decisionAt !== undefined) {
            const decision = el('button', event.breach?'Replay candle close':'Replay decision');
            decision.onclick = () => onReplay(event, event.decisionAt);
            cell.append(decision);
          }
        }
        tr.append(cell);return tr;
      }));
      distribution.replaceChildren(el('h3',s.kind==='category'?'Measured category counts and rates':'Measured outcome distribution'));
      const values=s.kind==='category'?s.categories.map(c=>({label:`${typeof c.value}: ${JSON.stringify(c.value)}`,count:c.count})):s.distribution.map(b=>({label:b.low===b.high?number(b.low):`${number(b.low)} to ${number(b.high)}${b.includesHigh?' (inclusive upper bound)':''}`,count:b.count}));
      if(!values.length)distribution.append(el('p','No measured outcomes in this filtered scope.'));
      for(const item of values){const line=el('p'),bar=el('meter');bar.max=s.measured;bar.value=item.count;bar.setAttribute('aria-label',item.label);line.append(el('span',`${item.label}: ${item.count} (${number(item.count/s.measured*100)}%) `),bar);distribution.append(line);}
      const groups=new Map();for(const {event} of view.rows){const value=compare.field.value==='rule'?ruleKey(event):event[compare.field.value]??'replay';groups.set(value,compare.field.value==='rule'?`${event.ruleId} v${event.ruleVersion}`:value);}
      options(compare.a,[['','Choose A'],...groups]);options(compare.b,[['','Choose B'],...groups]);
      options(compare.category,[['','Choose category'],...s.categories.map(c=>[JSON.stringify(c.value),`${typeof c.value}: ${JSON.stringify(c.value)}`])]);drawComparison();
    }catch(error){encounterTable.replaceChildren();stats.textContent='';ledger.replaceChildren();distribution.replaceChildren();comparisonText.textContent='';scope.textContent=error.message;}
  }
  const filterChanged=()=>{onFilter?.();draw();};
  for(const control of Object.values(controls))control.onchange=filterChanged;search.oninput=filterChanged;
  controls.outcome.onchange=()=>{outcomeChosen=true;filterChanged();};
  compare.field.onchange=draw;for(const id of ['a','b','category'])compare[id].onchange=drawComparison;
  return {
    clear({forget=false}={}){dataset=null;view=null;encounterTable.replaceChildren();panel.hidden=true;comparison.hidden=true;stats.textContent='';ledger.replaceChildren();distribution.replaceChildren();scope.textContent='';comparisonText.textContent='';
      if(forget){for(const id of ['class','outcome','instrument'])options(controls[id],[['','No loaded results']]);for(const id of ['a','b','category'])options(compare[id],[['','Choose a group']]);controls.collection.value='';controls.state.value='';search.value='';lastOwner=null;outcomeChosen=false;}},
    update(value){
      const owner=value.owner??'fixture';if(lastOwner!==null&&lastOwner!==owner)this.clear({forget:true});lastOwner=owner;
      dataset=value;panel.hidden=false;comparison.hidden=false;
      options(controls.class,[['','All classes and versions'],...value.rules.map(r=>[ruleKey(r),`${r.name} v${r.version}`])],true);
      options(controls.outcome,[['','Tracking only'],...value.rules.flatMap(r=>r.outcomes.map(o=>[JSON.stringify({ruleKey:ruleKey(r),id:o.id}),`${r.name} v${r.version}: ${o.field} · ${o.horizonMs/60000} min · ${measureLabel(o,r)}`]))],true);
      if(!outcomeChosen&&!controls.outcome.value&&controls.outcome.options.length>1)controls.outcome.selectedIndex=1;
      options(controls.instrument,[['','All loaded instruments'],...[...new Set(value.events.map(e=>e.instrument))].map(i=>[i,i])],true);
      draw();
    }
  };
}
