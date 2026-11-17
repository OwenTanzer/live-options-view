# Change record: findings repairs after Chej's review of 9a282b4

The `9a282b4` numerical verdicts are **superseded and not accepted**. Its outputs are preserved:
- `FINDINGS_superseded_9a282b4.md`
- `results_superseded_9a282b4.json`
- `feature_manifest_superseded_6026ca9.json`

The repairs below were committed (frozen) before any repaired feature or result was generated. The Nov 2–16 holdout was already viewed in `9a282b4`, so every repaired analysis is **exploratory, not fresh confirmation**.

| # | Review item | Defect in `9a282b4` | Repair | Regression / check |
|---|---|---|---|---|
| 1 | `features.py` set iteration | `snapshots()` received `set(symbols)` and built Gamma, Delta and OI columns in set-iteration order. That order depends on the Python hash seed. Flow columns used the sorted symbol order. Greeks and OI were therefore attached to the wrong contracts in a seed-dependent way, so every gamma-weighted feature in `6026ca9` is invalid. | `snapshots()` takes an ordered list, refuses a set, and builds every column by indexing a symbol-keyed row map with that list. | `tests/test_oa241_features.py`: columns match explicit keys, are identical under `PYTHONHASHSEED` 1, 2 and 31337, and a set is refused. Sept 25 real-data features are rebuilt under two seeds and compared. |
| 2 | `analysis.py` 10:00 lag-volume window | For t = 10:00 the prior dollar-volume window (t−60, t−30] lies before the open. The negative slice returned an empty sum, and the anchor was counted as "missing return". | The window is checked explicitly. 10:00 anchors are excluded as `prior_volume_window_before_open`, because the frozen control needs a full in-session prior window. Other exclusions are split into forward/lagged return, zero RV, zero dollar volume and missing exposure feature. | Exclusion counts by reason in `results.json`. |
| 3 | `analysis.py` pooled fits | LOSO and holdout used a pooled-intercept fit, not the frozen session-intercept specification. | Implemented as frozen (see ERRATA E2). The pooled numbers are reported alongside as superseded. | `superseded_pooled_fit_*` keys. |
| 4 | Input fixity and erratum | Only feature outputs (CSV) were hashed. Snapshot and NPZ inputs were not, and `analysis.py` did not check hashes. | Per session, the manifest records the archive manifest SHA-256 (segments verified), a digest over every chain snapshot file read, and the CSV and NPZ SHA-256. `analysis.py` refuses any CSV or NPZ that does not match. The 36-to-34 session discrepancy is recorded in ERRATA E1, with `PROTOCOL.md` preserved. | `load()` assertions. |

**Added uncertainty for the out-of-sample claim.** Leave-one-session-out MSE reductions now carry a session-bootstrap interval from paired per-session errors (`loso_mse_reduction_*_ci95`). Verdict wording follows those intervals.

Reproduction:

```sh
python research/oa241/findings/features.py ARCHIVE SNAPSHOTS SESSIONS.json FEATURES
python research/oa241/findings/analysis.py FEATURES > research/oa241/findings/results.json
python -m unittest tests/test_oa241_features.py
```

## Effect of the repairs on results (after rerun)

- **Feature fixity.** Repaired features are at `b655f75`. Sept 25 is byte-identical under `PYTHONHASHSEED` 1 and 2 and in the default run.
- **Signed exposure.** Superseded G_within correlated 0.30 with the repaired values on Sept 25, and about 0.91 on Oct 8 and Nov 10. G_oi_unsigned is identical, because gamma and OI were permuted together.
- **Volatility (G5a).** The M3-vs-M2 LOSO change moves from −0.6% (pooled fit, misaligned features) to −0.2%, with interval −0.8% … +0.2%. The verdict is tempered from "no out-of-sample information" to "no useful (≥ 2%) out-of-sample gain detected; coefficient sign undetermined".
- **Downside warning (D3).** The Brier difference moves from −0.0026 to −0.0007, with interval −0.0053 … +0.0029. Still unresolved.
- **Continuation and strike attraction (G5b, G5c).** Both remain unresolved; the estimates move by less than their intervals.
- **Exclusion accounting (G5a).** Exclusions are now reported by reason. The 36 excluded anchors are all 34 at 10:00 (33 lack a quote at or before 09:30:00; 1 has a prior volume window before the open) plus 2 in an Oct 8 quote gap.
