# Product Design Document — MOO-13: Live OI Viewer + Intraday Chain Pipeline

**Status:** Live / Active development
**Last updated:** 2026-06-23
**Repo:** `live-options-view` — github.com/OwenTanzer/live-options-view
**Live URL:** `options.moopertonic.net`
**Related:** Desktop OI viewer lives separately in `OwenTanzer/options-view`

---

## 1. Problem Statement

The existing desktop app (`OptionsView`) shows only the **morning snapshot** of QQQ's 0DTE options chain, sourced once per day at open. This gives no visibility into how the chain evolves intraday — which strikes are seeing volume flow, where gamma is being bought or sold, and whether the morning's OI structure is being tested or reinforced as price moves.

In high-volatility sessions (elevated KOSPI selloffs, JPY flight, NASDAQ double-tops), this blind spot is a material risk gap. We need intraday resolution.

The product is a live web view at `options.moopertonic.net`, accessible from any device with no installation required.

---

## 2. Architecture

```
┌─────────────────────────────────┐
│  tastytrade / DXLink            │
│  (websocket, real-time)         │
└───────────────┬─────────────────┘
                │ Quote, Trade, Summary, Greeks, TradeETH events
                ▼
┌─────────────────────────────────┐
│  Railway — collector.py         │  ← runs 6:00 AM – 4:15 PM ET daily
│                                 │
│  • QQQ 0DTE chain (±67 strikes)│──▶ intraday/YYYYMMDD/snapshot_HHMMSSffffff.csv  (60s)
│  • 14 macro/indicator tickers  │──▶ intraday/latest.json                          (60s)
│  • yfinance fallback for all   │──▶ intraday/prices.json                          (30s)
│                                 │──▶ intraday/health.json                          (15s)
└─────────────────────────────────┘
                │ S3-compatible PUT
                ▼
┌─────────────────────────────────┐
│  Cloudflare R2                  │
│  bucket: qqq-options-chain-data │
│                                 │
│  intraday/latest.json           │  OI heatmap data + metadata (last good snapshot)
│  intraday/prices.json           │  live macro price strip
│  intraday/health.json           │  collector lifecycle telemetry
│  intraday/YYYYMMDD/*.csv        │  archived per-session snapshots (every 60s)
│  derived/OIranges.csv           │  OI color calibration thresholds
│  auth/remember_token.json       │  rotated tastytrade remember-me token
└───────────────┬─────────────────┘
                │ public HTTP GET (via Cloudflare Worker)
                ▼
┌─────────────────────────────────┐
│  options.moopertonic.net        │
│  docs/index.html (static)       │
│                                 │
│  • Price strip    (30s poll)   │
│  • OI heatmap     (60s poll)   │
│  • No server-side logic        │
└─────────────────────────────────┘
```

---

## 3. Auth Flow

### 3.1 tastytrade session

1. Try R2-persisted remember-token first (`auth/remember_token.json`)
2. If rejected (403) or missing, fall back to `POST /sessions` with `TASTY_LOGIN` + `TASTY_PASSWORD`
3. If MFA challenge required, complete automatically using `TASTY_TOTP_SECRET` via `pyotp`
4. `GET /api-quote-tokens` → `streamer-token` + `streamer-url` (DXLink WebSocket endpoint)
5. Rotate and save new remember-token to R2 after each successful auth

Session is established once at startup. The token is rotated on every auth, persisted to R2, and reused on the next deployment to avoid repeated TOTP challenges.

### 3.2 DXLink connection sequence

```
SETUP → (server) AUTH_STATE:UNAUTHORIZED → AUTH (with streamer-token)
      → AUTH_STATE:AUTHORIZED → CHANNEL_REQUEST
      → CHANNEL_OPENED → FEED_SETUP (with acceptEventFields)
      → FEED_CONFIG → FEED_SUBSCRIPTION (batched, ≤200/message)
      → streaming FEED_DATA events
```

**Critical protocol details:**
- `acceptEventFields` must include `"eventType"` as the first field in every event type list, or the server strips it and all events become unclassifiable
- Subscriptions must wait for `FEED_CONFIG` before being sent, or the server silently drops them
- `FEED_CONFIG` is sent after each subscription batch; a `_subscribed` flag prevents the batch from being re-sent on subsequent `FEED_CONFIG` messages
- All subscriptions are batched at ≤200 per `FEED_SUBSCRIPTION` message to stay under the 65,536-byte WebSocket frame limit
- The DXLink server sends string `"NaN"` for `openInterest` and `dayVolume` on some options; all numeric fields go through `_to_int()` / `_to_float()` converters that handle this

### 3.3 Automatic token refresh

If DXLink auth fails 3+ consecutive times (server returns `UNAUTHORIZED`), the snapshot loop re-fetches a fresh streamer token from tastytrade and calls `feed.update_token()`. The `run_forever(reconnect=5)` loop then reconnects with the new token on the next natural retry. A `restart_if_dead()` check runs each snapshot loop iteration to revive the WebSocket thread if `ws.close()` ever killed it.

---

## 4. Option Chain Sourcing

