# Credit tape (OA-242) — release handoff for the isolated DTCC service

Steps and variable names only; no credentials. Owen and Chej own deployment and
provider settings. Nothing here has been deployed. Fresh SEC proof is separate.

## What runs

One process, `python -u -m market_data.credit_tape.service` (the image `CMD`):

| Part | Behaviour |
|---|---|
| Collector thread | DTCC slice-index route only (`capture.run(..., ticker=False, slices=True)`): `CFTC_CR` and `SEC_CR` slices from the public `Slice.json`. Ticker output is off; nothing with source `*-credits` is written. Replay slices drain on a separate backfill thread. |
| Panel / API / raw / export | `serve_private` handler, GitHub identity-only sign-in (PKCE, `scope=""`), numeric-ID allowlist, opaque 8-hour sessions, exact Host check. `/panel.js`, `/panel.css`, `/api/trades`, `/api/raw`, `/api/export` all require an allowed session; without one, `/` shows only the sign-in link. The sign-in flow follows directional-move-lab `scripts/serve_private.py@2bdebbe` (see **Sign-in flow** below). |
| Unauthenticated | `/healthz` (process up and configured) and `/healthz/collector` (per-channel status). Status, ages and counts only; never trade content; any Host. |
| Storage | `CREDIT_TAPE_ARCHIVE`, a directory **inside** the mounted volume named by `CREDIT_TAPE_VOLUME` (or Railway's `RAILWAY_VOLUME_MOUNT_PATH`). It holds raw ZIPs, commit records, done markers, `events.jsonl`, `alerts_outbox.jsonl`, `health.json`, and the one-time identity file `archive.json` (`archive_id`). |

Fails closed: exits 2 without starting, and without writing anything, in any of these cases:
- any gate variable is missing or invalid;
- `CREDIT_TAPE_DEV_LOCAL` is set;
- the interval is outside 5..300 s;
- **the persistent volume is absent.** No volume is named; the named path does not exist; it is not a mount point; or `CREDIT_TAPE_ARCHIVE` is outside it, or is the volume root itself. The service never falls back to the container's temporary filesystem. It creates `archive.json` once, on first start, and logs the `archive_id` on every start.

It exits 1
if the collector thread dies **or the replay (backfill) worker fails**: the
worker's error is written to `health.json` (`backfill_worker.state = "failed"`),
`/healthz/collector` turns 503, and the collector stops, so the platform restarts
the service. SIGTERM finishes the current pass, then exits 0. Publishing is
crash-safe, so a hard kill is also safe.

Raw originals: a slice is complete only when its original ZIP is on the volume
and matches the sha256 recorded at publish time. If a restart finds a commit whose
ZIP is missing or hash-invalid, the slice stays **pending** (not complete) and is
re-downloaded; it completes only when the downloaded bytes match the committed
sha256. Different bytes keep it pending with `last_error` and backoff
(`max_attempts` rises in `/healthz/collector`).

### Sign-in flow

Reference: directional-move-lab `scripts/serve_private.py@2bdebbe`. Its flow handling
is reused; its credentials, cookies and sessions are not.

- `/auth/login` allocates no server-side state. The flow is a signed, HttpOnly,
  10-minute cookie, and a repeated login reuses a live flow without extending it.
  The login returns 429 if a flow cannot be issued.
- The callback accepts GitHub's documented `?code=…&state=…`. If GitHub also sends
  RFC 9207 `iss`, it must be exactly `https://github.com/login/oauth`. Blank,
  repeated or unknown fields are refused.
- A callback first claims the flow as pending (at most 16 at once). **Failure
  cleanup:** a bad state, a failed code exchange or a non-allowlisted account all
  release the claim and clear the flow cookie.
- **One-time replay protection:** only a verified, allowlisted callback records the
  flow as spent (at most 128, pruned when the flow expires). The same flow can never
  sign in twice, and failed callbacks cannot use up sign-in capacity.
- Logout is POST with an exact `Origin`. It revokes the session and clears both cookies.

## Build

- Build context: the repository root. Dockerfile: `deployment/credit_tape/Dockerfile`, with `deployment/credit_tape/Dockerfile.dockerignore`.
- The image holds only `market_data/__init__.py` and `market_data/credit_tape/`, on the Python 3.12 standard library. There is no `pip install`.
- It shares nothing with `deployment/python/Dockerfile`, the image used by the options producers.

```sh
docker build -f deployment/credit_tape/Dockerfile -t credit-tape .
```

## Service settings (to be applied by Owen/Chej)

| Setting | Value |
|---|---|
| Service | New, separate service, e.g. `credit-tape`. No shared or reference variables with other services. |
| Source | `OwenTanzer/live-options-view`, the commit in the handoff email (or `main` after merge). |
| Builder | Dockerfile, at `deployment/credit_tape/Dockerfile`. Leave the start command empty so the image `CMD` runs. |
| Watch paths | `market_data/credit_tape/**`, `deployment/credit_tape/**` |
| Replicas | 1. There is a single collector, and the volume allows one writer. |
| Volume | A new private volume mounted at `/data`, attached to this service only. |
| Healthcheck path | `/healthz` |
| Restart policy | On failure |
| Networking | One public HTTPS domain; this becomes the origin. The app listens on `PORT`. |

Railway's volume docs say a service with a volume runs no replicas, and that a
redeploy has brief downtime so two deployments never share the volume. Please
confirm this in the console; the archive design assumes a single writer.

## Variables (names only)

| Variable | Value |
|---|---|
| `CREDIT_TAPE_VOLUME` | `/data`: the volume's mount path. It may be omitted if Railway sets `RAILWAY_VOLUME_MOUNT_PATH` for the attached volume; the service uses that instead. |
| `CREDIT_TAPE_ARCHIVE` | `/data/credit-tape` (absolute, inside the volume) |
| `CREDIT_TAPE_PUBLIC_ORIGIN` | `https://<service domain>`: exact origin, no port or path |
| `CREDIT_TAPE_GITHUB_CLIENT_ID` | The client ID of a **new** GitHub OAuth app used only for this service |
| `CREDIT_TAPE_GITHUB_CLIENT_SECRET` | That app's secret, stored as a sealed or secret variable |
| `CREDIT_TAPE_GITHUB_ALLOWED_IDS` | Comma-separated numeric GitHub user IDs (not logins) |
| `CREDIT_TAPE_INTERVAL` | Optional, default `20` (seconds; 5..300) |
| `PORT` | Set by the platform |
| `CREDIT_TAPE_DEV_LOCAL` | **Must be unset.** The service refuses to start if it is set. |

Set up the GitHub OAuth app with homepage `<origin>` and callback
`<origin>/auth/callback`, and request no scopes. Do not reuse the
directional-move-lab app, cookies or sessions.

## Verify after deploy

Let `ORIGIN` be the public origin.

1. **Start-up.** The log line should read
   `credit-tape service: slice-only collector every 20s, archive=/data/credit-tape, panel on :<PORT> for <ORIGIN> (<n> allowed ids)`.
   If it reads `not started: configuration incomplete or invalid (<NAME>)`, fix `<NAME>`.
2. **Gate.** Each of these must return `401` without a session:
   ```sh
   for p in /panel.js /api/trades "/api/raw?ref=dtcc-slices/raw/2026-01-01/X.zip" \
            "/api/export?from=2026-01-01T00:00:00&to=2026-01-02T00:00:00"; do
     curl -s -o /dev/null -w "%{http_code} $p\n" "$ORIGIN$p"; done
   ```
   `/` returns the sign-in page only. Then check sign-in:
   - signing in with an allowlisted GitHub account gives the panel;
   - any other account gets `403 Sign-in was not accepted.`
3. **Health for both channels.** The first answer can take one interval plus one index fetch.
   ```sh
   curl -s "$ORIGIN/healthz/collector"   # 200 when ok, 503 otherwise
   ```
   - `ok` is true only if all of these hold:
     - the collector heartbeat is younger than max(3×interval, 120 s);
     - `backfill_worker` is not `"failed"`;
     - the last index answer (200 or 304) is under 10 min old;
     - both `CFTC_CR` and `SEC_CR` have `state: "ok"`.
   - The per-channel states are:
     - `ok`;
     - `missing_from_index`: the channel was absent from the last full index;
     - `live_backlog`: a live slice has been pending for 15 min or more.
   - `slice_only` must be `true`.
   - The per-channel fields `pending_live`, `pending_backfill`, `max_attempts`, `last_live_dissem_age_s`, `last_live_completed_age_s` and `last_backfill_completed_age_s` are information only. A quiet market is not a failure.
   - After first start, `pending_backfill` counts the replay of the rolling ~24 h index and drains on its own thread. It does not block live slices.
   - Point an uptime monitor at `/healthz/collector`.
   - Keep the platform healthcheck on `/healthz`, so a quiet start does not fail the deploy.
4. **Panel.** Signed in, new rows show with a source of `CFTC-SLICES` or `SEC-SLICES` and `slice-index`. Each row's link downloads its original ZIP, with the hash in the `X-Original-SHA256` header.

## Restart verification

Run the read-only checker inside the service, e.g. via `railway ssh`, then from `/app`. `--volume` defaults to `CREDIT_TAPE_VOLUME` or `RAILWAY_VOLUME_MOUNT_PATH`.

```sh
python -m market_data.credit_tape.check_archive --archive "$CREDIT_TAPE_ARCHIVE" --volume /data > /data/check-before.json
# restart or redeploy the service, wait two intervals, confirm /healthz/collector is 200
python -m market_data.credit_tape.check_archive --archive "$CREDIT_TAPE_ARCHIVE" --volume /data --compare /data/check-before.json
```

It proves it read the actual persistent archive, then re-verifies it.

**It exits 0 only if:**

- **Same persistent archive**
  - `--volume` exists, is a mount point, and contains the archive.
  - The archive exists, is a credit-tape archive, and has its `archive.json` identity.
  - With `--compare`, the `archive_id` is identical before and after. A restart that came up on a fresh or temporary directory fails here.
- **Every completed commit validates.** The checker re-checks each one against:
  - **its original ZIP:** present and byte-identical (sha256) to the bytes committed;
  - **its log lines:** every committed event and alert line is present and identical, in the log the commit names;
  - **the ZIP content:** the ZIP is re-decoded, and its rows must match the committed publications (count, row order, record, `Dissemination Identifier`) and the slice event's `decoded_rows`. A recorded **decode failure** must still fail to decode, and its original and its event/alert lines are checked the same way. Rejected slices have no original, so only their lines are checked.
  - Problems are listed per commit, for example `original_missing`, `original_hash_mismatch`, `committed_event_line_missing`, `committed_alert_line_missing`, `committed_event_line_differs`, `publication_differs_from_original_row`.
- **No partial or duplicate output**
  - No logged line points to a missing commit record, and no `(commit_key, commit_line)` is duplicated.
  - There are no torn or unreadable lines.
  - Every commit has its done marker; a commit still waiting for its original fails.
  - There are no `.part` leftovers.
- **No ticker output of any kind:** no ticker event and no ticker directory.
- **With `--compare`:** no channel's publication count fell, and the number of completed commits did not fall.

## Rollback

**First release.** There is no earlier runnable image of this service. `ce25cd7`
and older commits have no service entrypoint or Dockerfile. So rollback is:

1. Stop the `credit-tape` deployment.
2. **Keep the volume** attached and intact. It is the archive (`archive.json`, raw
   ZIPs, commit records, done markers, logs). Do not delete the service or the
   volume as part of rollback.
3. Do not redeploy an older commit.

Restarting the same release later on the same volume resumes safely. Publishing is
idempotent by `source/channel/sliceId`: completed slices are not republished, and
open commits are finished or kept pending.

**Later releases.** Once a known-good release of this service exists, roll back by
redeploying that release's commit in the same service on the same volume, then run
the restart verification above.

**Either way:**

- To inspect the archive with the service down, attach the volume to a temporary
  service and run `check_archive`. Alternatively, run the panel locally on a copy:
  `CREDIT_TAPE_ARCHIVE=<copy> CREDIT_TAPE_DEV_LOCAL=1 python -m market_data.credit_tape.serve_private`.
- The existing options collector is not touched:
  - it uses a separate image (`deployment/python/Dockerfile`) and separate services and variables;
  - no R2, KV, D1 or Worker writes happen anywhere in this service;
  - `tests/changed_paths.py` classifies `market_data/credit_tape/*` as an image input of the producer image, but not as a watch path or runtime code of `main-collector`, `tradier-collector` or `banana-options-scanner`, so no options service restarts or promotes on these files.

## Evidence: kept separate

| Kind | What it shows | How |
|---|---|---|
| **Offline synthetic regression** (no network, synthetic fixtures) | Gate, sign-in flow, slice-only run, per-channel health, raw-pending rule, archive checker, worker failure, crash and restart safety | `python -m pytest tests/test_credit_tape.py`; `python -m market_data.credit_tape.synthetic_demo`, `synthetic_crash`, `synthetic_resilience`; `python tests/test_changed_paths.py` |
| **Public-source local rehearsal** (live public DTCC index, local only, not deployed) | The service runs end to end against the real `Slice.json` and slice files; restart after a hard kill is clean | The commands below. Not part of the regression results; archives stay outside the repo |
| Fresh SEC proof / full-coverage evidence | Out of scope here | Reported separately |

## Public-source local rehearsal (no deploy)

The service refuses a plain directory, so a rehearsal needs a real mount, e.g. a
named Docker volume. It uses the live public DTCC index; run the offline synthetic
regression separately (see above).

```sh
docker build -f deployment/credit_tape/Dockerfile -t credit-tape .
docker run --rm -p 8791:8791 -v credit-tape-rehearsal:/data \
  -e CREDIT_TAPE_VOLUME=/data -e CREDIT_TAPE_ARCHIVE=/data/credit-tape -e PORT=8791 \
  -e CREDIT_TAPE_PUBLIC_ORIGIN=https://credit-tape.example.test \
  -e CREDIT_TAPE_GITHUB_CLIENT_ID=placeholder -e CREDIT_TAPE_GITHUB_CLIENT_SECRET=placeholder \
  -e CREDIT_TAPE_GITHUB_ALLOWED_IDS=1 credit-tape
curl -s localhost:8791/healthz/collector
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: credit-tape.example.test" localhost:8791/api/trades   # 401
```
