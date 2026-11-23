# First real-archive window publication plan — #130 / F2 (bounded, unapproved)

Status: PLAN ONLY. No publication, no activation, no live R2 write,
no deployment has happened under this plan. Chej owns review/release and
real publication: Chej states tonight's reviewed release/activation is
already authorized and he owns release and real publication (Chej's
statement, recorded here as his; not Owen-verified in this chat). This
document is the preparation Chej asked for; every step below that touches
R2, a flag, or a deployment is Chej's release step, not Nova's, and must
not be run until the technical gates in "Pre-checks" pass. The
budget/retention numbers remain unapproved proposals.

Reviewed source for the code this plan publishes with:
`c8fcf529698bc57b930495f8c0272b5c1b16b2f4` on branch
`nova/f1-f2-renaissance` (base `014b48ad`), draft PR #204.
Image workflow: `.github/workflows/publish-market-mcp-renaissance.yml`
(pinned to exactly that SHA; see "Release-path return sheet").

## 0. What this plan corrects about the preserved #130 artifacts

The preserved #130 checkpoint
(`C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130`,
head `1f165d68`, clean) holds:

- `replay-cache/oa203/scanner/` — the read-through cache of the public
  OA-203 source blobs (237 blobs, per `full_universe_summary.json`).
- `replay-130-full/batch-01/` … `batch-14/` — 14 bounded batch replays
  (≤50 underlyings each, disjoint union = planned 661 underlyings).
  Each batch dir holds `replay_summary.json`, `source_lock.json` and
  the five ranked CSVs for its own subset. The locks are identical
  across batches (verified by `combine-full-universe.py`).
- `replay-130-full-combined/` — the combined ranked outputs:
  `full_universe_summary.json` (sha256
  `c70253f79aacd5e6e54ad63b3241506cc1370f8debce63d7574489939ba1fb6e`),
  `conflicts.csv` (sha256
  `c5942a099b42d5400e9a5d82030bc9016dca65a83158c9bdc2c7d7d2824ba89d`,
  54 conflicting symbols / 540 rows, excluded fail-closed),
  and five combined ranked CSVs with per-file sha256 in
  `full_universe_summary.json` (e.g. `overnight-2026-10-05_2026-10-06.csv`
  sha256 `0500e7d439044dfd710bc119657fb7e3bb662db9a22ce1a351f16b1afe24fa36`).

Two corrections to earlier notes:

1. The combined dir holds MORE than `conflicts.csv` +
   `full_universe_summary.json`: it holds the five combined ranked CSVs
   above. Even so, it holds NO `oa203/windows/` tree, and the ranked
   CSVs are qualified-basis rows only — they are not a publishable
   window (no `paths.csv.gz` full universe, no per-basis mid/exec
   evidence, no `summary.json`). The full-universe window (71,710
   contracts) therefore CANNOT be published from the preserved CSVs
   directly. A publishable emit must be rebuilt by re-running the real
   producer over the preserved cache (step 2). A full-universe combined
   emit would additionally need a combine-emit step that merges the 14
   batch emits with the same conflict semantics as
   `combine-full-universe.py`; that step now exists as `windows_combine.combine_emit` and requires the explicit `--coverage` flag (CombineCoverage: expected underlyings, batch names, source locks, scope) -- a combine without it fails closed. It is NOT
   part of this first publication. The first publication is one batch's
   emit (step 1), which is executable today with existing code.
2. A replay-built emit is NEVER `final`: `windows_outputs.write_window`
   forces state `partial` with reason `bounded_universe_replay` for
   `mode == "offline_replay"`, even when every session is final and
   complete. The first published window is therefore a genuine
   `partial` window, and the consumer must serve it as `partial`
   (rows + reasons), never as `available`.

## 1. The one window: batch-01 overnight, 2026-10-05 → 2026-10-06

- Kind: `overnight`
- Window id: `overnight-2026-10-05_2026-10-06`
- Bases: mid, exec, qualified (all three; the publisher validates all
  three and the consumer serves all three).