### 4.1 Symbol format

DXLink requires its own option symbol format, **not** OCC format:

| Format | Example | Notes |
|---|---|---|
| OCC | `QQQ260623C00713000` | Used in REST chain API |
| OCC-dot (wrong) | `.QQQ260623C00713000` | Silently returns 0 events |
| **dxFeed (correct)** | `.QQQ260623C713` | Plain numeric strike, no padding |

Strike encoding: integer strikes use the integer directly (`713`); half-strikes use `strike × 100` with trailing zeros stripped (`713.5` → `71350`).

### 4.2 Chain load

`GET /option-chains/QQQ` (flat endpoint, not `/nested`) is used because only the flat endpoint returns `streamer-symbol`. The collector builds dxFeed symbols via `_build_symbol()` rather than relying on the API's `streamer-symbol` field.

---

## 5. Data Pipeline

### 5.1 DXLink subscriptions

| Symbol class | Event types | Key fields |
|---|---|---|
| QQQ 0DTE chain (±67 strikes × 2 sides = up to 268 symbols) | Quote, Summary, Trade, Greeks | eventType, bidPrice, askPrice, openInterest, prevDayClosePrice, dayOpenPrice, dayVolume, price, volatility, delta, gamma, theta, vega |
| Price tickers (14 symbols) | Quote, Trade, TradeETH, Summary | eventType, bidPrice, askPrice, price, dayVolume, prevDayClosePrice |

DXLink does not deliver equity Quote events for ETFs/equities in the standard feed. All 14 price tickers fall back to yfinance.

### 5.2 yfinance fallback

Every price ticker is filled by yfinance if DXLink has no data. This covers pre-market and any DXLink outage. The most recent yfinance QQQ price is cached in `_last_spot` and used as the underlying price fallback for ATM centering in snapshots.

### 5.3 Snapshot cadence

| Output | Cadence | Guard |
|---|---|---|
| `intraday/YYYYMMDD/snapshot_*.csv` | Every 60s | Always written |
| `intraday/latest.json` | Every 60s | **Only written when bid_count > 0** — preserves last good snapshot during outages |
| `intraday/prices.json` | Every 30s | Always written |
| `intraday/health.json` | Every 15s | Always written |

The `latest.json` guard is critical for display continuity: when DXLink is down, `latest.json` retains the last snapshot with real option data rather than being overwritten with null bids.

### 5.4 Startup state restoration

On every session start (including redeployments), `restore_state()` reads the most recent today's snapshot CSV from R2 and seeds:
- `_prev_vol` — per-symbol cumulative volume baseline, so `VolDelta` is accurate from the first snapshot without a one-beat gap
- `_last_spot` — underlying price fallback
- `_last_prices` — all 14 macro prices for CSV columns

### 5.5 CSV schema

```
TradeDate, Expiration, Strike, Type, OptionSymbol, DTE,
OpenInterest, Volume, VolDelta,
Bid, Mid, Ask, Last,
IV, Delta, Gamma, Theta, Vega,
UnderlyingPrice,
QQQ, USO, VIX, SMH, IGV, 10Y, JPY_USD, BTC_USD, META, GOOGL, AMZN, TSLA, MU, SPCX, Silver
```

The 14 macro price columns are repeated on every row (denormalized). This allows any snapshot CSV to be loaded as a standalone DataFrame with full macro context for that timestamp.

`VolDelta` = `Volume - prev_snapshot_volume` (clamped ≥0), giving contracts traded in the last 60-second window.

> **Note:** `OpenInterest` reflects prior-day settled OI. OCC does not publish real-time intraday OI — this is a market structure limitation. `Volume` and `VolDelta` are the live intraday signals.

### 5.6 Tier classification

Each session is classified as `0DTE_Regular`, `0DTE_Weekly`, or `0DTE_Monthly` using a holiday-corrected NYSE calendar. Tier is embedded in `latest.json` and drives OI threshold multipliers in the viewer.

---

## 6. Price Ticker Reference

### Main strip (large tiles)

| Display label | DXLink symbol | yfinance fallback | Notes |
|---|---|---|---|
| QQQ | `QQQ` | `QQQ` | Underlying |
| USO | `USO` | `USO` | Oil ETF |
| VIX | `$VIX.X` | `^VIX` | CBOE VIX index |
| SMH | `SMH` | `SMH` | Semiconductor ETF |
| IGV | `IGV` | `IGV` | Software ETF |
| 10Y | `$TNX.X` | `^TNX` | CBOE 10-year yield index; value ÷ 10 = % |
| JPY/USD | `/6J:XCME` | `JPYUSD=X` | CME yen futures; displayed inverted as `¥155.3` |

### Secondary strip (small tiles)

| Display label | DXLink symbol | yfinance fallback | Notes |
|---|---|---|---|
| BTC/USD | `BTC/USD:CXERX` | `BTC-USD` | Coinbase spot via tastytrade crypto feed |
| META | `META` | `META` | Equity |
| GOOGL | `GOOGL` | `GOOGL` | Equity |
| AMZN | `AMZN` | `AMZN` | Equity |
| TSLA | `TSLA` | `TSLA` | Equity |
| MU | `MU` | `MU` | Micron Technology |
| SPCX | `SPCX` | `SPCX` | Space industry ETF |
| Silver | `/SI:XCME` | `SI=F` | CME silver futures, $/troy oz |

