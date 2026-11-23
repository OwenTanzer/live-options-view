# Provisional SLV/DRAM session

Owner: Brian; integration handoff: Chej. This is an explicitly activated,
partial-session paper experiment for 2026-11-23, separate from the inactive
catalog in #370/#372. No brokerage adapter or #201 code is included.

The collector entry point is `python -m market_data.live_market.provisional`.
Require `PROVISIONAL_SESSION_DATE=2026-11-23`, `COLLECT_SLV=1`, `COLLECT_DRAM=1`,
and a new `LIVE_QUOTE_KEY`. Use the shared Python Dockerfile in a new service,
one replica, no cron, no GitHub master trigger, restart policy NEVER. Copy only
the required read-only tastytrade OAuth and R2 connection settings. Existing
services and credentials remain unchanged. The provider supports concurrent
DXLink connections; this process adds one, at most 268 option symbols and two
underlyings, with no Candle/Order/Series subscription or Tradier stream.

The root-scoped storage wrapper permits only `intraday/slv/`, `intraday/dram/`,
`settlement/slv/`, and `settlement/dram/`. Confirm no existing owner before
starting. No shared health, price lane, weekly archive, baseline promotion,
One Clock publication or decision export is added. Coverage metadata records
the actual start time and partial session. It stops at the existing collector
session boundary and refuses automatic next-day startup.

The API chooses the provisional origin/key only for requests consisting solely
of SLV/DRAM option symbols. Set `PROVISIONAL_QUOTE_ORIGIN` and
`PROVISIONAL_QUOTE_KEY` through the authorized provider secret mechanism; do
not rotate existing keys. With no provisional origin the old behavior remains.
With an origin but missing key it fails closed. The quote server accepts only
its startup contract window. Release API changes through the exact-revision
protected API workflow before enabling the paper runner.

The new runner uses `deployment/python/Dockerfile.provisional-paper` from the
repository root with service-managed settings (no inherited Crassus manifest).
It uses `crassus/accounts.provisional.json` explicitly, containing
only the eleven new paper accounts. It requires a separate persistent `/data`
volume (`CRASSUS_DATA_ROOT=/data`), fresh account credentials, and the existing
bot registration capability. Do not copy the shared runner's account roster,
brokerage settings or One Clock exporter credentials. Keep
`CRASSUS_ONE_CLOCK_PUBLICATION=disabled`. Retain local decision logs and state.

After the affected capture session and finalization end, confirm no pending
paper orders/open positions need the provisional executor. Stop new entries,
reconcile holdings, and transfer account state/logs before giving a shared
runner ownership. Stop the old collector before enabling either root in the
shared collector; never overlap writers. Recheck current PR bases, exact-SHA
CI, ordinary review and parallel releases before merge. Preserve API-before-
assets order. Remove provisional quote routing only after the replacement
serves the same contracts. Retain partial-day evidence. If any prerequisite
fails, keep separation and report the held step; a clock time alone is not
permission to terminate a live session.
