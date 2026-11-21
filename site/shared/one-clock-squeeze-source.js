import { observation } from './one-clock.js';
export function squeezeSource({fetchImpl=fetch,onCoverage=()=>{}}={}) {
  return {id:'shortlist',version:'publication-1',owner:'short-squeeze-scanner',maxAgeMs:86400000,
    fields:{listed:'Published shortlist membership',rank:'Published rank',combined:'Combined score',factor:'Factor score',options:'Options score'},
    async read({session,instrument,cutoff,signal}) {
      const response=await fetchImpl('/api/one-clock/squeeze?'+new URLSearchParams({session,instrument,cutoff:String(cutoff)}),{signal,credentials:'same-origin',cache:'no-store'});
      if(!response.ok)throw new Error(`Shortlist archive request failed (${response.status})`);
      const body=await response.json();if(!Array.isArray(body.rows) || body.rows.length>20)throw new Error('Invalid shortlist response');
      const rows=body.rows.map(observation);
      if(rows.some(r=>r.source!=='shortlist' || r.session!==session || r.instrument!==instrument || r.origin!=='recorded'))throw new Error('Shortlist scope mismatch');
      if(!signal.aborted)onCoverage(body.coverage);
      return rows;
    }};
}
