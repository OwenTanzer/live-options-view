// Local real-Worker HTTP harness for the isolated #41/#86/#102 lifecycle.
// Real backend/index.js over Node HTTP with in-memory KV/R2 and a deterministic
// QQQ option quote origin. Binds both listeners to OS-assigned loopback ports
// and announces a nonce identity so the parent cannot attach to a stranger.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import worker, { updateBotIndex } from '../../backend/index.js';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

// Fixed regular-session time for the fixture contracts. Production clock
// behavior stays intact; only this isolated test process replaces Date.
const NativeDate = Date;
const fixtureNow = NativeDate.parse('2026-11-09T15:00:00Z');
globalThis.Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [fixtureNow])); }
  static now() { return fixtureNow; }
};

const BOT_KEY = 'lifecycle-integration-key';
const HOST = '127.0.0.1';
const nonce = randomUUID();

const objects = new Map();
// Issue #208: the lifecycle runs against D1, and any remaining KV access is a
// hard failure rather than a silent fallback. kv_touches must stay zero.
const counts = { lists: 0, gets: 0, puts: 0, kv_touches: 0 };
let revision = 0;

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

const DB = {
  prepare: prepared,
  async exec(sql) { sqlite.exec(sql); },
  async batch(statements) { return statements.map(statement => statement.run()); },
};

function forbiddenNamespace(name) {
  const fail = () => {
    counts.kv_touches++;
    throw new Error(`KV namespace ${name} touched at runtime; #208 requires zero KV operations`);
  };
  return { get: fail, put: fail, delete: fail, list: fail };
}
const users = forbiddenNamespace('USERS');
const sessions = forbiddenNamespace('SESSIONS');

const r2 = {
  async get(key) {
    counts.gets++;
    const item = objects.get(key);
    return item ? { etag: item.etag, json: async () => JSON.parse(item.value) } : null;
  },
  async put(key, value, options = {}) {
    counts.puts++;
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
  DB, USERS: users, SESSIONS: sessions, PAPER_TRADES: r2,
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
  const url = new URL(req.url, `http://${HOST}`);
  const symbols = (url.searchParams.get('symbols') || '').split(',').filter(Boolean);
  const now = new Date().toISOString();
  const quotes = symbols.map(symbol => {
    const strike = Number(symbol.slice(10, 15));
    return {
      symbol, instrument_class: 'option', bid: 2.4, ask: 2.5,
      strike, type: 'call', exp: '2026-11-09',
      quote_ts: now, bid_ts: now, ask_ts: now,
    };
  });
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ server_ts: now, quotes }));
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
    if (req.url === '/__control/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(counts));
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
