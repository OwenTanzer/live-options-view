# Share-limit follow-up plan

## Problem

PR #33 was stacked on the share-market branch. PR #32 reached `master` first,
then PR #33 merged into that already-merged feature branch, so the share-limit
commits never became ancestors of `master`. Review also identified that an R2
execution reservation could remain `pending` forever if a worker failed before
writing the account record.

## Plan

1. Branch from the current `master` and carry over the two share-limit commits
   without changing their UI or execution semantics.
2. Give pending reservations a short lease. Permit a same-intent retry to take
   over an expired lease with a conditional ETag write.
3. Refresh the lease immediately before account mutation so the ETag acts as a
   fence: a worker superseded by a retry cannot mutate or finalize the order.
4. Preserve idempotent terminal replay and the existing recovery path for an
   account write that succeeded before R2 finalization.
5. Add focused coverage for lease expiry, failure before the account write,
   stale-owner fencing, and successful recovery; then run the relevant Worker,
   UI, and regression checks.

## Delivery

Open this as a draft follow-up PR against `master`. Do not merge or mark it ready
as part of the review-response workflow.
