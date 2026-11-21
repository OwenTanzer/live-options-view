import { advanceSourceCatalog } from './one-clock-source-catalog.js';
const UUID='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
export const squeezeCatalog=Object.freeze({id:'squeeze-publications',version:'1',owner:'short-squeeze-scanner',
  prefix:'squeeze-scanner/v1/scheduled/publication-attempts/',
  classify(key) {
    const match=new RegExp('^squeeze-scanner/v1/scheduled/publication-attempts/('+UUID+')/('+UUID+')/(started|finished|failed)\\.json$').exec(key);
    return match?{group:match[1]+'/'+match[2],kind:match[3]}:null;
  }});
export const catalogSources=Object.freeze([squeezeCatalog]);
export async function advanceOneClockCatalogs(env,{now=Date.now()}={}) {
  if(env.ONE_CLOCK_CATALOG!=='enabled')return [];
  const results=[];
  for(const source of catalogSources) {
    try {results.push(await advanceSourceCatalog(env.DB,env.PAPER_TRADES,source,{now}));}
    catch {console.error(JSON.stringify({event:'one_clock_catalog_failed',source:source.id}));results.push({source:source.id,error:true});}
  }
  return results;
}
