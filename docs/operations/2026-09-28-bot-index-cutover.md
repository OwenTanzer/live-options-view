# Bot-index reader cutover — September 28, 2026

PR #103's dual writers were deployed, but production retained `BOT_INDEX_READS=legacy`.
The operator authorized completing outstanding deployments on September 28.

Two authenticated reconciliation passes, separated by more than a minute, each
scanned 37 account records and produced the same ready index of 31 bot identities.
Both comparisons matched every identity returned by the existing `/api/bots`
roster, with no missing or extra entries. Reconciliation recovered the legacy
`crassus_probetest5` identity from its account record. Accounts and balances were
not rewritten. The checked-in 32-account catalog also contains two Persephone
identities absent from the current production roster; their password settings
are not provisioned in the runner. This cutover preserves the existing inventory
and does not activate those strategies.

This commit changes readers to the ready R2 index. The existing settlement
schedule and dashboard refresh cadence remain unchanged. After deployment,
verify `/api/bots` succeeds with the same 31 identities and inspect scheduled
settlement and KV usage before closing #102. A rollback changes
`BOT_INDEX_READS` back to `legacy`; preserve the index and all account records.