- Source: batch-01 of the preserved replay only (50 underlyings,
  `AA, AAL, AAOI, AAOX, AAPL, ABBV, ABNB, ABR, …` — the exact 50 are
  read from `replay-130-full/batch-01/replay_summary.json`, never
  retyped). No other batch, no other window, no other day.
- Sessions: 2026-10-05 (77 sweeps, 365,408 rows kept, final,
  assessment `complete`), 2026-10-06 (77 sweeps, 355,832 rows kept,
  final, assessment `complete`). The replay also loads 2026-10-07
  (same batch) because the batch was built over Oct 5–7; only the
  Oct 5→6 overnight window is published from it.
- Expected state: `partial`, reasons `["bounded_universe_replay"]`
  (see correction 2 above).
- Expected counts (from preserved
  `replay-130-full/batch-01/replay_summary.json`, window
  `overnight-2026-10-05_2026-10-06`):
  - `contracts_evaluated`: 5946
  - mid: eligible 2574, rank 1 `ALAB261009P00210000` pct 70.833333
  - exec: eligible 2574, rank 1 `ALAB261009C00395000` pct 1.383333
  - qualified: eligible 1878, rank 1 `ALAB261009C00410000` pct 2.366667
  - `shortlist` per basis: `min(200, eligible)` (200 for all three).
- Full-universe reference (NOT published here, for the later
  combine-emit step): `overnight-2026-10-05_2026-10-06` over 71,710
  contracts, qualified-eligible 17,488, excluded 54,222, 54
  conflicting symbols, qualified rank 1 `CEG261009C00280000` pct
  15.866667, entry 1.5, exit 25.3 (from `full_universe_summary.json`).

## 2. Builder: re-run the real producer over the preserved cache

Run on the MSI, in the `live-options-view-f1f2` worktree at
`c8fcf529`, from the repo root. Nothing is copied or moved out of the
#130 checkpoint; the cache is read through it and every blob's SHA-256
is verified against the preserved batch-01 lock. The emit goes to a
NEW directory outside the checkpoint.

```powershell
# 1. Read the exact 50 underlyings from the preserved batch summary.
$batch = 'C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130\replay-130-full\batch-01'
$underlyings = (Get-Content "$batch\replay_summary.json" | ConvertFrom-Json).underlyings
$underlyings.Count  # must print 50

# 2. Re-run the real producer (bounded: 3 days, 50 underlyings).
#    --cache-dir reuses the preserved blobs; --source-lock verifies
#    every blob against the preserved lock; --emit-root writes the
#    oa203/windows/ tree. No network write, no R2, no collector.
python market_data/options_scanner/windows_replay.py `
  --days 2026-10-05 2026-10-06 2026-10-07 `
  --underlyings $underlyings `
  --out C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\first-window-batch-01 `
  --emit-root C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\first-window-batch-01-emit `
  --cache-dir 'C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130\replay-cache' `
  --source-lock "$batch\source_lock.json"
```

Acceptance of the builder run (fail closed, all must hold):

- Exit code 0; the run prints `5 windows over 5946 contracts` (5
  windows: 2 overnight + 2 sessions_2 + 1 sessions_3; weekly is
  skipped as an incomplete calendar week).
- `first-window-batch-01/replay_summary.json` window
  `overnight-2026-10-05_2026-10-06` matches the expected counts in
  section 1 exactly (contracts, per-basis eligible, rank-1 symbol/pct).
- `first-window-batch-01/source_lock.json` sources are a subset of
  the preserved batch-01 lock with identical (key, sha256) pairs; any
  blob whose hash disagrees with the lock aborts the run (the replay
  rejects it, it is not trusted).
- Emit exists at
  `first-window-batch-01-emit/oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-05_2026-10-06/{summary.json,rankings.csv,paths.csv.gz}`
  and its `summary.json` reads `state: "partial"`,
  `state_reasons: ["bounded_universe_replay"]`,
  `contracts_evaluated: 5946`.

## 3. Caps (measured, implemented)

