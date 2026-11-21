# Local verification — November 21, 2026

Branch: `steve/monster-hunt-btc-paper`.
Base: `0a3853df0692e8c9bdff762fc0267fc4f9eda447`.
Implementation commits observed and inspected in this shared checkout:

- `80758955aaad635584b5f7615215509ec7c21334` — private discovery and stream.
- `99861ad86f978d3893206370a6b5837e0cec6c0d` — paper ledger and settlement.
- `93f559d02ca1d106bb4f46d515df0f991c1c7808` — ticket, positions and mobile fixture.

[Draft pull request #295](https://github.com/OwenTanzer/live-options-view/pull/295) contains the implementation.

## Passing checks

All commands below ran locally against the implementation and fixtures. Node's
tests were run directly (without `--test` subprocess isolation, which is blocked
here). Node 24.15.0 supplies SQLite; Python is 3.14. No credentialed collector ran.

| Command | Result / coverage |
|---|---|
| `node tests/btc_options.test.mjs` | 12 pass: call/put round trips, quote guards, limits, insufficient funds, duplicate request, concurrent closes, post-mutation finalization recovery, independent entry disable, six ITM/ATM/OTM expiry cases, missing/later official mark, auth/public proxy exclusions, equity-insolvency retention of BTC obligations |
| `node tests/btc_options_ui.test.mjs` | Pass: executable-side marking, stale/missing/crossed quote, selection response race, held refresh away from chain, browser/server fee-book parity |
| `python -m unittest discover -s tests -p test_btc_options.py -v` | 6 pass: source identity, monthly filters, bounded window, held restart lookup, monthly roll, reconnect invalidation, withdrawn side, DST maintenance, fixture 403, preservation of existing subscription owners |
| `node tests/worker_logic.test.js` | Pass: existing auth/execution/settlement logic |
| `node tests/paper_trading.test.js` | Pass: existing browser trading workflow and module hash |
| `node tests/multi_underlying.test.js` | Pass: QQQ/SPY/IBIT and legacy execution/settlement |
| `node tests/account_closure.test.js` | Pass: terminal bot closure, retry recovery and history |
| `node tests/order_logic.test.js` | Pass |
| `node tests/live_quote_service.test.js` | Pass: interests, polling, health, HTTP lane behavior |
| `node tests/chain_underlying.test.js` | Pass: equity chain selection and isolation |
| `node tests/browser_data.test.js` | Pass |
| `node tests/r2_proxy_bounded.test.js` | Pass |
| `node tests/no_runtime_kv.test.mjs` | Pass |
| `node tests/storage_d1.test.mjs` | Pass |
| `node tests/settlement_cron_outcome.test.mjs` | 13 pass |
| `node tests/api_cron_schedule.test.mjs` | Pass: existing schedule unchanged |
| `node tests/capture_write_boundary.test.js` | Pass |
| `node tests/build_stamp.test.mjs` | 9 pass |
| `node tests/preview_isolation.test.mjs` | 6 pass |
| `node tests/preview_storage_boundary.test.mjs` | 7 pass |
| `python -m unittest discover -s tests -p test_price_lane.py -v` | 52 pass |
| `python -m unittest discover -s tests -p test_feed_reauth.py -v` | Pass |
| `python -m unittest discover -s tests -p test_price_observations.py -v` | 64 pass |
| `python -m unittest discover -s tests -p test_multi_underlying.py -v` | 13 pass after pinning its pre-existing date-sensitive fixture clock |

`node --check` passed for the new backend module, execution, paper-trade,
settlement, BTC UI, trade ticket and fetch-cycle modules. `python -m py_compile`
passed for BTC discovery, feed, quote service and price lane.

The round trips buy at 100 USD/BTC and close at 120 USD/BTC five fixture seconds
later. Gross cash debit/credit are 500/600; cash including fees is -502.50/+597.50.
Starting 10,000 becomes 10,095; gross realized profit is 100, fees 5, net 95.
Both side timestamps, sizes, metadata and fees are read back from D1. No fixture
quote is evidence of an actual CME observation.

The existing Python absence-reason test had an unpinned November 20 contract that
had become expired on November 21. It now uses the suite's existing fixed clock;
no production equity expiration behavior was changed to make it pass. Windows
checkout CRLF initially invalidated unchanged browser-module content hashes.
Checks were rerun against canonical LF script bytes; changed script references
match their canonical content.

## Browser and release checks

Steve verified the buy, reload, missing-bid, later-bid close, fee total and equity-selection change in headless Chromium at 390 px after adding the BTC API route to the preview gateway allowlist. The fixture uses the real gateway, account API and site scripts with disposable market data and storage.

All 78 changed-path tests passed on MSI, as did the asset boundary and preview transport checks. On Windows, the asset rollback test expects an exception for a case-sensitive asset configuration filename and fails identically on the untouched base revision and on this branch. GitHub Web CI and Collector CI passed on the initial three implementation commits. The pull request's Checks tab records the current revision.

## Open external acceptance

Authenticated tastytrade source discovery, exchange-time two-sided quotes/sizes,
account coverage, audience display/use approval and a permitted official CME final
mark delivery source remain unverified. No entitlement error was observed because
no authenticated live probe was attempted. The implementation consumes retained
official final-mark records; it does not include a licensed production importer.

Keep all new entries disabled until those gates and the tests above are complete.
First deployed weekend/mobile buy/reload/later-bid-close evidence is pending; when
liquidity is absent, retain that observation instead of substituting fixture fills.
See [product guide and staged controls](BTC_PAPER_OPTIONS.md).
