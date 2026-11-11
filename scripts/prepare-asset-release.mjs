// Purely local staging/plan generation. No provider SDK, deploy, migration or restart.
import { cpSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { resolve, relative, sep, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { ROOT, verifyArtifact } from './build-assets.mjs';

// Intentionally a narrow, fail-closed TOML subset, not a general TOML parser.
// New provider knobs require review here rather than bypassing an asset-only plan.
export function validateAssetConfig(config, manifest) {
  const fields=new Map(), tables=new Set(); let table='root';
  for(const raw of config.split(/\r?\n/)) {
    const line=raw.trim(); if(!line || line.startsWith('#'))continue;
    if(['[vars]','[assets]','[[services]]','[triggers]'].includes(line)) {
      table=line.replace(/\[|\]/g,'');
      if(tables.has(table))throw new Error('Asset config duplicate table: '+table);
      tables.add(table); continue;
    }
    const item=line.match(/^([A-Za-z_][A-Za-z_0-9]*)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|true|false|\[\s*\])\s*(?:#.*)?$/);
    if(!item)throw new Error('Unsupported asset config; explicit review required');
    const key=table+'.'+item[1], value=item[2];
    if(fields.has(key))throw new Error('Asset config duplicate key: '+key);
    fields.set(key,value.startsWith("'")?value.slice(1,-1):JSON.parse(value));
  }
  const expected={
    'root.name':manifest.targets.assets, 'root.main':'payload/frontend/worker.mjs',
    'vars.ENVIRONMENT':'production', 'assets.directory':'payload/site',
    'assets.binding':'ASSETS', 'assets.run_worker_first':true,
    'services.binding':'API', 'services.service':manifest.targets.api,
  };
  for(const [key,value] of Object.entries(expected)) {
    if(fields.get(key)!==value)throw new Error('Asset config contract mismatch: '+key);
  }
  // Omission can preserve an old combined Worker's cron during cutover.
  const crons=fields.get('triggers.crons');
  if(!Array.isArray(crons)||crons.length!==0)throw new Error('Asset config requires explicit empty triggers.crons');
  const date=fields.get('root.compatibility_date');
  if(typeof date!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(date)||
      Number.isNaN(new Date(date).getTime())||new Date(date).toISOString().slice(0,10)!==date) {
    throw new Error('Asset config compatibility_date must be a real ISO date');
  }
  for(const key of fields.keys()) {
    if(key in expected || key==='root.compatibility_date' || key==='triggers.crons')continue;
    if(key==='vars.ASSET_REVISION'&&fields.get(key)===manifest.revision)continue;
    throw new Error('Unexpected asset config field; explicit review required: '+key);
  }
}

const configHash = config => createHash('sha256').update(config).digest('hex');
// Commands are review material for this Windows/PowerShell operating procedure.
const literal = value => "'" + value.replaceAll("'", "''") + "'";
const deploymentCommand = (stage, revision) => `wrangler deploy --config ${literal(resolve(stage,'wrangler.assets.toml'))} --var ${literal('ASSET_REVISION:'+revision)}`;
export function verifyStage(stage) {
  const staging=resolve(stage), plan=JSON.parse(readFileSync(resolve(staging,'plan.json'),'utf8'));
  const manifest=verifyArtifact(resolve(staging,'payload'));
  const configPath=resolve(staging,'wrangler.assets.toml');
  if(lstatSync(configPath).isSymbolicLink())throw new Error('Stage config symlink refused');
  const config=readFileSync(configPath,'utf8'); validateAssetConfig(config,manifest);
  if(plan.state!=='prepared-not-approved-not-deployed'||plan.asset_revision!==manifest.revision||
      plan.asset_target!==manifest.targets.assets||plan.unchanged_api_target!==manifest.targets.api||
      plan.artifact!==resolve(staging,'payload')||plan.config_sha256!==configHash(config)||
      plan.proposed_command!==deploymentCommand(staging,manifest.revision)||
      !['producer_actions','schema_actions','recovery_actions'].every(k=>JSON.stringify(plan[k])==='[]')||
      !((plan.config_state==='applied-source'&&plan.config_source==='wrangler.assets.toml')||
        (plan.config_state==='proposal-only'&&plan.config_source==='patches/musef-225/wrangler.assets.toml.proposed'))) {
    throw new Error('Stage plan/config/payload contract mismatch');
  }
  return plan;
}

export function prepareRelease({artifact,out,current=null,configRoot=ROOT}) {
  const manifest=verifyArtifact(artifact,current?verifyArtifact(current):null);
  const staging=resolve(out);
  for (const base of [resolve(artifact),ROOT,resolve(configRoot)]) {
    const rel=relative(base,staging);
    if(!isAbsolute(rel)&&rel!=='..'&&!rel.startsWith('..'+sep))throw new Error('Stage outside artifact and source');
  }
  if(existsSync(staging))throw new Error('Staging directory must be new');
  const applied=resolve(configRoot,'wrangler.assets.toml');
  const configPath=lstatSync(applied,{throwIfNoEntry:false})?applied:resolve(configRoot,'patches/musef-225/wrangler.assets.toml.proposed');
  const realRoot=realpathSync(configRoot), configRelative=relative(realRoot,realpathSync(configPath));
  if(lstatSync(configPath).isSymbolicLink()||isAbsolute(configRelative)||configRelative==='..'||configRelative.startsWith('..'+sep)) {
    throw new Error('Asset config must be a regular source file inside config root');
  }
  const config=readFileSync(configPath,'utf8');
  validateAssetConfig(config,manifest); // refuse unsafe config before creating any stage
  mkdirSync(staging,{recursive:true});
  cpSync(resolve(artifact),resolve(staging,'payload'),{recursive:true});
  verifyArtifact(resolve(staging,'payload'),manifest);
  writeFileSync(resolve(staging,'wrangler.assets.toml'),config);
  const plan={state:'prepared-not-approved-not-deployed',asset_revision:manifest.revision,
    artifact:resolve(staging,'payload'),asset_target:manifest.targets.assets,
    unchanged_api_target:manifest.targets.api,producer_actions:[],schema_actions:[],
    recovery_actions:[],requires:'Nova C2 topology/trigger/resource review and existing production authority',
    config_state:configPath===applied?'applied-source':'proposal-only',
    config_source:relative(resolve(configRoot),configPath).split(sep).join('/'),
    config_sha256:configHash(config),
    proposed_command:deploymentCommand(staging,manifest.revision)};
  writeFileSync(resolve(staging,'plan.json'),JSON.stringify(plan,null,2)+'\n');
  return verifyStage(staging);
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=process.argv.slice(2),get=k=>args[args.indexOf(k)+1];
  if(args[0]==='--verify-stage')console.log(JSON.stringify(verifyStage(args[1])));
  else {
    if(!args.includes('--artifact')||!args.includes('--out'))throw new Error('Usage: --artifact VERIFIED_ARTIFACT --out NEW_STAGING [--current CURRENT_ARTIFACT], or --verify-stage STAGE');
    console.log(JSON.stringify(prepareRelease({artifact:get('--artifact'),out:get('--out'),current:args.includes('--current')?get('--current'):null})));
  }
}
