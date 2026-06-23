# Product Design Document — MOO-13: Live OI Viewer + Intraday Chain Pipeline

**Status:** Pre-deployment review  
**Date:** 2026-06-23  
**Repo:** `live-options-view` — github.com/OwenTanzer/live-options-view  
**Related:** Desktop OI viewer lives separately in `OwenTanzer/options-view`

---

## 1. Problem Statement

The existing desktop app (`OptionsView`) shows only the **morning snapshot** of QQQ's 0DTE options chain, sourced once per day at open. This is useful for identifying OI walls at the start of the session, but gives no visibility into how the chain evolves intraday — which strikes are seeing volume flow, where gamma is being bought or sold, and whether the morning's OI structure is being tested or reinforced as price moves.

In high-volatility sessions (elevated KOSPI selloffs, JPY flight, NASDAQ double-tops), this blind spot is a material risk gap. We need intraday resolution.

Additionally, the existing product is a desktop download. A web-accessible live view removes the distribution friction and reaches users who are in-session with no time to install anything.

---

## 2. Product Scope (MOO-13)

Two tightly coupled deliverables:

| Deliverable | Description |
|---|---|
| **Intraday chain collector** | Runs on Railway. Authenticates with tastytrade, subscribes to QQQ 0DTE chain + 10 macro tickers via DXLink websocket. Uploads snapshots to Cloudflare R2 every 11 minutes throughout the session. |
| **Live web viewer** | Static page on `moopertonic.net`. Reads R2 directly. Renders the calibrated OI heatmap (same color scheme as the desktop app) with live volume overlaid. Also shows a live price strip for 10 macro indicators, refreshing every 30 seconds. |

**Out of scope for this release:** historical intraday replay, WebSocket push to the browser (uses polling), multi-symbol heatmaps, authentication/access control.

---

## 3. Architecture

```
┌─────────────────────────────┐
│  tastytrade / DXLink        │
│  (websocket, real-time)     │
└────────────┬────────────────┘
             │ Quote, Trade, Summary, Greeks events
             ▼
┌─────────────────────────────┐
│  Railway — collector.py     │  ← runs 6:00 AM – 4:15 PM ET daily
│                             │
│  • QQQ 0DTE chain (±33)    │──▶ intraday/YYYYMMDD/snapshot_HHMM.csv
│  • 10 macro price tickers  │──▶ intraday/latest.json          (11 min)
│                             │──▶ intraday/prices.json          (30 sec)
└─────────────────────────────┘
             │
             ▼ S3-compatible PUT
┌─────────────────────────────┐
│  Cloudflare R2 (public)     │
│  pub-4d5c916b8cb74ffb8c0…  │
│                             │
│  intraday/latest.json       │  OI heatmap data + metadata
│  intraday/prices.json       │  live price strip data
│  intraday/YYYYMMDD/*.csv   │  archived per-session snapshots
│  derived/OIranges.csv       │  (existing) calibration thresholds
└──────────────┬──────────────┘
               │ public HTTP GET
               ▼
┌─────────────────────────────┐
│  Cloudflare Pages           │
│  moopertonic.net/live.html  │
│                             │
│  • Price strip  (30s poll) │
│  • OI heatmap  (60s poll)  │
│  • No server-side logic    │
└─────────────────────────────┘
```

---

## 4. Data Pipeline Detail

### 4.1 Auth flow

1. `POST /sessions` → `session-token` (tastytrade credentials in Railway env vars)
2. `GET /api-quote-tokens` → `streamer-token` + `streamer-url` (DXLink websocket endpoint)
3. DXLink SETUP → AUTH → CHANNEL_REQUEST → FEED_SETUP → FEED_SUBSCRIPTION

Session established once at startup. No scheduled refresh — if the session expires (24h), Railway restarts the process and re-authenticates.

### 4.2 Option chain sourcing

Chain structure is loaded once at startup via `GET /option-chains/QQQ/nested` (tastytrade REST). This returns all strikes + expirations with their DXLink streamer symbols (`.QQQ260623C00480000` format). The collector picks today's expiration; pre-market, it falls back to the nearest upcoming expiry.

Live quote/Greeks data flows via DXLink — the REST chain call provides structure only.

### 4.3 DXLink subscriptions

| Symbol class | Event types | Fields |
|---|---|---|
| QQQ option chain (±33 strikes × 2 sides) | Quote, Summary, Trade, Greeks | bid, ask, openInterest, prevDayClosePrice, dayVolume, price, volatility, delta, gamma, theta, vega |
| Price tickers (10 symbols) | Quote, Trade, Summary | bid, ask, price (last), dayVolume, prevDayClosePrice, dayOpenPrice |

### 4.4 Snapshot cadence

