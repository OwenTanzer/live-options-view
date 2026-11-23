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
        if(url.pathname==='/api/one-clock/opening-oi')return json({session,availableAt:cutoff-5000,
          calibration:{openingAtm:700,settledSession:'2026-11-06'},levels:[{strike:700,call:5,put:2,callOI:480,putOI:15}]});
        if(url.pathname==='/api/one-clock/chain'){
          const at=Number(url.searchParams.get('cutoff')),boundary=cutoff+377;
          const instrument=url.searchParams.get('instrument');
          if(at<boundary-1) return json({rows:[],coverage:{complete:false}});
          const spot=at===boundary-1?699:at===boundary+1?null:701;
          const data={symbol:instrument,expiration:session,date:session,timestamp:new Date(boundary-500).toISOString(),
            underlying_price:spot,rows:[{Strike:699,Type:'call',OpenInterest:99999},{Strike:700,Type:'call',OpenInterest:at===boundary?1:12,Bid:1,Ask:2},{Strike:701,Type:'call',OpenInterest:99999}],market_readings:[]};
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
      assert.equal(await page.locator('#history-clock-btc').getAttribute('href'),'/api/one-clock/btc-replay?one_clock_at='+(cutoff-1));
      assert.ok(await page.locator('#history-clock-btc').isVisible());
      const enter=async(time,symbol)=>{
        await page.locator('#history-clock-instrument').fill(symbol);
        await page.locator('#history-clock-time').fill(time);
        await page.locator('#history-clock-form button').click();
      };
      for(const [time,expected] of [['10:00:00.499','$699'],['10:00:00.500','$701'],['10:00:00.501','unavailable'],['10:03:00.501','unavailable']]){
        await enter(time,'QQQ');
        await page.waitForFunction(text=>document.querySelector('#history-clock-status').textContent.includes('Underlying spot '+text),expected);
        const reference=page.locator('#history-clock-rows tr').filter({has:page.locator('.strike-cell',{hasText:'700'})});
        assert.match(await reference.locator('.call-cell').getAttribute('class'),/c5/);
        assert.match(await reference.locator('.call-cell').getAttribute('title'),/opening reference OI 480/);
        const unqualified=page.locator('#history-clock-rows tr').filter({has:page.locator('.strike-cell',{hasText:'699'})}).locator('.call-cell');
        assert.match(await unqualified.getAttribute('class'),/c0/);assert.match(await unqualified.innerText(),/99K/);
        if(expected!=='unavailable')assert.match(await page.locator('#history-clock-rows .atm-row .strike-cell').innerText(),new RegExp(expected.slice(1)));
        assert.equal(await page.locator('#history-clock-rows .atm').count(),expected==='unavailable'?0:1);
      }
      // The Live and dated Historical renderers consume that identical map
      // even while spot moves and every current-chain count disagrees.
      const surfaces=await page.evaluate(async({session,cutoff})=>{
        const map=await loadOpeningOi(session,cutoff);
        const rows=[699,700,701].flatMap(Strike=>['put','call'].map(Type=>({Strike,Type,OpenInterest:1})));
        const checks=[];
        for(const spot of [699,701]){
          const data={symbol:'QQQ',date:session,expiration:session,snapshot_time:session,timestamp:new Date(cutoff).toISOString(),
            rows,underlying_price:spot,tier:'0DTE_Regular',openingOi:map};
          renderHeatmap(data);renderHistoryHeatmap(data);
          for(const id of ['heatmap-body','hist-heatmap-body']){
            const body=document.getElementById(id);
            const reference=[...body.rows].find(r=>r.querySelector('.strike-cell').textContent.startsWith('700'));
            checks.push([reference.querySelector('.call-cell').classList.contains('c5'),body.querySelector('.atm-row .strike-cell').textContent.includes(String(spot))]);
          }
        }
        renderHeatmap({symbol:'QQQ',date:session,expiration:session,snapshot_time:session,timestamp:new Date(cutoff).toISOString(),rows,
          underlying_price:699,tier:'0DTE_Regular',openingOi:null});
        const missing=document.querySelector('#heatmap-body .call-cell');
        checks.push([missing.classList.contains('c0'),missing.textContent.includes('1')]);
        return checks;
      },{session,cutoff});
      assert.deepEqual(surfaces,Array.from({length:5},()=>[true,true]));
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