Exactly the publisher's numbers (`docs/130-window-publisher.md`),
implemented from measured full-universe evidence: real producer,
14-batch combine, 71,710 contracts/window (2026-11-09) -- summary at
most 55,492 B, rankings at most 65,497 B (shortlist-capped at 600
rows), paths.gz at most 2,436,802 B, paths expanded at most
34,295,267 B. Only the expanded-paths ceiling changed (24 MiB to
48 MiB, 1.46x the measured maximum); the summary/rankings/compressed
caps are NOT raised. The old ~8 MB ranked-CSV figures were old-format
qualified-only CSVs, not new-format rankings members, and do NOT
justify a rankings raise. There is no cap-approval wait left on these
numbers; publication and activation remain Chej's release step.

| Bound | Value |
| --- | --- |
| Global cap | 8 GiB |
| Retention | 90 days (expired revisions, flat members, pointers, ledgers deleted together; deletion re-verified) |
| Reservation per revision | 20 MiB |
| Attempts per revision | 3 (conditional `budget/<rev>-<attempt>.json` tokens) |
| Revisions per window | 16 |
| Per-member bytes | summary ≤ 1 MiB, rankings ≤ 4 MiB, paths ≤ 4 MiB compressed / 48 MiB expanded (measured max 34,295,267 B) |
| Inventory | 1,000 keys per kind prefix, 32,000 total; truncated listings and unknown keys fail closed |

The byte cap binds before retention for full-universe windows near the
ceilings; exhaustion refuses new publications, never silently deletes
live data. There is no legacy data under `oa203/windows/`, so no
policy-start floor is needed.

## 4. Pre-checks, in order (OA203-style; all must pass before any R2 write)

1. Regression tests green on the MSI at `c8fcf529`:
   `python -m pytest tests/test_oa203_windows_publish.py tests/test_oa203_windows.py tests/test_oa203_windows_replay.py tests/test_oa203_windows_parity.py tests/test_oa203_windows_concurrency.py -q`
   (expected: 73 passed, 15 subtests passed) and
   `node --test market_mcp/*.test.js` (expected: 171 pass, 0 fail,
   1 skipped — the manifest-gated parity test).
2. Bounded builder (section 2) run and accepted against section 1's
   expected counts.
3. Local-backend publish first (section 5, step A) and local-consumer
   readback: `window_rankings` for all three bases plus pinned
   `result_detail` over the local tree, rows/counts/detail equal to
   the producer's emit exactly.
