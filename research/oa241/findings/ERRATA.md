# Protocol errata

`PROTOCOL.md` (frozen at `0e1d8cc`) is preserved unchanged. These entries record where its text and its own rules disagreed, or where an implementation departed from it. Each entry gives the effect.

## E1. Session count: 36 stated, 34 under the protocol's own rule

The Sample section says "As of freeze this is 36 sessions, Sept 21 – Nov 16, 2026". The same paragraph defines eligibility as status `complete` with no partial reasons and **zero gap and late-start seconds**.

Two complete sessions have small effective late starts:

| Session | Late start |
|---|---|
| Sept 21 | 2.25 s |
| Sept 22 | 3.16 s |

Applying the rule as written gives **34** sessions, Sept 25 – Nov 16. The "36" count was taken from the status column alone, before the late-start field was checked.

The rule governs, not the stated count. Both the superseded `9a282b4` run and the repaired run use 34 sessions. Sept 21 and 22 are listed as excluded in every results file. Including them would add two development sessions before Oct 30 and would not change the holdout.

## E2. LOSO and holdout fits (implementation deviation in `9a282b4`)

**Frozen specification for G5a.** The out-of-sample comparison uses models with session intercepts, and each held-out session's intercept is replaced by the pooled intercept.

**What `9a282b4` did instead.** It fit the training sessions with a single pooled intercept and no session intercepts, for both leave-one-session-out and the Nov 2–16 holdout.

**Repair.** The repaired code implements the frozen specification. It fits session intercepts on the training sessions and predicts the held-out session with the mean of those fitted intercepts. The superseded pooled-fit numbers are still reported (`superseded_pooled_fit_*` in `results.json`) so the effect of the deviation is visible.
