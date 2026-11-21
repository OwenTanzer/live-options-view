import { readLimited } from './common.js';

export const OWNER_ID = 125096971;
export const ACCOUNT = `kalshi:personal:${OWNER_ID}`;
export const SITE = 'https://options.moopertonic.net';
export const CREDIT_ORIGIN = 'https://prolific-essence-production.up.railway.app';

export function configured(env) {
  return env.KALSHI_PERSONAL_ENABLED === '1' && !!env.DB &&
    env.ENVIRONMENT !== 'preview' && env.ISOLATION === undefined;
}
export function privateJSON(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: {
    'Content-Type': 'application/json', 'Cache-Control': 'private, no-store',
    Vary: 'Cookie', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    'Cross-Origin-Resource-Policy': 'same-origin',
  } });
}
// Never accept an identity/header from the browser or an ordinary Options View session.
// The fixed upstream validates its existing opaque server-side CDS session.
export async function ownerIdentity(request) {
  const cookies = (request.headers.get('Cookie') || '').split(';').map(x => x.trim())
    .filter(x => x.startsWith('__Host-cds-session='));
  if (cookies.length !== 1 || !/^__Host-cds-session=[A-Za-z0-9_.~-]{16,512}$/.test(cookies[0])) return null;
  const response = await fetch(CREDIT_ORIGIN + '/api/private/identity', {
    method: 'GET', headers: { Cookie: cookies[0], Accept: 'application/json' },
    redirect: 'manual', cache: 'no-store', signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) return null;
  const data = JSON.parse(await readLimited(response, 1024));
  return data.github_id === OWNER_ID ? { github_id: OWNER_ID, account_id: ACCOUNT } : null;
}
