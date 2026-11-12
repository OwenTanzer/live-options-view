// Asset-only packaging. This command cannot deploy, restart or migrate anything.
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { resolve, relative, extname, dirname, isAbsolute, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stampHtml } from './stamp-build.mjs';
export const ROOT = fileURLToPath(new URL('../', import.meta.url));
export const FLOOR = '90cdc56c207ad0c733bb2f1b1bb15d995472bf47';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const ASSET_EXTENSIONS = new Set(['.html', '.js', '.css', '.json', '.svg', '.png', '.jpg', '.jpeg', '.ico', '.woff', '.woff2', '.txt', '.webp']);
export function filesUnder(root, prefix = '') {
  const result = [];
  for (const item of readdirSync(resolve(root, prefix), { withFileTypes: true })) {
    const name = prefix ? `${prefix}/${item.name}` : item.name;
    if (item.isSymbolicLink()) throw new Error(`Symlink refused: ${name}`);
    if (item.isDirectory()) result.push(...filesUnder(root, name));
    else if (item.isFile()) result.push(name);
  }
  return result.sort();
}
export function trackedAssetFiles(root = ROOT) {
  return execFileSync('git', ['ls-files', '-z', '--', 'site/'], {cwd:root,encoding:'utf8'})
    .split('\0').filter(Boolean).map(p=>p.slice('site/'.length)).filter(p=>ASSET_EXTENSIONS.has(extname(p))).sort();
}
export function buildAssets({ root = ROOT, out, revision, now = new Date() }) {
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  const output = resolve(out);
  if (existsSync(output)) throw new Error('Output must be new; never overwrite an artifact');
  const rel = relative(root, output);
  if (!isAbsolute(rel) && rel !== '..' && !rel.startsWith('..' + sep)) throw new Error('Build outside source worktree');
  mkdirSync(output, { recursive: true });
  // Never sweep ignored local files (including secrets) into an exact-SHA artifact.
  // The CLI additionally requires clean HEAD; this inventory names tracked assets only.
  filesUnder(resolve(root, 'site')); // reject links before copying any source asset
  const assets = trackedAssetFiles(root);
  for (const name of assets) {
    const dest = resolve(output, 'site', name);
    mkdirSync(dirname(dest), { recursive: true });
    if (name === 'index.html') writeFileSync(dest, stampHtml(readFileSync(resolve(root, 'site', name), 'utf8'), revision, now).html);
    else cpSync(resolve(root, 'site', name), dest);
  }
  for (const name of ['frontend/worker.mjs', 'frontend/preview-policy.mjs']) {
    mkdirSync(dirname(resolve(output, name)), { recursive: true });
    cpSync(resolve(root, name), resolve(output, name));
  }
  const schema = ['0001_accounts_sessions.sql', '0002_cutover_support.sql'].map(name => ({
    name, sha256: sha(readFileSync(resolve(root, 'backend/migrations', name))),
  }));
  const manifest = { format: 1, revision, api_contract: 'same-origin-d1-v1',
    serving_rollback_floor: FLOOR, schema, recovery: 'offline-only', legacy_kv_serving: false,
    targets: { assets: 'live-options-view', api: 'live-options-api', producers: [] },
    files: Object.fromEntries(filesUnder(output).map(name => [name, sha(readFileSync(resolve(output, name)))])) };
  writeFileSync(resolve(output, 'release.json'), JSON.stringify(manifest, null, 2) + '\n');
  return manifest;
}
export function verifyArtifact(out, reference = null) {
  const dir = resolve(out);
  const manifest = JSON.parse(readFileSync(resolve(dir, 'release.json'), 'utf8'));
  if (manifest.format !== 1 || !/^[a-f0-9]{40}$/.test(manifest.revision) ||
      manifest.api_contract !== 'same-origin-d1-v1' || manifest.serving_rollback_floor !== FLOOR ||
      manifest.recovery !== 'offline-only' || manifest.legacy_kv_serving !== false ||
      JSON.stringify(manifest.targets) !== JSON.stringify({ assets: 'live-options-view', api: 'live-options-api', producers: [] })) {
    throw new Error('Incompatible asset rollback contract');
  }
  const names = filesUnder(dir).filter(p => p !== 'release.json');
  if (JSON.stringify(names) !== JSON.stringify(Object.keys(manifest.files).sort())) throw new Error('Artifact inventory mismatch');
  for (const name of names) {
    if (!(name.startsWith('site/') && ASSET_EXTENSIONS.has(extname(name))) &&
        !['frontend/worker.mjs', 'frontend/preview-policy.mjs'].includes(name)) throw new Error(`Operational payload refused: ${name}`);
    if (lstatSync(resolve(dir, name)).isSymbolicLink() || sha(readFileSync(resolve(dir, name))) !== manifest.files[name]) throw new Error(`Hash mismatch: ${name}`);
  }
  for (const required of ['site/index.html', 'frontend/worker.mjs', 'frontend/preview-policy.mjs']) {
    if (!manifest.files[required]) throw new Error(`Missing required payload: ${required}`);
  }
  const expectedSchema = ['0001_accounts_sessions.sql', '0002_cutover_support.sql'].map(name => ({name, sha256: sha(readFileSync(resolve(ROOT, 'backend/migrations', name)))}));
  if (JSON.stringify(manifest.schema) !== JSON.stringify(expectedSchema) ||
      (reference && JSON.stringify(reference.schema) !== JSON.stringify(expectedSchema))) throw new Error('D1 schema contract mismatch');
  if (reference && (manifest.api_contract !== reference.api_contract || JSON.stringify(manifest.targets) !== JSON.stringify(reference.targets))) throw new Error('API/producer target drift');
  return manifest;
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  if (args[0] === '--verify') console.log(JSON.stringify(verifyArtifact(args[1])));
  else {
    const get = name => args[args.indexOf(name) + 1];
    if (!args.includes('--out') || !args.includes('--revision')) throw new Error('Usage: node scripts/build-assets.mjs --revision FULL_SHA --out NEW_OUTSIDE_DIRECTORY');
    const actual = execFileSync('git', ['rev-parse', 'HEAD'], {cwd: ROOT, encoding:'utf8'}).trim();
    if (get('--revision') !== actual) throw new Error('Requested revision differs from checked-out HEAD');
    execFileSync('git', ['merge-base', '--is-ancestor', FLOOR, actual], {cwd: ROOT, stdio:'inherit'});
    const dirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=all'], {cwd:ROOT,encoding:'utf8'}).trim();
    if (dirty) throw new Error('Package only a clean exact-revision worktree');
    console.log(JSON.stringify(buildAssets({ out: get('--out'), revision: actual })));
  }
}
