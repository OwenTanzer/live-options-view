# tastytrade OAuth operations

The Railway `live-options-view` collector uses a personal tastytrade OAuth application
with the single `read` scope. It must never receive `trade`, order-entry, write, or
OpenID scopes.

## Provisioning

Create the personal application and grant through tastytrade's official OAuth Applications
page. Use `http://localhost:8000` only when a redirect URI is required; personal refresh-token
authentication does not bind the Railway collector to that device or address.

Store the generated credentials only as secret variables on the Railway `production`
environment's `live-options-view` service:

- `TASTY_OAUTH_CLIENT_SECRET`
- `TASTY_OAUTH_REFRESH_TOKEN`
- `TASTY_OAUTH_SCOPES=read`

Never put their values in source, commits, issues, pull requests, test fixtures, logs, or
screenshots. The collector validates that the configured scope set is exactly `read` before
making any tastytrade request.

## Rotation

1. Create a replacement read-only application/grant in tastytrade.
2. Stage only the two replacement secret values on `live-options-view`; preserve all other
   Railway variables.
3. Keep the Railway start command suspended until hermetic tests and the controlled probe
   gates below pass.
4. After the replacement credentials are proven and restoration is explicitly reviewed,
   revoke the superseded grant in tastytrade.

The legacy R2 object at `auth/remember_token.json` is intentionally ignored. Do not read,
log, or delete it during the OAuth rollout; removal is a separate reviewed cleanup action.

## Rollout gates

Before restoring the collector:

1. Run `python -m unittest discover -s tests -p test_auth_safety.py -v`.
2. Run the complete collector CI suite.
3. Confirm production contains no call to `/sessions` and no password, TOTP, or
   remember-token fallback.
4. Apply the Railway variables while preserving the suspended start command.
5. Run `python scripts/probe_tasty_oauth.py --confirm-read-only` once in a controlled
   environment containing the production OAuth secrets. It exchanges the refresh token,
   obtains an API quote token, performs GET-only option-chain access, connects to DXLink,
   receives option market data, and exits. The probe contains no order or trade endpoint.
6. Review the probe result explicitly before changing the start command.
