// Exercise the shipped ceremony with a separate disposable authenticator per member.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomBytes, createHash } from 'node:crypto';
import { chromium } from 'playwright';
import {journalFillReplayHref} from '../site/shared/one-clock-journal-replay.js';
import worker from '../backend/index.js';
import { FakeD1 } from './helpers/d1_shim.mjs';

const origin = 'https://options.moopertonic.net';
const db = new FakeD1();
db.db.exec(FakeD1.schemaSql());
db.db.exec(readFileSync(new URL('../backend/migrations/0003_trade_journal.sql',import.meta.url),'utf8'));
db.db.exec(readFileSync(new URL('../backend/migrations/0009_one_clock_journal.sql',import.meta.url),'utf8'));
const session = '2026-11-09', fillAt = Date.parse(session+'T15:12:05Z'), asOf=Date.parse('2026-11-11T17:00:00Z');
const positionId='pos:0123456789abcdef', executionId='notion:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:order:7203';
const instrument={instrument_type:'option',underlying:'QQQ',option_type:'put',strike:600,multiplier:100,occ_symbol:'QQQ261109P00600000'};
const fill={execution_id:executionId,ts:new Date(fillAt).toISOString(),ts_kind:'execution_alert',side:'buy',qty:1,price:2,
  instrument,fee:.5,fee_basis:'estimated',order_ref:'7203'};
const pos={position_id:positionId,owner:'Owen',supported:true,contract:instrument,executions:[fill]};
await db.prepare('INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at) VALUES(?,?,?,?,?,?,?)')
  .bind('synthetic-journal',positionId,'active',1,'test',JSON.stringify(pos),asOf-1000).run();
