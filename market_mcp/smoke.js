#!/usr/bin/env node
'use strict';
// A real stdio client: launches the server, negotiates MCP, discovers tools,
// asks all three dataset questions and follows returned opaque references.
const {spawn}=require('node:child_process');const readline=require('node:readline');
const fs=require('node:fs/promises');const os=require('node:os');const path=require('node:path');const assert=require('node:assert/strict');
const {writeFixtures,SESSION}=require('./fixtures');
async function smoke({publicAccess=false,session=SESSION}={}){
  const root=publicAccess?null:await fs.mkdtemp(path.join(os.tmpdir(),'options-view-mcp-smoke-'));
  if(root)await writeFixtures(root);
  const child=spawn(process.execPath,[path.join(__dirname,'server.js'),...(publicAccess?['--public']:['--fixtures',root])],{stdio:['pipe','pipe','pipe'],windowsHide:true});
  const waiters=new Map();let id=0;const transcript=[];let stderr='';child.stderr.on('data',b=>stderr+=b.toString());
  const lines=readline.createInterface({input:child.stdout});lines.on('line',line=>{const message=JSON.parse(line);const w=waiters.get(message.id);if(w){clearTimeout(w.timer);waiters.delete(message.id);w.resolve(message);}});
  child.on('error',e=>{for(const w of waiters.values())w.reject(e);});
  const request=(method,params)=>new Promise((resolve,reject)=>{const next=++id;const timer=setTimeout(()=>{waiters.delete(next);reject(new Error('Smoke client deadline exceeded. '+stderr));},20000);waiters.set(next,{resolve,reject,timer});child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:next,method,...(params===undefined?{}:{params})})+'\n');});
  const call=async(name,filters={})=>{const response=await request('tools/call',{name,arguments:filters,_meta:{progressToken:name}});assert.ok(response.result,JSON.stringify(response.error));const value=response.result.structuredContent;assert.deepEqual(JSON.parse(response.result.content[0].text),value);transcript.push({tool:name,arguments:filters,isError:response.result.isError,response:value});return value;};
  try{
    const init=await request('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'market-mcp-smoke',version:'1'}});assert.equal(init.result.protocolVersion,'2025-11-25');
    child.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'})+'\n');
    const listed=await request('tools/list',{_meta:{}});assert.equal(listed.result.tools.length,5);
    await call('discover_sources',{session});
    const market=await call('market_context',{type:'call',limit:1});
    if(market.rows?.[0])await call('result_detail',{reference:market.rows[0].detail_reference});
    const squeeze=await call('squeeze_results',{limit:1});
    if(squeeze.rows?.[0])await call('result_detail',{reference:squeeze.rows[0].detail_reference});
    const returns=await call('return_rankings',{session,view:'clean',limit:1});
    if(returns.rows?.[0])await call('result_detail',{reference:returns.rows[0].detail_reference,limit:1});
    if(!publicAccess){assert.equal(market.status,'available');assert.equal(squeeze.status,'available');assert.equal(returns.status,'available');assert.ok(transcript.at(-1).response.quote_path.length);}
    return {kind:publicAccess?'public_anonymous_local_stdio':'synthetic_fixtures_local_stdio',session,tools:listed.result.tools.map(t=>t.name),transcript};
  }finally{
    child.stdin.end();child.kill();lines.close();for(const w of waiters.values())clearTimeout(w.timer);
    if(root){const actual=await fs.realpath(root);const intended=path.resolve(os.tmpdir());assert.ok(actual.toLowerCase().startsWith((intended+path.sep).toLowerCase())||actual.toLowerCase().startsWith((await fs.realpath(os.tmpdir())+path.sep).toLowerCase()));await fs.rm(actual,{recursive:true,force:true});}
  }
}
if(require.main===module){
  const argv=process.argv.slice(2);const publicAccess=argv.includes('--public');const at=argv.indexOf('--out');const output=at>=0?argv[at+1]:null;const sessionAt=argv.indexOf('--session');const session=sessionAt>=0?argv[sessionAt+1]:SESSION;
  smoke({publicAccess,session}).then(async result=>{if(output)await fs.writeFile(output,JSON.stringify(result,null,2)+'\n');process.stdout.write(JSON.stringify({kind:result.kind,session:result.session,queries:result.transcript.map(t=>({tool:t.tool,status:t.response.status,freshness:t.response.freshness.status,actual:t.response.actual,errors:t.response.errors})),output},null,2)+'\n');}).catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
}
module.exports={smoke};