---

## 7. Web Viewer (`docs/index.html`)

### 7.1 Price strip

Two rows of ticker tiles at the top of the page.

- **Main row (large):** QQQ · USO · VIX · SMH · IGV · 10Y · JPY/USD
- **Secondary row (small):** BTC/USD · META · GOOGL · AMZN · TSLA · MU · SPCX · Silver

Each tile shows ticker label, current price, and % change from prior close. Special formatting:
- `JPY/USD` — displayed as `¥155.3` (USD/JPY handle, inverted from raw CME quote)
- `BTC/USD` — integer with comma separator
- `VIX`, `10Y` — 2 decimal places; 10Y divides raw value by 10 to show `4.48%`

Source: `intraday/prices.json`, polled every 30 seconds.

### 7.2 OI heatmap

Three-column table: Calls | Strike | Puts, ±67 strikes centered on ATM.

**Cell contents (per option cell):**
- OI value (bold, abbreviated: `12.5K`) — primary display
- `bid × ask` (small, dimmed) — quote line below OI
- Volume (small superscript, top-right) — cumulative intraday volume
- Flow indicator (color-coded, bottom-right) — `VolDelta` contracts in last 60s

**Cell background:** OI bucket (0–5), calibrated via `derived/OIranges.csv` with tier multipliers.

| Level | Calls | Puts |
|---|---|---|
| 0 — zero OI | `#0d1117` | `#0d1117` |
| 1 — < p25 | `#0a1f14` | `#1a0d0d` |
| 2 — p25–p50 | `#0a3020` | `#2a0a0a` |
| 3 — p50–p75 | `#007730` | `#881100` |
| 4 — p75–p90 | `#00cc55` | `#ee3300` |
| 5 — > p90 (wall) | `#88ffcc` | `#ffaa88` |

**Flow indicator levels (VolDelta):**

| Level | Threshold | Display |
|---|---|---|
| 0 | 0 contracts | Hidden |
| 1 | < 20 | Dimmed white |
| 2 | 20–99 | Gold |
| 3 | 100–499 | Orange |
| 4 | ≥ 500 | White + orange glow |

**Strike column:** Shows actual strike price (e.g., `717`). ATM row shows `717 ★`. Tooltip shows offset from ATM.

**Status bar:**
- Green / "Live · last snap 13:25 ET" — data is fresh with bids
- Red / "Cached · 13:25 ET (feed reconnecting)" — DXLink down, showing last good snapshot
- Red / "Cached · 13:25 ET (3m ago)" — snapshot older than 2 minutes

Source: `intraday/latest.json`, polled every 60 seconds.

---

## 8. Infrastructure

### 8.1 Railway environment variables

| Variable | Purpose |
|---|---|
| `TASTY_LOGIN` | tastytrade username |
| `TASTY_PASSWORD` | tastytrade password |
| `TASTY_TOTP_SECRET` | Base32 TOTP secret for MFA auto-completion |
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 API token access key (read+write) |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET_NAME` | `qqq-options-chain-data` |

### 8.2 Cloudflare R2

- Bucket: `qqq-options-chain-data`
- Public access via Cloudflare Worker (not direct R2 public URL)
- CORS must allow `options.moopertonic.net` and `localhost` for browser fetch

### 8.3 Web hosting

- Static `docs/index.html` served via Cloudflare Worker at `options.moopertonic.net`
- Auto-deploys on every push to `master`
- No build step required

---

## 9. Known Limitations

| Limitation | Impact | Notes |
|---|---|---|
| OI is prior-day settled | OI walls are static until next morning | OCC doesn't publish real-time OI — by design. Volume/VolDelta are the live signals. |
| DXLink doesn't deliver equity Quote events | ETF/equity bid/ask not available via DXLink | All 14 price tickers use yfinance as primary source |
| 60-second snapshot granularity | Sub-minute flow not captured | VolDelta per 60s window is the resolution floor |
| DXLink intermittent 502s | Option data blanks during outages | `latest.json` guard preserves last good snapshot; viewer shows "Cached" status |
| No access control | Anyone with the URL can view | Cloudflare Access can gate it if needed |
| Single 0DTE expiration | Thursday dual-expiry (0DTE + Friday) not shown | Out of scope |

---

## 10. Future Work

- **Continuous tick logging** — write every DXLink event to R2 at tick level for sub-minute replay
- **Multi-expiry view** — show EoW alongside 0DTE in a side-by-side panel
- **MOO-12 integration** — feed intraday snapshots to ML associator for chain-evolution signals
- **Alert system** — detect strike crossing from p75 → p90 OI bucket and push notification
- **Access control** — gate behind Cloudflare Access for subscriber-only distribution
- **Historical replay** — scrub through today's intraday snapshots in the viewer
