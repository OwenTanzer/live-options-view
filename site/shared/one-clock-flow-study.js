// The audited Sept 25 extract retains exchange and snapshot times, not the
// historical One Clock publication receipts. Its cohort stays reconstructed.
const digest = '39c0926059972f38cdb3d81a4ca1942f1c9ea614017c92a1660ad8c54b6000b9';
const strikes = [740,741,742,743,744,745,746,747];
export async function loadFlowStudy(fetchImpl = fetch) {
  const response = await fetchImpl('/order-flow-replay.json', {credentials:'omit'});
  if (!response.ok) throw new Error('The September 25 archive could not be loaded.');
  const bytes = await response.arrayBuffer();
  const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)), b => b.toString(16).padStart(2,'0')).join('');
  if (actual !== digest) throw new Error('The order-flow archive differs from the audited session.');
  await import('./order-flow-exposure.js');
  await import('./order-flow-model.js');
  const model = window.OrderFlowModel;
  const replay = model.decodeReplay(JSON.parse(new TextDecoder().decode(bytes)));
  return flowStudy(replay, model);
}

export function flowStudy(replay, model) {
  const selection = {session:replay.session,instrument:'QQQ',origin:'reconstructed'};
  const {open:start,close} = replay.coverage;
  const rows = [], fields = {};
  for (const strike of strikes) {
    fields['k'+strike+'.flow'] = '$'+strike+' estimated flow gamma ($ per 1% QQQ)';
    fields['k'+strike+'.oi'] = '$'+strike+' unsigned OI concentration ($ per 1% QQQ)';
  }
  for (let at=start; at<=close; at+=60000) {
    const view = model.replayAt(replay, at), value = {};
    for (const strike of view.strikes) {
      const flow = strike.flow[1], concentration = strike.concentration;
      value['k'+strike.strike] = {
        flow: !view.coverage.incomplete && flow.covered === flow.of ? flow.base : null,
        oi: concentration.covered === concentration.of ? concentration.value : null
      };
    }
    rows.push({...selection,source:'orderFlow',id:'minute-'+at,revision:1,eventAt:at,availableAt:at,
      evidence:'/order-flow-replay.json#sha256='+digest+'&at='+at,value});
  }
  for (const snap of replay.snapshots) {
    if (snap.t<start || snap.t>close) continue;
    rows.push({...selection,source:'price',id:snap.key,revision:1,eventAt:snap.t,availableAt:snap.t,
      evidence:snap.key,value:Number.isFinite(snap.spot)?{spot:snap.spot}:null});
  }
  return {start,close,selection,rows,sources:[{id:'orderFlow',label:'Reconstructed order flow',fields},
    {id:'price',label:'Archived underlying price',fields:{spot:'Spot price'}}],
    rules:[{id:'sep25-flow-744',name:'QQQ $744 estimated flow gamma turns positive',version:1,instruments:['QQQ'],
      combine:'all',transition:'rising',missingInput:'reset',conditions:[
        {source:'orderFlow',field:'k744.flow',op:'>',value:0,maxAgeMs:60000}],
      outcomes:[{id:'spot-5m',source:'price',field:'spot',measure:'return',horizonMs:300000,toleranceMs:120000,maxAgeMs:120000}]}]};
}
