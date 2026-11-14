// Tests-only probe (M5.2): runs the site's only panel copy (site/js/news-panel.js), unchanged, on
// JSON supplied by the Python publisher tests, so validation is checked against the real renderer
// rather than against validatePage alone. A minimal fake DOM records what the panel builds.
//   stdin:  {"cases": [{"kind": "page", "page": {...}}
//                      | {"kind": "mount", "index": {...}, "pages": {"<key>": {...}}}]}
//   stdout: [{"threw": null | "<message>", "state": "<data-state>" | null, "text": "<rendered text>"}]
// "page" calls renderPage directly (an exception is reported in threw). "mount" runs the whole
// panel from index.json, fetching pages from the supplied map (a missing key is an HTTP 404).
'use strict';
const panel = require('../../site/js/news-panel.js');

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.disabled = false; this.value = ''; this.selected = false;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => ' ' + c.textContent).join(''); }
  set innerHTML(v) { throw new Error('innerHTML must not be used'); }
}
const doc = { createElement: tag => new Node(tag) };

async function run(c) {
  const root = new Node('div');
  if (c.kind === 'page') {
    try {
      panel.renderPage(doc, root, c.page, {});
    } catch (e) {
      return { threw: String(e && e.message || e), state: null, text: root.textContent };
    }
    return { threw: null, state: null, text: root.textContent };
  }
  const pages = c.pages || {};
  const fetchJson = key => key === panel.INDEX_KEY ? Promise.resolve(JSON.parse(JSON.stringify(c.index)))
    : key in pages ? Promise.resolve(JSON.parse(JSON.stringify(pages[key])))
    : Promise.reject(new Error(key + ': HTTP 404'));
  try {
    await panel.mount(doc, root, fetchJson, panel.INDEX_KEY);
  } catch (e) {
    return { threw: String(e && e.message || e), state: root.getAttribute('data-state'), text: root.textContent };
  }
  return { threw: null, state: root.getAttribute('data-state'), text: root.textContent };
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', d => { input += d; });
process.stdin.on('end', async () => {
  const out = [];
  for (const c of JSON.parse(input).cases) out.push(await run(c));
  process.stdout.write(JSON.stringify(out));
});
