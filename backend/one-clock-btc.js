// Read only private terminal receipts belonging to this authorized paper account.
import {jsonResponse,requireSession} from './common.js';
import {btcAllowed} from './btc-owner-link.js';
import {validBtcContract} from './btc-options.js';
import {hashSessionToken} from './storage.js';
import {instant} from '../site/shared/one-clock.js';

const PAGE=32;
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
const utcDay=ms=>new Date(ms).toISOString().slice(0,10);
function at(value) {try{return instant(value);}catch{return null;}}
function side(row,key,cutoff) {
  const received=at(row[key+'_received_ts']);
  const provider=at(row[key+'_ts']);
  if(received===null || received>cutoff || (row[key+'_ts']!=null && (provider===null || provider>cutoff))
      || !Number.isFinite(row[key]) || row[key]<0)return '—';
  return String(row[key]);
}
export async function privateBitcoinPage(bucket,record,cutoff,offset,expectedVersion=null) {
  const day=utcDay(cutoff);
  if(!Array.isArray(record.trades)||record.trades.length>10000)throw Error('Ledger bound');
  const eligible=record.trades.filter(trade=>trade?.instrument_type==='futures_option' &&
    at(trade.ts)!==null && utcDay(at(trade.ts))===day && at(trade.ts)<=cutoff)
    .sort((a,b)=>at(b.ts)-at(a.ts) || String(a.execution_request_id).localeCompare(String(b.execution_request_id)));
  const version=await hashSessionToken(JSON.stringify(eligible.map(t=>[t.execution_request_id,t.ts,t.sym])));
  if(expectedVersion!==null && version!==expectedVersion)return {restart:true};
  if(offset>eligible.length)throw Error('Archive page unavailable');
  const rows=[];
  let gaps=0;
  for(const trade of eligible.slice(offset,offset+PAGE)) {
    const id=trade.execution_request_id;
    if(!uuid.test(id)) {gaps++;continue;}
    const object=await bucket.get('paper-trades/requests/'+id+'.json');
    if(!object || !Number.isSafeInteger(object.size) || object.size>65536 || !(object.uploaded instanceof Date)) {gaps++;continue;}
    const published=object.uploaded.getTime();
    if(!Number.isFinite(published)||published<at(trade.ts)){gaps++;continue;}
    if(published>cutoff)continue;
    const receipt=await object.json(), row=receipt.source_quote, c=row?.contract;
    if(receipt.username!==record.username || receipt.status!=='filled' || receipt.execution_request_id!==id ||
       receipt.sym!==trade.sym || receipt.ts!==trade.ts || row?.symbol!==receipt.sym || !validBtcContract(c,receipt.sym)) {gaps++;continue;}
    rows.push({symbol:receipt.sym,venue:c.exchange,received:new Date(published).toISOString(),
      bid:side(row,'bid',cutoff),ask:side(row,'ask',cutoff)});
  }
  return {rows,gaps,version,next:offset+PAGE<eligible.length?offset+PAGE:null};
}
const escape=text=>String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export async function handleBitcoinReplay(request,env) {
  if(request.method!=='GET')return jsonResponse({error:'GET required'},405);
  const match=/^\?one_clock_at=([1-9]\d{12})(?:&offset=([1-9]\d{0,3})&version=([a-f0-9]{64}))?$/.exec(new URL(request.url).search);
  if(!match)return jsonResponse({error:'Invalid replay cursor'},400);
  const cutoff=Number(match[1]),offset=Number(match[2]??0);
  if(cutoff>Date.now() || offset%PAGE!==0)return jsonResponse({error:'Invalid replay scope'},400);
  const session=await requireSession(request,env);
  if(!session)return jsonResponse({error:'Sign in to your paper account to open the private Bitcoin tape'},401);
  if(!await btcAllowed(env,session,request))return jsonResponse({error:'Bitcoin data authorization required'},403);
  try {
    const page=await privateBitcoinPage(env.PAPER_TRADES,session.record,cutoff,offset,match[3]);
    if(page.restart)return jsonResponse({error:'Receipt list changed. Reopen this cutoff.'},409);
    const {rows,gaps,next,version}=page;
    if(!await btcAllowed(env,session,request))return jsonResponse({error:'Bitcoin data authorization required'},403);
    const cells=rows.map(row=>'<tr>'+[row.venue,row.symbol,row.received,row.bid,row.ask].map(v=>'<td>'+escape(v)+'</td>').join('')+'</tr>').join('');
    const more=next===null?'':'<a href="?one_clock_at='+cutoff+'&amp;offset='+next+'&amp;version='+version+'">Older receipts</a>';
    const html='<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bitcoin quote replay</title><link rel="stylesheet" href="/one-clock.css"><main><h1>Bitcoin quote replay</h1><p>Private paper-fill samples for '+utcDay(cutoff)+' UTC through '+new Date(cutoff).toISOString()+'. Each row became available when its terminal receipt was archived. Coverage is limited to quoted contracts at fills.</p><p>'+rows.length+' eligible receipts on this page · '+gaps+' unverifiable receipt references.</p><div class="table-wrap"><table><thead><tr><th>Venue</th><th>Contract</th><th>Archived UTC</th><th>Bid</th><th>Ask</th></tr></thead><tbody>'+cells+'</tbody></table></div>'+more+'</main>';
    return new Response(html,{headers:{'Content-Type':'text/html; charset=UTF-8','Cache-Control':'private, no-store',
      'Vary':'Cookie','Content-Security-Policy':"default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",'Referrer-Policy':'no-referrer'}});
  }catch{return jsonResponse({error:'Private Bitcoin archive unavailable'},503);}
}
