import { API_PATHS, previewError, publicReadAllowed } from './preview-policy.mjs';

function error(message, status) {
  return new Response(JSON.stringify({ error: message }), {
    status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}
function apiNamespace(path) {
  return path === '/api' || path.startsWith('/api/');
}
async function delegate(binding, request) {
  if (!binding || typeof binding.fetch !== 'function') return error('Service binding unavailable', 503);
  // Never fall back to fetch(request): that could contact a production URL.
  return binding.fetch(request);
}
export default {
  async fetch(request, env) {
    if (!['production', 'preview'].includes(env.ENVIRONMENT)) return error('Environment not configured', 503);
    const preview = env.ENVIRONMENT === 'preview';
    if (!preview && (env.ISOLATION !== undefined || env.PREVIEW_ORIGIN !== undefined ||
        env.API_ENVIRONMENT === 'preview' || env.PUBLIC_DATA_ENVIRONMENT === 'read-only')) {
      return error('Production label conflicts with preview isolation signals', 403);
    }
    if (preview) {
      const problem = previewError(request, env);
      if (problem) return error(problem, 403);
    }
    const url = new URL(request.url);
    let response;
    if (url.pathname === '/__environment') {
      if (request.method !== 'GET') return error('Method not allowed', 405);
      response = Response.json({ environment: env.ENVIRONMENT,
        isolation: preview ? 'disposable' : 'production',
        asset_revision: env.ASSET_REVISION || 'local-unbuilt',
        api_contract: 'same-origin-d1-v1', public_data: preview ? 'read-only' : 'api-binding' });
    } else if (apiNamespace(url.pathname)) {
      if (preview && !API_PATHS.has(url.pathname)) response = error('Unknown preview API route', 404);
      else response = await delegate(env.API, request);
    } else if (url.pathname === '/browser-data' || url.pathname.startsWith('/browser-data/')) {
      if (preview) {
        if (request.method !== 'GET') response = error('Read only', 405);
        else if (!publicReadAllowed(url)) {
          const keyOnly = new URL(url); keyOnly.search = '';
          const status = publicReadAllowed(keyOnly) ? 400 : 404;
          response = error(status === 400 ? 'Invalid query' : 'Public artifact not approved', status);
        }
        else {
          // Strip all credentials before even the read-only service sees this request.
          const read = new Request(request.url, { method: 'GET', credentials: 'omit', redirect: 'manual' });
          const upstream = await delegate(env.PUBLIC_DATA, read);
          const headers = new Headers();
          for (const name of ['Content-Type', 'Cache-Control', 'ETag', 'Retry-After']) {
            if (upstream.headers.has(name)) headers.set(name, upstream.headers.get(name));
          }
          response = new Response(upstream.body, { status: upstream.status, headers });
        }
      } else response = await delegate(env.API, request);
    } else if (url.pathname === '/r2-proxy' || url.pathname.startsWith('/r2-proxy/')) {
      response = preview ? error('Generic proxy disabled in preview', 403) : await delegate(env.API, request);
    } else {
      if (!['GET', 'HEAD'].includes(request.method)) response = error('Static assets are read only', 405);
      else {
        const headers = new Headers(request.headers);
        if (preview) headers.set('Accept-Encoding', 'identity');
        response = await delegate(env.ASSETS, preview ? new Request(request, {headers}) : request);
      }
      if (preview && request.method === 'GET' && response.status === 200 && response.headers.get('Content-Type')?.includes('text/html')) {
        if (response.headers.has('Content-Encoding') && response.headers.get('Content-Encoding') !== 'identity') {
          return error('Preview HTML requires identity encoding', 502);
        }
        let html = await response.text();
        // Existing source deliberately uses direct R2 off the production host.
        // In preview route it to our fixture binding before any script executes.
        html = html.replace(/const BROWSER_DATA = location\.origin === '[^']+'\s*\? '\/browser-data' : R2;/,
          "const BROWSER_DATA = '/browser-data';");
        const headers = new Headers(response.headers);
        for (const name of ['Content-Length', 'ETag', 'Last-Modified', 'Content-MD5', 'Digest', 'Content-Encoding']) headers.delete(name);
        response = new Response(html.replace(/<body([^>]*)>/i,
          '$&<aside id="preview-environment" style="background:#743800;color:white;padding:8px;text-align:center">LOCAL / DISPOSABLE PREVIEW — fixture market data; not production</aside>'),
          {status: response.status, headers});
      }
    }
    const result = new Response(response.body, response);
    result.headers.set('X-Options-Environment', env.ENVIRONMENT);
    result.headers.set('X-Asset-Revision', env.ASSET_REVISION || 'local-unbuilt');
    if (url.pathname === '/' || url.pathname.endsWith('.html') || apiNamespace(url.pathname) ||
        url.pathname === '/__environment' || preview) result.headers.set('Cache-Control', 'no-store');
    if (preview) {
      result.headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'");
      result.headers.set('Referrer-Policy', 'no-referrer');
      result.headers.set('X-Content-Type-Options', 'nosniff');
    }
    return result;
  },
};
