# #130 window consumer (#131 F2) — offline implementation

Status: implemented and tested offline (fixtures only). No push, no PR, no
deployment, no publication, no activation. Chej owns review/release.

## What was built

- New tool `window_rankings` in `market_mcp/server.js`. It does **not**
  overload `return_rankings` / `active_rankings`: a window is a different
  product (multi-session, per-basis, hindsight-labeled) and gets its own
  tool, dataset id (`options_windows`) and validators.
- Consumer `window()` in `market_mcp/consumer.js`, validators
  `windowSummary` / `windowPathsRow`, a `result_detail` branch for
  `options_windows`, and a `discover_sources` capability for windows.
- `market_mcp/source.js` KEY allowlist extended **only** for
  `oa203/windows/oa203-windows-v1/<kind>/<window_id>/{summary.json,rankings.csv,paths.csv.gz,latest.json}`
  with strict per-kind window-id shapes (see below). `latest.json` is
  the publisher's per-window pointer; the consumer reads it to pin a
  read to one revision (point 10 below), never as an index.
- Fixtures in `market_mcp/window_fixtures.js` built from the real Oct 5–7
  2026 replay shape (`docs/130-acceptance-matrix.md`); tests in
  `market_mcp/window.test.js`.

## Design decisions (recorded)

1. **New tool, not a parameter.** `window_rankings(window_id, kind, basis,
   underlying?, type?, limit?, offset?)`. `window_id` and `kind` are
   required and must agree; `basis` defaults to `qualified`.
2. **Policy is fixed to `oa203-windows-v1`.** The version is part of the
   key path. A summary at that path claiming another policy version is a
   typed `unsupported` answer (artifacts exist, this consumer does not
   understand them), not a failure. A missing or malformed policy is
   `incompatible_schema` → `failed`.
3. **States.** `final` serves rows (`available`); `partial` serves rows
   with its reasons (`partial`); `in_progress` returns the summary only,
   rows `[]`, status `in_progress` — the exit is not invented. An unknown
   state string is `unsupported`. A missing window is `missing`
   (`missing_publication`).
4. **Ranks cut only after full ranking.** The consumer cannot see the full
   universe (excluded rows live only in `paths.csv.gz`), so it verifies
   what the shortlist itself proves: per basis, exactly ranks
   `1..shortlist`, pct-descending with symbol tiebreak, rows grouped by
   basis in producer order, `shortlist == min(200, eligible)`, and
   `eligible + excluded == contracts_evaluated` with exclusion-reason sums
   matching `excluded` for every basis. Negative-count reconciliation is
   exact when nothing was truncated, and an upper bound when it was.
5. **No look-ahead, no stitching.** Entry qualification comes from
   entry-time evidence only: detail verifies each endpoint lies inside its
   own session, the exit follows the entry, and every quote stamp is at or
   before its observation time (never in the future). Contracts are one OCC
   symbol end to end; continuity exclusions (`missing_session`,
   `expires_before_exit`, `am_settled_on_exit_day`,
   `adjusted_deliverable_unknown`) keep rows excluded, never fabricated.
6. **Hindsight is labeled, never hidden.** `weekly_cheapest` and
   `weekly_cheapest_best_exit` require a `hindsight` label in the summary;
   the labels travel into every answer's warnings.
7. **Paths detail is digest-pinned when a pointer exists, otherwise
   consistency-verified.** The summary carries no member digests, but
   the publisher's `latest.json` pointer does: when it is present,
   detail checks the paths bytes' digest against the pointer's member
   digest, so a paths file from another revision is refused even when
   its row still matches the pinned ranking row
   (`verified: 'matches_published_pointer'`). Without a pointer, detail
   falls back to the earlier behaviour: it re-reads the summary and
   rankings and fails closed if either digest changed, then verifies
   the paths row against the pinned ranking row
   (identity, basis status, pct/entry/exit, sessions, endpoint/session
   bounds, quote stamps, and path arithmetic). A missing paths file leaves
   the pinned ranking standing with an explicit `rankings-only` warning
   (status `partial`); a mismatching paths row fails closed
   (`source_changed`); a paths file with conflicting attributions for one
   symbol fails closed (`incompatible_schema`) — no canonical path is
   asserted. The paths row is located by a line scan without materializing
   the whole file, but the read still counts against the existing
   artifact/expanded/query byte limits: a full-universe `paths.csv.gz`
   larger than the 48 MiB expanded ceiling fails closed with
   `excessive_response` instead of returning a partial path. Measured
   full-universe paths expand to at most 34,295,267 B (32.7 MiB), so
   the 48 MiB ceiling admits them with 1.46x headroom; the worst-case
   detail query totals ~50.5 MiB (48 expanded + ~2.4 compressed +
   ~0.1 summary/rankings/pointer), inside the 64 MiB query budget.
   The 10,000-row `csv_rows` limit does not block pinned detail at
   71,710 rows: `windowPathsRow` scans lines and parses only the header
   plus the one matching row, never the whole file.
8. **No window index.** There is no index of windows, so
   `discover_sources` advertises the fixed contract with availability
   `explicit_window_required` and never claims live data. The
   publisher's per-window `latest.json` is read by the consumer, but
   only to pin the window it was already asked for to one revision
   (point 10); it is never listed or followed to another window.
9. **Publisher exists offline, plus one live R2 trial.** `market_data/options_scanner/windows_publish.py`
   validates and publishes windows to a pluggable backend (fake and
   local-filesystem backends tested; see `docs/130-window-publisher.md`),
   and the first live window (overnight-2026-10-05_2026-10-06,
   rev_c1abd845...) was published to R2 on 2026-11-10 by Chej's conditional
   trial. Further live publication remains Chej's release step. This
   consumer reads whatever the publisher writes; it does not publish, set
   any env flag, start a collector, or deploy.
