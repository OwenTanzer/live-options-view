# Opening OI associator paper strategies

The four inactive Crassus catalog entries each use a separate paper login and the same QQQ opening observation. Each strategy buys one QQQ share when its final path forecast exceeds 2 basis points. A lower forecast leaves the account in cash. The runner records the forecast vector, artifact digest, last training session, frozen opening reference and quote timestamp in its decision ledger.

| Strategy ID | Account username | Forecast |
|---|---|---|
| `oi_associator_ridge` | `crassus_oi_ridge` | Ridge regression |
| `oi_associator_forest` | `crassus_oi_forest` | Random forest |
| `oi_associator_prototype` | `crassus_oi_prototype` | Nearest opening-OI prototype |
| `oi_associator_pls` | `crassus_oi_pls` | Partial least squares |

## Decision-time contract

1. On a regular full NYSE session, consider entry from 09:35:00 through 09:39:59 ET. `entry_grace_seconds` optionally shortens the default 300-second window. This accommodates the runner’s standard five-minute polling interval. A later cycle records a missed entry. The observation cutoff always stays at 09:35:00.
2. Read `GET /api/one-clock/opening-oi?session=YYYY-MM-DD&cutoff=<09:35 ET in epoch milliseconds>` on the configured Crassus HTTPS origin. The existing endpoint validates the append-only R2 opening packet, its availability timestamp, expiry tier, settling session and frozen opening spot. The adapter verifies the session, opening time, artifact lineage and publication cutoff. It requires raw `callOI` and `putOI` quantities for seven consecutive strikes centered on the opening spot rounded half-up. Unknown values within this ladder suppress entry. The collector verifies same-day expiry and carries forward prior-session settled OI. The day’s actual opening spot can first appear after the opening print.
3. Load the packaged JSON bundle at `crassus/oi_associator_v1.json`. Its training sample ends on July 31, 2026. Inference checks the bundle schema and requires a strictly later decision date. One fixed as-of request and successful forecast per session/model are cached across cycles. On restart, the historical cutoff and packaged artifact reproduce the same inputs for the same release.
4. Read the reconciled, fresh paper account history. A valid entry requires available cash and a flat dedicated book with no earlier QQQ purchase that session. Buy at a penny limit capped by the observed ask. The server independently prices the exact share, checks the limit and balance, and journals its fill. A rejected request does not constitute a purchase.
5. Sell a held share at the first regular-session cycle from 15:30 ET, or at the next available regular session if a previous exit remains outstanding. The bid provides the minimum sell price. The account history, exact equity symbol and fresh side-specific quote are required for both buys and sales. The exit runs even during a model or opening-feed outage. The runner’s mandatory exchange-calendar flatten also retains these checks for this cohort.

The four entries remain inactive in `accounts.example.json`. No additional provisioning-gate settings are required for this change. The session source, paper credentials and normal runner deployment are separate operational dependencies. The tracked roster contains only the four password environment variable names.

Run the offline adapter and Worker integration tests from the repository root:

```sh
python crassus/scripts/verify_oi_associator.py
```

The test supplies a synthetic opening reference to the packaged models and exercises real Crassus order submission against a loopback copy of the Worker with in-memory D1/R2. It verifies fresh quote checks, account isolation, fixed availability cutoff, restart and both exit routes. It does not request production quotes or place external paper orders.
