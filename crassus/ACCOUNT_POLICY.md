# Terminal bot account closure

Owner policy, September 6, 2026: when a bot receives a confirmed
insufficient-balance execution rejection, close its account and stop trading.
This is a terminal experiment rule, not a funding/reset policy. The trigger
is the server's balance rejection, including insufficient buying cash for an
order; it is not a claim that cash equals net liquidation value.

The Worker retains the username, credentials, balance and trade history and
marks the account closed. New buys and sells are rejected. The closing
rejection is retained with the account before R2 finalization, so replay can
recover a crash in between without another quote or trade. Earlier execution
IDs remain replayable for audit recovery. Bot settlement insolvency also
retains a closed account instead of deleting its identity and permitting
automatic re-registration with fresh funds.

Closed accounts remain readable and labeled in the roster. Historical
positions are retained; closure does not fabricate liquidation fills or reset
balances. The runner reconciles pending audit records before retirement and
skips closed accounts on later cycles and after restart. An unresolved audit
recovery is retried, even though the server prohibits new trading.

Operator-authenticated bot registration and metadata accept 3–40 character
usernames, covering the existing Phelps catalog. Human registration remains
3–20 characters. Existing identities are not renamed or overwritten.
