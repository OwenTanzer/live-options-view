import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { buildAssets, verifyArtifact, FLOOR, ROOT } from '../scripts/build-assets.mjs';
import { createHash } from 'node:crypto';
import { createPreview, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { pathToFileURL } from 'node:url';
import { assetBinding } from '../scripts/preview-local.mjs';
import { prepareRelease, validateAssetConfig, verifyStage } from '../scripts/prepare-asset-release.mjs';

function artifacts() {
  const parent=mkdtempSync(resolve(tmpdir(),'musef-rollback-'));
  const old=resolve(parent,'old'), current=resolve(parent,'current');
  // Two synthetic release IDs exercise the artifact protocol. They are not
  // claims that either source revision was deployed or checked out here.
  const before=buildAssets({out:old,revision:FLOOR,now:new Date('2026-11-11T22:00:00Z')});
  const after=buildAssets({out:current,revision:'a'.repeat(40),now:new Date('2026-11-11T22:01:00Z')});
  return {old,current,before,after};
}
test('asset-only rollback preserves API instance, account/session, schema and producer targets',async()=>{
  const {old,current,before,after}=artifacts();
  assert.deepEqual(verifyArtifact(old,after),before);
  const p=createPreview({assetRoot:resolve(current,'site'),revision:after.revision});
  let gateway=(await import(pathToFileURL(resolve(current,'frontend/worker.mjs')).href)).default;
  const call=(path,init)=>gateway.fetch(new Request(LOCAL_ORIGIN+path,init),p.env);
  try {
    const registered=await call('/api/register',{method:'POST',headers:{Origin:LOCAL_ORIGIN,'Content-Type':'application/json'},
      body:JSON.stringify({username:'rollback_reader',password:'disposable-password'})});
    assert.equal(registered.status,201);
    const cookie=registered.headers.get('Set-Cookie').split(';')[0];
    const api=p.env.API, db=p.apiEnv.DB;
    const head=await (await call('/')).text(); assert.match(head,/aaaaaaa-/);
    // This is the entire rollback action in local emulation: only asset binding
    // and observable asset revision change. No API deploy/schema command exists.
    p.env.ASSETS=assetBinding(resolve(old,'site')); p.env.ASSET_REVISION=before.revision;
    gateway=(await import(pathToFileURL(resolve(old,'frontend/worker.mjs')).href)).default;
    const restored=await (await call('/')).text(); assert.match(restored,/90cdc56/); assert.doesNotMatch(restored,/aaaaaaa-/);
    assert.equal(p.env.API,api); assert.equal(p.apiEnv.DB,db);
    assert.equal((await call('/api/me',{headers:{Cookie:cookie}})).status,200);
    assert.deepEqual(after.targets,before.targets); assert.deepEqual(before.targets.producers,[]);
    assert.deepEqual(before.schema,after.schema);
  } finally {p.close();}
});
for (const [label,mutate,pattern] of [
  ['legacy KV serving',m=>m.legacy_kv_serving=true,/rollback contract/],
  ['recovery serving',m=>m.recovery='serving',/rollback contract/],
  ['pre-D1 floor',m=>m.serving_rollback_floor='0'.repeat(40),/rollback contract/],
  ['API target drift',m=>m.targets.api='production-other',/rollback contract/],
  ['producer promotion',m=>m.targets.producers=['main-collector'],/rollback contract/],
  ['D1 schema mismatch',m=>m.schema[0].sha256='0'.repeat(64),/schema contract mismatch/],
]) test(`rollback refuses ${label}`,()=>{
  const {old,after}=artifacts(); const file=resolve(old,'release.json');
  const manifest=JSON.parse(readFileSync(file,'utf8'));mutate(manifest);writeFileSync(file,JSON.stringify(manifest));
  assert.throws(()=>verifyArtifact(old,after),pattern);
});
test('release/rollback staging changes only asset target and never executes a provider command',()=>{
  const {old,current,before}=artifacts();
  const stage=resolve(mkdtempSync(resolve(tmpdir(),'musef-stage-')),'release');
  const plan=prepareRelease({artifact:old,current,out:stage});
  assert.equal(plan.asset_revision,before.revision);assert.equal(plan.asset_target,'live-options-view');
  assert.equal(plan.unchanged_api_target,'live-options-api');assert.deepEqual(plan.producer_actions,[]);
  assert.deepEqual(plan.schema_actions,[]);assert.deepEqual(plan.recovery_actions,[]);
  assert.equal(plan.state,'prepared-not-approved-not-deployed');
  assert.deepEqual(verifyArtifact(resolve(stage,'payload')),before);
  const config=readFileSync(resolve(stage,'wrangler.assets.toml'),'utf8');
  assert.match(config,/run_worker_first = true/);assert.match(config,/payload\/frontend\/worker.mjs/);
  assert.doesNotMatch(config,/\[\[(r2_buckets|d1_databases|kv_namespaces)\]\]/);
  assert.match(config,/\[triggers\]\s*crons\s*=\s*\[\s*\]/);
  assert.throws(()=>prepareRelease({artifact:old,out:resolve(old,'nested')}),/outside artifact/);
});
test('rollback refuses tampered payload and unmanifested operational file',()=>{
  const {old,current}=artifacts();
  writeFileSync(resolve(old,'site/index.html'),'tampered');
  assert.throws(()=>verifyArtifact(old),/Hash mismatch/);
  writeFileSync(resolve(current,'collector.py'),'not an asset');
  assert.throws(()=>verifyArtifact(current),/inventory mismatch/);
});

const proposedConfig=readFileSync(resolve(ROOT,'patches/musef-225/wrangler.assets.toml.proposed'),'utf8');
function configFixture() {
  const parent=mkdtempSync(resolve(tmpdir(),'musef-config-')),root=resolve(parent,'source');
  mkdirSync(resolve(root,'patches/musef-225'),{recursive:true});
  writeFileSync(resolve(root,'patches/musef-225/wrangler.assets.toml.proposed'),proposedConfig);
  return {root,out:resolve(parent,'stage')};
}
test('stage explicitly identifies proposal fallback before config integration',()=>{
  const {old}=artifacts(),{root,out}=configFixture();
  const plan=prepareRelease({artifact:old,out,configRoot:root});
  assert.equal(plan.config_state,'proposal-only');
  assert.equal(plan.config_source,'patches/musef-225/wrangler.assets.toml.proposed');
  assert.equal(plan.config_sha256,createHash('sha256').update(proposedConfig).digest('hex'));
});
test('stage uses actual applied config, not stale proposal, and records exact bytes/hash',()=>{
  const {old}=artifacts(),{root,out}=configFixture();
  const applied=proposedConfig.replace('2026-06-23','2026-06-24');
  writeFileSync(resolve(root,'wrangler.assets.toml'),applied);
  writeFileSync(resolve(root,'patches/musef-225/wrangler.assets.toml.proposed'),'invalid proposal must be ignored');
  const plan=prepareRelease({artifact:old,out,configRoot:root});
  assert.equal(plan.config_state,'applied-source'); assert.equal(plan.config_source,'wrangler.assets.toml');
  assert.equal(plan.config_sha256,createHash('sha256').update(applied).digest('hex'));
  assert.equal(readFileSync(resolve(out,'wrangler.assets.toml'),'utf8'),applied);
});
test('invalid applied config cannot silently fall back to valid proposal or leave a stage',()=>{
  const {old}=artifacts(),{root,out}=configFixture();
  writeFileSync(resolve(root,'wrangler.assets.toml'),proposedConfig.replace('crons = []','crons = ["* * * * *"]'));
  assert.throws(()=>prepareRelease({artifact:old,out,configRoot:root}),/Unsupported asset config/);
  assert.equal(existsSync(out),false);
});
test('asset config rejects operational targets, bindings, paths, dates, labels and revision drift',()=>{
  const manifest={targets:{assets:'live-options-view',api:'live-options-api'},revision:'a'.repeat(40)};
  assert.doesNotThrow(()=>validateAssetConfig(proposedConfig,manifest));
  const changes=[
    ['name = "live-options-view"','name = "live-options-api"'],
    ['main = "payload/frontend/worker.mjs"','main = "../../backend/index.js"'],
    ['directory = "payload/site"','directory = "../../site"'],
    ['run_worker_first = true','run_worker_first = false'],
    ['ENVIRONMENT = "production"','ENVIRONMENT = "preview"'],
    ['service = "live-options-api"','service = "other-provider-service"'],
    ['binding = "API"','binding = "DB"'],
    ['compatibility_date = "2026-06-23"','compatibility_date = "2026-02-30"'],
    ['compatibility_date = "2026-06-23"',''],
    ['[vars]','[vars]\nISOLATION = "disposable"'],
    ['[vars]','[vars]\nASSET_REVISION = "wrong"'],
    ['[assets]','[assets]\nbinding = "DUPLICATE"'],
  ];
  for(const [oldValue,newValue] of changes) {
    assert.throws(()=>validateAssetConfig(proposedConfig.replace(oldValue,newValue),manifest),/Asset config|Unexpected asset config/);
  }
  for(const extra of ['\n[[d1_databases]]\nbinding = "DB"', '\n[[r2_buckets]]\nbinding = "PAPER_TRADES"',
    '\n[[kv_namespaces]]\nbinding = "USERS"','\n[triggers]\ncrons = ["0 * * * *"]',
    '\n[[services]]\nbinding = "EXTRA"', '\n[env.production]\nname = "other"']) {
    assert.throws(()=>validateAssetConfig(proposedConfig+extra,manifest),/Unsupported asset config|duplicate table/);
  }
});
test('applied config directory junction cannot disclose another source root',()=>{
  const {old}=artifacts(),{root,out}=configFixture();
  const outside=mkdtempSync(resolve(tmpdir(),'musef-config-outside-'));
  symlinkSync(outside,resolve(root,'wrangler.assets.toml'),'junction');
  assert.throws(()=>prepareRelease({artifact:old,out,configRoot:root}),/regular source file/);
  assert.equal(existsSync(out),false);
});
test('stage verification pins config bytes, command, target, metadata, actions and payload',()=>{
  const {old}=artifacts();
  const mutations=[
    stage=>writeFileSync(resolve(stage,'wrangler.assets.toml'),proposedConfig.replace('2026-06-23','2026-06-24')),
    ...[
      p=>p.proposed_command='wrangler deploy --config backend.toml',
      p=>p.asset_target='live-options-api', p=>p.producer_actions=['collector'],
      p=>p.config_source='other.toml', p=>p.asset_revision='b'.repeat(40),
      p=>p.artifact='other-payload',
    ].map(change=>stage=>{
      const path=resolve(stage,'plan.json'), plan=JSON.parse(readFileSync(path,'utf8'));
      change(plan); writeFileSync(path,JSON.stringify(plan));
    }),
    stage=>writeFileSync(resolve(stage,'payload/frontend/worker.mjs'),'tampered'),
  ];
  for(const mutate of mutations){
    const {root,out}=configFixture(); prepareRelease({artifact:old,out,configRoot:root});
    assert.equal(verifyStage(out).asset_target,'live-options-view');
    mutate(out); assert.throws(()=>verifyStage(out),/contract mismatch|Hash mismatch/);
  }
});
test('proposed PowerShell command quotes stage metacharacters literally, without execution',()=>{
  const {old}=artifacts(),{root,out}=configFixture();
  const path=out+"-reader's-$(not-a-command)";
  const plan=prepareRelease({artifact:old,out:path,configRoot:root});
  assert.ok(plan.proposed_command.includes("reader''s-$(not-a-command)"));
  assert.ok(plan.proposed_command.includes("--config '"));
  assert.deepEqual(verifyStage(path),plan);
});
test('asset cutover requires explicit empty cron clearing and rejects all active or extra triggers',()=>{
  const manifest={targets:{assets:'live-options-view',api:'live-options-api'},revision:'a'.repeat(40)};
  assert.doesNotThrow(()=>validateAssetConfig(proposedConfig,manifest));
  assert.doesNotThrow(()=>validateAssetConfig(proposedConfig.replace('crons = []','crons = [  ] # clear'),manifest));
  for(const config of [
    proposedConfig.replace('[triggers]\ncrons = []',''),
    ...['["*/15 12-23 * * 1-5"]','"[]"','false'].map(value=>proposedConfig.replace('crons = []','crons = '+value)),
    proposedConfig.replace('crons = []','crons = []\nother = true'),
    proposedConfig+'\n[triggers]\ncrons = []',
    proposedConfig.replace('crons = []','crons = []\ncrons = []'),
  ]) assert.throws(()=>validateAssetConfig(config,manifest),/Asset config|asset config/);
});
test('stage config integrity does not attest current source selection or freshness',()=>{
  const {old}=artifacts(),{root,out}=configFixture();
  const original=prepareRelease({artifact:old,out,configRoot:root});
  const applied=proposedConfig.replace('2026-06-23','2026-06-24');
  writeFileSync(resolve(root,'wrangler.assets.toml'),applied);
  // Unchanged stage still verifies: this is deliberately not live-source attestation.
  assert.deepEqual(verifyStage(out),original);
  const refreshed=prepareRelease({artifact:old,out:out+'-fresh',configRoot:root});
  assert.equal(refreshed.config_state,'applied-source');
  assert.notEqual(refreshed.config_source,original.config_source);
  assert.notEqual(refreshed.config_sha256,original.config_sha256);
  writeFileSync(resolve(root,'wrangler.assets.toml'),'invalid current config');
  assert.deepEqual(verifyStage(out+'-fresh'),refreshed);
  assert.throws(()=>prepareRelease({artifact:old,out:out+'-invalid',configRoot:root}),/Unsupported asset config/);
  assert.equal(existsSync(out+'-invalid'),false);
});
test('unsupported configuration diagnostics do not print raw secrets',()=>{
  const secret='not-a-real-secret-should-not-be-logged';
  assert.throws(()=>validateAssetConfig('unsupported '+secret,{}),error=>{
    assert.doesNotMatch(error.message,new RegExp(secret)); return /Unsupported asset config/.test(error.message);
  });
});
