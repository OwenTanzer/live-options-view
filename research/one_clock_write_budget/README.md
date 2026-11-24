# One Clock write-cost replay

This replay uses the production receipt timestamps and sequence ranges of 304 QQQ collector publications admitted in 27 sweeps from 14:30 to 21:15 UTC on November 23, 2026. The read-only source query grouped one_clock_publication_entries by received_at for collector-chain/QQQ and extracted the first and last price.spot in sequence order. The fixture contains one session and instrument. The D1 test replays the receipt insert and its triggers with a minimal one-session payload. Its counters exclude the separate publication-record insert, observations, other streams and other account traffic.

Keep every generation. A sweep can admit several sequential publications at the same instant, while a queued historical evaluation can still pin an earlier generation. In the first sweep the price at sequence 116 was 652.27 and the price at sequence 122 was 656.25. A last-sequence-only notice would lose the earlier native-change boundary consumed by snapshotVersion.

The candidate removes two indexes on native changes from the replay schema. Both lane-specific indexes remain. At all 304 generation targets and immediately before all 27 receipt cutoffs, the candidate returned the same native sequence, generation and timestamp. Miniflare measured 3,044 versus 2,436 entry-path writes and 1,323 snapshot reads on either schema. The publisher-integration suite independently passed all 20 cases on each schema, including a correction from 701 to 698 that preserved the earlier outcome as of its original cutoff.

Run node --test tests/d1-budget/usage.test.mjs after npm ci in tests/d1-budget. The SQL here is an offline candidate. Before a production migration, audit the account's other D1 readers for use of either general-purpose index.
