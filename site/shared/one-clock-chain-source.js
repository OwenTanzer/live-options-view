import { observation } from './one-clock.js';
export function archivedChainSource({fetchImpl=fetch,onCoverage=()=>{}}={}) {
  return {id:'chain',version:'archive-1',owner:'market_data.live_market',maxAgeMs:150000,
    async read({session,instrument,cutoff,signal}) {
      const query=new URLSearchParams({session,instrument,cutoff:String(cutoff)});
      const response=await fetchImpl('/api/one-clock/chain?'+query,{signal,credentials:'same-origin',cache:'no-store'});
      if(!response.ok) throw new Error(`Archive request failed (${response.status})`);
      const body=await response.json();
      if(!Array.isArray(body.rows) || body.rows.length>1) throw new Error('invalid archive response');
      const rows=body.rows.map(observation);
      if(rows.some(r=>r.source!=='chain' || r.session!==session || r.instrument!==instrument || r.origin!=='recorded')) throw new Error('archive scope mismatch');
      if(!signal.aborted) onCoverage(body.coverage);
      return rows;
    }};
}
