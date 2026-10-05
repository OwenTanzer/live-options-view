#!/usr/bin/env node
'use strict';
// Local validation of the remote-compatible Streamable HTTP transport. This
// listener deliberately binds only loopback. Production TLS/authentication,
// hosting, access grants and reverse-proxy boundaries are a separate decision.
const http=require('node:http');const {Source,LIMITS}=require('./source');const {Consumer}=require('./consumer');const {Protocol,VERSION}=require('./server');
function createHttpServer(consumer){
  let active=0;
  return http.createServer(async(req,res)=>{
    const reply=(status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(value===undefined?'':JSON.stringify(value));};
    // Origin validation remains relevant on loopback (browser DNS rebinding).
    const host=req.headers.host;
    if(!host||!/^127\.0\.0\.1:\d+$/.test(host)){reply(403,{error:'Loopback Host required.'});return;}
    if(req.headers.origin){let origin;try{origin=new URL(req.headers.origin);}catch{}if(!origin||origin.origin!==`http://${host}`){reply(403,{error:'Origin is not permitted.'});return;}}
    if(req.url!=='/mcp'){reply(404,{error:'Unknown route.'});return;}
    if(req.method!=='POST'){res.setHeader('Allow','POST');reply(405,{error:'This stateless transport supports POST JSON responses; no SSE or session deletion.'});return;}
    if(!req.headers.accept?.includes('application/json')||!req.headers.accept?.includes('text/event-stream')){reply(406,{error:'Accept application/json and text/event-stream.'});return;}
    if(!req.headers['content-type']?.toLowerCase().startsWith('application/json')){reply(415,{error:'Use application/json.'});return;}
    const version=req.headers['mcp-protocol-version'];
    if(version!==undefined&&version!==VERSION){reply(400,{error:'Unsupported MCP protocol version.'});return;}
    if(active>=4){reply(429,{error:'At most four concurrent requests.'});return;}
    active++;let timer;
    try{
      let size=0;const chunks=[];
      timer=setTimeout(()=>req.destroy(),5000);
      for await(const chunk of req){size+=chunk.length;if(size>LIMITS.input_bytes){reply(413,{error:'Request exceeds 16 KiB.'});return;}chunks.push(chunk);}
      clearTimeout(timer);let message;
      try{message=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{reply(400,{jsonrpc:'2.0',id:null,error:{code:-32700,message:'Parse error.'}});return;}
      if(message?.method!=='initialize'&&version!==VERSION){reply(400,{error:'Send the negotiated MCP-Protocol-Version: '+VERSION});return;}
      // Stateless HTTP has no negotiated transport session. Each request can
      // use the same tool schemas; opaque evidence references live in Consumer.
      const protocol=new Protocol(consumer);
      if(message?.method!=='initialize'){protocol.initialized=true;protocol.ready=true;}
      const result=await protocol.handle(message);
      if(result===null){reply(202);return;}
      reply(result.error?400:200,result);
    }catch{if(!res.headersSent)reply(400,{error:'Request could not be read.'});}
    finally{clearTimeout(timer);active--;}
  });
}
if(require.main===module){
  const argv=process.argv.slice(2);let source;
  if(argv.length===1&&argv[0]==='--public')source=new Source({publicAccess:true});
  else if(argv.length===2&&argv[0]==='--fixtures')source=new Source({directory:argv[1]});
  else{process.stderr.write('Usage: node market_mcp/http.js --fixtures <artifact-root> | --public\n');process.exit(2);}
  createHttpServer(new Consumer(source)).listen(8765,'127.0.0.1',()=>process.stderr.write('Local MCP HTTP: http://127.0.0.1:8765/mcp\n'));
}
module.exports={createHttpServer};
