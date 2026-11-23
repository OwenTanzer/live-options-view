// Local, in-memory Worker for the #97 integrated lifecycle test.
// Runs the real backend/index.js fetch handler over Node HTTP with memory
// KV/R2 and a deterministic local SPY quote origin. Both listeners bind
// OS-assigned loopback ports and announce a nonce identity to the parent.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import worker, { updateBotIndex } from '../../backend/index.js';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

const HOST = '127.0.0.1';
const BOT_KEY = 'sma-integration-key';
const nonce = randomUUID();

const objects = new Map();
let revision = 0;

// Issue #208: D1 is the account/session store. Any residual KV access is a hard
// error so this harness cannot quietly keep the old dependency alive.
const sqlite = new DatabaseSync(':memory:');
sqlite.exec(fs.readFileSync(
  path.join(REPO_ROOT, 'backend', 'migrations', '0001_accounts_sessions.sql'), 'utf8',
));
function prepared(sql) {
  let params = [];
  const statement = {
    bind(...values) { params = values; return statement; },
    async run() {
      const result = sqlite.prepare(sql).run(...params);
      return { success: true, meta: { changes: result.changes, last_row_id: result.lastInsertRowid } };
    },
    async first() { return sqlite.prepare(sql).get(...params) ?? null; },
    async all() { return { results: sqlite.prepare(sql).all(...params), success: true, meta: {} }; },
  };
  return statement;
}
const DB = { prepare: prepared, async exec(sql) { sqlite.exec(sql); },
             async batch(statements) { return statements.map(statement => statement.run()); } };
function forbiddenNamespace(name) {
  const fail = () => { throw new Error(`KV namespace ${name} touched at runtime; #208 requires zero KV operations`); };
  return { get: fail, put: fail, delete: fail, list: fail };
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
  DB, USERS: forbiddenNamespace('USERS'), SESSIONS: forbiddenNamespace('SESSIONS'), PAPER_TRADES: r2,
  BOT_REGISTRATION_KEY: BOT_KEY,
  LIVE_QUOTE_ORIGIN: null,
  LIVE_QUOTE_KEY: 'local-quote-key',
  BOT_INDEX_READS: 'r2',
  ASSETS: { async fetch() { return new Response('not found', { status: 404 }); } },
};

// Publish the R2 membership index before serving, exactly as the lifecycle
// harness does: metadata sync (strategy_state) writes membership, and an
// unready index would make that write fail.
await updateBotIndex(env, [], { ready: true });

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
