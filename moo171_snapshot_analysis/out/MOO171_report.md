# MOO-171 — Unsigned gamma concentration/activity vs. subsequent strike attraction

## Question

At comparable initial distances from QQQ spot, do strikes with higher unsigned
gamma-weighted open-interest concentration (C) or gamma-weighted interval
activity (A) show different subsequent movement toward/away from the strike,
beyond ordinary proximity and unweighted activity?

## Data

5 sessions (20260910, 20260911, 20260914, 20260915, 20260916), tastytrade/DXLink intraday QQQ
0DTE snapshots archived at ~60s cadence, `intraday/<date>/snapshot_*.csv`.

Coverage audit (`out/audit_report.json`, `out/source_manifest.json` for the
exact object keys/verified-content-sha256/config/executed-source-hashes used
by this run -- see its `git_base_revision`/`git_dirty`/`source_file_sha256`
fields for how the executed code itself is identified independent of commit
state, `representative_reconciliation` for one hand-checked source-row-to-C
calculation, and `multiplier_evidence` for what the archive can and cannot
establish about the 100-share contract multiplier): all
5 sessions reconcile exactly against collector logs = True.
400124 option rows. 400124 interval-volume observations
computed; 398472 (99.59%) usable (`ok`) after
excluding first-observations, re-entries, resets, and gaps >90s per the
issue's rules -- see `flag_counts_overall` in the audit report for the exact
breakdown. Session audits also check (timestamp, OptionSymbol) uniqueness,
OptionSymbol-vs-column identity, snapshot-level spot consistency, per-contract
OI stability against a first-eligible-session baseline, and repeated-value
run lengths: 0 duplicate (timestamp, OptionSymbol) rows,
0 OptionSymbol/column mismatches, and
0 spot-inconsistent snapshots found across all 5
sessions (see each session's entry in the audit report for the full detail).
**OpenInterest never changed within any regular-hours session for any of the
754 distinct contracts observed across all 5 days
(0 contracts with any intra-session OI change)** --
this audit establishes only that recorded OI did not change within the
observed regular-session contract histories. It does NOT independently
verify freshness, settlement origin, or that an unknown OI value was never
serialized as zero upstream of this analysis; it is consistent with (not
independent proof of) DESIGN.md's known-limitations statement that OI is
prior-day-settled data for the entire session. Separately,
at least one contract's Mid quote was unchanged for 378
consecutive regular-hours snapshots on the session with the longest such run
-- reported as a repetition count only, per the issue's caution that
repetition alone does not prove staleness.

Anchors: 325 retained (verified non-overlapping by
`outcomes.assert_non_overlapping`), 65 candidate
5-minute bins excluded (reasons in `out/excluded_anchor_bins.parquet`).
Outcome dataset: 1950 (anchor, strike) rows from up to 6 selected
strikes per anchor (3 nearest strictly above spot, 3 strictly below), each
requiring an anchor snapshot and a +5-minute outcome snapshot both within 90s
of their targets. Eligibility for C and A is tracked separately per strike
(`n_oi_gamma_valid`, `n_dv_valid` in the outcome dataset) -- a strike is only
selected if it has a real (non-missing) C and A, never a numeric zero
standing in for unavailable data.

126 of 1950 outcome rows
dropped before modeling, by cause: {'prev_5min_return': 30, 'vol_30min': 126}
-> 1824 modeled rows.

## Method

`toward[k,t] = (|S[t]-k| - |S[t+5m]-k|) / S[t]`, positive = closer to k at
the outcome snapshot. Regressed on log1p(C) / log1p(A) / log1p(gamma alone)
each in turn, plus a fixed baseline (|distance|, |distance|^2, side of spot,
prior 5-min return, a preceding 30-minute realized-volatility estimate that
REQUIRES actual ~30-minute coverage back from the anchor -- not merely enough
observation count, minutes since open, log unweighted OI, log unweighted
activity). Standard errors are clustered by (date, anchor) in every fitted
model here, pooled AND per-day -- the 6 strikes at one anchor share one
future price path and are never treated as independent observations.

This clustering does not by itself resolve every dependence concern: the
pooled model has 1824
modeled rows across many more anchor clusters than there are trading days --
there is no day-level clustering computed anywhere in this analysis, and
anchor-clustering does not address serial dependence *between* successive
anchors within the same session. Day-by-day and leave-one-day-out below are
descriptive session-level stability/sensitivity checks, not a resolution of
that residual dependence, and "leave-one-day-out" means refitting after
omitting one day's rows -- not held-out predictive validation.

Because log_C, log_A, and log_gamma_alone have different scales, their raw
fitted coefficients are not comparable to each other. The predictor
comparison below also reports each predictor's own standard deviation and an
SD-scaled coefficient (the fitted change in `toward` for a one-SD change in
that specific predictor).

## Findings — read the day-by-day results before the pooled ones

### log(C)

Pooled (n=1824, clustered by anchor): coefficient 8.79006e-05
(SE 3.4e-05, p=0.01004), SD-scaled effect 6.6342e-05
(predictor SD 0.7547). R^2 gain over baseline
(0.0411): 0.0018.

Day-by-day: 3 of 5 sessions show a positive
coefficient with a nominal p<0.05 individually (nominal under this day's
own anchor-clustering assumption, not a claim that residual serial
dependence between anchors is resolved); the remaining
2 do not (see `out/day_by_day_log_C.csv`
/ `out/plots/day_by_day_log_C.png` for every session's own
coefficient, SE, and cluster count).

Leave-one-day-out: the pooled coefficient ranges from 4.50167e-05 to
0.000202891 depending on which single day is excluded (`out/leave_one_day_out_log_C.csv`).

### log(A)

Pooled (n=1824, clustered by anchor): coefficient 8.58749e-05
(SE 3.7e-05, p=0.01935), SD-scaled effect 0.000126732
(predictor SD 1.476). R^2 gain over baseline
(0.0411): 0.0015.

Day-by-day: 3 of 5 sessions show a positive
coefficient with a nominal p<0.05 individually (nominal under this day's
own anchor-clustering assumption, not a claim that residual serial
dependence between anchors is resolved); the remaining
2 do not (see `out/day_by_day_log_A.csv`
/ `out/plots/day_by_day_log_A.png` for every session's own
coefficient, SE, and cluster count).

Leave-one-day-out: the pooled coefficient ranges from 4.51799e-05 to
0.000220322 depending on which single day is excluded (`out/leave_one_day_out_log_A.csv`).

### log(gamma alone)

Pooled (n=1824, clustered by anchor): coefficient 0.000579708
(SE 0.00018, p=0.001129), SD-scaled effect 5.71246e-05
(predictor SD 0.09854). R^2 gain over baseline
(0.0411): 0.0028.

Day-by-day: 2 of 5 sessions show a positive
coefficient with a nominal p<0.05 individually (nominal under this day's
own anchor-clustering assumption, not a claim that residual serial
dependence between anchors is resolved); the remaining
3 do not (see `out/day_by_day_log_gamma_alone.csv`
/ `out/plots/day_by_day_log_gamma_alone.png` for every session's own
coefficient, SE, and cluster count).

Leave-one-day-out: the pooled coefficient ranges from 0.000406834 to
0.000865105 depending on which single day is excluded (`out/leave_one_day_out_log_gamma_alone.csv`).

### Reconciling SD-scaled effect size against R^2 gain

log(gamma alone) has the smallest SD-scaled coefficient of the
three predictors (5.71246e-05),
and its own separate model also has
the largest R^2 gain over baseline
(0.0028 for log(gamma alone)). This is
not a contradiction to resolve away: a standardized partial coefficient (the
fitted change in `toward` per one-SD change in that specific predictor,
holding the baseline terms fixed) and a separate model's R^2 gain (how much
additional outcome variance that predictor plus the baseline jointly
explain, relative to the baseline alone) answer different questions, even
though both come from the same linear fitted model. The SD-scaled
coefficient scales the predictor's coefficient by its TOTAL standard
deviation, while the R^2 gain depends on the predictor's RESIDUAL variance
after controlling for the baseline terms (roughly coefficient^2 times that
residual variance). A predictor whose variation overlaps less with the
baseline terms keeps more residual variance, so it can add more R^2 than
another predictor while having a smaller per-SD coefficient. Neither number by itself
establishes whether gamma-weighting by OI (C) or activity (A) adds
information beyond gamma alone, or the reverse; a nested comparison against
a baseline-plus-gamma model would speak to that more directly and is not
reported here (it would be a post-hoc addition, not a pre-specified test).

