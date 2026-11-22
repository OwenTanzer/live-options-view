import {observation} from './one-clock.js';

export function botDecisionSource({getBot,fetchImpl=fetch,onCoverage=()=>{}}) {
  return {id:'bot-decision',version:'audit-1',owner:'crassus',maxAgeMs:300000,
    async read({session,instrument,cutoff,signal}) {
      const bot=getBot(), rows=[], seen=new Set();
      if(!bot)return [];
      let after=null;
      for(let page=0;page<250;page++) {
        const query=new URLSearchParams({bot,session,instrument,cutoff:String(cutoff)});
        if(after)query.set('after',after);
        const response=await fetchImpl('/api/one-clock/bot-decisions?'+query,
          {signal,credentials:'same-origin',cache:'no-store'});
        if(!response.ok)throw new Error('Paper decision receipts unavailable (' + response.status + ')');
        const body=await response.json();
        if(signal.aborted)return [];
        if(!Array.isArray(body.rows)||body.rows.length>40)throw new Error('Invalid decision page');
        for(const data of body.rows) {
          const row=observation(data);
          if(row.source!=='bot-decision'||row.session!==session||row.instrument!==instrument||
             row.value?.bot!==bot||row.origin!=='recorded'||row.availableAt===null||
             row.availableAt>cutoff||seen.has(row.id))throw new Error('Decision scope mismatch');
          seen.add(row.id);rows.push(row);
        }
        if(body.next===null) {onCoverage({...body.coverage,pages:page+1});return rows;}
        if(!/^[0-9a-f]{64}$/.test(body.next)||(after!==null&&body.next<=after))throw new Error('Invalid decision continuation');
        after=body.next;
      }
      throw new Error('Decision page bound exceeded');
    }};
}
