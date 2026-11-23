// tests/helpers/d1_shim.mjs -- a real SQLite database behind D1's API shape.
//
// Node's built-in `node:sqlite` gives the tests the same engine D1 runs, so the
// schema and the SQL in backend/storage.js are exercised for real: UNIQUE
// constraints, conditional UPDATE ... WHERE version = ?, and ON CONFLICT
// clauses all behave as they will in production. Only the network wrapper is
// fake.

import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

class Statement {
  constructor(db, sql, params, flags) {
    this.db = db;
    this.sql = sql;
    this.params = params;
    this.flags = flags ?? { failNextAll: false, failAfterRuns: null };
  }

  bind(...params) {
    return new Statement(this.db, this.sql, params, this.flags);
  }

  async run() {
    if (this.flags.failAfterRuns !== null && this.flags.failAfterRuns !== undefined) {
      if (this.flags.failAfterRuns <= 0) throw new Error('simulated D1 run failure');
      this.flags.failAfterRuns -= 1;
    }
    const prepared = this.db.prepare(this.sql);
    const result = prepared.run(...this.params);
    return { success: true, meta: { changes: result.changes, last_row_id: result.lastInsertRowid } };
  }

  async first() {
    const row = this.db.prepare(this.sql).get(...this.params);
    return row ?? null;
  }

  async all() {
    if (this.flags.failNextAll) {
      this.flags.failNextAll = false;
      throw new Error('simulated D1 scan failure');
    }
    const results = this.db.prepare(this.sql).all(...this.params);
    return { results, success: true, meta: {} };
  }
}

export class FakeD1 {
  constructor() {
    this.db = new DatabaseSync(':memory:');
    this.queries = [];
    this.flags = { failNextAll: false, failAfterRuns: null };
  }

  static schemaSql() {
    return ['0001_accounts_sessions.sql', '0002_cutover_support.sql']
      .map(name => fs.readFileSync(path.join(ROOT, 'backend', 'migrations', name), 'utf8'))
      .join('\n');
  }

  prepare(sql) {
    this.queries.push(sql);
    return new Statement(this.db, sql, [], this.flags);
  }

  async exec(sql) {
    this.db.exec(sql);
    return { count: 0, duration: 0 };
  }

  async batch(statements) {
    // D1 batch is atomic; SQLite in a transaction gives the same guarantee.
    this.db.exec('BEGIN');
    const results = [];
    try {
      for (const statement of statements) results.push(await statement.run());
      this.db.exec('COMMIT');
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
    return results;
  }

  raw(sql, ...params) {
    return this.db.prepare(sql).all(...params);
  }
}

export function makeStorage() {
  const d1 = new FakeD1();
  d1.db.exec(FakeD1.schemaSql());
  return d1;
}

/** A legacy KV namespace stand-in for migration tests. */
export class FakeKV {
  constructor() {
    this.data = new Map();
    this.complete = true;
    this.listCalls = 0;
    this.failOnPage = null;
    this.failNextList = false;
  }

  async get(key) {
    return this.data.has(key) ? this.data.get(key) : null;
  }

  async put(key, value, options = {}) {
    void options;
    this.data.set(key, value);
  }

  async delete(key) {
    this.data.delete(key);
  }

  async list({ prefix = '', limit = 1000, cursor } = {}) {
    this.listCalls += 1;
    if (this.failNextList) {
      this.failNextList = false;
      throw new Error('simulated KV list failure');
    }
    if (this.failOnPage && this.listCalls === this.failOnPage) {
      throw new Error('simulated KV list failure');
    }
    const keys = [...this.data.keys()].filter(k => k.startsWith(prefix)).sort();
    const start = cursor ? keys.indexOf(cursor) + 1 : 0;
    const page = keys.slice(start, start + limit);
    const done = start + page.length >= keys.length;
    return {
      keys: page.map(name => ({ name })),
      list_complete: done,
      cursor: done ? undefined : page[page.length - 1],
    };
  }
}
