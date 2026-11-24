# Crypto reader

The Crypto button uses the shared tab registry. The table caption reports the
actual row count and labels live top-50 membership or the embedded fallback.
The MCP reader accepts up to 50 rows and retains the producer's universe block
in both discovery and query responses.

BTC research estimates refresh with the existing five-minute snapshot poll.
An expiry timer removes the displayed estimate at its target end and requests
the next reading. Missing or invalid target ends cannot remain live. An older
in-flight response cannot replace a newer snapshot or estimate.

Offline checks: `node tests/crypto_panel.test.js`,
`node tests/crypto_predictions_panel.test.mjs`, `node tests/boot.test.js`,
`node tests/tab_switching.test.js`, and
`node --test market_mcp/crypto_snapshot.test.js market_mcp/consumer.test.js`.
The dedicated Crypto workflow also runs collector and News fixture checks.
