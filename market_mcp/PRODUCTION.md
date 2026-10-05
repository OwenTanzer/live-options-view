# Market MCP production followup: original local review record

Current verification and provisioned-service state: [ROLLOUT.md](ROLLOUT.md).
The record below is the preserved pre-publication handoff; its pending-state
statements are historical where superseded by that continuation.

This uncommitted followup is based on PR #119 head
`6dbb2bdee174ddfbf4408a1d90e0e8f55a85bf16`. It adds a production HTTP resource
server and a review-only deployment plan. No followup publication, image upload,
service creation, deployment, OAuth registration or access grant has occurred.
The first-slice history in [READINESS.md](READINESS.md) remains historical.

Published PR #119 and its separate merge-readiness review retain their own scope.
The published PR's CI result does not validate this local followup. Before
publishing the followup, compare it with the final approved PR #119 head and
rebase if that review changes the base, preserving its accepted fixes. This
record makes no merge-readiness decision for PR #119.

## Runtime and verification

The dedicated [Dockerfile](Dockerfile) uses Node 22, a non-root user and a 256 MB
V8 old-space limit. Its explicit copies and Dockerfile-specific context allowlist
include only six MCP runtime modules, `docs/shared.js` and the squeeze calendar.
No scanner, Worker, Discord code, test signing keys, credentials or fixture
artifacts enter the image. Build from the repository root:

```sh
node --test market_mcp/*.test.js
node market_mcp/production-smoke.js --out production-smoke.json
node --max-old-space-size=256 market_mcp/load.js --out production-load.json
docker build -f market_mcp/Dockerfile -t market-mcp-check .
```

Node 22.23.3 was tested locally on Windows. Docker/Podman is unavailable here;
the Linux image has **not** been built or run. The local CI change supplies a
Linux build and an offline image import/startup check, but has not been published
or executed remotely. Container build/run is still a release gate.

