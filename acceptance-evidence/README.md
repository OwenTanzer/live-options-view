# Options View acceptance — Steve, November 8, 2026

Source revision: `62ae9ffc36b2af8707e6709ca816bb1baf9d81df`. This branch stores evidence only; it is not a runtime release or a PR awaiting merge.

## A1 deployment and behavior

- Collector: running deployment `aa33dae0-1cc5-4d37-9609-785f03881db5`, revision `1104e64809d2161158baae8eba2a73026b61084d` (#142), image `sha256:5a900b80f55dad179507d85856bc8cb59d63f6dee2e18b3d9f1671464251b588`. A later failed attempt did not replace it. Runtime logs show outside-session sleep, not a post-fix session.
- Scanner: SUCCESS build-only cron deployment `f99f6c45-60f4-4b81-8150-51c18115e909`, source `62ae9ff`, image `sha256:bbb1350654b435830430a9bae747a377af62db0396a5e2c26cbf48c22a39be2c`. Existing schedule, /data volume and scanner command preserved; next cron November 9 12:45 UTC. Active snapshot publication stays disabled. No live run is claimed.
- Market MCP: SUCCESS deployment `05766984-efad-41c8-81b3-06ea5bb4b77c`, image `ghcr.io/owentanzer/market-mcp-read-only@sha256:a649beb7a23c0f2698f8a7bb89edf58d07f5c1cb4bc7692896dfebdd3cb3c851`, source `62ae9ff`. `/healthz` 200; unauthenticated `/mcp` 401. Owner restriction and resource limits unchanged.
- Local host tests 134/134; established image verification 80/80 plus non-root/entrypoint/authentication fixtures/load/shutdown. These do not substitute for authenticated deployed acceptance.
- `public-acceptance.json`: actual public artifacts read through a local stdio MCP server at the stated revision. Missing publication is a non-error answer, stale snapshot/detail fields remain aged, November 6 final coverage retains one truncated sweep, and November 6 failed squeeze attempt remains separate from October 16 success. `run=latest_attempt` reconciles zero failed-run rows; last success remains available.
- Authenticated ChatGPT connector calls remain blocked. Concurrent A0 issue [OA-232](https://linear.app/objecta/issue/OA-232) records the owner-only reconnect error and Auth0 authentication-factor step for Owen. Do not repeat reconnect prompts.
- Earliest successive post-fix collector sessions: November 9 and 10. Scanner market-session and degradation/cadence observations remain separate from its image deployment. Active-cadence acceptance needs the existing activation-policy decision; no producer was enabled for testing.

## A2 desktop navigation — complete

MSI through Remote Desktop Commander / UFO2, dedicated Chrome window. Served build `62ae9ff-20261108175920`.
1. Opened live Options View and clicked the unique Move analysis hyperlink.
2. Observed a new tab at `https://directional-analysis-production.up.railway.app/`, title Sign in, Private research workbench / Sign in with GitHub.
3. Closed only the new tab; returned to Options View.
4. Clicked Historical, observed Historical OI/session data and persistent Move analysis link; clicked Live, observed QQQ live panel and session-closed wording.
5. Saved screenshots and UI text; closed only the owned verification window. No research-app authentication claimed.

## Scope

Nova's PRs and working trees untouched. No runtime source repair was necessary. The scanner config-file binding request was rejected by Railway before change; the supported explicit Dockerfile path and existing start command were used instead. Full details and continuation ownership are in the [shared Log](https://app.notion.com/p/4123c519147b811a876bee52759dccfc).

## A3 full research replay — complete

On mains-powered MSI, Python 3.14.4 / NumPy 2.4.5 / pandas 3.0.6 / SciPy 1.18.1. Freshly fetched four QQQ source files matched frozen hashes. From `analysis/brian-r2-20261024`, executed `C:\Python314\python.exe work/round3/reproduce.py run` then `C:\Python314\python.exe work/round3/reproduce.py compare`. Both exited 0. Start 2026-11-08 18:41:49 UTC; end 18:58:09 UTC; launch-to-exit 983.57 seconds.

Primary 10,717 rows / 168 sessions; delayed 10,549 rows / 168 sessions. Training-maturity and prefix checks passed. All 13 published CSV tables match exact bytes after canonical LF normalization: calibration, clipping-audit, daily-losses, episode-concentration, fit-ledger, floor-events, monthly, paired-contrasts, scores, state-exposure, support-summary, timing-examples, truncation-checks. `A3-reproduction.json` holds every table hash and both replay preflight/consumption records. Raw sources and per-origin predictions stay in the ignored private replay directory; published numerical originals are unchanged. The compare command only refreshed the receipt in the isolated source checkout. This is full numerical reproduction, not an endorsement of interpretation or tradability. No replay remains running.
