# Production rollout verification — October 5, 2026

Brian's continuation preserves the original uncommitted MOOPERLIGHT checkout and
patch. Windows CIM identified MOOPERLIGHT and MooperLight\\MooperTest; the native
hostname/DNS call returned 4QJNH1BEUGUA2. The requested filesystem path and branch
were verified independently. The patch SHA-256 is
`dcc92fd230f9550263c8d59b6ca20879f3bb1ade455fc78449b36566d99bf21c`.
Exact-base `git apply --check` passed on Windows and Linux. All 17 prepared files
matched the original checkout after newline normalization before further edits.
The evidence ZIP was not used as source.

## Source and reconciliation

PR #119 was approved by KingEnderdragon and merged as
`640b8e508dd4b400ff544f4c73cbbcd16b33ae85`, preserving reviewed head `6dbb2bdee174ddfbf4408a1d90e0e8f55a85bf16`.
The formal approval was on that exact head; no branch protection was bypassed.
Before #119 merged, master was `f5c16f83b8ed10cfffbdcd428b7ef92491378c02`, including merged #116
and #117. Their changes are confined to Crassus code, docs and workflow; no MCP
runtime, shared.js, or squeeze-calendar change overlaps this follow-up.
This follow-up was originally stacked on #119 and is now retargeted to master.
Owen subsequently authorized merging after independent review and all branch
protections pass. A separate formal approving review of #120 remains required.

## Demonstrated repair and checks

A fresh Windows Node 22.23.3 run reproduced a test race: four requests had not
entered the server after the test's fixed 25 ms sleep. The test now holds verified
authentication at a synchronization barrier before body timers start, observes
four active slots and a fifth-request 429, then releases and tests body expiry
and recovery. No runtime behavior was changed by this repair.

- Windows Node 22.23.3: 66/66 MCP tests, 48/48 producer tests, seven authenticated
  fixture queries and the synthetic load passed.
- Linux Ubuntu 24.04, x86-64, rootless Podman 4.9.3: built the dedicated Dockerfile
  from the repository root, explicitly using its context ignore file.
- The built Node 22.23.3 image ran as UID 1000, with no collector, root Railway
  config, fixture data or signing-key helpers baked into it. Missing required
  configuration exits 1 with redacted startup_failed.
- All 66 MCP tests passed inside that image with networking disabled and a
  512 MiB memory limit. Tests and fixture helpers were mounted read-only into
  their individual paths; shipped runtime modules were not replaced.
- The actual production.js child entrypoint passed startup, health 200, missing
  token 401, wrong-owner 403, initialize/list/query/detail and SIGTERM exit 0.
  This test explicitly injects public test keys and fixture sources using a
  test-only preload. It proves process wiring, not live issuer/client access.
- Seven authenticated fixture HTTP queries, seven fixture stdio queries and
  48/48 producer tests passed on Linux. Squeeze display and momentum checks pass.
- Synthetic container load: 64 queries plus retained detail, four active
  requests, 64 references / 10,561,792 serialized bytes, p95 65 ms, peak RSS
  89,710,592 bytes, CPU 617 ms. This is not a Railway spending forecast.

`verify-image.sh` reproduces image checks in Docker (CI) or rootless Podman:

```sh
docker build -f market_mcp/Dockerfile -t market-mcp-check .
bash market_mcp/verify-image.sh market-mcp-check
# Podman uses --ignorefile market_mcp/Dockerfile.dockerignore when building.
```

CI now executes the built-image suite, actual-entrypoint fixture and load and
uploads their evidence. The local image ID is
`ea2f8450eb1ce69fd596ca11bbdcf1d20b9d03602b6ffc540daeadccca179eac`;
its locally loaded OCI manifest digest is
`sha256:496683a45e448a02da9d216a36d065a3151787f5c8591b9b40663c1e3bb97a26`.
Neither identifier is a published registry reference. Registry publication and
verification of the exact deployed digest remain release gates.

## Container host permissions

The initial default Podman store inherited home-directory default ACLs, causing
non-root traversal to fail even in the upstream Node image. With Owen's explicit
permission, a new dedicated store was created at
`/home/owen/.local/share/market-mcp-containers`, inherited ACLs removed only there,
and its owner-only mode set to 0700. The same exported image loaded and ran
successfully as UID 1000. Use `podman --root` with that path for this work.
Existing home-directory/Jacob ACLs, host security policy and default Podman
storage were not modified. No sudo was needed for this scoped repair.

## Provisioned shell, not a deployment

Project `ea9f4549-e34f-47cd-a09e-573b24d696a8`, production environment
`2dcc91a4-fafc-408f-bef8-e5ce24b2ec6a`:

- New service: `market-mcp-read-only`, ID `7222ee91-be95-4aea-843b-2ff1258e21da`.
- Reserved endpoint: `https://market-mcp-read-only-production.up.railway.app/mcp`.
- No source/image attached, variables, volume or deployment. The service does
  not yet implement that endpoint; HTTPS/application validation remains pending.
- Railway accepted and read back 0.2 CPU, 500,000,000 memory bytes, one us-west2
  replica, explicit Node start command, /healthz, 60-second health check,
  30-second drain and zero overlap. Sleep false, cron null and ON_FAILURE with
  three retries were submitted; default-valued fields are omitted from readback.