| Output | Cadence | Contents |
|---|---|---|
| `intraday/latest.json` | Every 11 minutes | Full OI/volume chain snapshot + metadata (tier, expiration, underlying price) |
| `intraday/prices.json` | Every 30 seconds | Price, bid/ask, % change from prev close for all 10 tickers |
| `intraday/YYYYMMDD/snapshot_HHMM.csv` | Every 11 minutes | Archived CSV in same schema as daily files |

### 4.5 CSV schema (intraday snapshots)

Identical to the existing daily backfill schema for compatibility with future ML pipeline (MOO-12):

```
TradeDate, Expiration, Strike, Type, OptionSymbol, DTE,
OpenInterest, Volume, Bid, Mid, Ask, Last,
IV, Delta, Gamma, Theta, Vega, UnderlyingPrice
```

> **Important:** `OpenInterest` in intraday snapshots reflects the prior day's settled OI (OCC doesn't publish real-time OI intraday — this is a market structure limitation, not a pipeline bug). `Volume` is the live intraday accumulation and is the primary real-time signal.

### 4.6 Tier classification

The collector classifies today as `0DTE_Regular`, `0DTE_Weekly`, or `0DTE_Monthly` using the same holiday-corrected NYSE calendar logic as the desktop app. Tier is embedded in `latest.json` and used by the web viewer to apply the correct OI threshold multipliers from `OIranges.csv`.

---

## 5. Web Viewer Feature Spec

### 5.1 Price strip

Located at the top of `moopertonic.net/live.html`.

**Main row (large):** QQQ · USO · VIX · SMH · IGV · JPY/USD  
**Secondary row (small):** BTC/USD · META · GOOGL · AMZN · TSLA

Each tile shows:
- Ticker label
- Current price (last trade; bid/ask mid if no trade yet)
- % change from prior close (sourced from DXLink `Summary.prevDayClosePrice`)
- Green/red flash animation on price tick
- Special formatting: JPY/USD displays as `¥155.3` (USD/JPY handle, inverted from raw CME quote); BTC as integer; VIX to 2dp

Polling: `intraday/prices.json` every **30 seconds** (independent of heatmap cycle).  
Pre-market behavior: equities and JPY/USD available from ~6:00–6:30 AM ET. BTC/USD is 24/7. VIX may be flat until ~6:30 AM ET.

### 5.2 OI heatmap

Renders ±20 strikes from ATM (same default window as the desktop app).

**Cell background color:** OI bucket level (0–5), calibrated using `OIranges.csv` with tier-adjusted thresholds. Same 6-level green/red palette as the desktop app.

| Level | Calls | Puts |
|---|---|---|
| 0 — zero OI | `#0d1117` | `#0d1117` |
| 1 — < p25 | `#0a1f14` | `#1a0d0d` |
| 2 — p25–p50 | `#0a3020` | `#2a0a0a` |
| 3 — p50–p75 | `#007730` | `#881100` |
| 4 — p75–p90 | `#00cc55` | `#ee3300` |
| 5 — > p90 (wall) | `#88ffcc` | `#ffaa88` |

**Cell text:** OI value (abbreviated: `12.5K`). Volume shown as superscript in top-right of cell.

**Strike column:** Displays as offset from ATM (`+3`, `ATM`, `-2`). Hover shows absolute strike price.

**Header:** QQQ price · expiration date · tier badge (Regular / Weekly / Monthly, color-coded).

**Status bar:** Live dot (green), last snapshot time, countdown to next refresh.

Polling: `intraday/latest.json` every **60 seconds**.

---

## 6. Price Ticker Symbol Reference

| Display label | DXLink symbol | Notes |
|---|---|---|
| QQQ | `QQQ` | ETF equity |
| USO | `USO` | Oil ETF |
| VIX | `$VIX.X` | CBOE VIX index (dxfeed format) |
| SMH | `SMH` | Semiconductor ETF |
| IGV | `IGV` | Software ETF |
| JPY/USD | `/6J:XCME` | CME yen futures, USD-per-JPY; displayed inverted as `¥155.3` |
| BTC/USD | `BTC/USD:CXERX` | Coinbase spot via tastytrade crypto feed |
| META | `META` | Equity |
| GOOGL | `GOOGL` | Equity |
| AMZN | `AMZN` | Equity |
| TSLA | `TSLA` | Equity |

### Symbol risk

Three symbols have non-standard formats and need verification on first deploy:

| Symbol | Risk | Fallback to check |
|---|---|---|
| `$VIX.X` | dxfeed index format — tastytrade may use a different prefix | `VIX.XO`, `VIX` |
| `/6J:XCME` | CME front-month format — may require active contract suffix | `/6JU6:XCME` (Sep), `/6JZ6:XCME` (Dec) |
| `BTC/USD:CXERX` | Exchange routing code may differ | `BTC/USD`, `BTC/USD:XCBT` |

