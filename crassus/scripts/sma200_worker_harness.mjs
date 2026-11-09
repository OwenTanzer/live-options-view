// Local, in-memory Worker for the #97 integrated lifecycle test.
// Runs the real backend/index.js fetch handler over Node HTTP with memory
// KV/R2 and a deterministic local SPY quote origin. Both listeners bind
// OS-assigned loopback ports and announce a nonce identity to the parent.
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import worker, { updateBotIndex } from '../../backend/index.js';

const HOST = '127.0.0.1';
const BOT_KEY = 'sma-integration-key';
const nonce = randomUUID();

const users = new Map();
const sessions = new Map();
const objects = new Map();
let revision = 0;

function kv(store) {
  return {
    async get(key) { return store.get(key) ?? null; },
    async put(key, value) { store.set(key, String(value)); },
    async delete(key) { store.delete(key); },
    async list({ prefix = '', limit = 1000 } = {}) {
      return { keys: [...store.keys()].filter(k => k.startsWith(prefix)).slice(0, limit).map(name => ({ name })), list_complete: true };
    },
  };
}

const r2 = {
  async get(key) {
    const item = objects.get(key);
    return item ? { etag: item.etag, json: async () => JSON.parse(item.value) } : null;
  },
  async put(key, value, options = {}) {
    const current = objects.get(key);
    if (options.onlyIf?.etagDoesNotMatch === '*' && current) return null;
    if (options.onlyIf?.etagMatches && current?.etag !== options.onlyIf.etagMatches) return null;
    const etag = `e${++revision}`;
    objects.set(key, { value: String(value), etag });
    return { etag };
  },
  async delete(key) { objects.delete(key); },
  async list({ prefix = '', limit = 1000 } = {}) {
    return { objects: [...objects.keys()].filter(k => k.startsWith(prefix)).slice(0, limit).map(key => ({ key })) };
  },
};

const env = {
  USERS: kv(users), SESSIONS: kv(sessions), PAPER_TRADES: r2,
  BOT_REGISTRATION_KEY: BOT_KEY,
  LIVE_QUOTE_ORIGIN: null,
  LIVE_QUOTE_KEY: 'local-quote-key',
  BOT_INDEX_READS: 'r2',
  ASSETS: { async fetch() { return new Response('not found', { status: 404 }); } },
};

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

const quoteServer = http.createServer((req, res) => {
  const now = new Date().toISOString();
  const body = JSON.stringify({
    server_ts: now,
    quotes: [{
      symbol: 'SPY', instrument_class: 'equity', bid: 499.9, ask: 500.1,
      quote_ts: now, bid_ts: now, ask_ts: now,
    }],
  });
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(body);
});

const workerServer = http.createServer(async (req, res) => {
  try {
    if (req.url === '/__control/identity') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        pid: process.pid, nonce, host: HOST,
        worker_port: workerPort, quote_port: quotePort,
      }));
      return;
    }
    const body = await readBody(req);
    const request = new Request(`http://${HOST}:${workerPort}${req.url}`, {
      method: req.method,
      headers: req.headers,
      body: body.length ? body : undefined,
    });
    const response = await worker.fetch(request, env);
    res.writeHead(response.status, Object.fromEntries(response.headers.entries()));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch (error) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: String(error) }));
  }
});

let quotePort;
let workerPort;
await new Promise(resolve => quoteServer.listen(0, HOST, resolve));
quotePort = quoteServer.address().port;
env.LIVE_QUOTE_ORIGIN = `http://${HOST}:${quotePort}`;
await updateBotIndex(env, [], { ready: true });
await new Promise(resolve => workerServer.listen(0, HOST, resolve));
workerPort = workerServer.address().port;
if (!quotePort || !workerPort || quotePort === workerPort) {
  throw new Error(`invalid OS-assigned loopback ports: ${quotePort}/${workerPort}`);
}
console.log(JSON.stringify({
  ready: true, pid: process.pid, nonce, host: HOST,
  worker_port: workerPort, quote_port: quotePort,
}));

const shutdown = () => { workerServer.close(); quoteServer.close(); };
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
