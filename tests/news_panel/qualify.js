// Tests-only helper (G3; G6 contract; NOT RUN when it was written or ported). The committed fixture
// views (tests/fixtures/news/views/) are builder output with unqualified news/v1 keys, and stay that
// way. The panel, the proxy and the gateway accept only the published form: the fixed index
// news/v2/views/index.json plus immutable pages of one incarnation,
// news/v2/views/p/<gen>.<n>/<mode>/<stream>/<date>/page-NNNN.json. This mirrors
// market_data/news/views.qualify_generation (n = 1) followed by commit_index:
//   gen   = <build_as_of as YYYYMMDDTHHMMSSZ>-<first 16 hex of files_digest(unqualified fixture bytes)>
//           (files_digest: SHA-256 over sorted "len(key):key:len(body):" + body; identical to Python's,
//           so the gen equals what the publisher would derive for these bytes);
//   inc   = <gen>.1, the id pages and the index carry as "gen";
//   pages get "gen": inc and same-incarnation prev_page/next_page; the index gets schema
//   news-view-index/v2, state "published", "gen": inc, "logical_gen", rewritten first_page values,
//   "pages_sha256" (here over this helper's JSON text, not Python's encode(): the panel only checks its
//   format) and a fixed test commit nonce.
// tombstone(state) is the body withdrawal ("withdrawn") or the first run ("unpublished") leaves.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const VIEWS = path.join(__dirname, '../fixtures/news/views');
const SOURCE_INDEX_KEY = 'news/v1/views/index.json';   // the fixture's (builder's) unqualified index
const INDEX_KEY = 'news/v2/views/index.json';          // the published index
const INDEX_SCHEMA = 'news-view-index/v2';
const TEST_NONCE = '0123456789abcdef0123456789abcdef';
const UNQ = /^news\/v1\/views\/(collector|archive|publication)\/(all|ai_buildout|oil)\/([0-9]{4}-[0-9]{2}-[0-9]{2})\/page-([0-9]{4})\.json$/;

function listKeys(dir = VIEWS) {
  const out = [];
  (function walk(d) {
    for (const name of fs.readdirSync(d)) {
      const full = path.join(d, name);
      if (fs.statSync(full).isDirectory()) walk(full);
      else out.push(path.relative(dir, full).split(path.sep).join('/'));
    }
  })(dir);
  return out.sort();
}

function filesDigest(files) {
  const h = createHash('sha256');
  for (const key of Object.keys(files).sort()) {
    const body = Buffer.isBuffer(files[key]) ? files[key] : Buffer.from(files[key], 'utf8');
    h.update(`${key.length}:${key}:${body.length}:`, 'utf8');
    h.update(body);
  }
  return h.digest('hex');
}

function stamp(iso) {
  // "2026-11-09T11:15:00Z" -> "20261109T111500Z" (seconds precision, like strftime('%Y%m%dT%H%M%SZ')).
  return new Date(iso).toISOString().replace(/[-:]/g, '').slice(0, 15) + 'Z';
}

function qualifyKey(inc, key) {
  const m = UNQ.exec(key);
  if (!m) throw new Error('not an unqualified page key: ' + key);
  return `news/v2/views/p/${inc}/${m[1]}/${m[2]}/${m[3]}/page-${m[4]}.json`;
}

function tombstone(state = 'withdrawn', nonce = TEST_NONCE) {
  return { schema_version: INDEX_SCHEMA, state, commit: { nonce } };
}

// files: {unqualified key: Buffer|string}; n: the incarnation number. Returns
// {gen: <incarnation id>, logicalGen, files: {published key: object}, q(key)}.
function qualifyFiles(files, n = 1) {
  const index = JSON.parse(String(files[SOURCE_INDEX_KEY]));
  const logicalGen = stamp(index.build_as_of) + '-' + filesDigest(files).slice(0, 16);
  const gen = `${logicalGen}.${n}`;
  const q = key => (key === SOURCE_INDEX_KEY || key === INDEX_KEY ? INDEX_KEY : qualifyKey(gen, key));
  const out = {};
  for (const key of Object.keys(files)) {
    if (key === SOURCE_INDEX_KEY) continue;
    const page = JSON.parse(String(files[key]));
    page.gen = gen;
    for (const field of ['prev_page', 'next_page']) {
      if (typeof page[field] === 'string') page[field] = q(page[field]);
    }
    out[q(key)] = page;
  }
  const pagesDigest = filesDigest(Object.fromEntries(Object.entries(out).map(([k, v]) => [k, JSON.stringify(v)])));
  out[INDEX_KEY] = Object.assign({}, index, {
    schema_version: INDEX_SCHEMA, state: 'published', gen, logical_gen: logicalGen, pages_sha256: pagesDigest,
    commit: { nonce: TEST_NONCE },
    views: index.views.map(v => Object.assign({}, v, { first_page: q(v.first_page) })),
  });
  return { gen, logicalGen, files: out, q };
}

// The committed fixture, published form. byKey accepts an unqualified fixture key or a published one.
function loadQualified(dir = VIEWS) {
  const raw = {};
  for (const key of listKeys(dir)) raw[key] = fs.readFileSync(path.join(dir, key));
  const { gen, logicalGen, files, q } = qualifyFiles(raw);
  const byKey = key => files[key in files ? key : q(key)];
  return {
    gen, logicalGen, files, q, byKey,
    index: files[INDEX_KEY],
    text: key => (key in files ? JSON.stringify(files[key]) : null),
  };
}

module.exports = { VIEWS, INDEX_KEY, SOURCE_INDEX_KEY, INDEX_SCHEMA, TEST_NONCE, listKeys, filesDigest, qualifyKey,
  qualifyFiles, loadQualified, tombstone };
