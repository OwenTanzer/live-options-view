# Offline backtesting and study archive

[Synthetic days](synthetic_days/README.md) generates resampled market sessions
and replays the Crassus strategy code from this checkout. Use its guide to run
it offline.

## Archived studies

Both studies are preserved at the revision tagged
[archive/options-view-studies-2026-11-08](https://github.com/OwenTanzer/live-options-view/tree/archive/options-view-studies-2026-11-08).
The links below pin the exact commit, including reports, manifests, scripts,
fixtures and checked-in outputs. Use the archived revision and each study's
reproduction instructions to rerun it.

| Study | What it preserves |
|---|---|
| [October price and variance study](https://github.com/OwenTanzer/live-options-view/blob/06e5a42bddb8db79b5747cbb8236ac46312c7839/research/studies/brian-r2-20261024/README.md) | Brian's forecast tests, frozen protocol, results and final audit. |
| [MOO-171 snapshot analysis](https://github.com/OwenTanzer/live-options-view/blob/06e5a42bddb8db79b5747cbb8236ac46312c7839/research/snapshot_analysis/README.md) | Archive coverage audit, concentration and activity measures, frozen source manifest and report. |

For example, open the archived checkout in a separate worktree:

~~~sh
git fetch origin tag archive/options-view-studies-2026-11-08
git worktree add --detach ../options-view-studies archive/options-view-studies-2026-11-08
~~~

[Opening OI associators](oi_associator/README.md) fits four trading models and records their held-out paper ledgers.