Deploying a separately approved immutable OCI image would avoid the repository's
root Python Dockerfile and `railway.toml`, and would attach no GitHub autodeploy
source. The exact registry, image digest and publication are pending. No new
legacy Config as Code file is proposed: Railway's current documentation marks
that mechanism deprecated. [Railway Config as Code](https://docs.railway.com/config-as-code/reference).

## HTTP behavior and resource bounds

`production.js` binds `0.0.0.0:$PORT`; its explicit resource URL must be HTTPS
with path `/mcp`. TLS is supplied by the approved platform edge. Do not expose a
public plaintext TCP proxy. Host and browser Origin use exact configured
allowlists; forwarded headers do not confer authority. Empty
`MCP_ALLOWED_ORIGINS` explicitly denies browser origins.

POST `/mcp` implements stateless Streamable HTTP protocol `2025-11-25`, JSON
responses and notification HTTP 202. GET/DELETE `/mcp` return 405 after
authentication; there is no SSE stream or transport session. Public GET
`/healthz` returns only readiness and becomes unavailable when signing keys
expire. Public protected-resource metadata is available at
`/.well-known/oauth-protected-resource/mcp` and the root alias. The Railway
health-check host may be allowed on `/healthz` alone.

| Bound | Behavior |
| --- | --- |
| Active requests / sockets | Four / 32; excess active requests return 429. |
| Headers / request body | 16 KiB each; header/body deadlines five seconds. |
| HTTP deadline / response | 25 seconds / 768 KiB including duplicated MCP content. |
| Tool result | 256 KiB before text/structured duplication. |
| Pilot quota | 120 MCP requests/minute and 500 authenticated requests/UTC day; 429 includes Retry-After. |
| Retained references | 64 and 16 MiB of serialized data, fifteen-minute TTL; count/byte eviction and periodic pruning. |
| Upstream artifacts | Fixed public origin, four MiB raw / 24 MiB expanded; 64 MiB and twelve artifact reads per query, fifteen-second source deadline. |
| JSONL parsing | At most 100,000 physical lines, 10,000 retained manifest records; 256 KiB per manifest line. Quote lines at most 16 KiB; retain at most 50 matching quote rows per sweep, six sweeps per page. Unrelated quote rows are discarded during incremental parsing. |
| CSV parsing | At most 10,000 physical records, 128 columns and 4,096 characters/cell. |
| Logging | Strict event/status/duration/port fields only; at most 60 entries/minute plus a suppressed-count summary. No headers, tokens, claims, URLs, bodies, producer payloads or exception text. |

Quotas and references are process-local and reset on restart. They are **not a
durable spending cutoff**. Serialized retention is a bound on serialized data,
not a claim that JavaScript heap/RSS equals that size. Snapshot references retain
only the queried row and readings. SIGTERM/SIGINT reject new work, drain existing
requests for up to 25 seconds, close sockets and clear references. Configure at
least 30 seconds of platform draining. Restart loses references; clients repeat
the originating query. One always-on replica and zero deployment overlap avoid
cross-process references. Service sleep after ten idle minutes would lose all retained references before their maximum fifteen-minute lifetime.
Count/byte eviction can also remove references sooner.

Incremental JSONL parsing repairs an independently reproduced heap exhaustion:
a 24 MiB expanded gzip full of blank lines now returns partial detail with
`excessive_response`, and the next query remains usable. This is covered by a
separate Node process under the 256 MB old-space limit.

## Authentication contract

The resource server uses only public discovery from the fixed issuer
`https://dev-oraxxi11mrzuff2h.us.auth0.com/` and its fixed
`/.well-known/jwks.json`. Public issuer metadata advertises authorization code
and S256 PKCE; that discovery is not a grant or a tested live login.

Access tokens must have an RS256 signature from a bounded validated RSA key set,
the exact issuer, the exact approved market resource audience, `market:read`, the
verified owner subject and an approved dedicated public client `azp`. An optional
second audience can only be the issuer's `/userinfo`. Time claims are checked
again after asynchronous key refresh, and token lifetime is at most one hour.
Token-supplied key URLs, other signing algorithms, foreign audiences, unapproved
owners/clients and machine/password grants fail closed. Discord tokens, clients
and grants are not reused.

Signing-key cache is bounded to sixteen keys / 64 KiB, refreshed with a
three-second deadline, five-minute expiry, single-flight requests and a
thirty-second cooldown. Expired-key outages return 503 and fail readiness.
There are no client-selected URLs, redirects or private credential reads.
The server does not store tokens, exchange authorization codes or pass tokens
to producers. Offline JWT validation has no immediate per-token revocation;
an urgent stop must stop this resource service, and tokens expire within one hour.

The local tests include a mock authorization-code/S256 PKCE exchange, wrong
verifier and repeated-code denial, then a real authenticated HTTP MCP flow.
The inspectable smoke performs initialization, notification, tool listing and
seven discovery/query/detail calls. Ephemeral test keys are local only. Injected
fixture evidence has `source_mode: injected_test_artifacts` and null public
links; its unchanged October 2 timestamps are stale at current retrieval.
These checks do not establish a live Chej connection or live Auth0 grant.

The live client must request the exact resource in both authorization and token
requests, and the Auth0 integration must select the same API via `audience`.
Actual Chej support for this mapping must be verified before declaring it usable.
Do not change the tenant default audience to make an unverified client work.
[MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
[Auth0 code flow with PKCE](https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce),
[Auth0 API audience](https://auth0.com/docs/secure/tokens/access-tokens/get-access-tokens).

## Exact future configuration and approvals

[deployment-plan.json](deployment-plan.json) is review-only and has not been
applied. The deployment scope approved in the parent thread is a new isolated
market service with at most **US$10/month additional recurring pilot spend**.
An exact access grant still requires separate approval at action time.

1. Reconcile the base with the final approved PR #119 head after its separate
   merge-readiness review. Review the final local patch and approve followup code/image publication.
   Build and run the Linux image before selecting its immutable digest.
2. Create only a new `market-mcp-read-only` service in project
   `ea9f4549-e34f-47cd-a09e-573b24d696a8`, production environment
   `2dcc91a4-fafc-408f-bef8-e5ce24b2ec6a`. Never replace or alter Discord service
   `5aa162c3-d2e7-468a-8fd0-a36e441285b2`.
3. Request one `us-west2` replica, sleep disabled, no cron or volume, 0.5 GB RAM
   and 0.2 vCPU, three failure restarts, overlap zero, drain thirty seconds,
   `/healthz` with a sixty-second health-check timeout. Platform acceptance and
   readback of fractional limits must be verified before spending.
4. Approve and verify the exact new HTTPS domain and `/mcp` resource. With
   separate action-time approval, register a market-only RS256 API for that
   exact audience and only `market:read`, with access tokens no longer than one
   hour. Register a dedicated approved public client with authorization code
   and S256 PKCE, the actual Chej redirect URI and owner-only access. Do not add
   machine, password or implicit grants or reuse Discord registration.
5. Verify actual Chej `resource`/Auth0 `audience` mapping, S256 exchange and
   challenge metadata before accepting the connector. The new service ID,
   domain, image digest, client ID, exact callback and verified owner subject
   remain unknown; do not infer them from old clients or fixtures.
6. Set the explicit environment values in the plan. Keep Origin allowlist empty
   unless an exact approved browser client origin is needed. No secrets are
   required by the resource server.
7. After the separate approvals, test edge TLS, owner/client/audience/scope
   denials, readiness and the full live client flow. Monitor only the new
   service's cost and stop its pilot before exceeding US$10/month. Do not change
   the workspace spending cutoff. Preserve Big Banana's paused autodeploy and
   verify merge/deployment isolation; its branch/watch settings alone do not
   establish the enabled state.

At the documented usage rates, continuously reaching the proposed memory/CPU
caps would nominally contribute about US$5 RAM plus US$4 CPU/month before
egress. [Railway resource usage pricing](https://docs.railway.com/pricing/plans#resource-usage-pricing).
This is not a price guarantee or a measured forecast, and the platform
limits have not been applied or verified. The synthetic local load fills 64
references with four concurrent reads and reports latency, CPU, heap and RSS;
it does not model worst-case artifacts or Linux/Railway usage.

Rollback is scoped to disconnecting the new market client and stopping the new
service; revoke a market grant only when separately authorized. Producer,
Discord, trading, account and existing hosting state remain outside this patch.
