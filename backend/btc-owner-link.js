import { jsonResponse, readJsonBody, requireSession } from './common.js';
import { accountId, hashSessionToken } from './storage.js';

const RP = 'options.moopertonic.net', ORIGIN = 'https://' + RP;
const denied = () => jsonResponse({ error: 'Owner verification failed. No access was linked.' }, 403);
// Owen is an existing, invitation-provisioned journal principal, not a paper
// username or client claim. Other journal members are NOT this feed's subscriber.
// The source rights receipt must establish that this configured source belongs
// to that principal; it cannot authorize redistribution to other people.
export function ownerLinkAvailable(env) {
  return env.BTC_OWNER_LINK === 'enabled' &&
    typeof env.BTC_OWNER_RIGHTS_RECEIPT === 'string' && env.BTC_OWNER_RIGHTS_RECEIPT.trim().length > 0 &&
    typeof env.LIVE_QUOTE_ORIGIN === 'string' && env.LIVE_QUOTE_ORIGIN.length > 0;
}
export async function ownerSourceBinding(env) {
  return hashSessionToken(JSON.stringify(['btc-owner-v1', env.LIVE_QUOTE_ORIGIN, env.BTC_OWNER_RIGHTS_RECEIPT]));
}
export async function btcAllowed(env, session) {
  if (!ownerLinkAvailable(env) || !session?.token || session.record?.is_bot || session.record?.account_closed) return false;
  try {
    const proof = await env.DB.prepare(`SELECT 1 AS allowed FROM btc_owner_sessions p
      JOIN sessions s ON s.token_hash=p.token_hash AND s.username=p.username
      JOIN users u ON u.username=p.username
      JOIN trade_journal_members m ON m.credential_id=p.credential_id
      WHERE p.token_hash=? AND p.username=? AND p.source_binding=?
      AND p.expires>? AND s.expires_at>? AND m.member='Owen' AND m.public_key IS NOT NULL`)
      .bind(await hashSessionToken(session.token), accountId(session.username), await ownerSourceBinding(env),
        Math.floor(Date.now()/1000), Math.floor(Date.now()/1000)).first();
    return !!proof;
  } catch { return false; } // Missing migration/source fails closed; no legacy allowlist.
}

export async function handleBtcOwnerLink(request, env) {
  if (request.method !== 'POST') return jsonResponse({ error: 'Method not allowed' }, 405);
  if (new URL(request.url).origin !== ORIGIN || request.headers.get('Origin') !== ORIGIN ||
      request.headers.get('Sec-Fetch-Site') === 'cross-site' ||
      request.headers.get('Content-Type')?.split(';')[0].trim() !== 'application/json') return denied();
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Sign in to your paper account first' }, 401);
  if (session.record.is_bot || session.record.account_closed) return denied();
  const { body, error } = await readJsonBody(request);
  if (error) return error;
  const tokenHash = await hashSessionToken(session.token), username = accountId(session.username);
  const now = Math.floor(Date.now()/1000), db = env.DB;
  try {
    if (body?.action === 'unlink') {
      await db.batch([
        db.prepare('DELETE FROM btc_owner_challenges WHERE token_hash=?').bind(tokenHash),
        db.prepare('DELETE FROM btc_owner_sessions WHERE token_hash=?').bind(tokenHash),
      ]);
      return jsonResponse({ ok: true });
    }
    if (!ownerLinkAvailable(env)) return jsonResponse({ error: 'Owner linking is not available for this source' }, 503);
    if (body?.confirm !== true || !['options', 'verify'].includes(body?.action)) return denied();
    const binding = await ownerSourceBinding(env);
    const { generateAuthenticationOptions, verifyAuthenticationResponse } = await import('@simplewebauthn/server');
    if (body.action === 'options') {
      // Discoverable assertion: no account list, member names or credential IDs
      // are sent to the browser. No registration or invitation flow here.
      const options = await generateAuthenticationOptions({ rpID: RP, userVerification: 'required' });
      await db.batch([
        db.prepare('DELETE FROM btc_owner_challenges WHERE expires<=?').bind(now),
        db.prepare(`INSERT INTO btc_owner_challenges(token_hash,username,challenge,source_binding,expires)
          SELECT token_hash,username,?,?,? FROM sessions WHERE token_hash=? AND username=? AND expires_at>?
          ON CONFLICT(token_hash) DO UPDATE SET challenge=excluded.challenge,source_binding=excluded.source_binding,expires=excluded.expires`)
          .bind(options.challenge, binding, now+300, tokenHash, username, now),
      ]);
      return jsonResponse(options);
    }
    // Atomic single use, including zero-counter synced passkeys. A failed
    // attempt consumes this challenge; another tab's options replaces it.
    const ceremony = await db.prepare(`DELETE FROM btc_owner_challenges
      WHERE token_hash=? AND username=? RETURNING challenge,source_binding,expires`).bind(tokenHash, username).first();
    if (!ceremony || ceremony.expires<=now || ceremony.source_binding!==binding || typeof body.response?.id !== 'string') return denied();
    const member = await db.prepare(`SELECT credential_id,public_key,counter FROM trade_journal_members
      WHERE credential_id=? AND member='Owen' AND public_key IS NOT NULL`).bind(body.response.id).first();
    if (!member) return denied();
    let result;
    try {
      result = await verifyAuthenticationResponse({ response: body.response,
        expectedChallenge: ceremony.challenge, expectedOrigin: ORIGIN, expectedRPID: RP,
        requireUserVerification: true, credential: { id: member.credential_id,
          publicKey: Uint8Array.from(atob(member.public_key), c => c.charCodeAt(0)), counter: member.counter } });
    } catch { return denied(); }
    if (!result.verified) return denied();
    const saved = await db.batch([
      db.prepare(`UPDATE trade_journal_members SET counter=? WHERE credential_id=? AND public_key=? AND counter=? AND member='Owen'`)
        .bind(result.authenticationInfo.newCounter, member.credential_id, member.public_key, member.counter),
      // changes() ties the proof to the successful credential CAS in this same
      // transaction. Re-check the paper session after the asynchronous ceremony.
      db.prepare(`INSERT INTO btc_owner_sessions(token_hash,username,credential_id,source_binding,expires)
        SELECT token_hash,username,?,?,MIN(expires_at,?) FROM sessions
        WHERE token_hash=? AND username=? AND expires_at>? AND changes()=1
        ON CONFLICT(token_hash) DO UPDATE SET credential_id=excluded.credential_id,source_binding=excluded.source_binding,expires=excluded.expires`)
        .bind(member.credential_id, binding, now+8*3600, tokenHash, username, now),
    ]);
    return saved[1].meta.changes === 1 ? jsonResponse({ ok: true, linked: true }) : denied();
  } catch { return jsonResponse({ error: 'Owner verification unavailable. No access was linked.' }, 503); }
}
