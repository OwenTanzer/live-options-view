'use strict';
// Event Builder reads frozen episode pulses. The decision ledger retains the direction and exclusions.
function eventStudy(rows, session) {
  const selection={session,instrument:'QQQ',origin:'reconstructed'};
  const times=new Map();
  const rules=[];
  for(const row of rows) {
    if (!times.has(row.t)) times.set(row.t,{});
    times.get(row.t)['bot'+row.id]=row.episode;
    if (!rules.some(r=>r.id==='qqq-flow-'+row.id)) {
      rules.push({id:'qqq-flow-'+row.id,name:'QQQ order flow '+row.id,version:1,instruments:['QQQ'],
        combine:'all',transition:'rising',missingInput:'reset',
        conditions:[{source:'flow-bots',field:'bot'+row.id,op:'==',value:true,maxAgeMs:60000}],outcomes:[]});
    }
  }
  const observations=[...times].map(([t,value])=>({...selection,source:'flow-bots',id:'minute-'+t,revision:1,
    eventAt:t,availableAt:t,evidence:'order-flow-bots/v1/'+session+'/'+t,value}));
  return {selection,rules,observations};
}
module.exports={eventStudy};
