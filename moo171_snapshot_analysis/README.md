# MOO-171: archived QQQ snapshot analysis

Audits and analyzes unsigned gamma-weighted open-interest concentration (C)
and interval activity (A) in the existing archived `intraday/{date}/snapshot_*.csv`
tastytrade/DXLink QQQ 0DTE snapshots, per the MOO-171 spec. Unsigned only --
not signed dealer inventory, not a hedging-flow estimate. See
`out/MOO171_report.md` for the write-up and explicit limitations.

## Pipeline

1. `r2_source.py` — cached, read-only R2 access (`intraday/` prefix only).
2. `panel.py` — causal panel construction + coverage/exclusion audit:
   duplicate (timestamp, OptionSymbol) detection, OptionSymbol-vs-column
   identity checks, snapshot-level spot consistency, per-contract OI
   stability against a first-eligible-session baseline, repeated-value run
   lengths, and a three-way premarket/regular/afterhours session-phase
   classification. Recomputes interval volume from consecutive cumulative
   `Volume` observations (never trusts the saved `VolDelta`).
3. `measures.py` — `C[k,t]` and `A[k,t]` per the issue's formulas, kept
   deliberately separate. Missing/unusable inputs are preserved as
   unavailable (NaN), never admitted as an observed zero.
4. `outcomes.py` — five-minute anchors (verified non-overlapping against
   the actual retained timestamps, not just a nominal grid --
   `assert_non_overlapping` is a generated-data check, not only a unit
   test), 3-nearest-strikes-per-side selection, and the `toward[k,t]`
   outcome.
5. `baselines.py` — prior 5-min return, a preceding 30-min realized-vol
   estimate that REQUIRES actual ~30-minute coverage (not merely enough
   observation count), minutes since open (all causal, anchor-at-or-before
   only).
6. `regression.py` — the baseline-comparison model with standard errors
   clustered by anchor in every fitted model (pooled AND per-day -- never
   falls back to independent-row uncertainty), day-by-day coefficients,
   leave-one-day-out stability, and SD-scaled coefficients so predictors
   with different scales are actually comparable.

## Running

```bash
pip install -r requirements.txt
# needs R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME
# in env, e.g. via `railway run` linked to a service that has them (moo144-tradier-probe
# or live-options-view in the live-market-monitor project)
python run_audit.py      # step 1, REPRODUCTION mode (default): verify against the frozen
                          # out/source_manifest.json, then inventory + coverage audit ->
                          # out/audit_report.json, out/reproduction_check.json,
                          # out/option_rows.parquet, out/spot_series.parquet (gitignored, regenerable)
python run_audit.py --new-run   # only when deliberately starting a new run: writes a NEW manifest
python run_measures.py   # steps 2-3 construction -> out/measures.parquet (gitignored),
                          # out/anchors.parquet, out/outcome_dataset.parquet
python run_report.py     # step 3 analysis -> out/MOO171_report.md, out/plots/, out/*.csv
```

`out/option_rows.parquet` and `out/measures.parquet` are gitignored (large,
trivially regenerable from the immutable R2 archive) along with `r2_cache/`
(the local disk cache of raw R2 reads). Everything else under `out/` --
the report, plots, small aggregated CSVs/parquets, `audit_report.json`, and
`source_manifest.json` (the frozen object keys/etags/config/code-revision
used by the run that produced the committed evidence) -- is committed.

### Reproduction vs. new run

`run_audit.py` with no flags treats the committed `out/source_manifest.json`
as the frozen run. Before anything under `out/` is replaced, it checks that
the config constants and every `*.py` file's sha256 match the frozen values,
that the listed snapshot keys equal the frozen key set exactly, and that
every snapshot's bytes (cached or fetched) hash to the frozen sha256. The hash
check happens in `r2_source` before the bytes are parsed. On any mismatch it
exits 1 and leaves every `out/` file, including the manifest, untouched. On
success it keeps the manifest byte-for-byte and records the verification in
`out/reproduction_check.json`. Only `--new-run` writes a new manifest.

Both modes hand-reconcile one representative C and one representative A value
from raw rows (for A: each contract's consecutive cumulative `Volume`
observations, timestamps, elapsed interval, derived dV, flags, and
Gamma/spot). They then assert agreement with
`measures.compute_concentration_and_activity`. `run_measures.py` repeats that
check against the `measures.parquet` it writes, and any disagreement exits 1.

## Tests

```bash
python -m pytest tests/ -v
```

All tests run against synthetic in-memory fixtures (`FakeSource` in
`tests/test_panel.py`) -- no R2 credentials or network access required.
