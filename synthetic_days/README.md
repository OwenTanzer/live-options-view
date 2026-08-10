# synthetic_days -- fake-but-statistically-real trading days for strategy backtesting

Pre-PR practice tool: generates synthetic QQQ 0DTE trading days (underlying
price path + full options chain snapshots) that share the real market's
statistical fingerprint, then runs `crassus` strategies against dozens of
them before they ever see a live market.

Not a parametric simulator (no GARCH/jump-diffusion fitting). It **resamples
real historical data** this repo already collects, so the stylized facts
(fat-tailed returns, volatility clustering, the open/close vol smile, real
premium/spread-by-moneyness shape, real OI distribution across strikes) come
along for free instead of needing to be modeled and risk being modeled
wrong.

## How it connects to the existing R2 buckets

Same bucket (`qqq-options-chain-data`) and env vars
(`R2_ACCOUNT_ID`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`/`R2_BUCKET_NAME`)
every other script in this repo already uses. `r2_sources.py` is read-only
against three prefixes, all populated by scripts that already exist here:

| Prefix | Written by | Used for |
|---|---|---|
| `history/QQQ/5/{YYYY}.json` | `backfill_history.py` | bootstrapping the underlying price path |
| `history/QQQ/options_0dte/{DATE}.json` | `backfill_options_history.py` | cross-sectional chain shape: premium and spread-by-moneyness, OI distribution across strikes (many days) |
| `intraday/{YYYYMMDD}/snapshot_*.csv` | `collector.py` (live) | intraday time-of-day shape: how much of EOD OI/volume/spread-width is in place at each point in the session (few days -- collector coverage is short so far) |

Every read is cached to `./r2_cache/` (gitignored), so R2 credentials are
only needed once per new date range; every subsequent run works offline.

```
        R2 (qqq-options-chain-data)
   ┌───────────────┬───────────────┬────────────────────┐
   │ history/5min   │ history/       │ intraday/YYYYMMDD/  │
   │                │ options_0dte   │ snapshot_*.csv       │
   └──────┬─────────┴──────┬─────────┴──────────┬───────────┘
          │                │                    │
   price_path.py     chain_synth.py       chain_synth.py
   (block bootstrap   (smile/OI donor,    (intraday OI/spread/
    of 5-min returns)  Black-Scholes)      volume ramp)
          │                │                    │
          └───────┬────────┴──────────┬─────────┘
                   ▼                   ▼
              day_generator.py  ->  out/synthetic_YYYY-MM-DD.json
                   │
                   ▼
           backtest_bridge.py  ->  runs a real crassus strategy
                                    against N synthetic days
                   │
                   ▼
           validate_stats.py   ->  proves synthetic vs real
                                    stats actually match
```

## Methodology

**Price path (`price_path.py`):** moving block bootstrap over real 5-min
log-returns. Historical days are chopped into blocks (default 30 min); a
synthetic day is built by concatenating randomly-drawn blocks, always from
the *same intraday position* across donor days (a 10:00-10:30 block always
comes from some real day's 10:00-10:30), which is what preserves the
open/close volatility smile instead of scrambling it. 5-min closes are
upsampled to the collector's real ~60s cadence via a Brownian bridge per bar,
scaled to that specific bar's own realized (high-low) range -- a historically
calm bar gets a calm sub-path, a violent one gets a noisy one.

**Options chain (`chain_synth.py`):** one real EOD chain day is drawn as a
"donor" for the premium/OI/spread *shape* (not level); strikes are
re-centered each snapshot on the synthetic day's own current spot; mid price
(as a fraction of spot) and relative spread are looked up by nearest signed
moneyness on the matching call/put side of the donor. Bid/ask spread width
and OI/volume levels are then scaled by the real intraday time-of-day ramp
for that minute of the session. IV/greeks are left `None` rather than
synthesized: a live R2 pull confirmed MarketData.app's historical 0DTE
endpoint never populates them, and no strategy in `crassus/crassus/strategies/`
reads those fields anyway (checked directly) -- inventing numbers nothing
downstream consumes, and that inverting from a near-zero-time-to-expiry price
would make unreliable anyway, wasn't worth the complexity.

**Validation (`validate_stats.py`):** don't take the above on faith --
compares pooled real vs. synthetic 5-min return moments, ACF of squared
returns (the volatility-clustering signature), ACF of raw returns, daily
range distribution, and -- most directly relevant to this repo --
`canopus_down_day_14`'s own signal condition (down >=0.25% from the 9:30
reference by 2:45pm ET), real frequency vs. synthetic frequency.

## Known limitations

- **Intraday chain shape is data-starved.** `collector.py` has only been
  running since ~July, so the time-of-day OI/spread/volume ramp is
  calibrated from however many real intraday days exist in R2 at generation
  time -- could be a handful. `day_generator.py` prints how much real
  intraday coverage it found; treat the ramp as low-confidence until that
  grows. `options_0dte` EOD history (since 2025-04) has much deeper coverage
  and drives the smile/OI-shape donor, which is the more load-bearing piece.
- **Holidays/early closes aren't modeled** in the synthetic calendar --
  `day_generator.py` only skips weekends when labeling synthetic dates.
- **No macro/event regime tagging.** A synthetic day can accidentally splice
  a calm-regime block next to a high-vol-regime block since blocks are drawn
  independently; `validate_stats.py`'s ACF checks are the guardrail that
  would catch this degrading the vol-clustering signature if it became a
  real problem, not a per-block regime filter.
- **`VolDelta` is always 0** in synthetic rows -- each synthetic snapshot is
  an independent draw of the chain at that spot/time, not a running series,
  so there's no real "volume since last snapshot" to report.

## Usage

```bash
cd synthetic_days
pip install -r requirements.txt

# first run needs R2 creds to populate ./r2_cache/; every run after is offline
export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...

python day_generator.py --n-days 30 --seed 7 --out ./out
python validate_stats.py --days ./out
python backtest_bridge.py --strategy canopus_down_day_14 --days ./out
```

`backtest_bridge.py --strategy` accepts any registered `crassus` strategy_id
(`canopus_down_day_14`, `max_pain_qqq`, `put_call_ratio_qqq`, `oi_skew_qqq`,
`momentum_qqq`, ...) -- it imports the real strategy module unmodified from
`../crassus/crassus/strategies/`.