- The existing Discord service `5aa162c3-d2e7-468a-8fd0-a36e441285b2` was untouched.
  Big Banana was inspected read-only; latest deployment remains September 28.
  A later read-only Railway agent serviceAutoDeployTool check explicitly
  returned enabled:false, confirming the autodeploy pause before #119 merged.

The budget remains $10/month additional, no plan upgrade or workspace cutoff.
Current Railway pricing is $10/GB-month RAM, $20/vCPU-month CPU and $0.05/GB
outbound. Saturating the accepted compute limits is nominally $9/month before
egress. Actual service-level usage and a stop-before-budget procedure must be
verified at launch; process-local request limits are not a durable cost cutoff.

## Owner access gate and remaining work

No OAuth API, client, credentials, grant or tenant defaults have been changed.
Proposed audience equals the reserved /mcp endpoint. Scope is market:read; issuer
is https://dev-oraxxi11mrzuff2h.us.auth0.com/. Use a separate dedicated public
client, authorization code plus S256 PKCE, maximum 3600-second access tokens,
and exact owner subject and client allowlists. Browser origins stay empty.

The existing setup page records ChatGPT callback
https://chatgpt.com/connector_platform_oauth_redirect and owner subject
auth0|6ac12294d1f5dacb7466a76f. These are proposed bindings for a NEW dedicated
market client, not permission to reuse a Discord client or grant. Reverify at
configuration time. The new client ID and live resource-to-audience mapping
remain unverified. Present those exact values
and owner restriction for action-time approval before registration/access
configuration. Do not infer them from the Discord client or test fixtures.

After approval and release review: publish the tested immutable image, attach
only that image to this service, configure the approved public metadata values,
verify HTTPS and denial cases, and run initialize/list/query/detail through
Chej's actual client. Observe resource usage and verify service-scoped rollback.
Rollback is stopping the new service/disconnecting its market client; do not
alter producers, Discord, Big Banana, or revoke grants without authorization.
No live Chej connection is claimed.

## Final review follow-up

Jayden's two nonblocking #119 notes are repaired here. Tool description and
invalid-reference errors explicitly describe early count/byte eviction. Detail
sources label retained versus newly fetched evidence with an explicit boolean;
stored originating evidence and its retrieval timestamps are not mutated.
Envelope retrieval_time is response assembly time. Regression checks cover all
three datasets, repeated details, failed archive validation and early eviction.
The revised suite contains 68 tests. Independent review of the initial published
head found no additional blocking code defect and independently passed all 66
original tests both on host and inside the exact Node22 image. Final repair
review and rebuilt-image checks are recorded in the PR before merge.

Final repair verification: rebuilt image ID
`72de093360b2e93681ccc43e89164584a0e37870e1a887cec095683c6b44d99d`,
local OCI manifest digest
`sha256:0a83254f8562252c8f570e56c9b449f65873f3caa07e058f59bfc6d120766b46`.
All 68 tests passed in this non-root Node22 image, plus actual entrypoint,
seven-query authenticated smoke, shutdown and load. Peak RSS 92,725,248 bytes,
p95 66 ms, CPU 607 ms. No live registry/deployment assertion.
A separate AI reviewer inspected the final provenance/eviction repairs and
independently ran both new regressions, 2/2 passing, with no blocking findings.
That technical review does not replace GitHub's required collaborator approval.


## PR120 requested-change repair: authentication quota isolation

Jayden's Changes requested review on e8d5345 identified a shared pre-authentication
minute counter that let strangers exhaust the owner's allowance. A fixed-clock
regression reproduced 429 instead of 200 for a valid owner immediately after
125 missing/malformed/wrong-owner requests.

The repaired transport charges the owner's 120/minute allowance only after
successful authentication. A separate bounded counter admits 120 authentication
denials (401/403) per minute, then returns 429 with Retry-After for further
denials. Authentication infrastructure failures (503) charge neither counter.
Verification continues before classification; this is quota isolation, not a
cap on verification computation or immunity to concurrent/network flooding.
The shared four active slots and 32 sockets remain unchanged.

The regression now passes, verifies all 120 owner requests still succeed after
the rejected flood, enforces the next owner's 429, and checks minute reset.
The existing daily quota/reset and concurrency tests also pass. An independent
reviewer inspected the repair and reran those three focused tests: 3/3 passed,
no blocking findings. Jayden's formal Changes requested remains until re-review.

Validation: 69/69 host tests and 69/69 tests against the rebuilt non-root
Node22.23.3 image; actual-entrypoint fixture health200, missing-token401,
wrong-owner403, initialize/list/query/detail and SIGTERM exit0; seven authenticated
fixture queries; synthetic load64 queries plus detail, peak4 active, p95 64ms,
RSS92,303,360 bytes, CPU610ms. These are local fixture measurements, not live
Chej or Railway acceptance. Evidence on verified Linux Box owen-MPL1P as owen:
../repair-quota-host-tests.txt and ../repair-quota-image/.

Local image ID: 6185f9abd3c073c455c6ca2e27834b8d517a14f7d2a486c4f13e2bf76b28c8fb.
Local manifest digest (supersedes preceding revision):
sha256:4c69686b5455e44e7e882c6b52c6713090bbfffcffc4182183da78fcbe2323a3.
Not registry-published or deployed. No credentials, access grants, Railway
settings, or original MOOPERLIGHT prepared files were changed by this repair.
