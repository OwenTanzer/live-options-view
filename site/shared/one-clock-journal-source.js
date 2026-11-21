import { observation } from './one-clock.js';
export function journalSource({fetchImpl=fetch,onBody=()=>{}}={}) {
  return {id:'private-journal',version:'1',owner:'trade-journal',maxAgeMs:86400000,
    async read({signal,...selection}) {
      const query=new URLSearchParams({date:selection.session,cutoff:String(selection.cutoff)});
      const response=await fetchImpl(`/api/trade-journal?${query}`,{signal,credentials:'same-origin',cache:'no-store'});
      const body=await response.json();
      if(!response.ok){const error=new Error(body.error??'Journal unavailable');error.status=response.status;throw error;}
      if(body.view!=='recorded-as-of' || body.date!==selection.session || body.cutoff!==selection.cutoff)throw new Error('Journal scope mismatch');
      if(signal.aborted)throw new DOMException('Aborted','AbortError');
      const rows=body.positions.map(position=>observation({source:'private-journal',id:position.position_id,
        revision:position.recording_revision,eventAt:position.opened_ts,availableAt:position.available_at,
        session:selection.session,instrument:selection.instrument,origin:'recorded',evidence:`private-journal:${position.position_id}`,
        value:position}));
      onBody(body,selection);return rows;
    }};
}
