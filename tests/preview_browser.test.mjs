// Local browser acceptance; no desktop input or provider requests.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright'); // NODE_PATH may point at an isolated tool install.
const out=process.env.MUSEF_EVIDENCE_DIR || resolve(tmpdir(),'musef-preview-evidence');
mkdirSync(out,{recursive:true});
const preview=createPreview({assetRoot:process.env.MUSEF_ASSET_ROOT});
const originalFetch=globalThis.fetch;globalThis.fetch=fixtureNetwork;
const browser=await chromium.launch({headless:true,channel:'chromium'});
const requests=[],errors=[];
try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    const r=route.request(),url=new URL(r.url()); requests.push(url.href);
    if(url.origin!==LOCAL_ORIGIN) throw new Error('Non-local browser request: '+url.origin);
    const response=await preview.fetch(new Request(r.url(),{method:r.method(),headers:await r.allHeaders(),
      ...(r.postDataBuffer()?{body:r.postDataBuffer()}:{})}));
    await route.fulfill({status:response.status,body:Buffer.from(await response.arrayBuffer()),headers:Object.fromEntries(response.headers)});
  });
  await page.goto(LOCAL_ORIGIN);
  await page.locator('#preview-environment').waitFor({state:'visible'});
  await page.locator('#auth-toggle').click();
  await page.locator('#auth-username').fill('browser_reader');
  await page.locator('#auth-password').fill('disposable-password');
  await page.locator('#auth-submit').click();
  await page.waitForFunction(()=>document.querySelector('#user-name').textContent==='browser_reader',null,{timeout:5000}).catch(async error=>{
    console.error(JSON.stringify({errors,authError:await page.locator('#auth-error').textContent(),requests,api:preview.calls.api}));
    await page.screenshot({path:resolve(out,'preview-failure.png'),fullPage:true});throw error;
  });
  assert.equal(await page.evaluate(async()=> (await fetch('/api/me')).status),200);
  await page.screenshot({path:resolve(out,'preview-account.png'),fullPage:true});
  await page.locator('#tab-history').click();
  await page.waitForFunction(()=>document.querySelector('#hist-status').textContent.startsWith('Trade date 2026-10-27'));
  assert.equal(await page.locator('#hist-date option').count(),1);
  assert.ok(await page.locator('#hist-heatmap-wrap').isVisible());
  assert.ok(await page.locator('#hist-sidebar').isVisible());
  await page.screenshot({path:resolve(out,'preview-history.png'),fullPage:true});
  const productionBlocked=await page.evaluate(async()=>{
    try{await fetch('https://options.moopertonic.net/api/logout',{method:'POST',credentials:'include'});return false;}
    catch{return true;}
  });
  assert.equal(productionBlocked,true);assert.ok(requests.every(r=>new URL(r).origin===LOCAL_ORIGIN));
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:resolve(out,'preview-mobile-history.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  const receipt={tested_at:new Date().toISOString(),environment:'local-disposable-emulation',
    rendered:['account registration/session','Historical heatmap and sidebar','mobile Historical'],
    production_mutation_blocked_by_csp:productionBlocked,request_count:requests.length,
    all_requests_local:true,page_errors:errors,provider_validation:false};
  writeFileSync(resolve(out,'browser-receipt.json'),JSON.stringify(receipt,null,2));
  console.log(JSON.stringify(receipt));
} finally {await browser.close();preview.close();globalThis.fetch=originalFetch;}
