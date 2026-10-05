'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const http=require('node:http');
const {createHttpServer}=require('./http');const {Consumer}=require('./consumer');const {Source,LIMITS}=require('./source');const {fixtures,NOW}=require('./fixtures');
async function listener(fn){const files=fixtures();const server=createHttpServer(new Consumer(new Source({read:async key=>files[key]}),{now:()=>NOW}));await new Promise(r=>server.listen(0,'127.0.0.1',r));try{await fn(`http://127.0.0.1:${server.address().port}/mcp`);}finally{await new Promise(r=>server.close(r));}}
const headers={'Content-Type':'application/json',Accept:'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'};
async function post(url,message,extra={}){return fetch(url,{method:'POST',headers:{...headers,...extra},body:JSON.stringify(message)});}
test('Streamable HTTP initialization, discovery, context and retained detail end-to-end',async()=>listener(async url=>{
  let response=await post(url,{jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'http-smoke',version:'1'}}});assert.equal(response.status,200);assert.equal((await response.json()).result.protocolVersion,'2025-11-25');assert.equal(response.headers.get('mcp-session-id'),null);
  response=await post(url,{jsonrpc:'2.0',method:'notifications/initialized'});assert.equal(response.status,202);assert.equal(await response.text(),'');
  response=await post(url,{jsonrpc:'2.0',id:2,method:'tools/list'});assert.equal((await response.json()).result.tools.length,5);
  response=await post(url,{jsonrpc:'2.0',id:3,method:'tools/call',params:{name:'discover_sources',arguments:{}}});assert.equal((await response.json()).result.structuredContent.capabilities.length,3);
  response=await post(url,{jsonrpc:'2.0',id:4,method:'tools/call',params:{name:'market_context',arguments:{limit:1}}});const context=(await response.json()).result.structuredContent;
  response=await post(url,{jsonrpc:'2.0',id:5,method:'tools/call',params:{name:'result_detail',arguments:{reference:context.rows[0].detail_reference}}});const detail=(await response.json()).result.structuredContent;assert.equal(detail.actual.payload_sha256,context.actual.payload_sha256);assert.equal(detail.row.Bid,1);
}));
test('HTTP rejects remote Origin/Host, arbitrary routes, verbs, media types and versions',async()=>listener(async url=>{
  const request={jsonrpc:'2.0',id:1,method:'ping'};
  assert.equal((await post(url,request,{Origin:'https://attacker.example'})).status,403);
  const rawHostStatus=await new Promise((resolve,reject)=>{const req=http.request(url,{method:'POST',headers:{...headers,Host:'attacker.example:8765'}},res=>{res.resume();resolve(res.statusCode);});req.on('error',reject);req.end(JSON.stringify(request));});assert.equal(rawHostStatus,403);
  assert.equal((await fetch(url)).status,405);assert.equal((await fetch(url,{method:'DELETE'})).status,405);
  assert.equal((await post(url.replace('/mcp','/api/me'),request)).status,404);
  assert.equal((await post(url,request,{Accept:'application/json'})).status,406);
  assert.equal((await post(url,request,{'Content-Type':'text/plain'})).status,415);
  assert.equal((await post(url,request,{'MCP-Protocol-Version':'future'})).status,400);
}));
test('HTTP bounds request bodies and returns typed tool errors as MCP results',async()=>listener(async url=>{
  let response=await fetch(url,{method:'POST',headers,body:'x'.repeat(LIMITS.input_bytes+1)});assert.equal(response.status,413);
  response=await fetch(url,{method:'POST',headers,body:'{'});assert.equal(response.status,400);
  response=await post(url,{jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'market_context',arguments:{url:'https://attacker.example'}}});assert.equal(response.status,200);const result=(await response.json()).result;assert.equal(result.isError,true);assert.equal(result.structuredContent.errors[0].code,'invalid_filter');
}));
