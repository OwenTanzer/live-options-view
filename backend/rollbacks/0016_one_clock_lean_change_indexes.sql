CREATE INDEX IF NOT EXISTS one_clock_changes_at_cutoff ON one_clock_native_changes
  (session,instrument,received_at,generation);
CREATE INDEX IF NOT EXISTS one_clock_changes_at_generation ON one_clock_native_changes
  (session,instrument,generation);
