// backend/browser-data.js (B4) -- public read-only data: the /browser-data
// allowlist proxy and the /api/live-quotes passthrough, moved verbatim from
// worker.js. The router in worker.js still owns the route table.
// fetchLiveQuotes is exported for internal use by paper-trade/execution; it
// is not one of the Worker's public exports.

import { R2_ORIGIN, jsonResponse } from './common.js';

// Only these public artifacts are exposed to the browser through this route.
// Never use the private bucket binding or a caller-provided destination.
export const BROWSER_DATA_ORIGIN = 'https://live-options-view.otmooper12.workers.dev';
export const BROWSER_DATA_PREFIX = '/browser-data/';
// OI skew meter bot-state projection (Skew + Direction H1, INACTIVE). The key
// is prepared but DISABLED: no projector writes it yet, and a public grant for
// an object that does not exist is not justified on its own (see the news
// grant removal below). Flip to true only in the approved release that also
// enables the projector; frontend/preview-policy.mjs carries the same key and
// flag, and tests/r2_proxy_bounded.test.js pins them together.
export const OI_SKEW_PROJECTION_KEY = 'crassus/oi-skew/v1/latest.json';
export const OI_SKEW_PROJECTION_PUBLIC = true;
export const BROWSER_DATA_KEYS = new Set([
  'intraday/latest.json',
  'intraday/spy/latest.json',
  'intraday/ibit/latest.json',
  'intraday/prices.json',
  'intraday/health.json',
  'derived/OIranges.csv',
  'macro/eia_steo.json',
  'manifest.json',
  'squeeze-scanner/v1/scheduled/latest.json',
  'squeeze-scanner/v1/scheduled/latest-attempt.json',
  'squeeze-scanner/v1/scheduled/latest-schedule.json',
  ...(OI_SKEW_PROJECTION_PUBLIC ? [OI_SKEW_PROJECTION_KEY] : []),
]);
// Only the three QQQ archive filename families consumed by Historical.
// Validate calendar dates, not just eight arbitrary digits. No prefix proxying.
export function isPublicArchiveKey(key) {
  const match = /^(?:raw\/(?:opex\/)?qqq_chain_|prices\/qqq_intraday_)([0-9]{4})([0-9]{2})([0-9]{2})\.csv$/.exec(key);
  if (!match) return false;
  const date = `${match[1]}-${match[2]}-${match[3]}`;
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date;
}
// MediaFlow news view pages read by the News panel (accepted MediaFlow M4b-M6,
// 8639c6d; G3 generations; G6 fresh namespaces): exactly the fixed index
// news/v2/views/index.json, or one immutable incarnation
// p/<YYYYMMDDTHHMMSSZ-16hex>.<n> (a real UTC instant; n = 1..9999, no leading
// zero) / mode / stream / real calendar date / page 0001-0020 (views.py's
// MODES, STREAM_KEYS and pages_per_view). site/js/news-panel.js applies the
// same check before every request, and frontend/preview-policy.mjs mirrors it.
// Exact keys only: the prospective news/v1/views/** prefix grant removed
// 2026-11-11 stays removed, and the legacy news/v1/views index and g/<gen>
// pages are no longer served (404), like today.json and unqualified pages.
export const NEWS_VIEW_INDEX_KEY = 'news/v2/views/index.json';
// Upstream deadline for News keys only (G3; below the panel's 20 s request
// deadline). It bounds the wait for the upstream response; the panel's own
// deadline also covers the body.
export const NEWS_PROXY_DEADLINE_MS = 15000;
// MediaFlow R1: the sanitized research projection is one immutable page of a
// generation, p/<gen>.<n>/research/view.json, named by the index (the
// retired mutable news/v2/research/view.json grant stays removed). The private
// research ledger (news/v1/research/**) and any other research key are never
// served; there is no research prefix grant.
function realDate(y, mo, d) {
  const date = `${y}-${mo}-${d}`;
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date;
}
export function isPublicNewsViewKey(key) {
  if (key === NEWS_VIEW_INDEX_KEY) return true;
  const research = /^news\/v2\/views\/p\/([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z-[0-9a-f]{16}\.[1-9][0-9]{0,3}\/research\/view\.json$/.exec(key);
  if (research) {
    return realDate(research[1], research[2], research[3]) && Number(research[4]) <= 23 &&
      Number(research[5]) <= 59 && Number(research[6]) <= 59;
  }
  const match = /^news\/v2\/views\/p\/([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z-[0-9a-f]{16}\.[1-9][0-9]{0,3}\/(?:collector|archive|publication)\/(?:all|ai_buildout|oil)\/([0-9]{4})-([0-9]{2})-([0-9]{2})\/page-([0-9]{4})\.json$/.exec(key);
  if (!match) return false;
  if (!realDate(match[1], match[2], match[3]) || Number(match[4]) > 23 || Number(match[5]) > 59 ||
      Number(match[6]) > 59) return false;
  const page = Number(match[10]);
  if (page < 1 || page > 20) return false;
  return realDate(match[7], match[8], match[9]);
}
// Traversal guard (was the %2e check inside the removed news prefix rule):
// literal and percent-encoded dot traversal (%2e normalizes to a dot
// segment in URL parsing, so the raw form must be refused here too),
// backslash and NUL are refused for every key shape, before the allowlist.
export function isPublicBrowserDataKey(key) {
  if (key.includes('..') || /%2e/i.test(key) || key.includes('\\') || key.includes('\0')) return false;
  return BROWSER_DATA_KEYS.has(key) || isPublicArchiveKey(key) || isPublicNewsViewKey(key);
}
const MAX_LIVE_QUOTE_SYMBOLS = 100;
export async function handleBrowserData(request, url) {
  const origin = request.headers.get('Origin');
  if (url.origin !== BROWSER_DATA_ORIGIN ||
      (origin !== null && origin !== BROWSER_DATA_ORIGIN)) {
    return new Response('Forbidden', { status: 403 });
  }
  if (request.method !== 'GET') {
    return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET' } });
  }
  const key = url.pathname.slice(BROWSER_DATA_PREFIX.length);
  if (!isPublicBrowserDataKey(key)) return new Response('Not found', { status: 404 });
  // Accept only the existing numeric cache-buster, never arbitrary query data.
  if (url.search && !/^\?_=[0-9]{1,20}$/.test(url.search)) {
    return new Response('Invalid query', { status: 400 });
  }
  // News keys (G3) get a finite upstream deadline and, for the index, no-store.
  // Every other key keeps its existing behaviour.
  const news = isPublicNewsViewKey(key);
  const ctrl = news && typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), NEWS_PROXY_DEADLINE_MS) : null;
  try {
    const upstream = await fetch(`${R2_ORIGIN}/${key}${url.search}`, {
      method: 'GET',
      headers: {},
      credentials: 'omit',
      redirect: 'manual',
      cf: { cacheTtl: 0, cacheEverything: false },
      ...(ctrl ? { signal: ctrl.signal } : {}),
    });
    // Preserve status and stream bytes without parsing. Limit response headers
    // so upstream cookies/auth cannot affect this origin, and Location cannot
    // make the browser follow a redirect that the Worker refused to follow.
    const headers = new Headers();
    for (const name of ['Content-Type', 'Content-Encoding', 'Content-Length',
      'Cache-Control', 'ETag', 'Last-Modified', 'Retry-After']) {
      const value = upstream.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    // The index is the only mutable News object: never cacheable, whatever upstream says.
    if (key === NEWS_VIEW_INDEX_KEY) headers.set('Cache-Control', 'no-store');
    return new Response(upstream.body, {
      status: upstream.status, statusText: upstream.statusText, headers,
    });
  } catch {
    if (ctrl && ctrl.signal.aborted) return new Response('News upstream deadline exceeded', { status: 504 });
    return new Response('Public data upstream unavailable', { status: 502 });
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}
export const LEGACY_R2_PROXY_PREFIX = '/r2-proxy/';
// Bounded legacy route (G4b): the same public-key/read policy as
// /browser-data/, nothing wider. Rewrites the URL onto /browser-data/ and
// delegates to handleBrowserData so the origin/method/key/query/upstream
// discipline cannot drift between the two routes. Bare /r2-proxy and
// /r2-proxy/ map to the empty key and return 404, matching /browser-data.
export async function handleLegacyR2Proxy(request, url) {
  const key = url.pathname === '/r2-proxy' ? '' : url.pathname.slice(LEGACY_R2_PROXY_PREFIX.length);
  const rewritten = new URL(url);
  rewritten.pathname = BROWSER_DATA_PREFIX + key;
  return handleBrowserData(request, rewritten);
}
export async function handleLiveQuotes(url, env) {
  const symbols = parseRequestedSymbols(url.searchParams.get('symbols'));
  if (!symbols) {
    return jsonResponse({ error: `Request 1-${MAX_LIVE_QUOTE_SYMBOLS} valid symbols` }, 400);
  }
  try {
    const upstream = await fetchLiveQuotes(symbols, env);
    const response = new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
        'Cache-Control': 'no-store',
      },
    });
    const retryAfter = upstream.headers.get('Retry-After');
    if (retryAfter) response.headers.set('Retry-After', retryAfter);
    return response;
  } catch (error) {
    return jsonResponse({ error: 'Live quote service unavailable' }, 503);
  }
}
export function fetchLiveQuotes(symbols, env) {
  if (!env.LIVE_QUOTE_ORIGIN || !env.LIVE_QUOTE_KEY) {
    throw new Error('Live quote service is not configured');
  }
  const upstreamUrl = new URL('/live-quotes', env.LIVE_QUOTE_ORIGIN);
  upstreamUrl.searchParams.set('symbols', symbols.join(','));
  // Live quote data must never be served from an intermediate cache: the
  // upstream URL is the cache key, and a stale cached 503 (pre-#222, no
  // session field) kept the option lane backing off after the producer was
  // redeployed, while new symbol lists returned fresh bodies. A per-request
  // nonce forces a fresh origin read every time. The collector ignores
  // unknown query params (it reads only `symbols`).
  upstreamUrl.searchParams.set('_', `${Date.now()}-${Math.random().toString(36).slice(2)}`);
  return fetch(upstreamUrl, {
    headers: { 'X-Live-Quote-Key': env.LIVE_QUOTE_KEY, 'Cache-Control': 'no-cache' },
    cf: { cacheTtl: 0, cacheEverything: false },
  });
}
function parseRequestedSymbols(value) {
  if (!value) return null;
  const symbols = [...new Set(value.split(',').map(symbol => symbol.trim().replace(/\s/g, '')).filter(Boolean))];
  if (!symbols.length || symbols.length > MAX_LIVE_QUOTE_SYMBOLS) return null;
  if (symbols.some(symbol => symbol.length > 64 || !/^[A-Za-z0-9._/:+-]+$/.test(symbol))) return null;
  return symbols;
}
