'use strict';

// This module is the market-artifact I/O boundary. No credential/environment lookup,
// Worker API, directory listing, provider request, or client-selected URL.
const fs = require('node:fs/promises');
const path = require('node:path');
const https = require('node:https');
const zlib = require('node:zlib');
const { createHash } = require('node:crypto');

const ORIGIN = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const LIMITS = Object.freeze({ artifact_bytes: 4 * 1024 * 1024, expanded_artifact_bytes: 24 * 1024 * 1024, query_bytes: 64 * 1024 * 1024,
  output_bytes: 256 * 1024, requests: 12, timeout_ms: 15000, rows: 50, path_sweeps: 6,
  input_bytes: 16384, references: 64, reference_bytes: 16 * 1024 * 1024, reference_ttl_ms: 15 * 60 * 1000,
  jsonl_lines: 100000, jsonl_rows: 10000, jsonl_line_bytes: 256 * 1024, quote_line_bytes: 16384,
  csv_rows: 10000, csv_columns: 128, csv_cell_chars: 4096 });
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const KEY = new RegExp('^(?:intraday/(?:spy/)?latest\\.json|squeeze-scanner/v1/scheduled/(?:latest(?:-attempt|-schedule)?\\.json|runs/' + UUID + '/(?:manifest|results|inputs)\\.json)|oa203/scanner/\\d{4}-\\d{2}-\\d{2}/(?:summary\\.json|universe\\.json|leaderboard\\.csv|sweeps/(?:manifest\\.jsonl|sweep_\\d{4}\\.jsonl\\.gz)))$');

class DataError extends Error {
  constructor(code, message, key = null) { super(message); this.code = code; this.key = key; }
}
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
function permitted(key) {
  if (typeof key !== 'string' || !KEY.test(key)) throw new DataError('invalid_reference', 'Artifact is outside the market allowlist.');
}
function publicRead(key, remaining, timeout, request = https.get) {
  permitted(key);
  return new Promise((resolve, reject) => {
    let settled = false;
    const fail = e => { if (!settled) { settled = true; reject(e); } };
    const req = request(ORIGIN + '/' + key, { headers: { Accept: '*/*', 'Cache-Control': 'no-cache' } }, res => {
      if (res.statusCode !== 200) {
        fail(new DataError(res.statusCode === 404 ? 'missing_artifact' : res.statusCode === 429 ? 'rate_limited' : 'upstream_failure',
          `Public artifact returned HTTP ${res.statusCode}; redirects are never followed.`, key));
        res.destroy(); req.destroy();
        return;
      }
      let size = 0; const chunks = [];
      res.on('data', chunk => {
        size += chunk.length;
        if (size > remaining) { fail(new DataError('excessive_response', 'Artifact or query byte limit exceeded.', key)); res.destroy(); req.destroy(); }
        else chunks.push(chunk);
      });
      res.on('error', () => fail(new DataError('upstream_failure', 'Public artifact stream failed.', key)));
      res.on('end', () => { if (!settled) { settled = true; resolve(Buffer.concat(chunks)); } });
    });
    const timer = setTimeout(() => { fail(new DataError('upstream_timeout', 'Public artifact deadline exceeded.', key)); req.destroy(); }, timeout);
    req.on('error', () => fail(new DataError('upstream_failure', 'Public artifact transport failed.', key)));
    req.on('close', () => clearTimeout(timer));
  });
}

class Source {
  constructor({ directory = null, publicAccess = false, read = null } = {}) {
    if (!directory && !publicAccess && !read) throw new Error('Choose an explicit fixture directory or --public.');
    this.directory = directory && path.resolve(directory);
    this.mode = directory ? 'local_artifacts' : read ? 'injected_test_artifacts' : 'public_anonymous';
    this.reader = read; // injectable only from code for hermetic tests
  }
  query() {
    const started = Date.now(); let requests = 0, bytes = 0;
    const evidence = [];
    const get = async key => {
      permitted(key);
      if (++requests > LIMITS.requests) throw new DataError('excessive_request', 'Query request limit exceeded.', key);
      const remainingMs = LIMITS.timeout_ms - (Date.now() - started);
      if (remainingMs <= 0) throw new DataError('upstream_timeout', 'Query deadline exceeded.', key);
      const cap = Math.min(LIMITS.artifact_bytes, LIMITS.query_bytes - bytes);
      let raw;
      if (this.reader) raw = Buffer.from(await this.reader(key));
      else if (this.directory) {
        try {
          const root = await fs.realpath(this.directory);
          const target = await fs.realpath(path.join(root, key));
          if (!target.startsWith(root + path.sep)) throw new DataError('invalid_reference', 'Local artifact escapes its root.', key);
          const handle = await fs.open(target, 'r');
          try {
            const stat = await handle.stat();
            if (!stat.isFile() || stat.size > cap) throw new DataError('excessive_response', 'Local artifact byte limit exceeded.', key);
            raw = Buffer.alloc(stat.size);
            const readResult = await handle.read(raw, 0, raw.length, 0);
            if (readResult.bytesRead !== raw.length) throw new DataError('upstream_failure', 'Local artifact changed during read.', key);
          } finally { await handle.close(); }
        } catch (e) {
          if (e instanceof DataError) throw e;
          throw new DataError(e.code === 'ENOENT' ? 'missing_artifact' : 'upstream_failure', 'Local artifact could not be read.', key);
        }
      } else raw = await publicRead(key, cap, remainingMs);
      if (raw.length > cap) throw new DataError('excessive_response', 'Artifact or query byte limit exceeded.', key);
      bytes += raw.length;
      const entry = { locator: key, link: this.mode === 'public_anonymous' ? ORIGIN + '/' + key : null,
        retrieval_time: new Date().toISOString(), bytes: raw.length, retrieved_sha256: hash(raw),
        checksum_verification: 'not_verified_against_producer' };
      evidence.push(entry);
      if (key.endsWith('.gz')) {
        try { raw = zlib.gunzipSync(raw, { maxOutputLength: Math.min(LIMITS.expanded_artifact_bytes, LIMITS.query_bytes - bytes) }); }
        catch { throw new DataError('excessive_or_malformed_artifact', 'Gzip is damaged or exceeds the expanded byte limit.', key); }
        bytes += raw.length;
        if (bytes > LIMITS.query_bytes) throw new DataError('excessive_response', 'Expanded query byte limit exceeded.', key);
      }
      return raw.toString('utf8');
    };
    return { evidence, get, json: async key => {
      const text = await get(key);
      try { return JSON.parse(text); }
      catch { throw new DataError('incompatible_schema', 'Artifact is not valid JSON.', key); }
    }, optionalJson: async key => {
      try { const text = await get(key); return { value: JSON.parse(text), error: null }; }
      catch (e) { return { value: null, error: errorRecord(e, key) }; }
    } };
  }
}
function errorRecord(e, key = null) {
  return { code: e.code || 'incompatible_schema', message: e instanceof DataError ? e.message : 'Artifact does not satisfy the supported contract.', locator: e.key || key };
}
module.exports = { Source, DataError, LIMITS, ORIGIN, UUID, hash, permitted, publicRead, errorRecord };