4. Trial-window R2 conditional reservation / readback / delete
   (Chej's release step): reserve a trial window's budget token with
   `IfNoneMatch: *`, verify the readback digests, delete the trial
   keys and re-verify the listing no longer holds them — including a
   two-publisher race against the trial budget, to verify the listing
   reflects a just-created token (the reserve-then-verify fix assumes
   this; it is proven offline only).
5. Verified deployed revision: the deployed market_mcp image digest
   is the digest published by the Renaissance workflow for exactly
   `c8fcf529` (see "Release-path return sheet"); deploy by digest,
   never by tag.
6. Saved activation flag naming exactly this one window
   (`overnight/overnight-2026-10-05_2026-10-06`, batch-01 emit) and
   the caps in section 3. No flag, no publication.

## 5. Publisher commands

`windows_publish.py` has no CLI; the exact invocation is a short
Python driver. Run it on the MSI from the repo root at `c8fcf529`.

### Step A — local backend first (always; proves the publish before R2)

```python
from pathlib import Path
from market_data.options_scanner import windows_publish as pub

emit = Path(r"C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\first-window-batch-01-emit")
store = Path(r"C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\first-window-batch-01-store")

# Dry run first: validates everything, uploads nothing.
print(pub.publish_window(emit, "overnight", "overnight-2026-10-05_2026-10-06",
                         pub.LocalFilesystemBackend(store), dry_run=True))
# Then the real local publish.
result = pub.publish_window(emit, "overnight", "overnight-2026-10-05_2026-10-06",
                            pub.LocalFilesystemBackend(store))
print(result["status"], result["state"], result["revision"])
```

Expected: dry run reports the validation result with no files
written; the publish returns `status: "published"`,
`state: "partial"`, and a `revision` of the form
`rev_<sha256(summary.json)>`. The store then holds
`oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-05_2026-10-06/`
with the three flat members, the `rev_*/` members, `latest.json`
(whose member digests match the emit's files exactly) and
`latest-attempt.json`.

### Step B — R2 backend (Chej's release step ONLY, after pre-checks 1–6)

Same driver, with the backend swapped — nothing else changes:

```python
from market_data.options_scanner import windows_publish as pub
result = pub.publish_window(emit, "overnight", "overnight-2026-10-05_2026-10-06",
                            pub.r2_backend())  # reuses the scanner's existing upload client; no new credentials
print(result["status"], result["state"], result["revision"])
```

`r2_backend()` is never used in tests and must not be called before
pre-checks 1–6 pass. Release/activation authority is Chej's (he states tonight's reviewed release/activation is already authorized); the pre-checks are technical gates, not an approval wait. A validation refusal
uploads nothing at all, including no ledger entry; a readback digest
mismatch fails closed with no pointer written.

## 6. Verification (live, after step B)

All reads below are against the public origin
`https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev`
(live-verified 2026-11-09 16:29 PT for the scanner prefix; re-probe
at release time — see the return sheet).

```bash
BASE=https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-05_2026-10-06
curl -fsS "$BASE/latest.json"            # pointer: state partial, member digests
curl -fsS "$BASE/summary.json" | sha256sum   # must equal the pointer's summary digest
curl -fsS "$BASE/rankings.csv" | sha256sum  # must equal the pointer's rankings digest
curl -fsS "$BASE/paths.csv.gz" | sha256sum  # must equal the pointer's paths digest
```

Then, from the attached client against the deployed market_mcp
service (Chej-owned release, deploy by the workflow digest):

- `window_rankings` with `window_id overnight-2026-10-05_2026-10-06`,
  `kind overnight`, for each basis `mid`, `exec`, `qualified`:
  status `partial`, reasons `["bounded_universe_replay"]`, rows
  exactly the emit's rows (mid/exec rank 1 as in section 1;
  qualified rank 1 `ALAB261009C00410000` pct 2.366667).
- `result_detail` for the rank-1 row of each basis:
  `verified: "matches_published_pointer"`, entry/exit/sessions
  matching the emit.
- Any `source_changed` refusal, any `available` (final) status, or
  any row/count mismatch is a failed verification: stop, do not
  proceed to a second window, and roll back per section 7.

## 7. Rollback

- Before any R2 write: delete the local emit/store directories; the
  checkpoint is untouched (it is never written to).
- After an R2 write: delete ONLY the keys this publication wrote,
  validated against the pointer's member digests and the
  `latest-attempt.json` ledger — the flat members, the `rev_*/`
  members, `latest.json`, `latest-attempt.json`, and the
  `budget/<rev>-<attempt>.json` tokens for this window/revision.
  Re-list the prefix afterwards and confirm it is empty. Never
  delete keys belonging to another window or revision, and never
  delete by prefix without the digest check.
- Unset the activation flag saved in pre-check 6.
- The branch itself rolls back by reverting the release-preparation
  commits (this plan, the workflow, the guard) in reverse order; the
  released checkout (`014b48ad`) and the #130 checkpoint (`1f165d68`)
  are never modified, so there is nothing to restore there.

## 8. What this plan does NOT do

- It does NOT approve publication or activation: those are Chej's release step (Chej states tonight's reviewed release/activation is already authorized and he owns release and real publication; Chej's statement, not Owen-verified in this chat), gated by the technical pre-checks, not by an approval wait. The budget/retention/cap numbers are implemented from measured evidence (see section 3); they are not an approval wait.
- It does NOT publish a full-universe window: the preserved combined
  CSVs cannot be published as a window (no paths, qualified basis
  only). The combine-emit step has since been RUN for measurement
  (14-batch combine, 5 windows, 71,710 contracts each, in the
  measurement scratch dir) to size the caps; a full-universe first
  publication still needs its own reviewed release step, run with the
  required `--coverage` flag.
