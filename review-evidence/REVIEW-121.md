# Issue 121 acceptance — November 8, 2026 Pacific

Source revision: 762efafe88425c73dcecb9aefa9963ea80c29781.
Served https://options.moopertonic.net build: 762efaf-20261109050901.
PRs 122, 123 and 124 independently confirmed merged.

## Deployed acceptance
Remote Desktop Commander / UFO2 on MSI, dedicated Chrome window:
- Initial QQQ chain showed 656.51. Selected SPY using the native Options chain underlying dropdown; heading became SPY 0DTE Live OI, price 736.00, view-only and snapshot-relative OI annotation appeared.
- SPY strike 756 displayed call OI 6.6K, bid/ask 0.00/0.01; put OI 399, bid/ask rounded 19.9/20.1. Connector exact row returned call OI 6629, bid/ask 0/0.01; put OI 399, bid/ask 19.93/20.06. Snapshot key intraday/spy/20261106/snapshot_161426703634.csv, timestamp 2026-11-06T21:14:26.703657+00:00, SHA256 cc41b46903d6d8cece9ae938c07e939eb25c4824ef064084af102b0fd4018698.
- The same public latest JSON digest was independently fetched; its exact archived CSV returned HTTP 200 (40308 bytes).
- Selected QQQ again: original QQQ heading and 656.51 restored. Screenshots and native page text retained.
- Separate quote strip SPY tile is a different, explicitly stale price artifact, not the selected chain snapshot. The strip revision belongs to Steve's #178/#179 quest.
- Session-closed and stale states were visible. This was a Sunday inspection of Friday's actual published data, not a live-market quote or new acquisition.
- Connector discovery reports SPY available. Explicit SPY request and retained detail return the same SPY snapshot; omitted underlying returns QQQ. Detail retained the original SPY identity after QQQ queries.
- Dedicated browser window and control REPL closed after inspection.

## Regression verification
- Collector: 18 SPY collection checks passed.
- MCP: 13 SPY tests passed.
- Website chain_underlying: all checks passed, including simulated delayed-response and rapid-switch cases.
- Initial Windows checkout converted LF to CRLF and failed the literal content-hash assertion. Re-extracted exact Git archive bytes locally and reran successfully. No source modification or deployment was needed; original failure retained in chain-tests.txt.

## Disposition
Issue 121 acceptance satisfied: merged implementation, actual production SPY publication, deployed website switching and matching connector evidence, isolated-state regressions. November 9-10 successive-session first.csv checks remain separately scoped to #142. No account action or trading was performed.
