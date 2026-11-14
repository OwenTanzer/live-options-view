// No backend imports or configurable network destinations in the asset gateway.
export const API_PATHS = new Set([
  '/api/register', '/api/login', '/api/logout', '/api/me', '/api/bots',
  '/api/bot-metadata', '/api/bot-index/reconcile', '/api/paper-trade',
  '/api/settle', '/api/live-quotes',
]);
// OI skew meter projection key: prepared, DISABLED (mirrors
// backend/browser-data.js; no backend import in the gateway).
export const OI_SKEW_PROJECTION_KEY = 'crassus/oi-skew/v1/latest.json';
export const OI_SKEW_PROJECTION_PUBLIC = true;
const PUBLIC_KEYS = new Set([
  'intraday/latest.json', 'intraday/spy/latest.json', 'intraday/ibit/latest.json',
  'intraday/prices.json', 'intraday/health.json', 'derived/OIranges.csv',
  'macro/eia_steo.json', 'manifest.json',
  'squeeze-scanner/v1/scheduled/latest.json',
  'squeeze-scanner/v1/scheduled/latest-attempt.json',
  'squeeze-scanner/v1/scheduled/latest-schedule.json',
  ...(OI_SKEW_PROJECTION_PUBLIC ? [OI_SKEW_PROJECTION_KEY] : []),
]);
export function publicReadAllowed(url) {
  if (!url.pathname.startsWith('/browser-data/')) return false;
  if (url.search && !/^\?_=[0-9]{1,20}$/.test(url.search)) return false;
  const key = url.pathname.slice('/browser-data/'.length);
  // Traversal guard, mirroring backend/browser-data.js: literal and
  // percent-encoded dot traversal, backslash and NUL are refused for every
  // key shape before the allowlist. tests/r2_proxy_bounded.test.js pins
  // backend/gateway agreement. (The prospective news/v1/views/** prefix grant
  // removed 2026-11-11 stays removed; only the exact News view keys below.)
  if (key.includes('..') || /%2e/i.test(key) || key.includes('\\') || key.includes('\0')) return false;
  if (PUBLIC_KEYS.has(key)) return true;
  // MediaFlow News views (G3; G6 fresh namespaces): exactly the index
  // news/v2/views/index.json, or one immutable incarnation
  // p/<YYYYMMDDTHHMMSSZ-16hex>.<n> (a real UTC instant; n = 1..9999, no leading
  // zero) / mode / stream / real calendar date / page 0001-0020. Mirrors
  // isPublicNewsViewKey in backend/browser-data.js and the News panel's own key
  // check. Legacy news/v1/views keys are no longer allowed.
  if (key === 'news/v2/views/index.json') return true;
  // MediaFlow R1: one incarnation's research page (the retired mutable
  // news/v2/research/view.json key stays refused); never the private research
  // ledger (news/v1/research/**) or any other research key.
  const r = /^news\/v2\/views\/p\/(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-[0-9a-f]{16}\.[1-9][0-9]{0,3}\/research\/view\.json$/.exec(key);
  if (r) {
    const day = `${r[1]}-${r[2]}-${r[3]}`;
    const at = new Date(`${day}T00:00:00Z`);
    return Number.isFinite(+at) && at.toISOString().slice(0, 10) === day &&
      Number(r[4]) <= 23 && Number(r[5]) <= 59 && Number(r[6]) <= 59;
  }
  const n = /^news\/v2\/views\/p\/(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-[0-9a-f]{16}\.[1-9][0-9]{0,3}\/(?:collector|archive|publication)\/(?:all|ai_buildout|oil)\/(\d{4})-(\d{2})-(\d{2})\/page-(\d{4})\.json$/.exec(key);
  if (n) {
    const real = (y, mo, d) => {
      const day = `${y}-${mo}-${d}`;
      const at = new Date(`${day}T00:00:00Z`);
      return Number.isFinite(+at) && at.toISOString().slice(0, 10) === day;
    };
    if (!real(n[1], n[2], n[3]) || Number(n[4]) > 23 || Number(n[5]) > 59 || Number(n[6]) > 59) return false;
    const page = Number(n[10]);
    if (page < 1 || page > 20) return false;
    return real(n[7], n[8], n[9]);
  }
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