- It does NOT claim the mixed-flat pin survives a flat-only read.
  See "Availability limitation" in `docs/130-window-publisher.md`
  and `docs/131-window-consumer.md`: the pointer check is a refusal
  (availability) mechanism, not an immutability guarantee. The
  consumer only reads flat keys; the immutable `rev_*/` members are
  not what it reads; a window with no `latest.json`, or any reader
  that reads flat keys without the pointer, can be served or can
  misread a mixed revision.
- It does NOT claim Owen authorized a formal-review bypass: no Owen-verified bypass is recorded in this chat, and the PR #202 one-off bypass is explicitly not a general waiver. Chej states tonight's reviewed release/activation is already authorized and he owns release and real publication (Chej's statement, recorded here as his). This plan remains preparation; Chej executes the release.

## Release-path return sheet

Exact commands, revisions, and identifiers. IDs are given only as
verified; anything uncorroborated is marked as such with the command
to confirm it.

### Live-verified (probed 2026-11-09 16:29 PT by Nova)

- Market MCP service URL: `https://market-mcp-read-only-production.up.railway.app`
  - `GET /healthz` → 200 `{"status":"ok"}`
  - `GET /mcp` → 401 `{"error":"missing_token"}` (endpoint exists, auth required)
- R2 public origin: `https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev`
  - Active scanner pointer `oa203/scanner/2026-11-09/inprogress/latest.json`
    → 200, sweep 72, published 20:41:38Z, cadence `on_time`; all four
    member digests matched the downloaded files.
  - Final daily summary 2026-11-09 → `final: true`, `complete`, 76 sweeps.
  - Windows prefix `oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-05_2026-10-06/summary.json`
    → 404 at that probe (no window published yet).
- Re-probe at release time before trusting any of the above:
  `curl -sS -o /dev/null -w "%{http_code}\n" https://market-mcp-read-only-production.up.railway.app/healthz`
  and the `latest.json` / window-key curls in section 6.

### NOT live-verified — stale deployment-plan assertions (do not cite as live)

`market_mcp/deployment-plan.json` and `market_mcp/ROLLOUT.md` are
STALE (October 5, 2026) and do NOT establish current service state.
They assert these IDs; none has been corroborated from a live source:

- Railway project `ea9f4549-e34f-47cd-a09e-573b24d696a8`
- Railway environment `2dcc91a4-fafc-408f-bef8-e5ce24b2ec6a`
- Service `market-mcp-read-only` `7222ee91-be95-4aea-843b-2ff1258e21da`
- Discord service `5aa162c3-d2e7-468a-8fd0-a36e441285b2` (must not change, per the plan)
- Local verified manifest digest `sha256:4c69686b5455e44e7e882c6b52c6713090bbfffcffc4182183da78fcbe2323a3`
  (a local manifest digest, not a published image digest)
- Image `publication_status: "not_published"`, `uri: null`

Confirm at release time (Chej, who owns deployment) with the Railway
dashboard or CLI against the live service URL above, e.g.
`railway status` / `railway service` in the linked project, and
confirm the deployed service's image digest matches the digest the
Renaissance workflow publishes for `c8fcf529`. Do not deploy by
these IDs until confirmed.

### Git — exact commands (MSI worktree)

```powershell
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\live-options-view-f1f2' fetch origin --prune
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\live-options-view-f1f2' rev-parse HEAD
# expected: c8fcf529698bc57b930495f8c0272b5c1b16b2f4
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\live-options-view-f1f2' log --oneline -5 origin/nova/f1-f2-renaissance
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\live-options-view-f1f2' diff --name-only 014b48ad c8fcf529
# expected: the 22 reviewed paths listed in market_mcp_release_guard.py
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\live-options-view-f1f2' status --porcelain=v1
# expected: empty (clean)
```

Preserved checkouts (must stay untouched; verify, never write):

```powershell
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\dear-future-chej-use-chej-and-3\work\release' rev-parse HEAD
# expected: 014b48ad9707039871b784d42499602fd899ee74, status clean
git -C 'C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130' rev-parse HEAD
# expected: 1f165d68b5d4e6ddf34c472191a6d3dc5c0342db, status clean
```

### Workflow — pinned image

