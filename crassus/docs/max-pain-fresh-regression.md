# Mouse and Rat fresh-entry regression — November 25, 2026

This is a regression comparison on the **same 67 previously inspected sessions**,
not fresh validation. The rule was supplied before this comparison; no thresholds
or qualification gates were retuned after viewing these results.

After any confirmed sale, count only distinct qualified boards strictly after
the fill. One opposite-direction board or two consecutive neutral boards unlock
the account. The exit board cannot count. Duplicates do not advance the count;
missing, stale, sparse, invalid or same-direction readings interrupt neutral
confirmation. The existing 15-minute same-contract cooldown after stop/target
sales remains; ordinary signal sales add no contract cooldown.

## Reproduce

From a Linux checkout with Crassus requirements installed and git history present:

```sh
python crassus/scripts/verify_max_pain_episodes.py
python crassus/scripts/replay_max_pain_fresh.py --baseline-ref 1c48553 --output /tmp/max-pain-regression.json
```

The script invokes the actual baseline and patched strategy decision functions,
with reconciled synthetic fills and durable per-account state. The Runner's
last-15-minute flatten gate is reproduced in the harness. The separate test suite
exercises the real Runner account entry point against its local mock Worker,
including confirmed sale and restart. Nothing connects to a broker or submits
production orders.

The committed gzip fixture contains 5,153 selected boards across 67 sessions,
June 26–November 13, 2026. It retains full decision-relevant rows, each raw CSV's
filename and SHA-256, selection times, original drop counts and session close.
SHA-256: `d3b4a06d0cf253b42f4b26a1236a592bd5f7d0645a2fc57b7311bc92a00cc577`.

Selection exactly reuses Bentworth's retained `expanded.parse_job((day, True))`
over the union of `additional-early.json["kept"]` and
`additional-late.json["kept"]`. The original five-minute selection, first board
within 90 seconds, frozen-board removal and archive qualification are unchanged.
Thus skipped observations are not invented as neutral readings; gaps in this
selected replay cannot establish what the live policy would have seen in between.

Source SHA-256:
- replay.py: `d53116dd62867c18ef33a4fc8cc53a8b1802a12fcfc2a3125b59e38bc9e4e69e`
- expanded.py: `23cf1f03c7e750482b0d6e048ab2f2bdef89fbb3ca90c3344f5e46ba54c7bf56`
- additional-early.json: `356a0e333fd83ca3ceb8dc2ddd647165f4601b2681cea1165d014133cf79f35b`
- additional-late.json: `460cfca3cbfcb93bcf85a27ee9261bd6d9bd5e655b4a1b8fda2e9e35dc4277a5`

## Results

All amounts are dollars for one-contract ask-to-bid trades, after $1.30 per round
trip. The old implementation reproduces the previously reported totals exactly.

| Bot | Current net / trades | Patched net / trades | Removed re-entries | Paired net change |
|---|---:|---:|---:|---:|
| Mouse | -291.70 / 179 | -955.20 / 144 | 35 | -663.50 |
| Rat | -287.70 / 179 | -1,071.60 / 142 | 37 | -783.90 |

No added/re-timed re-entry keys occurred in the unstressed comparison. Removed
re-entries compare exact (entry time, contract) keys after each day's first trade,
rather than assuming a trade-count reduction identifies the same removed trades.
Mouse improved on 7 days, worsened on 12, tied on 48. Rat improved on 7, worsened
on 13, tied on 47. Mean paired daily changes are -9.903 and -11.700 respectively.
No patched position remained unresolved.

| Bot | Current with adverse cent | Patched with adverse cent | Paired change |
|---|---:|---:|---:|
| Mouse | -878.00 | -1,457.20 | -579.20 |
| Rat | -885.70 | -1,641.60 | -755.90 |

The stress is a full replay with entry ask +$0.01 and exit bid -$0.01 (floored at
zero), including the resulting change in threshold decisions. It is not simply
$2 subtracted per completed trade. Stressed current Mouse takes 180 trades;
stressed current Rat 179, patched Mouse 144 and patched Rat 142.

Rat minus Mouse is +4.00 currently and -116.40 patched; under adverse fills it is
-7.70 currently and -184.40 patched. Cash remains zero. These results do not show
a profitable edge, and the stronger reset loses more on these selected sessions.

[Full daily paired results and trade ledgers](max-pain-fresh-regression.json)
preserve all four arms for both bots. Archived quotes are treated as immediately
available for simulated fills; actual publication delay, queueing, intervening
quotes and live execution quality are not reconstructed. Stops are observed at
decision times and do not guarantee threshold fills.

## Verification and release boundary

The final targeted suite has 15 passing test groups. Four new behavioral groups
produce 12 assertion failures on the old implementation and pass after the fix.
Baseline Max Pain checks: 45 passed. General exits: 65 passed. Linux fixture
execution was required because Windows directory fsync raises PermissionError.
The local environment lacks pandas_market_calendars, producing the existing
conservative calendar fallback in the Runner fixture; hosted CI installs the
declared dependencies. This is not a live calendar or production execution test.

The code catalog retains both accounts inactive. Read-only production inspection
found both already active in the runtime overlay on older revision c2b6229,
deployment 629ee466-6b52-4968-9fc0-66420eb74d30. The runner uses the paper API,
separate usernames and corresponding strategy IDs; no broker mapping is present.
No roster, credential, producer, other bot or Cloudflare KV change was made.

Ordinary independent approval and exact-head checks precede merge. A merge under
the Crassus watch scope can redeploy the shared runner, so use a coordinated safe
window after its session **and finalization**, not an unverified wall-clock
assumption. The earliest normal-session candidate is after November 25 16:00
Eastern / 21:00 UTC, subject to checking all shared accounts and finalization.
After release, verify exact deployed revision, both strategy versions, paper-only
mappings, input health and first decisions. Owen subsequently authorized paper
activation after those checks; merge alone is not release or activation evidence.
