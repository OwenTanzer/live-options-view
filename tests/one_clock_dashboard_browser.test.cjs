// A synthetic, network-isolated session through the actual dashboard.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const session = '2026-11-09';
const cutoff = Date.parse(session+'T15:00:00.123Z');
const params = new URLSearchParams({one_clock_session:session,one_clock_instrument:'QQQ',one_clock_at:String(cutoff)});

(async()=>{
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_BIN?{executablePath:process.env.CHROME_BIN}:{channel:'chromium'})});
  try {
    for (const width of [1280,390]) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors = [], requests = [];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const url = new URL(route.request().url());
        requests.push(url);
        const json = body=>route.fulfill({json:body});
        if(url.pathname==='/api/one-clock/chain'){
          const at=Number(url.searchParams.get('cutoff')),boundary=cutoff+377;
          const instrument=url.searchParams.get('instrument');
          if(at<boundary-1) return json({rows:[],coverage:{complete:false}});
          const spot=at===boundary-1?699:at===boundary+1?null:701;
          const data={symbol:instrument,expiration:session,date:session,timestamp:new Date(boundary-500).toISOString(),
            underlying_price:spot,rows:[{Strike:700,Type:'call',OpenInterest:12,Bid:1,Ask:2}],market_readings:[]};
          return json({rows:[{source:'chain',id:'fixture-chain',revision:1,instrument,session,origin:'recorded',
            eventAt:boundary-500,availableAt:at,evidence:'fixture-chain',value:data}],coverage:{complete:true}});
        }
        if(url.pathname==='/api/one-clock/bots/roster')return json({bots:['sample']});
        if(url.pathname==='/api/one-clock/bot-decisions'){
          const row={source:'bot-decision',session,instrument:'QQQ',origin:'recorded',revision:1,evidence:'fixture',
            id:'decision',eventAt:cutoff-1000,availableAt:cutoff,value:{bot:'sample',outcome:'no_trade',action:null,inputAgeMs:1400},
            details:{anchor:{anchor:.14}}};
          return json({rows:Number(url.searchParams.get('cutoff'))>=cutoff?[row]:[],next:null,coverage:{complete:false}});
        }
        if(url.pathname==='/api/one-clock/bots') {
          const row={source:'bot-ledger',session,instrument:'QQQ',origin:'recorded',revision:1,evidence:'fixture',
            id:'receipt',eventAt:cutoff-1000,availableAt:cutoff,value:{side:'buy',sym:'QQQ',price:100,qty:2},
            details:{bot:'sample',eventSession:session}};
          return json({rows:[row],coverage:{complete:true},next:null,version:'1'.repeat(64)});
        }
        const path='site'+(url.pathname==='/'?'/index.html':url.pathname);
        if(url.origin==='https://dashboard.test' && fs.existsSync(path) && fs.statSync(path).isFile())
          return route.fulfill({path});
        return route.fulfill({status:404,json:{}});
      });
      await page.goto('https://dashboard.test/?'+params);
      await page.waitForFunction(()=>window.oneClockDashboard?.snapshot().mode==='replay');
      assert.equal(await page.locator('#clock-journal').getAttribute('href'),'/trade-journal.html?'+params);
      assert.equal(await page.locator('#history-clock-kalshi').getAttribute('href'),'/kalshi.html?one_clock_at='+cutoff);
      await page.locator('#tab-live').click();
      assert.equal(await page.locator('#live-panel').isVisible(),false);
      await page.waitForFunction(()=>document.querySelector('#history-clock-status').textContent.includes('No qualified snapshot'));
      const chainCalls = requests.filter(u=>u.pathname==='/api/one-clock/chain').length;
      await page.locator('#tab-squeeze').click();
      assert.equal(await page.locator('#clock-shortlist-view').isVisible(),true);
      assert.equal(await page.locator('#squeeze-panel').isVisible(),false);
      const polls=requests.filter(u=>u.pathname.includes('/squeeze-scanner/')).length;
      await page.evaluate(()=>fetchSqueezeLatest());
      assert.equal(requests.filter(u=>u.pathname.includes('/squeeze-scanner/')).length,polls);
      await page.locator('#tab-bots').click();
      assert.equal(await page.locator('#bots-panel').isVisible(),false);
      await page.locator('#history-clock-load-bots').click();
      await page.locator('#history-clock-bot').selectOption('sample');
      await page.waitForFunction(()=>document.querySelectorAll('#history-clock-bot-rows tr').length===1);
      await page.waitForFunction(()=>document.querySelector('#clock-decisions').textContent.includes('no_trade'));
      assert.match(await page.locator('#clock-decisions').textContent(),/0\.14/);
      assert.equal(await page.evaluate(()=>window.oneClockDashboard.snapshot().cutoff),cutoff);
      assert.match(await page.locator('#history-clock-bot-inventory').textContent(),/"qty": 2/);
      await page.locator('#history-clock-time').fill('10:00:00.122');
      await page.locator('#history-clock-form button').click();
      await page.waitForFunction(()=>document.querySelector('#history-clock-bot-status').textContent.includes('0 visible'));
      assert.equal(await page.locator('#history-clock-bot-rows tr').count(),0);
      await page.waitForFunction(()=>document.querySelector('#clock-decisions-status').textContent.startsWith('0 recorded'));
      assert.equal(requests.filter(u=>u.pathname==='/api/one-clock/chain').length,chainCalls);
      assert.equal(requests.some(u=>u.pathname==='/api/bots'),false);
      await page.locator('#tab-history').click();
      const enter=async(time,symbol)=>{
        await page.locator('#history-clock-instrument').fill(symbol);
        await page.locator('#history-clock-time').fill(time);
        await page.locator('#history-clock-form button').click();
      };
      for(const [time,expected] of [['10:00:00.499','$699'],['10:00:00.500','$701'],['10:00:00.501','unavailable'],['10:03:00.501','unavailable']]){
        await enter(time,'QQQ');
        await page.waitForFunction(text=>document.querySelector('#history-clock-status').textContent.includes('Underlying spot '+text),expected);
        assert.match(await page.locator('#history-clock-rows .strike-cell').textContent(),/^700/);
        assert.equal(await page.locator('#history-clock-rows .atm').count(),expected==='unavailable'?0:1);
      }
      await enter('10:00:00.500','IBIT');
      await page.waitForFunction(()=>document.querySelector('#history-clock-status').textContent.startsWith('IBIT'));
      assert.ok(await page.locator('#history-order-flow-link').isHidden());
      await enter('10:00:00.500','BTC');
      await page.getByText('CME BTC chain archive unavailable at this cursor. IBIT is a separate instrument.').waitFor();
      assert.equal(await page.locator('#history-clock-rows tr').count(),0);
      await page.locator('#tab-bots').click();
      await page.locator('#clock-mode').selectOption('live');
      await page.waitForFunction(()=>window.oneClockDashboard.snapshot().mode==='live');
      assert.equal(await page.locator('#bots-panel').isVisible(),true);
      assert.equal(await page.locator('#history-clock').isVisible(),false);
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log('Desktop and mobile shared-cursor dashboard checks passed with isolated fixtures.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
