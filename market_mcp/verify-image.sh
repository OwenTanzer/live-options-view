#!/usr/bin/env bash
# Run from the repository root. Test files are temporary read-only mounts.
set -euo pipefail
image=${1:-market-mcp-check}
engine=${CONTAINER_ENGINE:-docker}
runner=("$engine")
if [[ -n ${PODMAN_ROOT:-} ]]; then
  [[ "$engine" == podman ]]
  runner+=(--root "$PODMAN_ROOT")
fi
evidence=${IMAGE_EVIDENCE_DIR:-market-mcp-image-evidence}
mkdir -p "$evidence"
test_files=$(mktemp -d)
trap 'rm -rf "$test_files"' EXIT
"${runner[@]}" image inspect "$image" > "$evidence/image-inspect.json"
"${runner[@]}" run --rm --network none --entrypoint node "$image" -e "const a=require('node:assert/strict'),f=require('node:fs');a.equal(process.versions.node.split('.')[0],'22');a.notEqual(process.getuid(),0);require('./market_mcp/consumer');require('./market_mcp/production');for(const p of ['/app/collector.py','/app/railway.toml','/app/market_mcp/test-auth.js','/app/market_mcp/fixtures.js','/app/market_mcp/container-smoke.js'])a.equal(f.existsSync(p),false);const r=require('node:child_process').spawnSync(process.execPath,['market_mcp/production.js']);a.equal(r.status,1);a.match(r.stderr.toString(),/startup_failed/);console.log(JSON.stringify({node:process.version,uid:process.getuid(),startup_missing_config:r.status,files:f.readdirSync('/app/market_mcp')}));" > "$evidence/image-check.json"
args=(run --rm --network none --memory 512m --pids-limit 128 --entrypoint node)
for file in market_mcp/*.test.js market_mcp/fixtures.js market_mcp/test-auth.js market_mcp/smoke.js market_mcp/production-smoke.js market_mcp/load.js market_mcp/container-smoke.js; do
  # Creating fresh files avoids inheriting source-file ACLs into the container.
  cat "$file" > "$test_files/$(basename "$file")"
  chmod 644 "$test_files/$(basename "$file")"
  args+=(-v "$test_files/$(basename "$file"):/app/$file:ro")
done
"${runner[@]}" "${args[@]}" "$image" --max-old-space-size=256 --test market_mcp/consumer.test.js market_mcp/http.test.js market_mcp/production.test.js > "$evidence/tests.txt" 2>&1
"${runner[@]}" "${args[@]}" "$image" market_mcp/container-smoke.js > "$evidence/entrypoint.json"
"${runner[@]}" "${args[@]}" "$image" market_mcp/production-smoke.js > "$evidence/authenticated-smoke.json"
"${runner[@]}" "${args[@]}" "$image" --max-old-space-size=256 market_mcp/load.js > "$evidence/load.json"
cat "$evidence/image-check.json" "$evidence/entrypoint.json"
tail -10 "$evidence/tests.txt"