## Explicit limitations (do not read past these)

- Unsigned measures only. C and A are gamma-weighted open-interest and
  activity concentration -- not signed dealer inventory, not a hedging-flow
  estimate, and not validated against any independent position/trade data.
- Every inference above is over a handful of trading days; nothing here
  supports a trading gate, a causal dealer-hedging claim, or a
  predictive-performance claim. Anchor-level clustering addresses the
  shared-future-path dependence within an anchor; it does not establish
  that sessions or successive anchors are independent, and day-by-day /
  leave-one-day-out are reported as descriptive stability checks, not a
  substitute asymptotic guarantee.
- No threshold search was performed; the 5-minute horizon, 90-second
  staleness cap, and 3-strikes-per-side selection are exactly the issue's
  specified values, not tuned choices.
- `toward` observes one endpoint-to-endpoint distance comparison per anchor,
  not intraminute touches/crossings; it does not establish sustained
  pinning.
- Reported OI can legitimately read 0 well into a session for a given 0DTE
  contract, and some historical unknown OI/volume values were already
  serialized as zero upstream of this analysis and cannot be reconstructed
  by this code -- this is a real data-quality/provenance limitation on C
  specifically, not an artifact of this analysis, and not evidence that
  every zero observed here is an ordinary settled-position reading.
