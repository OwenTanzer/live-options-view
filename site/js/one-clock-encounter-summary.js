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
