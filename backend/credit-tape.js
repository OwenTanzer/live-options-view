function error(message, status) {
  return new Response(message, {status, headers: {'Cache-Control': 'private, no-store'}});
}
const CREDIT_ORIGIN = 'https://prolific-essence-production.up.railway.app';
const CREDIT_PATHS = new Set(['/credit-tape/', '/credit-tape/table.css', '/credit-tape/table.js',
  '/credit-tape/auth/login', '/credit-tape/auth/callback', '/credit-tape/auth/logout']);
const RETURN_PATHS = new Map([['btc', '/?chain=BTC'], ['kalshi', '/kalshi.html']]);
const returnCookie = (age, destination = 'btc') => `__Host-btc-return=${age ? destination : ''}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${age}`;

export async function creditTape(request) {
  const url = new URL(request.url);
  if (url.origin !== 'https://options.moopertonic.net' || !CREDIT_PATHS.has(url.pathname)) {
    return error('Not found', 404);
  }
  const logout = url.pathname.endsWith('/logout');
  if (logout ? request.method !== 'POST' : !['GET','HEAD'].includes(request.method)) {
    return error('Method not allowed', 405);
  }
  if (logout && request.headers.get('Origin') !== url.origin) return error('Forbidden', 403);
  const headers = new Headers();
  if (logout) headers.set('Origin', url.origin);
  const cookies = (request.headers.get('Cookie') || '').split(';').map(value => value.trim())
    .filter(value => /^__Host-cds-(session|flow)=/.test(value));
  if (cookies.length) headers.set('Cookie', cookies.join('; '));
  let query = url.pathname.endsWith('/callback') ? url.search : '';
  if (url.pathname === '/credit-tape/' &&
      /^\?view=(?:treu-sample|treu(?:&file=UK_TREU_\d{8}_\d{4}\.csv&sha=[a-f0-9]{64})?)$/.test(url.search)) {
    query = url.search;
  }
  const upstream = await fetch(CREDIT_ORIGIN + url.pathname + query, {
    method: request.method, headers, redirect: 'manual', cache: 'no-store'
  });
  const outgoing = new Headers({'Cache-Control': 'private, no-store', 'Vary': 'Cookie'});
  for (const name of ['Content-Type', 'Content-Security-Policy', 'X-Content-Type-Options',
    'Referrer-Policy', 'X-Frame-Options', 'Location', 'Content-Disposition', 'X-Original-SHA256']) {
    if (upstream.headers.has(name)) outgoing.set(name, upstream.headers.get(name));
  }
  for (const cookie of upstream.headers.getSetCookie()) {
    if (/^__Host-cds-(session|flow)=/.test(cookie)) outgoing.append('Set-Cookie', cookie);
  }
  // Fixed navigation hint only, never OAuth state or an identity claim. The
  // trusted upstream still owns approval/state/callback/session validation.
  if (url.pathname.endsWith('/login')) {
    const destination = url.search === '?return=btc' ? 'btc' : url.search === '?return=kalshi' ? 'kalshi' : null;
    outgoing.append('Set-Cookie', returnCookie(destination ? 600 : 0, destination));
  }
  if (logout) outgoing.append('Set-Cookie', returnCookie(0));
  if (url.pathname.endsWith('/callback')) {
    const hints = (request.headers.get('Cookie') || '').split(';').map(c => c.trim())
      .filter(c => c.startsWith('__Host-btc-return='));
    const destination = hints.length === 1 ? RETURN_PATHS.get(hints[0].slice('__Host-btc-return='.length)) : null;
    const location = upstream.headers.get('Location');
    const target = location ? new URL(location, url.origin) : null;
    if (destination && [302,303].includes(upstream.status) && target?.origin === url.origin &&
        target.pathname === '/credit-tape/' && !target.search && !target.hash) outgoing.set('Location', destination);
    outgoing.append('Set-Cookie', returnCookie(0));
  }
  return new Response(upstream.body, {status: upstream.status, headers: outgoing});
}