10. **A read is pinned to one revision by the pointer.** The flat keys
    are written one at a time, so a concurrent republish can leave a
    stable mixed revision on disk (new summary, old rankings, or vice
    versa) that the before/after digest check cannot see: it only
    detects changes *during* the read. After the re-reads, `window()`
    therefore reads `latest.json` and checks the flat summary/rankings
    digests (and the pointer's kind/window/state) against the pointer's
    member digests; a mismatch is a `source_changed` refusal, never a
    served mix. A window with no pointer keeps the old behaviour
    (served, with a `not revision-pinned` warning): the publisher
    always writes a pointer, so a missing one means a legacy or
    hand-placed window. `in_progress` and `unsupported` answers return
    before the pointer read, so they make no extra request; a served
    `window_rankings` call now makes 5 artifact reads (summary,
    rankings, both re-reads, pointer).

## Availability limitation — the pointer is a refusal, not an immutability guarantee

The pointer check in point 10 refuses a mixed revision; it does not
make flat reads immutable, and it does not protect any read that does
not use it. The consumer only reads flat keys (`summary.json`,
`rankings.csv`, `paths.csv.gz`) plus `latest.json`; the immutable
`rev_<sha>/` members are not what it reads, so the pin does NOT
survive a flat-only read:

- Pointer present and digests match: the read is pinned to that one
  revision; the consumer serves it.
- Pointer present and flat summary/rankings digests (or detail paths
  digest) mismatch the pointer's member digests: the consumer refuses
  with `source_changed` and serves nothing. That is an AVAILABILITY
  loss — a correct window becomes unreadable until the flat tree is
  repaired or republished — not a silent wrong answer, and not a
  guarantee that the data is intact.
- No pointer: the consumer serves the flat keys with a
  `not revision-pinned` warning. A mixed flat revision can then be
  served without any refusal; the publisher always writes a pointer,
  so a missing one means a legacy or hand-placed window.
- Any reader that reads the flat keys without `latest.json` gets no
  protection at all. Do not claim the immutable pin survives a
  flat-only read: the `rev_*/` copy is an audit artifact, not a read
  path.

## Producer-publisher-consumer parity (proven, offline)

`tests/test_oa203_windows_parity.py` builds every supported kind with
the real producer (`windows.evaluate` + `windows_outputs.write_window`),
publishes each with the real publisher to a local-filesystem backend,
and runs the real consumer over the same trees
(`market_mcp/parity.test.js`, via a local `Source` directory). The
consumer's rows, ranks, pcts, entry/exit and pinned detail match the
producer's own outputs exactly for all 6 kinds x 3 bases x final and
genuine partial states (12 variants; partial is producer-emitted, last
session assessed partial with the producer's own reasons). `in_progress`
windows are never published, so they have no parity row; the consumer's
in-progress handling is covered by `market_mcp/window.test.js`.

## Supported-window matrix (implemented)

| Kind | Bases | States served | Notes |
| --- | --- | --- | --- |
| `weekly_cheapest` | mid / exec / qualified | final, partial, in_progress (summary only) | hindsight entry; label required |
| `weekly_fixed` | mid / exec / qualified | final, partial, in_progress (summary only) | fixed open endpoint |
| `weekly_cheapest_best_exit` | mid / exec / qualified | final, partial, in_progress (summary only) | hindsight both ends; label required |
| `overnight` | mid / exec / qualified | final, partial, in_progress (summary only) | gaps (weekend/holiday) included per policy |
| `sessions_2` | mid / exec / qualified | final, partial, in_progress (summary only) | rolling |
| `sessions_3` | mid / exec / qualified | final, partial, in_progress (summary only) | rolling |

Explicitly **unsupported** (typed answer, not silent): any policy other
than `oa203-windows-v1`; any state other than
`final`/`partial`/`in_progress`; any window id that does not match its
kind (`weekly-YYYY-Www-<cheapest|fixed|cheapest_best_exit>`,
`overnight-YYYY-MM-DD_YYYY-MM-DD`,
`sessions[23]-YYYY-MM-DD_YYYY-MM-DD`); monthly or other kinds (no such
producer contract exists).

## What remains for deployed retrieval (not done here)

1. **Publisher**: implemented offline in
   `market_data/options_scanner/windows_publish.py` (see
   `docs/130-window-publisher.md`); it writes
   `oa203/windows/<policy>/<kind>/<window_id>/{summary.json,rankings.csv,paths.csv.gz}`
   to a pluggable backend, with retention/budget policy and a
   per-window `latest.json` pointer (no index). The first live window was
   published to R2 on 2026-11-10 (Chej's conditional trial); further live
   publication remains Chej's release step, gated by the release checks in
   `docs/130-window-publisher.md`.
2. **Live R2**: verify the published keys are readable at the public origin
   and that a real complete week exists (the inspected Oct 5–9 week is not
   complete: Oct 8 is `partial`).
3. **Connected-client check**: run `window_rankings` for a real window
   (all three bases) plus pinned `result_detail` from an attached client
   once the market_mcp service is deployed (Chej-owned release).
4. **Concurrency re-check on live R2**: the offline interleaving tests
   (`tests/test_oa203_windows_concurrency.py`) prove the logic against
   a local backend; the same races against R2 additionally assume the
   listing reflects a just-created token (R2/S3 strong read-after-write
   consistency on list). Verify that assumption with the trial-window
   pre-check before any live write.
