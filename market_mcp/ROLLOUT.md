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

PR #119 remains draft at `6dbb2bdee174ddfbf4408a1d90e0e8f55a85bf16`.
Its latest Chej review is COMMENT, not a formal approving human review.
Master is `f5c16f83b8ed10cfffbdcd428b7ef92491378c02`, including merged #116
and #117. Their changes are confined to Crassus code, docs and workflow; no MCP
runtime, shared.js, or squeeze-calendar change overlaps this follow-up.
This follow-up is stacked on #119 to keep the production diff separately
reviewable. Owen retains manual merge control; protections are not bypassed.
Reconcile onto the actual accepted master after #119 merges and rerun checks
before a production release.

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
  The connector does not expose its autodeploy pause toggle, so the pause is
  preserved by no mutation but not independently certified from that response.

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

The actual Chej callback URI, resource-to-Auth0-audience mapping, dedicated client
ID and verified owner subject are still unverified. Present those exact values
and owner restriction for action-time approval before registration/access
configuration. Do not infer them from the Discord client or test fixtures.

After approval and release review: publish the tested immutable image, attach
only that image to this service, configure the approved public metadata values,
verify HTTPS and denial cases, and run initialize/list/query/detail through
Chej's actual client. Observe resource usage and verify service-scoped rollback.
Rollback is stopping the new service/disconnecting its market client; do not
alter producers, Discord, Big Banana, or revoke grants without authorization.
No live Chej connection is claimed.
