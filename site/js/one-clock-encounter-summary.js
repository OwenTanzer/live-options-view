export function renderEncounterSummary(container,events,rules) {
  container.replaceChildren();
  for(const rule of rules.filter(r=>r.levelEncounter)) {
    const matching=events.filter(e=>e.ruleId===rule.id && e.ruleVersion===rule.version && e.encounter);
    const table=document.createElement('table'), caption=table.createCaption();
    caption.textContent=rule.name+' v'+rule.version+' · direction relative to strike';
    const header=table.createTHead().insertRow();
    for(const label of ['OI group','Minutes','Touches','Rejection','Continuation','Tie','Missing','Pending','Continuation / measured']) {
      const th=document.createElement('th');th.scope='col';th.textContent=label;header.append(th);
    }
    const body=table.createTBody();
    for(const group of rule.levelEncounter.groups) {
      const cohort=matching.filter(e=>e.encounter.cohort===group);
      for(const spec of rule.outcomes) {
        const counts={rejection:0,continuation:0,tie:0,missing:0,pending:0};
        for(const event of cohort) {
          const o=event.outcomes.find(o=>o.id===spec.id);
          const bin=o?.state==='measured'?o.value:o?.state??'missing';
          if(Object.hasOwn(counts,bin))counts[bin]++;
        }
        const n=counts.rejection+counts.continuation+counts.tie;
        const row=body.insertRow();
        for(const v of [group,spec.horizonMs/60000,cohort.length,counts.rejection,counts.continuation,
          counts.tie,counts.missing,counts.pending,n?(100*counts.continuation/n).toFixed(1)+'%':'—'])row.insertCell().textContent=String(v);
      }
    }
    container.append(table);
  }
}

export function renderReceipt(container,event) {
  container.replaceChildren();
  const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
  summary.textContent='Full receipt';pre.textContent=JSON.stringify(event,null,2);details.append(summary,pre);
  if(event.encounter) {
    const {strike,cohort,contact,approach,calibration,lowerTime,upperTime}=event.encounter;
    const iso=t=>new Date(t).toISOString();
    const heading=document.createElement('h3');
    heading.textContent=event.instrument+' $'+strike+' · '+cohort+' · '+contact+' encounter';
    container.append(heading);
    const paragraph=text=>{const p=document.createElement('p');p.textContent=text;container.append(p);};
    const [flag,from,to]=event.inputEvidence;
    paragraph('The '+calibration.tier+' calibration qualified this strike at '+iso(flag.availableAt)+'. Call bucket '+flag.value.call+', put bucket '+flag.value.put+'.');
    paragraph('Price moved '+approach+' from $'+from.value+' at '+iso(lowerTime)+' to $'+to.value+' at '+iso(upperTime)+'. The encounter became observable at '+iso(event.triggeredAt)+'.');
    for(const outcome of event.outcomes) {
      const horizon=(outcome.targetAt-upperTime)/60000;
      const measurement=outcome.state==='measured'?outcome.value+' at '+iso(outcome.measuredAt):outcome.state+(outcome.reason?' · '+outcome.reason.replaceAll('-',' '):'');
      paragraph(horizon+' min · '+measurement);
    }
    const link=document.createElement('a');
    link.href='/?'+new URLSearchParams({one_clock_session:event.session,one_clock_instrument:event.instrument,one_clock_at:String(event.triggeredAt)})+'#history-clock';
    link.textContent='Open Historical at the encounter';container.append(link);
  } else details.open=true;
  container.append(details);container.hidden=false;
}
