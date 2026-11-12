// No backend imports or configurable network destinations in the asset gateway.
export const API_PATHS = new Set([
  '/api/register', '/api/login', '/api/logout', '/api/me', '/api/bots',
  '/api/bot-metadata', '/api/bot-index/reconcile', '/api/paper-trade',
  '/api/settle', '/api/live-quotes',
]);
const PUBLIC_KEYS = new Set([
  'intraday/latest.json', 'intraday/spy/latest.json', 'intraday/ibit/latest.json',
  'intraday/prices.json', 'intraday/health.json', 'derived/OIranges.csv',
  'macro/eia_steo.json', 'manifest.json',
  'squeeze-scanner/v1/scheduled/latest.json',
  'squeeze-scanner/v1/scheduled/latest-attempt.json',
  'squeeze-scanner/v1/scheduled/latest-schedule.json',
]);
export function publicReadAllowed(url) {
  if (!url.pathname.startsWith('/browser-data/')) return false;
  if (url.search && !/^\?_=[0-9]{1,20}$/.test(url.search)) return false;
  const key = url.pathname.slice('/browser-data/'.length);
  // Traversal guard, mirroring backend/browser-data.js: literal and
  // percent-encoded dot traversal, backslash and NUL are refused for every
  // key shape before the allowlist. tests/r2_proxy_bounded.test.js pins
  // backend/gateway agreement. (The prospective news/v1/views/** grant was
  // removed 2026-11-11: no accepted MediaFlow source exists.)
  if (key.includes('..') || /%2e/i.test(key) || key.includes('\\') || key.includes('\0')) return false;
  if (PUBLIC_KEYS.has(key)) return true;
  const m = /^(?:raw\/(?:opex\/)?qqq_chain_|prices\/qqq_intraday_)(\d{4})(\d{2})(\d{2})\.csv$/.exec(key);
  if (!m) return false;
  const date = `${m[1]}-${m[2]}-${m[3]}`;
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isFinite(+parsed) && parsed.toISOString().slice(0, 10) === date;
}
export function previewError(request, env) {
  if (env.ISOLATION !== 'disposable') return 'Preview requires disposable isolation';
  for (const name of ['DB', 'PAPER_TRADES', 'USERS', 'SESSIONS', 'LIVE_QUOTE_KEY', 'BOT_REGISTRATION_KEY']) {
    if (env[name] !== undefined) return 'Raw write store or secret forbidden on preview gateway';
  }
  let origin;
  try { origin = new URL(env.PREVIEW_ORIGIN); } catch { return 'Missing preview origin'; }
  if (origin.origin !== env.PREVIEW_ORIGIN ||
      !(origin.protocol === 'https:' || (origin.protocol === 'http:' && origin.hostname === 'localhost'))) {
    return 'Invalid preview origin';
  }
  // Do not accept a production public origin as a preview identity.
  if (['live-options-view.otmooper12.workers.dev', 'options.moopertonic.net'].includes(origin.hostname)) {
    return 'Production origin is not preview';
  }
  if (new URL(request.url).origin !== origin.origin) return 'Wrong preview request origin';
  const supplied = request.headers.get('Origin');
  if (supplied !== null && supplied !== origin.origin) return 'Cross-origin preview request';
  if (env.API_ENVIRONMENT !== 'preview' || env.PUBLIC_DATA_ENVIRONMENT !== 'read-only') {
    return 'Preview binding identity missing';
  }
  return null;
}