The collector logs `WARN {label} ({symbol}) NO DATA` immediately after the 20-second flush, and repeats the warning in every `prices.json` push cycle. Railway logs are the diagnostic surface.

---

## 7. Infrastructure Requirements

### 7.1 Railway environment variables

| Variable | Value |
|---|---|
| `TASTY_LOGIN` | tastytrade username |
| `TASTY_PASSWORD` | tastytrade password |
| `R2_ACCOUNT_ID` | Cloudflare account ID (from R2 dashboard) |
| `R2_ACCESS_KEY_ID` | R2 API token access key (write-enabled) |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET_NAME` | `pub-4d5c916b8cb74ffb8c0abd7dfadb02cf` |

### 7.2 Cloudflare R2 — CORS configuration

Required for `moopertonic.net` to fetch `latest.json` and `prices.json` from the browser.

In R2 dashboard → bucket → Settings → CORS:

```json
[
  {
    "AllowedOrigins": ["https://moopertonic.net", "http://localhost"],
    "AllowedMethods": ["GET"],
    "AllowedHeaders": ["*"],
    "MaxAgeSeconds": 60
  }
]
```

Without this, the web page silently fails to load data (CORS block in browser console).

### 7.3 Cloudflare Pages

- **New Pages project** required — separate from the `options-view` project (desktop download page)
- Source: `OwenTanzer/live-options-view`, build output directory: `docs`
- `live.html` deploys automatically on every push to `master`
- No build command required (pure static HTML)
- Accessible at the Cloudflare Pages URL (or a custom subdomain of moopertonic.net)

### 7.4 R2 write permissions

The R2 API token must have **Object Read & Write** on the target bucket. The existing token may be read-only (used historically for the public manifest). A new token with write access may need to be generated in the Cloudflare dashboard.

---

## 8. Known Limitations

| Limitation | Impact | Notes |
|---|---|---|
| OI is prior-day settled | OI walls are static until next morning | By design — OCC doesn't publish live OI. Volume is the live intraday signal. |
| 11-minute snapshot granularity | Intraday events can't be pinpointed to the minute | Acceptable for v1; future work to log continuous DXLink events to R2 |
| No access control on `live.html` | Anyone with the URL can see the live feed | Acceptable for now; Cloudflare Access can gate it if needed |
| Railway restarts lose session mid-session | Re-auth adds ~30s gap in data | DXLink reconnect is automatic; gap shows as a missing snapshot file |
| Pre-market option chain availability | Today's 0DTE expiry may not appear in the chain until near-open | Collector falls back to nearest future expiry; snapshots labeled correctly |
| Single 0DTE expiration shown | On Thursdays, there may also be a Friday chain of interest | Out of scope for v1 |

---

## 9. Pre-Deployment Checklist

### Credentials
- [ ] tastytrade credentials confirmed active and not MFA-blocked for API access
- [ ] R2 write-enabled API token created (separate from existing read-only token)
- [ ] All Railway env vars entered and saved

### Infrastructure
- [ ] Railway service created, repo `live-options-view`, root directory `/`, start command `python collector.py`
- [ ] R2 CORS rule applied (verify with browser DevTools → Network on `live.html`)
- [ ] New Cloudflare Pages project created for `live-options-view` (separate from `options-view`), serving from `docs/`

### First-run validation (pre-market)
- [ ] Railway logs show tastytrade session established
- [ ] DXLink channel opens and AUTHORIZED message appears
- [ ] Post-flush health check: all 11 tickers (10 price + QQQ underlying) show `OK` or known `WARN`s are investigated
- [ ] `intraday/prices.json` appears in R2 within 30 seconds of startup
- [ ] `moopertonic.net/live.html` price strip populates (not showing `—` across the board)
- [ ] Any `WARN` symbols identified → correct DXLink symbol in `PRICE_TICKERS` → redeploy

### At-open validation
- [ ] `intraday/latest.json` appears in R2 after first 11-minute mark
- [ ] OI heatmap renders on `live.html` with color and OI values
- [ ] ATM row highlighted correctly vs. current QQQ price
- [ ] Volume superscripts updating across snapshots
- [ ] CSV archived at `intraday/YYYYMMDD/snapshot_HHMM.csv`

---

## 10. Future Work (post v1)

- **Continuous intraday logging** — write every DXLink event to R2 at tick level rather than polling snapshots; enables minute-by-minute replay
- **Multi-expiry view** — show EoW alongside 0DTE in a side-by-side panel
- **MOO-12 integration** — feed intraday snapshots to ML associator for chain-evolution-based signals
- **Access control** — gate `live.html` behind Cloudflare Access for subscriber-only distribution
- **Alert system** — detect when a strike crosses from p75 to p90 OI bucket and push a notification