- File: `.github/workflows/publish-market-mcp-renaissance.yml`
- Pinned source SHA: `c8fcf529698bc57b930495f8c0272b5c1b16b2f4`
  (dispatch input default and `ACCEPTED_SOURCE_SHA`; the guard also
  pins reviewed base `014b48ad9707039871b784d42499602fd899ee74` and
  the exact 22-path reviewed set — no `--allow` override exists).
- Guard: `.github/scripts/market_mcp_release_guard.py` +
  `test_market_mcp_release_guard.py` (11 tests). Reuses the existing
  `market_mcp_source_guard.py` / `market_mcp_tag_guard.py` tests in
  the guard-tests job. The tag guard takes the new SHA from the
  `SOURCE_SHA` env the workflow sets.
- MERGE SEQUENCING (verified 2026-11-09 ~18:10 PT): `c8fcf529` is
  NOT currently an ancestor of `origin/master` (`94301e08`) — master
  has moved on since the branch was pushed — so the guard's ancestry
  check correctly BLOCKS a dispatch today. That is the design, not a
  defect: like the historical workflow, this one publishes only a
  reviewed commit AFTER it merges to master. Dispatch only after PR
  #204 merges (which also puts this workflow file itself on master,
  as the publish job's `github.ref == 'refs/heads/master'` condition
  requires); after the merge `c8fcf529` becomes an ancestor of master
  and the guard passes. The base→source diff is unaffected by the
  merge (the guard checks the pinned commit, not the merge commit).
- Triggers: `pull_request` ONLY for the workflow/guard files (runs
  guard-tests; publishes nothing); `workflow_dispatch` ONLY for the
  publish job, and only when `github.repository ==
  'OwenTanzer/live-options-view'` and `github.ref ==
  'refs/heads/master'`. Concurrency group
  `publish-market-mcp-renaissance`, `cancel-in-progress: false`.
- Image: `ghcr.io/owentanzer/market-mcp-read-only`, run tag
  `c8fcf529698bc57b930495f8c0272b5c1b16b2f4-run-<run_id>-<run_attempt>`.
  Docker is NOT installed on the MSI, so no local image digest exists
  and none is claimed here. The digest comes only from the dispatch
  run: Chej dispatches the workflow (GitHub UI or
  `gh workflow run publish-market-mcp-renaissance.yml --ref master -f source_sha=c8fcf529698bc57b930495f8c0272b5c1b16b2f4`),
  then reads the `Image: ghcr.io/owentanzer/market-mcp-read-only@sha256:…`
  line from the run's step summary. Deploy by that digest, never by tag.
- The historical workflow `.github/workflows/publish-market-mcp-reviewed.yml`
  is untouched and remains pinned to `4d3daafa907e31222448ed35202be4c964002aa1`
  with its overview-only paths; this plan does not modify it.

### Tests — exact commands and expected counts (MSI, at c8fcf529)

```powershell
python -m pytest tests/test_oa203_windows_publish.py tests/test_oa203_windows.py tests/test_oa203_windows_replay.py tests/test_oa203_windows_parity.py tests/test_oa203_windows_concurrency.py -q
# expected: 73 passed, 15 subtests passed
node --test market_mcp/*.test.js
# expected: 171 pass, 0 fail, 1 skipped (parity.test.js, manifest-gated)
python -m unittest discover -s .github/scripts -p 'test_market_mcp_release_guard.py' -v
# expected: 11 tests, OK
python -c "import yaml; yaml.safe_load(open('.github/workflows/publish-market-mcp-renaissance.yml'))"
# expected: parses (note: PyYAML reads the `on:` key as boolean True; the file itself is valid GitHub Actions YAML)
```

### Rollback commands

```powershell
# Unpublish the one window (only the keys section 7 names), then re-list to confirm empty:
# (Chej's release step; exact key list comes from the publication's latest.json + ledger.)
# Unset the activation flag saved in pre-check 6.
# Branch rollback, if the release-preparation commits must go:
git -C 'C:\Users\Owen\Documents\Codex\2026-11-09\renaissance-f1f2\work\live-options-view-f1f2' log --oneline -6
# revert the release-path commit(s) in reverse order; never force-push, never reset the shared branch.
```
