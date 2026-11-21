import { observation } from './one-clock.js';
import { sessionInstant } from './one-clock-session.js';

export function botLedgerSource({getBot,fetchImpl=fetch,onCoverage=()=>{}}) {
  return {id:'bot-ledger',version:'receipt-1',owner:'paper-execution',maxAgeMs:86400000,
    async read({session,instrument,cutoff,signal}) {
      const bot=getBot(); if(!bot) return [];
      const rows=[],seen=new Set();let offset=0,version=null,complete=true;
      for(let page=0;page<250;page++) {
        const query=new URLSearchParams({bot,session,instrument,cutoff:String(cutoff),offset:String(offset),...(version?{version}:{})});
        const response=await fetchImpl('/api/one-clock/bots?'+query,{signal,credentials:'same-origin',cache:'no-store'});
        if(!response.ok) throw new Error(response.status===409?'Ledger changed; reload this cursor.':`Bot ledger request failed (${response.status})`);
        const body=await response.json(); if(signal.aborted) return [];
        if(!Array.isArray(body.rows) || body.rows.length>40 || !/^[0-9a-f]{64}$/.test(body.version) ||
           (version && version!==body.version) || typeof body.coverage?.complete!=='boolean') throw new Error('Invalid ledger page');
        version=body.version;complete=complete && body.coverage.complete;
        for(const input of body.rows) {
          const row=observation(input);
          if(row.source!=='bot-ledger' || row.session!==session || row.instrument!==instrument || row.origin!=='recorded' || row.details?.bot!==bot || seen.has(row.id)) throw new Error('Ledger scope or identity mismatch');
          seen.add(row.id);rows.push(row);
        }
        if(body.next===null) {onCoverage({...body.coverage,complete,pages:page+1});return rows;}
        if(!Number.isSafeInteger(body.next) || body.next!==offset+40) throw new Error('Invalid ledger continuation');
        offset=body.next;
      }
      throw new Error('Ledger page bound exceeded; inventory unavailable');
    }};
}
// Quantity inventory only: never infer a mark, cash balance, strategy version,
// fees or profit from today's account record. Gaps invalidate the whole book.
export function botInventory(rows,{session,complete}) {
  if(!complete) return {opening:null,current:null};
  const opening=new Map(),current=new Map();
  const add=(map,row)=>map.set(row.value.sym,(map.get(row.value.sym)??0)+(row.value.side==='buy'?1:-1)*row.value.qty);
  const start=sessionInstant(session,0,0,0);
  for(const row of rows) {add(current,row);if(row.eventAt<start && row.availableAt<=start)add(opening,row);}
  const output=map=>[...map].filter(([,qty])=>qty!==0).sort(([a],[b])=>a.localeCompare(b)).map(([sym,qty])=>({sym,qty}));
  return {opening:output(opening),current:output(current)};
}
