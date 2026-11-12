// Local emulation only, not a Cloudflare/IAM emulator. No credentials read.
import http from 'node:http';
import { readFileSync, existsSync, statSync, realpathSync } from 'node:fs';
import { resolve, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import gateway from '../frontend/worker.mjs';
import api from '../backend/index.js';
import { withD1 } from '../tests/helpers/test_env.mjs';
import { publicReadAllowed } from '../frontend/preview-policy.mjs';
import { assertOperationalKey, CaptureWriteRefused } from '../backend/operational-storage.js';
export const LOCAL_ORIGIN = 'http://localhost:8787';
const ROOT = fileURLToPath(new URL('../', import.meta.url));
// G4a product module integrated (Nova, 2026-11-11): the preview store uses
// the production boundary directly and fails on import errors; no private
// copy of the prefix rule remains in this file.
export const OPERATIONAL_POLICY_SOURCE = 'g4a-product';
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };
export function assetBinding(root = resolve(ROOT, 'site')) {
  const base = resolve(root);
  return { async fetch(request) {
    const url = new URL(request.url);
    let pathname;
    try { pathname = decodeURIComponent(url.pathname); } catch { return new Response('Bad path', { status: 400 }); }
    const target = resolve(base, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!target.startsWith(base + sep) || !existsSync(target) || !statSync(target).isFile() ||
        !realpathSync(target).startsWith(realpathSync(base) + sep) || extname(target) === '.md') {
      return new Response('Not found', { status: 404 });
    }
    return new Response(request.method === 'HEAD' ? null : readFileSync(target), {
      headers: { 'Content-Type': MIME[extname(target)] || 'application/octet-stream' },
    });
  } };
}
export function fixtureDocuments() {
  const date = '2026-10-27', key = date.replaceAll('-', '');
  return {
    'manifest.json': JSON.stringify({ dates: [date] }),
    [`raw/qqq_chain_${key}.csv`]: 'Expiration,Type,Strike,UnderlyingPrice,OpenInterest,Volume,DTE\n' +
      ['call', 'put'].flatMap(type => [499, 500, 501].map(strike => `${date},${type},${strike},500,1000,20,0`)).join('\n'),
    [`prices/qqq_intraday_${key}.csv`]: `datetime,open,high,low,close\n${date}T13:30:00Z,500,502,499,501\n${date}T13:35:00Z,501,503,500,502`,
    'intraday/health.json': JSON.stringify({ fixture: true, status: 'closed' }),
    'intraday/prices.json': JSON.stringify({ fixture: true, prices: {} }),
  };
}
export function fixtureNetwork(input) {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  if (url.origin !== 'https://preview-quotes.invalid' || url.pathname !== '/live-quotes') {
    throw new Error(`Preview network denied: ${url.origin}${url.pathname}`);
  }
  const now = new Date().toISOString();
  return Promise.resolve(Response.json({ fixture: true, session: 'regular', quotes:
    (url.searchParams.get('symbols') || '').split(',').filter(Boolean).map(symbol => ({
      symbol, instrument_class: 'equity', bid: 499, ask: 500,
      bid_ts: now, ask_ts: now, quote_ts: now, market_open: true,
    })) }));
}
export function createPreview({ assetRoot = resolve(ROOT, 'site'), revision = 'local-unbuilt' } = {}) {
  const objects = new Map();
  let serial = 0;
  const bucket = {
    async get(key) {
      const item = objects.get(key);
      return item ? { etag: item.etag, json: async () => JSON.parse(item.value), text: async () => item.value } : null;
    },
    async put(key, value, options = {}) {
      try { assertOperationalKey(key); }
      catch (error) { throw new CaptureWriteRefused('Capture write denied: ' + error.message, key); }
      const old = objects.get(key), condition = options.onlyIf;
      if ((condition?.etagDoesNotMatch === '*' && old) || (condition?.etagMatches && condition.etagMatches !== old?.etag)) return null;
      const item = { value: String(value), etag: String(++serial) };
      objects.set(key, item);
      return { etag: item.etag };
    },
  };
  const apiEnv = withD1({ PAPER_TRADES: bucket, BOT_INDEX_READS: 'r2',
    LIVE_QUOTE_ORIGIN: 'https://preview-quotes.invalid', LIVE_QUOTE_KEY: 'fixture-not-a-secret' });
  apiEnv.ASSETS = { fetch: async () => new Response('Unknown API route', { status: 404 }) };
  const docs = fixtureDocuments();
  const calls = { api: [], reads: [] };
  const env = { ENVIRONMENT: 'preview', ISOLATION: 'disposable', PREVIEW_ORIGIN: LOCAL_ORIGIN,
    API_ENVIRONMENT: 'preview', PUBLIC_DATA_ENVIRONMENT: 'read-only', ASSET_REVISION: revision,
    ASSETS: assetBinding(assetRoot),
    API: { async fetch(request) {
      calls.api.push(new URL(request.url).pathname);
      return api.fetch(request, apiEnv);
    } },
    PUBLIC_DATA: { async fetch(request) {
      const url = new URL(request.url);
      calls.reads.push({ url: request.url, headers: Object.fromEntries(request.headers) });
      if (request.method !== 'GET' || !publicReadAllowed(url)) return new Response('Not found', { status: 404 });
      const key = url.pathname.slice('/browser-data/'.length);
      return new Response(docs[key] || '', { status: key in docs ? 200 : 404,
        headers: { 'Content-Type': key.endsWith('.csv') ? 'text/csv' : 'application/json' } });
    } },
  };
  return { env, apiEnv, objects, calls, docs, policySource: OPERATIONAL_POLICY_SOURCE,
    fetch: request => gateway.fetch(request, env),
    close: () => apiEnv.DB.db.close() };
}
export async function startPreview({ port = 8787, assetRoot, revision } = {}) {
  if (port !== 8787) throw new Error('Use localhost:8787 to preserve the existing backend Origin contract');
  const preview = createPreview({ assetRoot, revision });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fixtureNetwork; // All backend outbound network is fixture-only.
  const server = http.createServer(async (req, res) => {
    try {
      if (req.headers.host !== 'localhost:8787') { res.writeHead(403); res.end('Wrong host'); return; }
      const chunks = []; let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 16384) { res.writeHead(413); res.end('Body too large'); return; }
        chunks.push(chunk);
      }
      const request = new Request(new URL(req.url, LOCAL_ORIGIN), { method: req.method,
        headers: req.headers, ...(chunks.length ? { body: Buffer.concat(chunks) } : {}) });
      const response = await preview.fetch(request);
      const headers = Object.fromEntries(response.headers);
      const cookies = response.headers.getSetCookie();
      if (cookies.length) headers['set-cookie'] = cookies;
      res.writeHead(response.status, headers);
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch (error) { res.writeHead(500, { 'Content-Type': 'text/plain' }); res.end(error.message); }
  });
  await new Promise((ok, fail) => { server.once('error', fail); server.listen(port, '127.0.0.1', ok); });
  return { ...preview, server, async stop() {
    await new Promise(ok => server.close(ok)); preview.close(); globalThis.fetch = originalFetch;
  } };
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const instance = await startPreview();
  console.log(`DISPOSABLE FIXTURE PREVIEW ${LOCAL_ORIGIN} — memory D1 + operational objects; network disabled; no cron`);
  for (const sig of ['SIGINT', 'SIGTERM']) process.once(sig, async () => { await instance.stop(); process.exit(0); });
}
