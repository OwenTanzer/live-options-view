# #118 readiness record — October 5, 2026

## Verified scope and base

Machine: `MOOPERLIGHT`; isolated checkout of
`https://github.com/OwenTanzer/live-options-view.git`.
Base: `master` at `fbbc3456da8025b6b1021f3d7282b2d6cae570e3`.
Branch: `feat/118-read-only-market-mcp`.
The original checkout on `agent/generalize-crassus-bot-cards` was preserved.

Current issue #118 and open PR inventory were read from GitHub. #108/#110 were
verified merged; #116 and #117 were open. Inventory/ownership and producer
contracts govern this slice. Neither checkout contained a repository AGENTS.md
or `.agents/skills/SKILL.md`; the outer local `.agents` directories were empty.
The parent protection standard was read; its settings-mutation procedure was
outside the user's explicit security-settings boundary and was not applied.
No local memory was needed to override current sources.

## Evidence and tests

Anonymous, fixed-origin reads successfully returned all three product families.
The real local stdio smoke completed seven queries with **no source errors**:

| Product | Observed example |
| --- | --- |
| QQQ context + retained row detail | Snapshot `intraday/20261005/snapshot_112657758692.csv`, collection `2026-10-05T15:26:57.758708+00:00`; fresh at retrieval. CSV itself was not read/verified. |
| Squeeze shortlist + exact-run detail | Success run `5dfbd437-78f4-49a9-95c2-ca02a8edaf1c`, session October 2; WOLF combined rank 1. Newer October 5 attempt `1e4aa183-2c56-4541-8f74-83df85bc3af4` was partial, so freshness stayed stale with its warning. Manifest/results/inputs digest checks succeeded. |
| Selected October 2 returns + one sweep detail | Clean leader `SPCX261002C00157500`, producer all rank 2, clean rank 1. One timestamped contract quote from the first bounded sweep was returned; session remains historical. |

These examples are observations at retrieval, not ongoing availability claims.
Full local fixture/public JSON transcripts were saved outside tracked source;
the documented smoke commands regenerate inspectable outputs. No source family
was fixture-only in this run. Producer capabilities marked unsupported remain
unavailable even though their raw ingredients may exist.

Validation:

- 51 new Node tests passed: 48 consumer/stdio/bounds/failure tests and 3 real
  loopback Streamable HTTP integration tests.
- Existing OA-203 suite: 48 tests passed.
- Existing squeeze schedule/status, VWAP/RVOL and momentum display suites passed.
- Synthetic-fixture real stdio smoke: seven queries passed.
- Public-artifact real stdio smoke: seven queries passed, including all details.
- Independent reviewer reproduced defects and reviewed the repairs. Regression
  cases cover clean ranks, falsy schemas, missing RVOL states, retained detail
  provenance/status, newer failed/missed squeeze slots, session rebuild races,
  protocol metadata, symlink/origin/path bounds and oversize/timeouts.
- Independent final review: 51/51 tests passed; raw HTTP checks verified the
  four-active-request cap, five-second body expiry and successful recovery.
  No remaining demonstrated in-scope defect was reported.

Initial Node subprocess/Python temporary-directory failures were sandbox access
failures. A scoped test execution exception resolved them; final suites passed.
No provider/scanner code, strategy behavior or private ledger was changed.

## Deployment isolation observed (read-only Railway inspection)

Only `market_mcp/**` and `.github/workflows/market-mcp-ci.yml` are added.
No changed path matches the Cloudflare `deploy.yml` master-path trigger.
The added workflow runs tests/smoke only.

Actual Railway production config was read without variable values:

| Service | Source branch | Watch scope relevant to this patch |
| --- | --- | --- |
| `live-options-view` | master | Explicit collector/config paths; excludes these additions |
| `crassus-runner` | master | `/crassus/**`; excludes these additions |
| `moo169-tradier-collector` | master | Explicit launcher/probe/collector/dependency paths; excludes these additions |
| `moo144-tradier-probe` | moo-144-probe-recovery | Different branch |
| `oa203-banana-scanner-AWsT` | master | No watchPatterns supplied; current autodeploy enabled/paused state was not verified |

