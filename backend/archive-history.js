import { authorizeJournalViewer } from './journal-passkeys.js';
import { ownerIdentity, privateJSON, SITE } from './kalshi-access.js';

export async function archiveHistory(request, env) {
  const url = new URL(request.url);
  if (url.origin !== SITE || env.ENVIRONMENT === 'preview' || env.ISOLATION !== undefined)
    return privateJSON({error: 'Unavailable'}, 404);
  if (request.method !== 'GET') return privateJSON({error: 'Read only'}, 405);
  const [digest, symbol, interval, provider] = ['snapshot','symbol','interval','provider'].map(k => url.searchParams.get(k));
  if (!/^[a-f0-9]{64}$/.test(digest ?? '') || !/^[A-Z0-9_]{1,32}$/.test(symbol ?? '') ||
      !['1m','1h','1d'].includes(interval) || !['coinbase','tiingo','coingecko'].includes(provider))
    return privateJSON({error:'Unknown historical series'}, 400);
  const journal = await authorizeJournalViewer(request, env);
  if (journal.session?.member !== 'Owen' && !await ownerIdentity(request)) return privateJSON({error:'Owner sign-in required'}, 401);
  const object = await env.ARCHIVE_HISTORY.get(`crypto/${digest}/${symbol}/${interval}/${provider}.json.gz`);
  if (!object) return privateJSON({error:'Archive series unavailable'}, 404);
  return new Response(object.body, {encodeBody:'manual', headers:{
    'Content-Type':'application/json', 'Content-Encoding':'gzip', 'Cache-Control':'private, no-store',
    'Vary':'Cookie', 'X-Content-Type-Options':'nosniff', 'Cross-Origin-Resource-Policy':'same-origin'
  }});
}
