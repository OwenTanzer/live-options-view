# Inactive Trump / OI skew paper hybrids

The IBIT, SLV and DRAM catalog entries remain `active: false`. No brokerage
route, credential provisioning or collector activation is part of this change.
SLV and DRAM are not registered One Clock sources; their publication and
decision export remain outside One Clock.

Each account uses its own root and session anchor. Entry requires directional
agreement. A current neutral, opposite or thin OI band closes a holding; an
opposite Trump reading also closes. Quiet or neutral Trump sentiment alone
retains. Mandatory runner flattening remains separate.

Missing, empty or foreign boards, invalid spot/time, duplicate/out-of-order
observations and sentiment fetch errors retain holdings and prohibit entries.
At runtime both market observation time and sentiment fetch time must be no
more than 360 seconds old and not in the future, matching the forward-skew
causal window. The existing sentiment reader filters posts to its 180-minute
window; fetch age and post age are different quantities. Non-finite sentiment
is unavailable. An observed thin band is distinct from missing market data,
and a repeated thin observation cannot trigger another decision.

Every decision retains root, agreement and available readings in metadata,
including market observation, anchor and sentiment fetch/post timestamps.
The runner records its usual account/strategy identity and snapshot lineage.
Offline checks: `python scripts/verify_trump_oi_skew.py`, reference/forward-skew
fixtures, strategy-state and runner attribution suites. No real-account tests
are required or authorized by this document.
