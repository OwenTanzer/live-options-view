import { BROWSER_DATA_PREFIX, LEGACY_R2_PROXY_PREFIX, handleBrowserData, handleLegacyR2Proxy, handleLiveQuotes } from './browser-data.js';
import { handleSettle, settleAllBots, settlementPriceServer, validateSettleRequest } from './settlement.js';
import { handleBotIndexReconcile, handleBotMetadata, handleBots } from './bots.js';
import { handleLogin, handleLogout, handleMe, handleRegister } from './auth.js';
import { handlePaperTrade } from './paper-trade.js';
import { cutoverFenceActive } from './fence.js';

// Re-exported from backend/common.js so the Worker's public API (the 31
// exports pinned by tests/worker_logic.test.js) stays on backend/index.js.
export { BOT_USERNAME_RE, STARTING_BALANCE, STRATEGY_ID_RE, computeBookFromTrades, parseCookies } from './common.js';
export { settleAllBots, settlementPriceServer, validateSettleRequest } from './settlement.js';
export { EXECUTION_RESERVATION_LEASE_MS, exactShareQuote, executionIntentMatches, executionPriceForOrder, executionReservationExpired, existingExecutionResponse, finalizeExecutionRequest, renewExecutionReservation, reserveExecutionRequest, validateTradeIntent } from './execution.js';
export { BOT_INDEX_KEY, handleBotIndexReconcile, handleBotMetadata, handleBots, netPositions, repairLegacyBotMarker, updateBotIndex } from './bots.js';
export { MAX_PASSWORD_LEN, MIN_PASSWORD_LEN, USERNAME_RE, constantTimeEqual, derivePasswordHash, randomSaltBase64 } from './auth.js';

const MUTATING_API_PATHS = new Set([
  '/api/register', '/api/login', '/api/logout', '/api/paper-trade', '/api/settle',
  '/api/bot-metadata', '/api/bot-index/reconcile',
]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/browser-data' || url.pathname.startsWith(BROWSER_DATA_PREFIX)) {
      return handleBrowserData(request, url);
    }

    // Bounded legacy route (G4b): /r2-proxy/ now enforces the same
    // public-key/read policy as /browser-data/ — allowlisted keys only, GET
    // only, same origin/query discipline, same upstream discipline, and no
    // Access-Control-Allow-Origin passthrough. The only consumer is
    // site/diag.html; the main app uses /browser-data/. Preview still denies
    // it (frontend/worker.mjs returns 403 there).
    if (url.pathname === '/r2-proxy' || url.pathname.startsWith(LEGACY_R2_PROXY_PREFIX)) {
      return handleLegacyR2Proxy(request, url);
    }

    // Post-cutover fence check. The migration route is retired from this
    // Worker, but the fence remains as a safety interlock: a stale armed fence
    // pauses writes instead of being ignored. Reads keep working throughout.
    if (request.method === 'POST' && MUTATING_API_PATHS.has(url.pathname) &&
        await cutoverFenceActive(env)) {
      return new Response(JSON.stringify({
        error: 'Cutover in progress; writes are paused. Retry in a moment.',
      }), {
        status: 503,
        headers: { 'Content-Type': 'application/json', 'Retry-After': '30', 'Cache-Control': 'no-store' },
      });
    }

    if (request.method === 'POST' && url.pathname === '/api/register') {
      return handleRegister(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/login') {
      return handleLogin(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/logout') {
      return handleLogout(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/bots') {
      return handleBots(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/bot-metadata') {
      return handleBotMetadata(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/bot-index/reconcile') {
      return handleBotIndexReconcile(request, env);    }

    if (request.method === 'GET' && url.pathname === '/api/me') {
      return handleMe(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/paper-trade') {
      return handlePaperTrade(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/settle') {
      return handleSettle(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/live-quotes') {
      return handleLiveQuotes(url, env);
    }

    const response = await env.ASSETS.fetch(request);

    // HTML documents (the single-file app + diagnostic page) must never be
    // cached — a stale cached copy silently keeps serving old JS against a
    // live backend indefinitely, with no user-visible signal that anything
    // is wrong. Everything else (none currently — this is a single-file
    // static site) can use normal caching.
    if (url.pathname === "/" || url.pathname.endsWith(".html")) {
      const noCache = new Response(response.body, response);
      noCache.headers.set("Cache-Control", "no-store");
      return noCache;
    }

    return response;
  },

  // Cloudflare Cron Trigger (see wrangler.toml [triggers]). Settlement used to
  // be entirely client-triggered via /api/settle, called opportunistically
  // from the dashboard's own render loop -- fine for a human's own account,
  // but a bot nobody is watching never opens a dashboard, so its expired
  // positions just piled up unsettled (issue #42). This sweeps every bot
  // account server-side on a schedule instead of relying on a viewer.
  async scheduled(event, env, ctx) {
    // A settlement sweep during the cutover window would be a second writer
    // racing the copy; skip it, loudly, and let the next cron pick it up.
    if (await cutoverFenceActive(env)) {
      console.log(JSON.stringify({ event: 'settlement_skipped_cutover_fence' }));
      return;
    }
    ctx.waitUntil(settleAllBots(env));
  },
}
