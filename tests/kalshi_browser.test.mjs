// Rendered chain acceptance over real handlers; every provider response is synthetic.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { handleKalshi } from '../backend/kalshi.js';
import { writeState } from '../backend/kalshi-store.js';
import { fixture, envFixture, network, COOKIE, TICKER } from './helpers/kalshi_fixture.mjs';
import { SITE } from '../backend/kalshi-access.js';
let chromium;
try { ({chromium}=createRequire(import.meta.url)('playwright')); }
catch { ({chromium}=createRequire(new URL('../backend/package.json',import.meta.url))('playwright')); }
const out=process.env.KALSHI_EVIDENCE_DIR||join(tmpdir(),'kalshi-chain-fixtures'); mkdirSync(out,{recursive:true});
const realFetch=globalThis.fetch;
const browser=await chromium.launch({headless:true});
try {
  for(const [label,viewport] of [['phone',{width:390,height:844}],['desktop',{width:1365,height:900}]]) {
    const f=fixture(),env=envFixture(); globalThis.fetch=network(f);
    const names=['New York daily high: 60° or above','Will the Mars mission launch before 2030?','Will the Riemann hypothesis be proved?','Who performs the next Bond song?','Will the next GTA arrive before 2028?','Annual GDP growth above 2%?','Los Angeles high above 75°?','Will CO₂ pass 440 ppm?','Miami high between 80° and 81°?','Will fusion reach its next milestone?','Chicago snowfall above 6 inches?','Will a lunar base open before 2035?','Next year: warmest on record?'];
    const markets=names.map((title,i)=>({ticker:i?'KXHIGHNY-FIXTURE'+i:TICKER,title:'SYNTHETIC · '+title,outcome:'Yes',category:i%2?'Science and Technology':'Climate and Weather',cutoff:f.raw.close_time,series:'KXHIGHNY'}));
    await writeState(env,'discovery',{markets,scope:'Synthetic visual fixture only.'});
    const context=await browser.newContext({viewport});
    await context.addCookies([{name:'__Host-cds-session',value:COOKIE.split('=')[1],url:SITE,secure:true,httpOnly:true,sameSite:'Lax'}]);
    const page=await context.newPage(),errors=[],orderIds=[]; let loseFirstResponse=true;
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const r=route.request(),u=new URL(r.url()); assert.equal(u.origin,SITE);
      if(u.pathname.startsWith('/api/kalshi/')) {
        if(u.pathname.endsWith('/paper'))orderIds.push(r.postDataJSON().execution_request_id);
        const response=await handleKalshi(new Request(r.url(),{method:r.method(),headers:await r.allHeaders(),
          ...(r.postDataBuffer()?{body:r.postDataBuffer()}: {})}),env);
        if(u.pathname.endsWith('/paper')&&loseFirstResponse){loseFirstResponse=false;await route.abort('failed');return;}
        await route.fulfill({status:response.status,headers:Object.fromEntries(response.headers),body:await response.text()});return;
      }
      if(u.pathname==='/credit-tape/auth/logout'){await context.clearCookies();await route.fulfill({status:204,body:''});return;}
      const allowed=new Map([['/kalshi.html','kalshi.html'],['/kalshi.css','kalshi.css'],['/js/kalshi.js','js/kalshi.js']]);
      assert.ok(allowed.has(u.pathname),'Unexpected browser destination');
      await route.fulfill({status:200,contentType:u.pathname.endsWith('.html')?'text/html':u.pathname.endsWith('.css')?'text/css':'application/javascript',
        body:readFileSync(new URL('../site/'+allowed.get(u.pathname),import.meta.url),'utf8')});
    });
    try {
      await page.goto(SITE+'/kalshi.html');
      const row=page.locator('.contract').first(),buy=row.locator('.yes .buy'),sell=row.locator('.yes .sell');
      await buy.waitFor(); await page.waitForFunction(()=>!document.querySelector('.yes .buy').disabled);
      assert.equal(await page.locator('.contract').count(),12);
      assert.match(await buy.textContent(),/45¢/); assert.match(await row.locator('.no .buy').textContent(),/60¢/);
      assert.equal(await sell.isDisabled(),true); assert.equal(await page.locator('#detail').isHidden(),true);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      const titleBox=await row.locator('h3').boundingBox(),priceBox=await buy.boundingBox();
      if(label==='phone'){assert.ok(titleBox.y<priceBox.y);assert.ok(priceBox.y+priceBox.height<viewport.height);}
      await page.screenshot({path:join(out,label+'-chain.png'),fullPage:true});
      await page.screenshot({path:join(out,label+'-first-screen.png')});
      await row.locator('input').fill('4'); assert.equal(await buy.isDisabled(),true);
      await row.locator('input').fill('1'); await buy.click();
      await page.waitForFunction(()=>document.querySelector('#receipt').textContent.includes('Retry the same action'));
      await buy.click();
      await page.waitForFunction(()=>document.querySelector('#receipt').textContent.includes('Paper buy:'));
      assert.equal(orderIds.length,2); assert.equal(orderIds[0],orderIds[1]);
      await page.reload();
      await page.waitForFunction(()=>document.querySelector('#held').textContent==='1');
      await page.waitForFunction(()=>!document.querySelector('.yes .sell').disabled);
      await sell.click();
      await page.waitForFunction(()=>document.querySelector('#receipt').textContent.includes('Paper sell:'));
      await page.waitForFunction(()=>document.querySelector('#held').textContent==='0');
      const fills=JSON.parse(await page.locator('#fills').textContent());assert.equal(fills.length,2);assert.deepEqual(fills.map(t=>t.side),['buy','sell']);
      await page.locator('#next').click(); await page.waitForFunction(()=>document.querySelectorAll('.contract').length===1);
      await page.locator('#prev').click(); await page.waitForFunction(()=>document.querySelectorAll('.contract').length===12);
      await page.locator('#search').fill('Mars'); await page.waitForFunction(()=>document.querySelectorAll('.contract').length===1);
      assert.match(await page.locator('.contract h3').textContent(),/Mars/);
      await page.locator('#search').fill(''); await page.waitForFunction(()=>document.querySelectorAll('.contract').length===12);
      await page.waitForFunction(()=>!document.querySelector('.yes .buy').disabled);
      // Expire browser display without changing server time; stale prices cannot be clicked.
      await page.evaluate(()=>{const now=Date.now;Date.now=()=>now()+11000;});
      await page.waitForFunction(()=>document.querySelector('.yes .buy').disabled);
      assert.match(await buy.textContent(),/—/);
      await page.locator('#logout').click();await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Signed out'));
      assert.equal(await page.locator('#workspace').isHidden(),true);assert.equal(await page.locator('#fills').textContent(),'');
      assert.deepEqual(errors,[]);
    } finally {await context.close();env.DB.db.close();}
  }
  console.log('PASS phone/desktop chain, immediate prices, inline buy/sell, size/holdings guards, lost-response idempotency, reload, paging/search, stale hiding and sign-out');
} finally {await browser.close();globalThis.fetch=realFetch;}
