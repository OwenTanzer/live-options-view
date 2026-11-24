// Run with the repository's Playwright browser tests. Every request is a local fixture.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const origin='http://127.0.0.1:41273';
const saved={
  id:'saved-account-rule',version:1,name:'Saved rule',instruments:['BTC'],combine:'all',transition:'rising',missingInput:'hold',
  conditions:[{source:'scanner',field:'count',op:'>=',value:4,maxAgeMs:150000}],
  outcomes:[{id:'move',source:'chain',field:'bid',measure:'change',horizonMs:900000,toleranceMs:150000,maxAgeMs:150000}]
};
const metadata={sources:{scanner:{count:'Print count'},chain:{bid:'Best bid'},news:{articles:'Articles'}},instruments:['QQQ','BTC'],rules:[],defaultSession:'2026-11-20'};
const browser=await chromium.launch({headless:true,channel:'chromium',executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
try {
  const page=await browser.newPage();
  const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  let release;
  const held=new Promise(resolve=>{release=resolve;});
  const accountPosts=[];
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    assert.equal(url.origin,origin);
    if(url.pathname==='/api/one-clock/research') {
      if(request.method()==='POST') {
        const body=request.postDataJSON();accountPosts.push(body);
        return route.fulfill({json:{rule:body.rule}});
      }
      await held;
      return route.fulfill({json:metadata});
    }
    assert.match(url.pathname,/^\/(one-clock\.(html|css)|(?:js|shared)\/[a-z0-9-]+\.js)$/);
    const contentType=url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':'text/html';
    return route.fulfill({contentType,body:await readFile(new URL('../site'+url.pathname,import.meta.url))});
  });
  await page.addInitScript(rule=>localStorage.setItem('one-clock.rules.v1',JSON.stringify([rule])),saved);
  await page.goto(origin+'/one-clock.html',{waitUntil:'domcontentloaded'});
  const condition=page.locator('#conditions .condition').first(),outcome=page.locator('#outcomes .outcome').first();
  const field=(row,name)=>row.locator('[data-field="'+name+'"]');
  assert.equal(await field(condition,'source').inputValue(),'scanner');
  assert.equal(await field(outcome,'field').inputValue(),'bid');
  await page.locator('#rule-name').fill('Unsaved edits');
  await field(condition,'value').fill('19');
  await page.locator('#add-condition').click();
  const newRow=page.locator('#conditions .condition').last();
  release();
  await newRow.locator('select[data-field="source"] option[value="news"]').waitFor({state:'attached'});
  assert.equal(await page.locator('#rule-name').inputValue(),'Unsaved edits');
  assert.equal(await page.locator('#instrument').inputValue(),'BTC');
  assert.equal(await field(condition,'field').inputValue(),'count');
  assert.equal(await field(condition,'value').inputValue(),'19');
  assert.equal(await field(outcome,'field').inputValue(),'bid');
  await field(newRow,'source').selectOption('news');
  assert.equal(await field(newRow,'field').inputValue(),'articles');
  await page.locator('#account-save').click();
  await page.waitForFunction(()=>document.getElementById('form-status').textContent.includes('to your account'));
  const sent=accountPosts[0].rule;
  assert.equal(sent.name,'Unsaved edits');assert.equal(sent.conditions[0].value,19);
  assert.deepEqual(sent.conditions.map(c=>[c.source,c.field]),[['scanner','count'],['news','articles']]);
  assert.equal(sent.outcomes[0].field,'bid');assert.deepEqual(errors,[]);
  console.log('Passed account metadata, saved field restoration, in-flight edits, and submitted rule verification.');
} finally {await browser.close();}