- This revision's source manifest (`out/source_manifest.json`) freezes the
  configuration and exact object keys used AS OF THIS RUN. It is not a
  claim that the original pre-review analysis was frozen before its
  outcomes were inspected -- this correction is retrospective.

## Predictor comparison table (pooled, clustered by anchor)
| predictor       |          coef |            se |           p |   predictor_std |   sd_scaled_coef |   r_squared |    n |
|:----------------|--------------:|--------------:|------------:|----------------:|-----------------:|------------:|-----:|
| (baseline only) | nan           | nan           | nan         |     nan         |    nan           |   0.0410797 | 1824 |
| log_C           |   8.79006e-05 |   3.41416e-05 |   0.0100359 |       0.754739  |      6.6342e-05  |   0.0428445 | 1824 |
| log_A           |   8.58749e-05 |   3.67194e-05 |   0.0193519 |       1.47578   |      0.000126732 |   0.0426124 | 1824 |
| log_gamma_alone |   0.000579708 |   0.00017803  |   0.001129  |       0.0985403 |      5.71246e-05 |   0.0439249 | 1824 |

## log(C) tables

### Day-by-day
|     date |   n |   n_anchor_clusters |         coef |          se |           p |
|---------:|----:|--------------------:|-------------:|------------:|------------:|
| 20260910 | 360 |                  60 |  0.000345284 | 0.000172422 | 0.0452254   |
| 20260911 | 366 |                  61 |  0.000278255 | 9.93806e-05 | 0.00511204  |
| 20260914 | 366 |                  61 |  0.00031084  | 8.44565e-05 | 0.000232801 |
| 20260915 | 366 |                  61 | -2.16206e-05 | 0.000120589 | 0.857709    |
| 20260916 | 366 |                  61 | -0.00038561  | 0.000472428 | 0.414368    |

### Leave-one-day-out
|   held_out |    n |        coef |          se |          p |
|-----------:|-----:|------------:|------------:|-----------:|
|   20260910 | 1464 | 9.27834e-05 | 3.37232e-05 | 0.00593547 |
|   20260911 | 1458 | 8.01981e-05 | 3.86912e-05 | 0.0381935  |
|   20260914 | 1458 | 4.50167e-05 | 4.28961e-05 | 0.293977   |
|   20260915 | 1458 | 9.87109e-05 | 3.58605e-05 | 0.00591173 |
|   20260916 | 1458 | 0.000202891 | 5.53358e-05 | 0.00024586 |

## log(A) tables

### Day-by-day
|     date |   n |   n_anchor_clusters |         coef |          se |           p |
|---------:|----:|--------------------:|-------------:|------------:|------------:|
| 20260910 | 360 |                  60 |  0.000368521 | 0.000187345 | 0.049175    |
| 20260911 | 366 |                  61 |  0.000270833 | 9.37005e-05 | 0.00384739  |
| 20260914 | 366 |                  61 |  0.000409524 | 0.000118599 | 0.000554378 |
| 20260915 | 366 |                  61 | -2.42475e-05 | 0.000122584 | 0.843199    |
| 20260916 | 366 |                  61 | -0.000407012 | 0.000475657 | 0.392173    |

### Leave-one-day-out
|   held_out |    n |        coef |          se |           p |
|-----------:|-----:|------------:|------------:|------------:|
|   20260910 | 1464 | 9.19728e-05 | 3.59779e-05 | 0.0105772   |
|   20260911 | 1458 | 7.36696e-05 | 4.26279e-05 | 0.0839518   |
|   20260914 | 1458 | 4.51799e-05 | 4.27404e-05 | 0.290476    |
|   20260915 | 1458 | 9.62362e-05 | 3.94332e-05 | 0.0146675   |
|   20260916 | 1458 | 0.000220322 | 6.37461e-05 | 0.000547774 |

## log(gamma alone) tables

### Day-by-day
|     date |   n |   n_anchor_clusters |        coef |          se |          p |
|---------:|----:|--------------------:|------------:|------------:|-----------:|
| 20260910 | 360 |                  60 |  0.0014097  | 0.000719362 | 0.0500366  |
| 20260911 | 366 |                  61 |  0.00101787 | 0.000328441 | 0.00194119 |
| 20260914 | 366 |                  61 |  0.00156246 | 0.000480953 | 0.00115949 |
| 20260915 | 366 |                  61 |  7.2575e-05 | 0.000499956 | 0.884582   |
| 20260916 | 366 |                  61 | -0.0013209  | 0.00209441  | 0.528249   |

### Leave-one-day-out
|   held_out |    n |        coef |          se |           p |
|-----------:|-----:|------------:|------------:|------------:|
|   20260910 | 1464 | 0.00063901  | 0.000178186 | 0.000335526 |
|   20260911 | 1458 | 0.000537733 | 0.000215764 | 0.0126944   |
|   20260914 | 1458 | 0.000406834 | 0.000211571 | 0.0544901   |
|   20260915 | 1458 | 0.000646952 | 0.000190336 | 0.000676328 |
|   20260916 | 1458 | 0.000865105 | 0.000228562 | 0.000153714 |
