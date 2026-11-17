import { jsonResponse, parseCookies, readJsonBody } from './common.js';

const ROOT = 'options.moopertonic.net';
const ORIGIN = 'https://' + ROOT;
const SESSION = '__Host-journal';
const CEREMONY = '__Host-journal-ceremony';
const ttl = 8 * 3600;
const utf8 = new TextEncoder();
const bytes = n => crypto.getRandomValues(new Uint8Array(n));
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2,'0')).join('');
const hash = async text => hex(await crypto.subtle.digest('SHA-256', utf8.encode(text)));
const cookie = (name,value,age) => name+'='+value+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age='+age;
const denied = () => jsonResponse({error:'Journal access denied. Check your invitation or try your passkey again.'},401);

export async function authorizeJournalViewer(request, env) {
  const token = parseCookies(request)[SESSION];
  if (!token) return {response:denied()};
  const session = await env.DB.prepare(
    'SELECT member FROM trade_journal_sessions JOIN trade_journal_members USING(credential_id) WHERE token_hash=? AND expires>unixepoch()'
  ).bind(await hash(token)).first();
  return session ? {session} : {response:denied()};
}

// Stateless until a valid authenticator responds. Tie the challenge to this
// browser cookie, operation, invitation and a five-minute lifetime.
async function challenge(db, state, operation, member) {
  const {secret} = await db.prepare('SELECT secret FROM trade_journal_auth_key WHERE singleton=1').first();
  const key = await crypto.subtle.importKey('raw',utf8.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  return hex(await crypto.subtle.sign('HMAC',key,utf8.encode([state,operation,member].join('|'))));
}

export async function handleJournalPasskey(request, env) {
  if (request.headers.get('Origin') !== ORIGIN || new URL(request.url).origin !== ORIGIN) return denied();
  const {body,error} = await readJsonBody(request);
  if (error) return error;
  const {action,invite,response} = body ?? {};
  const db = env.DB;
  if (action === 'logout') {
    const token = parseCookies(request)[SESSION];
    if (token) await db.prepare('UPDATE trade_journal_sessions SET token_hash=NULL WHERE token_hash=?').bind(await hash(token)).run();
    return jsonResponse({ok:true},200,{'Set-Cookie':cookie(SESSION,'',0)});
  }
  if (!['options','verify'].includes(action)) return denied();
  const enroll = typeof invite === 'string' && invite.length > 0;
  if (enroll && !/^[a-zA-Z0-9_-]{43}$/.test(invite)) return denied();
  const inviteHash = enroll ? await hash(invite) : '';
  const member = enroll && await db.prepare(
    'SELECT member FROM trade_journal_members WHERE invite_hash=? AND invite_until>unixepoch() AND credential_id IS NULL'
  ).bind(inviteHash).first();
  if (enroll && !member) return denied();
  const now = Math.floor(Date.now()/1000);
  const state = action === 'options' ? now+'.'+hex(bytes(32)) : parseCookies(request)[CEREMONY];
  if (!/^\d{10}\.[0-9a-f]{64}$/.test(state) || now-Number(state.slice(0,10)) > 300 || Number(state.slice(0,10)) > now) return denied();
  const { generateRegistrationOptions, verifyRegistrationResponse, generateAuthenticationOptions,
    verifyAuthenticationResponse } = await import('@simplewebauthn/server');
  const material = await challenge(db,state,enroll?'create':'get',inviteHash);
  if (action === 'options') {
    const options = enroll
      ? await generateRegistrationOptions({rpID:ROOT,rpName:'Options View Journal',userName:member.member,
          userID:utf8.encode('journal-'+member.member),challenge:material,supportedAlgorithmIDs:[-7,-257],
          authenticatorSelection:{residentKey:'required',userVerification:'required'}})
      : await generateAuthenticationOptions({rpID:ROOT,challenge:material,userVerification:'required'});
    return jsonResponse(options,200,{'Set-Cookie':cookie(CEREMONY,state,300)});
  }
  // SimpleWebAuthn encodes a supplied string as UTF-8 before base64url.
  const expectedChallenge = btoa(material).replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
  const verification = {response,expectedChallenge,expectedOrigin:ORIGIN,expectedRPID:ROOT,requireUserVerification:true};
  let credentialID;
  if (enroll) {
    let result;
    try {result=await verifyRegistrationResponse(verification);} catch {return denied();}
    if (!result.verified) return denied();
    const cred=result.registrationInfo.credential;
    const saved=await db.prepare('UPDATE trade_journal_members SET credential_id=?,public_key=?,counter=?,invite_hash=NULL,invite_until=NULL WHERE member=? AND invite_hash=? AND credential_id IS NULL AND invite_until>unixepoch()')
      .bind(cred.id,btoa(String.fromCharCode(...cred.publicKey)),cred.counter,member.member,inviteHash).run();
    if (!saved.meta.changes) return denied();
    credentialID=cred.id;
  } else {
    if (typeof response?.id !== 'string') return denied();
    const member=await db.prepare('SELECT * FROM trade_journal_members WHERE credential_id=?').bind(response.id).first();
    if (!member) return denied();
    let result;
    try {result=await verifyAuthenticationResponse({...verification,credential:{id:member.credential_id,
      publicKey:Uint8Array.from(atob(member.public_key),c=>c.charCodeAt(0)),counter:member.counter}});}catch{return denied();}
    if (!result.verified) return denied();
    const saved=await db.prepare('UPDATE trade_journal_members SET counter=? WHERE credential_id=? AND counter=?')
      .bind(result.authenticationInfo.newCounter,member.credential_id,member.counter).run();
    if (!saved.meta.changes) return denied();
    credentialID=member.credential_id;
  }
  const token=hex(bytes(32));
  await db.prepare('DELETE FROM trade_journal_sessions WHERE expires<=unixepoch()').run();
  const saved=await db.prepare('INSERT OR IGNORE INTO trade_journal_sessions(token_hash,credential_id,challenge,expires) VALUES(?,?,?,?)')
    .bind(await hash(token),credentialID,expectedChallenge,now+ttl).run();
  if (!saved.meta.changes) return denied();
  return jsonResponse({ok:true},200,{'Set-Cookie':cookie(SESSION,token,ttl)});
}
