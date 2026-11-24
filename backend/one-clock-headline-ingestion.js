import companies from '../market_data/news/companies-v2.json' with {type:'json'};
import panel from '../site/js/news-panel.js';
import {archiveNews,archiveOrigin,availableMillisecond} from '../site/js/one-clock-news.js';
import {headlineDay,hasHeadlinePriceRoute,headlineSource,quoteSource} from '../site/shared/one-clock-headlines.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {instant} from '../site/shared/one-clock.js';
import {equitySession} from './one-clock-calendar.js';
import {ingestedAsOf} from './one-clock-ingestion.js';
import {NEWS_R2_ORIGIN,isPublicNewsViewKey} from './browser-data.js';

const companiesBySymbol=new Map(companies.map(c=>[c.ticker,c]));
const instruments=[...companiesBySymbol.keys()];
const priceKey='intraday/prices.json';
const checkedTime=value=>typeof value==='string' && /T.*(?:Z|[+-]\d\d:\d\d)$/.test(value)
  ? availableMillisecond(value) : NaN;
const ambiguous=new Set(['BP','COP','SPY','META']);
const escape=s=>s.replace(/[.*+?^$(){}|[\]\\]/g,'\\$&');
const patterns=companies.map(c=>[c,[
  new RegExp((ambiguous.has(c.ticker)?'\\$':'\\$?\\b')+c.ticker+'\\b','i'),
  ...[...c.name_phrases,...c.distinctive].map(p=>new RegExp('\\b'+p.split(' ').map(escape).join('\\s+')+'\\b','i')),
]]);

// Older sanitized generations carry only the title. Matching against the
// same allowlist keeps those archives usable without inferring an excerpt.
function mappingFor(item) {
  if(item.company_mapping) {
    const m=item.company_mapping,c=companiesBySymbol.get(m.ticker);
    return m.instrument_mapping==='mapped' && c && c.instrument_id===m.instrument_id ? {c,basis:m.mapping_basis,matched:m.matched_text} : null;
  }
  const title=item.title.replace(/\bmeta-analysis\b/ig,' ');
  const hits=patterns.flatMap(([c,tests])=>{
    const match=tests.map(r=>r.exec(title)).find(Boolean);
    return match?[{c,basis:'retained-title',matched:match[0]}]:[];
  });
  return hits.length===1?hits[0]:null;
}

export function projectHeadlines(pages,{session,instrument,cutoff}) {
  const bounds=headlineDay(session),rows=new Map();
  for(const page of pages) {
    if(page.mode!=='archive' || page.stream!=='all' || archiveOrigin(page.data_label)!=='recorded')continue;
    for(const item of page.rows) {
      const at=Math.max(checkedTime(item.observed_at),checkedTime(item.archived_at),checkedTime(item.event_at));
      if(!Number.isSafeInteger(at) || at<bounds.open || at>bounds.close || at>cutoff)continue;
      if(typeof item.title!=='string' || !item.item_id || !item.revision_id)continue;
      const m=mappingFor(item);
      if(m?.c.ticker!==instrument)continue;
      const id=JSON.stringify([item.item_id,item.revision_id]);
      const row={source:headlineSource,instrument,session,origin:'recorded',id,revision:1,eventAt:at,availableAt:at,
        evidence:item.revision_id,value:{ticker:instrument,instrumentId:m.c.instrument_id,title:item.title,itemId:item.item_id,
          revisionId:item.revision_id,mappingBasis:m.basis,matchedText:m.matched,observedAt:item.observed_at,archivedAt:item.archived_at}};
      if(rows.has(id) && JSON.stringify(rows.get(id))!==JSON.stringify(row))throw Error('Conflicting headline revision');
      rows.set(id,row);
    }
  }
  return [...rows.values()];
}

const shared={version:'1',demandDriven:true,owner:'One Clock',instruments,
  async read(bucket,scope,artifact){return {rows:[artifact.row]};}};
const artifact=row=>({key:row.id,version:'1',eventAt:row.eventAt,row});

