// Entire browser workflow over actual Worker handlers and disposable SQLite.
// All provider responses/identity in this file are explicitly SYNTHETIC fixtures.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { handleKalshi } from '../backend/kalshi.js';
import { fixture, envFixture, network, COOKIE } from './helpers/kalshi_fixture.mjs';
import { SITE } from '../backend/kalshi-access.js';
let chromium;
try { ({ chromium } = createRequire(import.meta.url)('playwright')); }
catch { ({ chromium } = createRequire(new URL('../backend/package.json', import.meta.url))('playwright')); }
const out = process.env.KALSHI_EVIDENCE_DIR || join(tmpdir(), 'kalshi-browser-fixtures');
mkdirSync(out, { recursive: true });
const f = fixture(), env = envFixture(), realFetch = globalThis.fetch;
globalThis.fetch = network(f);
let browser;
try {
  browser = await chromium.launch({ headless: true });
  for (const [label, viewport] of [['phone',{width:390,height:844}],['desktop',{width:1365,height:900}]]) {
    const context = await browser.newContext({ viewport });
    await context.addCookies([{name:'__Host-cds-session',value:COOKIE.split('=')[1],url:SITE,secure:true,httpOnly:true,sameSite:'Lax'}]);
    const page = await context.newPage(); const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const r=route.request(),u=new URL(r.url()); assert.equal(u.origin,SITE);
      if (u.pathname.startsWith('/api/kalshi/')) {
        const response=await handleKalshi(new Request(r.url(),{method:r.method(),headers:await r.allHeaders(),
          ...(r.postDataBuffer()?{body:r.postDataBuffer()}: {})}),env);
        await route.fulfill({status:response.status,headers:Object.fromEntries(response.headers),body:await response.text()});
        return;
      }
      if (u.pathname === '/credit-tape/auth/logout') {
        await context.clearCookies(); await route.fulfill({status:204,body:''}); return;
      }
      const allowed=new Map([['/kalshi.html','kalshi.html'],['/kalshi.css','kalshi.css'],['/js/kalshi.js','js/kalshi.js']]);
      assert.ok(allowed.has(u.pathname),'Unexpected browser destination');
      let body=readFileSync(new URL('../site/'+allowed.get(u.pathname),import.meta.url),'utf8');
      if (u.pathname.endsWith('.html')) body=body.replace('<main>','<main><p>LOCAL SYNTHETIC TEST — no live evidence or fills</p>');
      await route.fulfill({status:200,contentType:u.pathname.endsWith('.html')?'text/html':u.pathname.endsWith('.css')?'text/css':'application/javascript',body});
    });
    await page.goto(SITE+'/kalshi.html');
    await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('research enabled'));
    if(label==='phone') await page.locator('#discover').click();
    else await page.locator('#check').click();
    await page.locator('#markets button').first().click();
    await page.waitForFunction(()=>!document.querySelector('#submit').disabled);
    assert.match(await page.locator('#clock').textContent(),/exchange quote timestamp unavailable/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:join(out,`${label}-ticket.png`),fullPage:true});
    await page.locator('#qty').fill('1'); await page.locator('#submit').click();
    await page.waitForFunction(()=>document.querySelector('#receipt').textContent.includes('Paper fill:'));
    await page.reload(); await page.waitForFunction(()=>document.querySelector('#positions').textContent.includes('held'));
    await page.locator('#archives').click(); await page.locator('#archive-list button').first().click();
    await page.waitForFunction(()=>document.querySelector('#replay').textContent.includes('ARCHIVED OBSERVATION'));
    assert.match(await page.locator('#replay').textContent(),/ARCHIVED OBSERVATION/);
    await page.locator('#markets button').first().click(); await page.waitForFunction(()=>!document.querySelector('#submit').disabled);
    // Fast-forward just the browser clock; the stale guard must remove visible prices.
    await page.evaluate(()=>{ const realNow=Date.now; Date.now=()=>realNow()+11000; });
    await page.waitForFunction(()=>document.querySelector('#quotes').textContent.includes('Prices hidden'));
    assert.equal(await page.locator('#submit').isDisabled(),true);
    await page.locator('#logout').click(); await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Signed out'));
    assert.equal(await page.locator('#workspace').isHidden(),true); assert.equal(await page.locator('#fills').textContent(),'');
    assert.deepEqual(errors,[]); await context.close();
  }
  console.log('PASS synthetic phone/desktop owner flow, paper fill/reload, replay, stale-price hiding, sign-out and no horizontal overflow');
} finally { await browser?.close(); globalThis.fetch=realFetch; env.DB.db.close(); }
