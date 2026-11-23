# Multi-underlying browser evidence (QQQ / SPY / IBIT)

Produced by `tests/multi_underlying_browser.test.js` — the optional rendered
integration, which drives a real Chromium against the shipped page, the shipped
Worker route and synthetic snapshots. Nothing here is a production request, and
no screenshot is a claim about live market data.

Run it with a Playwright installation:

```
NODE_PATH=<playwright installation>/node_modules node tests/multi_underlying_browser.test.js
```

## What these runs establish

The fixtures are the shapes the collector actually publishes. QQQ and SPY use a
contiguous $1 ladder (81 and 71 strikes) around the ±20 window. IBIT uses the
exact 41 strikes published on 2026-11-10: whole dollars 30–39 and 51–59, half
dollars 39.5–50.

- `desktop-qqq.png`, `desktop-spy.png` — 41 rendered rows each, every call and
  put cell populated, header live with time, session and age.
- `desktop-ibit.png` — the mixed IBIT ladder from 53 down to 30 around the
  41.5 ATM star, every call and put cell populated, and a header that reads
  `snapshot 16:14 ET · After hours · 6m ago · retained` (#178).
- `narrow-ibit.png`, `narrow-spy.png`, `narrow-qqq.png` — the same chains at
  390px, populated, no horizontal overflow of the document.
- `desktop-ibit-missing.png` — a 404 IBIT snapshot is reported as unavailable
  and leaves no stale snapshot line on screen.

The run also asserts, from the page's own `/api/live-quotes` requests, that
quote interest covers both sides of exactly the rendered strikes, that every
requested IBIT contract is on the published ladder, that no contract is asked
for twice, and that switching back to QQQ leaves IBIT contracts out of the
request.

## What these runs do not establish

- No deployed readback: this is the local page and Worker source at the commit
  containing the fix, not `options.moopertonic.net`.
- Quote *values* are synthetic. The production option-quote lane was returning
  HTTP 503 for every option contract while this work was done (see below), so
  populated quote chips here prove contract selection, not quote availability.
- The table is wider than 390px by design (`#heatmap-wrap` scrolls); "no
  overflow" means the document does not scroll horizontally, not that the whole
  table is visible at once.

## Separate observation: option-quote availability (2026-11-10, 23:19Z)

Recorded while diagnosing the "Options: backing off" status that accompanied
#219. The chain grid defect is independent of it.

| Request | Result |
| --- | --- |
| `symbols=QQQ,SPY` (equities) | 200, live provider quotes |
| `symbols=QQQ` (equity) | 200 |
| `symbols=NVDA` (equity) | 502 |
| `symbols=IBIT` (equity) | 503 |
| `symbols=QQQ261110C00677000` (1 option) | 503, `Retry-After: 5` |
| `symbols=QQQ261110C00677000,QQQ261110P00677000` | 503 |
| `symbols=SPY261110C00750000`, `QQQ261113C00677000`, `IBIT261113C00041000` | 503 each |
| `symbols=IBIT261110C00041000` | 200 **only** from the retained 19:39:43Z response |

So every option-contract request failed across all three underlyings and
multiple expiries, while equity requests succeeded for QQQ and SPY. The page's
behaviour under that condition is correct: it reports `unavailable` and then
`backing off` with the upstream `Retry-After`, and it keeps the last values
rather than blanking them. The producer/lane cause is not established here and
is not fixed by this change.