export const headlineIngestion={...shared,id:'mapped-news-archive',outputs:[headlineSource],
  fields:{[headlineSource]:{ticker:'Mapped company ticker'}},
  async discover(bucket,{session,instrument,cutoff}) {
    const selection={session,instrument,cutoff:Math.min(cutoff,headlineDay(session).close),origin:'recorded'};
    const started=Date.now(),lease=()=>Date.now()-started<15000;
    let bytes=0;
    const fetchJson=async key=>{
      if(!lease() || !isPublicNewsViewKey(key))throw Error('Invalid news view request');
      const response=await fetch(NEWS_R2_ORIGIN+'/'+key,{redirect:'error',signal:AbortSignal.timeout(Math.max(1,15000-(Date.now()-started)))});
      if(!response.ok)throw Error('News archive unavailable');
      const reader=response.body.getReader(),chunks=[];
      try {
        for(;;){const {done,value}=await reader.read();if(done)break;
          bytes+=value.length;if(bytes>4*1024*1024)throw Error('News archive exceeds read budget');chunks.push(value);}
      }finally{await reader.cancel();}
      return JSON.parse(await new Blob(chunks).text());
    };
    const {rows,partial}=await archiveNews(selection,fetchJson,panel,lease,projectHeadlines);
    return {artifacts:rows.map(artifact),complete:true,missingMinutes:partial?1:0};
  }};

export const headlineQuoteIngestion={...shared,id:'mapped-price-strip',outputs:[quoteSource],
  fields:{[quoteSource]:{price:'Mapped quote price'}},
  async discover(bucket,{session,instrument,cutoff}) {
    if(!hasHeadlinePriceRoute(instrument))return {artifacts:[],complete:true};
    const object=await bucket.get(priceKey);
    if(!object || object.size>1024*1024)return {artifacts:[],complete:false,reason:'price-strip-unavailable'};
    const doc=JSON.parse(await object.text()),raw=doc.prices?.[instrument],bounds=headlineDay(session);
    const c=companiesBySymbol.get(instrument);
    const providerId=['SPY','QQQ'].includes(instrument)?c.instrument_id:instrument;
    const quoteAt=checkedTime(raw?.observed_at),received=checkedTime(raw?.collector_received_at);
    const uploaded=object.uploaded?.getTime(),published=checkedTime(doc.timestamp);
    if(doc.schema_version!==2 || raw?.instrument_id!==providerId || raw.source_symbol!==instrument ||
      !Number.isFinite(raw.price) || raw.price<=0 || ![quoteAt,received,published,uploaded].every(Number.isSafeInteger) ||
      quoteAt>received || Math.max(received,published)>uploaded || uploaded>cutoff ||
      quoteAt<bounds.open)
      return {artifacts:[],complete:true,missingMinutes:1};
    const id=JSON.stringify([instrument,raw.source,raw.price_basis,quoteAt,received,raw.price]);
    const row={source:quoteSource,instrument,session,origin:'recorded',id,revision:1,eventAt:quoteAt,availableAt:uploaded,
      evidence:priceKey+'#'+id,value:{ticker:instrument,instrumentId:c.instrument_id,providerInstrumentId:providerId,
        price:raw.price,source:raw.source,priceBasis:raw.price_basis,collectorReceivedAt:received,publishedAt:published}};
    return {artifacts:[artifact(row)],complete:true};
  }};

export async function replayHeadlines(db,namespace,body,now) {
  const {session,ruleId,version}=body,cutoff=body.cutoff===undefined?now:instant(body.cutoff);
  const bounds=equitySession(session);
  if(!bounds || cutoff>now)throw Error('Invalid headline replay cutoff');
  const saved=await db.prepare('SELECT payload FROM one_clock_rules WHERE namespace=? AND rule_id=? AND version=?').bind(namespace,ruleId,version).first();
  const rule=saved && JSON.parse(saved.payload);
  if(rule?.transition!=='per-headline')throw Error('Choose a saved mapped headline study');
  const selection={session,instrument:rule.instruments[0],cutoff,origin:'recorded'};
  // Historical source replay can inspect publications the observer retained
  // subsequently. Their original public availability still gates every value.
  const retained=await ingestedAsOf(db,{...selection,cutoff:now});
  const observations=retained.filter(r=>[headlineSource,quoteSource].includes(r.source) && r.available_at<=cutoff)
    .map(r=>JSON.parse(r.payload));
  const events=evaluateEvents({rule,selection,observations,sessionClose:bounds.close});
  return {...selection,rule,events:events.map(e=>({...e,collection:'replay'})),
    coverage:{retainedVersions:observations.length,pending:0,gaps:0,indexComplete:false,
      reason:'Replay of retained news and sampled quotes; source history is partial.'},complete:false};
}
