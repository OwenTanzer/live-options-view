// Exercise the shipped ceremony with a separate disposable authenticator per member.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomBytes, createHash } from 'node:crypto';
import { chromium } from 'playwright';
import worker from '../backend/index.js';
import { FakeD1 } from './helpers/d1_shim.mjs';

const origin = 'https://options.moopertonic.net';
const db = new FakeD1();
db.db.exec(FakeD1.schemaSql());
db.db.exec(readFileSync(new URL('../backend/migrations/0003_trade_journal.sql',import.meta.url),'utf8'));
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
    await page.route('**/*',async route=>{
      const r=route.request(), url=new URL(r.url());
      assert.equal(url.origin,origin);
      if (!url.pathname.startsWith('/api/')) {
        if (!['/trade-journal.html','/js/trade-journal-loads.js','/js/journal-passkeys.js'].includes(url.pathname)) return route.abort();
        return route.fulfill({path:'site'+url.pathname});
      }
      const headers=await r.allHeaders();
      const req=new Request(r.url(),{method:r.method(),headers,body:r.postData() ?? undefined});
      if(r.postDataJSON()?.action==='verify' && !r.postDataJSON().invite) replay=req.clone();
      const resp=await worker.fetch(req,{DB:db});
      if (resp.status===401 && r.method()==='POST') console.log(member,'auth request denied:',r.postDataJSON()?.action);
      if(resp.headers.get('Set-Cookie')?.startsWith('__Host-journal=')) carriedCookie=resp.headers.get('Set-Cookie').split(';')[0];
      return route.fulfill({status:resp.status,headers:Object.fromEntries(resp.headers),body:await resp.text()});
    });
    await page.goto(origin+'/trade-journal.html');
    await page.locator('#access summary').click();
    await page.locator('#invitation').fill(invite);
    await page.click('#enroll');
    await page.locator('#journal:not([hidden]) #restore').waitFor({timeout:12000});
    const repeat=await worker.fetch(new Request(origin+'/api/trade-journal/passkey',{method:'POST',headers:{Origin:origin},body:JSON.stringify({action:'options',invite})}),{DB:db});
    assert.equal(repeat.status,401);
    await page.click('#lock');
    await page.locator('#access:not([hidden])').waitFor();
    await page.click('#unlock');
    await page.locator('#journal:not([hidden]) #restore').waitFor({timeout:12000});
    assert.equal((await worker.fetch(replay,{DB:db})).status,401);
    await db.prepare('UPDATE trade_journal_members SET credential_id=NULL WHERE member=?').bind(member).run();
    const revoked=await worker.fetch(new Request(origin+'/api/trade-journal?date=2026-11-13',{headers:{Cookie:carriedCookie}}),{DB:db});
    assert.equal(revoked.status,401);
    await page.close();
    console.log(member,'enrollment, single-use invitation, sign-out, passkey sign-in, replay rejection and revocation passed');
  }
} finally {await browser.close();}
