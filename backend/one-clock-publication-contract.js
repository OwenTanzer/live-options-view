import {instant,observation} from '../site/shared/one-clock.js';
import {validSession,sessionAt} from '../site/shared/one-clock-session.js';
export const PUBLICATION_LIMITS=Object.freeze({entryBytes:16384,records:10,evidence:4,evidenceBytes:4*1024*1024,streams:12,gets:192,bytes:2*1024*1024});
const slug=/^[a-z][a-z0-9-]{0,63}$/;
const hash=/^[a-f0-9]{64}$/;
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function only(value,keys){if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).some(k=>!keys.includes(k)))throw new Error('publication schema');}
function text(value,max=200){if(typeof value!=='string'||!value.length||value.length>max)throw new Error('publication text bound');return value;}
export function publicationStream(descriptor,instrument) {
  if(!descriptor||!slug.test(descriptor.id)||descriptor.audience!=='public'||!Array.isArray(descriptor.instruments)||!descriptor.instruments.includes(instrument)||!/^[A-Z][A-Z0-9.-]{0,14}$/.test(instrument)||!['new-york','utc'].includes(descriptor.sessionBasis))throw new Error('unregistered public stream');
  text(descriptor.owner,100);
  if(!descriptor.outputs||Array.isArray(descriptor.outputs)||!Object.keys(descriptor.outputs).length||Object.keys(descriptor.outputs).length>10)throw new Error('publication descriptor outputs');
  for(const [source,fields] of Object.entries(descriptor.outputs)) {
    if(!slug.test(source)||!fields||Array.isArray(fields)||!Object.keys(fields).length||Object.keys(fields).length>32)throw new Error('publication descriptor fields');
    for(const [field,type] of Object.entries(fields))if(!/^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(field)||!['number','string','boolean'].includes(type))throw new Error('publication descriptor field type');
  }
  return descriptor.id+'/'+instrument;
}
export function publicationKey(stream,sequence) {
  if(!/^[a-z][a-z0-9-]{0,63}\/[A-Z][A-Z0-9.-]{0,14}$/.test(stream)||!Number.isSafeInteger(sequence)||sequence<1||sequence>=1e12)throw new Error('publication sequence bound');
  return `publications/v1/${stream}/entries/${String(sequence).padStart(12,'0')}.json`;
}
export async function publicationDigest(bytes){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');}

export async function decodePublication(bytes,{descriptor,instrument,sequence,previousDigest,uploaded,cutoff}) {
  const stream=publicationStream(descriptor,instrument),availableAt=instant(uploaded);
  publicationKey(stream,sequence);
  if(sequence===1?previousDigest!==null:!hash.test(previousDigest))throw new Error('publication predecessor');
  if(bytes.byteLength>PUBLICATION_LIMITS.entryBytes)throw new Error('publication entry byte bound');
  if(availableAt>instant(cutoff))return {deferred:true};
  const entry=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
  only(entry,['schema','stream','sequence','publicationId','owner','previousDigest','createdAt','records','evidence']);
  if(entry.schema!=='one-clock.publication.v1'||entry.stream!==stream||entry.sequence!==sequence||entry.owner!==descriptor.owner||!uuid.test(entry.publicationId)||entry.previousDigest!==previousDigest)throw new Error('publication identity or chain mismatch');
  if(instant(entry.createdAt)>availableAt)throw new Error('publication creation time');
  if(!Array.isArray(entry.records)||!entry.records.length||entry.records.length>PUBLICATION_LIMITS.records||!Array.isArray(entry.evidence)||!entry.evidence.length||entry.evidence.length>PUBLICATION_LIMITS.evidence)throw new Error('publication evidence bound');
  for(const ref of entry.evidence) {
    only(ref,['key','sha256','bytes','contentType','schema']);
    if(!hash.test(ref.sha256)||ref.key!==`publications/v1/${stream}/blobs/${ref.sha256}`||!Number.isSafeInteger(ref.bytes)||ref.bytes<1||ref.bytes>PUBLICATION_LIMITS.evidenceBytes||!['application/json','text/csv'].includes(ref.contentType))throw new Error('publication evidence scope');
    text(ref.schema,100);
  }
  const identities=new Set(),rows=entry.records.map(record=>{
    only(record,['source','id','revision','session','eventAt','value']);
    if(!Object.hasOwn(descriptor.outputs,record.source)||!validSession(record.session))throw new Error('publication output scope');
    const fields=descriptor.outputs[record.source];
    const eventAt=instant(record.eventAt),session=descriptor.sessionBasis==='new-york'?sessionAt(eventAt):new Date(eventAt).toISOString().slice(0,10);
    if(session!==record.session||eventAt>availableAt)throw new Error('publication event scope');
    const id=text(record.id,500),identity=JSON.stringify([record.source,id]);
    if(identities.has(identity))throw new Error('duplicate publication record');identities.add(identity);
    if(record.value!==null) {
      only(record.value,Object.keys(fields));
      if(Object.keys(record.value).length!==Object.keys(fields).length)throw new Error('publication fields missing');
      for(const [field,type] of Object.entries(fields)){
        const value=record.value[field];
        if(value===null)continue;
        if(typeof value!==type||type==='number'&&!Number.isFinite(value)||type==='string'&&value.length>2000)throw new Error('publication field type');
      }
    }
    return observation({source:record.source,id:`native:${stream}:${id}`,revision:record.revision,session,instrument,origin:'recorded',eventAt,availableAt,
      evidence:publicationKey(stream,sequence),value:record.value,
      details:{schema:entry.schema,owner:entry.owner,publicationId:entry.publicationId,sequence,originalRecordId:id,evidence:entry.evidence,evidenceVerification:'publisher-attested; bodies not read by projection consumer'}});
  });
  return {stream,sequence,publicationId:entry.publicationId,digest:await publicationDigest(bytes),availableAt,rows};
}