const browser = await chromium.launch({channel:'chromium',headless:true});
try {
  for (const member of ['Owen','Jayden','Jacob']) {
    const invite = randomBytes(32).toString('base64url');
    const hash = createHash('sha256').update(invite).digest('hex');
    await db.prepare('INSERT INTO trade_journal_members(member,invite_hash,invite_until) VALUES (?,?,unixepoch()+3600)').bind(member,hash).run();
    const page = await browser.newPage();
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('WebAuthn.enable');
    await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',
      hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
    let replay, carriedCookie;
    const journalReads = [];
    await page.route('**/*',async route=>{
      const r=route.request(), url=new URL(r.url());
      assert.equal(url.origin,origin);
      if (!url.pathname.startsWith('/api/')) {
        if(url.pathname==='/trade-journal')return route.fulfill({path:'site/trade-journal.html',contentType:'text/html'});
        if (!['/trade-journal.html','/js/trade-journal-loads.js','/js/journal-passkeys.js',
          '/shared/one-clock.js','/shared/one-clock-session.js','/shared/one-clock-journal-source.js','/shared/one-clock-replay-link.js',
          '/shared/one-clock-journal-replay.js','/shared/one-clock-chain-source.js'].includes(url.pathname)) return route.abort();
        return route.fulfill({path:'site'+url.pathname});
      }
      if(url.pathname==='/api/one-clock/chain') {
        const t=Number(url.searchParams.get('cutoff'));
        assert.ok(!r.url().includes(executionId));
        return route.fulfill({json:{rows:[{source:'chain',id:'chain-'+t,revision:1,instrument:'QQQ',session,
          origin:'recorded',eventAt:t-1000,availableAt:t,evidence:'fixture',value:{underlying_price:600,
            rows:[{OptionSymbol:instrument.occ_symbol,Bid:2,Ask:2.1}]}}]}});
      }
      const headers=await r.allHeaders();
      const req=new Request(r.url(),{method:r.method(),headers,body:r.postData() ?? undefined});
      if(r.postDataJSON()?.action==='verify' && !r.postDataJSON().invite) replay=req.clone();
      const resp=await worker.fetch(req,{DB:db});
      if (r.method() === 'GET' && url.pathname === '/api/trade-journal') {
        journalReads.push({date:url.searchParams.get('date'), cutoff:url.searchParams.get('cutoff'), status:resp.status});
      }
      if (resp.status===401 && r.method()==='POST') console.log(member,'auth request denied:',r.postDataJSON()?.action);
      if(resp.headers.get('Set-Cookie')?.startsWith('__Host-journal=')) carriedCookie=resp.headers.get('Set-Cookie').split(';')[0];
      return route.fulfill({status:resp.status,headers:Object.fromEntries(resp.headers),body:await resp.text()});
    });
    const cutoff = Date.parse('2026-11-09T14:30:01.523Z');
    const query = new URLSearchParams({one_clock_session:'2026-11-09',one_clock_instrument:'QQQ',one_clock_at:String(cutoff)});
    await page.goto(origin+'/trade-journal.html?'+query);
    await page.locator('#access summary').click();
    await page.locator('#invitation').fill(invite);
    const linkedRead = {date:'2026-11-09', cutoff:String(cutoff), status:200};
    const beforeEnrollment = journalReads.length;
    await page.click('#enroll');
    await page.locator('#journal:not([hidden]) #restore').waitFor({timeout:12000});
    assert.deepEqual(journalReads.slice(beforeEnrollment),[linkedRead]);
    assert.equal(await page.locator('#journal-view').inputValue(),'recorded');
    assert.equal(await page.locator('#day').inputValue(),'2026-11-09');
    assert.equal(await page.locator('#journal-cutoff').inputValue(),'09:30:01.523');
    const repeat=await worker.fetch(new Request(origin+'/api/trade-journal/passkey',{method:'POST',headers:{Origin:origin},body:JSON.stringify({action:'options',invite})}),{DB:db});
    assert.equal(repeat.status,401);
    await page.click('#lock');
    await page.locator('#access:not([hidden])').waitFor();
    const beforeSignIn = journalReads.length;
    await page.click('#unlock');
    await page.locator('#journal:not([hidden]) #restore').waitFor({timeout:12000});
    assert.deepEqual(journalReads.slice(beforeSignIn),[linkedRead]);
    if(member==='Owen'){
      const href=journalFillReplayHref({journalFill:{positionId,executionId,fillAt}},asOf);
      await page.goto(origin+href);
      await page.getByText('Selected fill',{exact:true}).waitFor();
      await page.getByText(/5 underlying and 5 exact-contract observations/).waitFor();
      assert.equal(await page.locator('#journal-asof-date').inputValue(),'2026-11-11');
      assert.equal(await page.locator('#day').inputValue(),session);
      assert.equal(await page.locator('#lanes svg').count(),2);
      const wrong=journalFillReplayHref({journalFill:{positionId,executionId:executionId+'0',fillAt}},asOf);
      await page.goto('about:blank'); await page.goto(origin+wrong);
      await page.getByText(/This execution was unavailable/).waitFor();
      assert.equal(await page.locator('#lanes svg').count(),0);
    }
    const beforeInvalid = journalReads.length;
    await page.goto(origin+'/trade-journal.html?one_clock_session=2026-11-09');
    await page.getByText('Invalid event replay link.',{exact:true}).waitFor();
    assert.equal(journalReads.length,beforeInvalid);
    await page.goto(origin+'/trade-journal.html');
    await page.locator('#journal:not([hidden]) #restore').waitFor({timeout:12000});
    assert.equal(await page.locator('#journal-view').inputValue(),'retrospective');
    assert.deepEqual(journalReads.slice(beforeInvalid),[
      {date:await page.locator('#day').inputValue(), cutoff:null, status:200}
    ]);
    assert.equal((await worker.fetch(replay,{DB:db})).status,401);
    await db.prepare('DELETE FROM trade_journal_members WHERE member=?').bind(member).run();
    await db.prepare('INSERT OR IGNORE INTO trade_journal_members(member,invite_hash,invite_until) VALUES (?,?,unixepoch()+604800)').bind(member,hash).run();
    const oldInvite=await worker.fetch(new Request(origin+'/api/trade-journal/passkey',{method:'POST',headers:{Origin:origin},body:JSON.stringify({action:'options',invite})}),{DB:db});
    assert.equal(oldInvite.status,401);
    assert.equal((await db.prepare('SELECT credential_id,invite_hash FROM trade_journal_members WHERE member=?').bind(member).first()).credential_id,null);
    const revoked=await worker.fetch(new Request(origin+'/api/trade-journal?date=2026-11-13',{headers:{Cookie:carriedCookie}}),{DB:db});
    assert.equal(revoked.status,401);
    await page.close();
    console.log(member,'enrollment, single-use invitation, sign-out, passkey sign-in, replay rejection and revocation passed');
  }
} finally {await browser.close();}