Project `live-market-monitor`: `c27d4273-6b16-4921-bd61-0c4f27a8c8ae`;
production: `9008da9e-6888-4a58-a33b-e3eef5cc01f5`.
OA-203 service: `2037a86b-16a2-4c64-a10f-458b2a2d1f47`.
Publishing this feature branch/draft PR is outside these production source
branches. Parent history records Owen explicitly paused Big Banana autodeploy
(`Sentinel_34143474dda481919f7fcbd06bfd6feb`). Source branch and missing watch
patterns alone do not establish that autodeploy is currently enabled or that a
merge would trigger a scanner deployment. **Merge/deployment isolation remains
an unresolved configuration check:** verify the current enabled/paused state
and applicable behavior before proceeding. Preserve Owen's pause; never reenable
autodeploy as part of this task. No watch paths or branch protections were changed.

## Proposed remote connection and remaining prerequisites

Deployment authorization was received, but a local listener is not a Chej
connection. Prefer a dedicated `market-mcp-read-only` service in the already
authorized `chej-read-only` Railway project
`ea9f4549-e34f-47cd-a09e-573b24d696a8`, production environment
`2dcc91a4-fafc-408f-bef8-e5ce24b2ec6a`, rather than placing this consumer in a
trading process. The proposed new service has no ID yet and has not been created.
It would use this same repository's specifically reviewed revision, isolated
build/start configuration, one replica, no volume, and fixed public artifact reads.

The existing `chej-read-only` service (`5aa162c3-d2e7-468a-8fd0-a36e441285b2`)
was inspected read-only. It runs `discord_bridge.server`, has an existing OAuth
issuer/JWKS/client/resource/owner configuration and a public TLS domain. It is
not a preexisting market MCP service. Its credentials, revocation state, routes
and grants have not been copied, changed or extended.

The implemented HTTP adapter is stateless Streamable HTTP `2025-11-25`, POST
JSON responses; SSE GET is optional and unsupported. Client connections must
preserve fifteen-minute opaque references and route calls to the one process.
Remote hosting must supply a reviewed TLS endpoint, authentication/resource
audience and allowed-host/origin configuration; the current executable only
binds loopback. Choosing/creating credentials or extending persistent OAuth
access still needs action-time approval. General deployment authorization does
not provide those grants. No unauthenticated remote endpoint is opened.

Costs: no new hosting has run. Current [Railway resource pricing](https://docs.railway.com/pricing/plans)
lists RAM at $10/GB-month, CPU at $20/vCPU-month and egress at $0.05/GB; builds
are free. For example, 0.125 GB steady memory is about $1.25/month before CPU
and egress. Actual memory/CPU/traffic and remaining plan credit are unverified;
this is an estimate, not a quote or zero-cost assurance. The original no-cost
boundary means a runtime usage budget must be authorized before provisioning.

Before deployment:

1. Obtain the repository's required approval/checks for the exact revision;
   verify OA-203's current autodeploy enabled/paused state and deployment
   isolation without bypassing branch protection or changing trading services.
   Preserve Owen's existing autodeploy pause; do not reenable it.
2. Confirm the proposed dedicated service/project and approve a runtime budget.
3. Choose and approve the TLS/auth/client-access route at action time. Implement
   and independently test only the corresponding production listener/gateway
   configuration; do not reuse existing bridge secrets without authorization.
4. Build/deploy the reviewed isolated service. Observe its exact deployment
   success and verify no scanner/collector/trading deployment was triggered.
5. From Chej's actual MCP client, initialize, list tools, discover, run the
   three example queries and follow one reference. Verify unauthorized requests
   fail and source limitations/status/times survive the remote path.

Rollback: remove the new client connection and disable/remove only the new
market service (or restore its prior tested image). References are ephemeral;
restart invalidates them and clients repeat their query. No producer artifacts
or private records are written, so there is no data migration to reverse.
Access/grant revocation must follow the separately approved auth mechanism.

No deployed/connected MCP is claimed. Existing account/trading services,
credentials, grants and security settings remain outside this patch.
