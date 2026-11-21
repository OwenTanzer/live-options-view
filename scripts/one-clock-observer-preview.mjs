// Disposable, loopback-only fixture. No production credentials or storage.
import http from 'node:http';
import fs from 'node:fs';
import {createPreview} from './preview-local.mjs';
import {makeStorage} from '../tests/helpers/d1_shim.mjs';
import {createStorage} from '../backend/storage.js';
import {handleOneClockResearch} from '../backend/one-clock-research.js';
import {registerObservedRule,observeBatch} from '../backend/one-clock-observer.js';
import {observerSessions,equitySession} from '../backend/one-clock-calendar.js';
import {demoRows,demoRules,demoStart} from '../site/shared/one-clock-demo.js';
const preview=createPreview(),DB=makeStorage(),env={DB,ONE_CLOCK_OBSERVER:'enabled'};
for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])await DB.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
const storage=createStorage(env);await storage.users.insert('fixture',{username:'fixture',version:0});await storage.sessions.create('disposable-fixture','fixture',3600);
const day=observerSessions(Date.now())[0],T=equitySession(day).open,namespace='account:fixture';
const rule=demoRules[0];await registerObservedRule(DB,{namespace,rule,registeredAt:T});
const rows=demoRows.filter(r=>r.source!=='shortlist').map(r=>({...r,session:day,origin:'recorded',eventAt:T+r.eventAt-demoStart,availableAt:T+r.availableAt-demoStart}));
for(const offset of [301000,1261000,3601000])for(const source of ['price','skew'])await observeBatch(DB,{namespace,batchId:source+offset,source,cursor:String(offset),selection:{session:day,instrument:'QQQ',origin:'recorded',cutoff:T+offset},sessionClose:equitySession(day).close,observations:rows.filter(r=>r.source===source&&r.availableAt<=T+offset),collection:'replay'});
const server=http.createServer(async(req,res)=>{
  try {
    const expected=`http://localhost:${server.address().port}`;
    if(req.headers.host!==`localhost:${server.address().port}`||req.headers.origin&&req.headers.origin!==expected||!['GET','POST'].includes(req.method)){res.writeHead(403);res.end();return;}
    const url=new URL(req.url,'http://localhost:8787');
    let response;
    if(url.pathname==='/api/one-clock/research') {
      let body='',bytes=0;for await(const chunk of req){bytes+=chunk.length;if(bytes>16384)throw new Error('fixture body bound');body+=chunk;}
      response=await handleOneClockResearch(new Request(url,{method:req.method,headers:{Cookie:'session=disposable-fixture',Origin:'http://localhost:8787','Content-Type':'application/json'},...(req.method==='POST'?{body}:{})}),env,url);
    } else {
      if(req.method!=='GET'){res.writeHead(403);res.end();return;}
      response=await preview.fetch(new Request(url));
      if(url.pathname==='/one-clock.html')response=new Response((await response.text()).replace('<body>','<body><aside style="padding:12px;background:#713600;color:white">DISPOSABLE FIXTURE: all observations below are synthetic engineering records. Account writes exist only in memory.</aside>'),response);
    }
    res.writeHead(response.status,Object.fromEntries(response.headers));res.end(Buffer.from(await response.arrayBuffer()));
  }catch(error){res.writeHead(500);res.end(error.message);}
});
server.listen(0,'127.0.0.1',()=>console.log(`Disposable observer fixture http://localhost:${server.address().port}/one-clock.html · ${day}, trigger 09:35:01 New York`));
for(const signal of ['SIGINT','SIGTERM'])process.once(signal,()=>server.close(()=>{DB.db.close();preview.close();process.exit(0);}));
