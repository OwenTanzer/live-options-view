-- Drop the two general-purpose indexes on one_clock_native_changes.
-- Replay-tested offline: 608 fewer entry-path writes across 304 QQQ
-- publications (2026-11-23), snapshot results unchanged.
-- Preserves every native generation and both lane indexes
-- (one_clock_lane_at_time, one_clock_lane_at_generation).
-- Outside-repository reader check: PENDING on Owen's side — do not apply
-- to production D1 until confirmed. Kalshi minute capture is untouched.
DROP INDEX IF EXISTS one_clock_changes_at_cutoff;
DROP INDEX IF EXISTS one_clock_changes_at_generation;
