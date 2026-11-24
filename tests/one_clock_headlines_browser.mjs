import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';
import {headlineDefinition} from '../site/shared/one-clock-headlines.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';

const session='2026-11-09',at=Date.parse(session+'T10:05:00Z'),receipt=at+900001,base=at-10000;
const observations=[
  {source:'company-quote',id:'base',eventAt:base,availableAt:base+1,value:{price:100}},
  {source:'company-headline',id:'news',eventAt:at,availableAt:at,value:{title:'NVDA guides higher',revisionId:'revision',itemId:'story'}},
  {source:'company-quote',id:'endpoint',eventAt:receipt-1,availableAt:receipt,value:{price:104}},
].map(r=>({...r,session,instrument:'NVDA',revision:1,origin:'recorded',evidence:r.id,value:{...r.value,ticker:'NVDA',instrumentId:'eq.nvda'}}));
const browser=await chromium.launch({channel:'chrome',headless:true});
try {
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  let saved,active=false;
  await page.route('https://headline.test/**',async route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/api/one-clock/research'){
      const request=route.request(),body=request.method()==='POST'?request.postDataJSON():{};
      if(body.action==='save'){
        assert.deepEqual(body.rule.conditions,headlineDefinition('NVDA').conditions);saved=body.rule;
        return route.fulfill({json:{rule:saved}});
      }
      if(body.action==='activate'){assert.equal(saved.id,body.ruleId);active=true;return route.fulfill({json:{active}});}
      if(body.action==='replay-headlines'){
        const selection={session,instrument:'NVDA',cutoff:body.cutoff,origin:'recorded'};
        const events=evaluateEvents({rule:saved,observations,selection,sessionClose:at+6*3600000});
        return route.fulfill({json:{...selection,events,rule:saved,coverage:{reason:'Retained news and quotes.'}}});
      }
      return route.fulfill({json:{rules:saved?[{rule:saved}]:[],sources:{},activations:[],defaultSession:session,instruments:['NVDA','QQQ']}});
    }
    if(url.pathname.startsWith('/api/'))return route.fulfill({status:401,body:'{}'});
    return route.fulfill({path:fileURLToPath(new URL('../site'+url.pathname,import.meta.url))});
  });
  await page.goto('https://headline.test/one-clock.html');
  await page.locator('#new-rule').click();
  await page.locator('#study-kind').selectOption('headline');
  assert.match(await page.locator('#headline-instrument').textContent(),/SHEL · headlines only/);
  await page.locator('#headline-instrument').selectOption('NVDA');
  await page.locator('#rule-name').fill('NVDA headline');
  await page.locator('#account-save').click();
  await page.waitForFunction(()=>document.querySelector('#form-status').textContent.includes('your account') || document.querySelector('#form-status').classList.contains('error'));
  assert.match(await page.locator('#form-status').textContent(),/Saved NVDA headline v1 to your account/);
  await page.locator('#account-activate').click();
  await page.getByText(/Observation activated/).waitFor();assert.ok(active);
  await page.locator('#encounter-day').fill(session);await page.locator('#encounter-run').click();
  await page.locator('#headline-replay-controls').waitFor();
  await page.locator('#observed-results').getByLabel('Headline endpoint timing').selectOption('delayed');
  await page.locator('#observed-ledger').getByText(/15 min elapsed/).waitFor();
  await page.locator('#observed-ledger').getByText('Replay quote receipt').click();
  await page.locator('#observed-evidence').waitFor();
  assert.match(await page.locator('#observed-evidence').textContent(),/eq.nvda/);
  await page.locator('#headline-replay-time').fill(new Date(receipt-1).toISOString().slice(0,16));
  await page.locator('#headline-replay-read').click();
  await page.locator('#observed-ledger').getByText('pending',{exact:true}).waitFor();
  assert.doesNotMatch(await page.locator('#observed-evidence').textContent(),/"price": 104/);
  assert.deepEqual(errors,[]);
  console.log('Chromium passed account save, activate, delayed Results, receipt replay and 1 ms rewind.');
}finally{await browser.close();}
