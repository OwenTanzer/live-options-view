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
   │ history/QQQ/5  │ history/       │ intraday/YYYYMMDD/  │
   │                │ options_0dte   │ snapshot_*.csv       │
   └──────┬─────────┴──────┬─────────┴──────────┬───────────┘
          │                │                    │
   price_path.py     chain_synth.py       chain_synth.py
   (block bootstrap   (premium/OI/spread  (intraday OI/spread/
    of 5-min returns)  donor by moneyness) volume/premium-decay ramp)
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
re-centered each snapshot on the synthetic day's own current spot. Price is
split into intrinsic value (an exact function of moneyness -- no donor
needed) plus extrinsic/time value, looked up by nearest signed moneyness on
the matching call/put side of the donor and then **scaled by a real
intraday decay curve** -- for each real intraday chain row, its own
extrinsic value at time t divided by *that same option's own* EOD extrinsic
value, binned by (15-minute time bucket x moneyness bucket, 5 buckets --
deep ITM/near ITM/ATM/near OTM/deep OTM on one shared axis for calls and
puts). An earlier version computed one pooled ATM-straddle ratio and
applied it to every strike and side uniformly; review correctly pointed out
OTM and ITM options don't decay identically to ATM ones, so the lookup is
now moneyness-conditional, not one scalar for the whole chain. A moneyness
bucket with no real samples anywhere falls back to the ATM bucket's own
curve (the best available proxy for decay *shape*), then to a flat 1.0 only
if even the ATM bucket has nothing. 0DTE time value is largest at the open
and bleeds off to the donor's EOD level by the close, so a flat reuse of
the EOD premium all day would misprice every entry/exit before 3:45pm.
Bid/ask spread width and OI/volume levels are scaled by a similar (time-only,
not moneyness-conditional) real intraday ramp. IV/greeks are left `None`
rather than synthesized: a live R2 pull confirmed MarketData.app's
historical 0DTE endpoint never populates them, and no strategy in
`crassus/crassus/strategies/` reads those fields anyway (checked directly)
-- inventing numbers nothing downstream consumes, and that inverting from a
near-zero-time-to-expiry price would make unreliable anyway, wasn't worth
the complexity.

**Validation (`validate_stats.py`):** don't take the above on faith --
compares pooled real vs. synthetic 5-min return moments (both resampled to
the *same* 5-min resolution -- the synthetic path is generated at 60s
cadence, so this downsamples to the bootstrapped 5-min closes rather than
diffing 1-min-vs-5-min returns, which structurally understates synthetic
std/kurtosis/ACF regardless of generator quality), ACF of squared returns
(the volatility-clustering signature), ACF of raw returns, daily range
distribution, and -- most directly relevant to this repo --
`canopus_down_day_14`'s own signal condition (down >=0.25% from the 9:30
reference by 2:45pm ET), real frequency vs. synthetic frequency. This isn't
just a printout: `check_acceptance` turns the same comparison into a
pass/fail gate (exit code 1 on failure) with loose, directional-correctness
thresholds -- proving fat tails and volatility clustering are *present* and
in the right ballpark, not tight parity with one particular real sample
(see that function's docstring for why tight thresholds would just make CI
flaky on sampling noise).

## Validated against the live bucket

Run against real R2 data (830 real historical sessions, 80 EOD chain donor
days, 27/27 intraday time-of-day buckets calibrated from real collector
days): real 5-min QQQ returns show kurtosis 53 and positive/decaying
ACF(returns^2) across lags 1-5 (the real volatility-clustering signature).
Once compared at matching 5-min resolution, synthetic days land std=0.001091
vs. real std=0.001094 (near-exact), kurtosis 12.8 (real gap vs. 53, but a
real gap now -- not a resolution artifact), and ACF(returns^2) ~0.08-0.19 vs.
real's 0.11-0.38, both clearly positive and nothing like the ~0 an iid
simulator would show.

## CI

`.github/workflows/synthetic-days-ci.yml` runs `day_generator.py` ->
`validate_stats.py` (the acceptance gate) -> `backtest_bridge.py` on every
push/PR touching this directory, entirely against the checked-in hermetic
fixture at `tests/fixtures/r2_cache/` (`SYNTHETIC_DAYS_CACHE` points at it)
-- **no R2 credentials are exposed to or needed by CI.** The fixture is a
small (~25 real-*shaped* trading days, 10 EOD chain donor days, 2 intraday
days), deterministically-seeded dataset generated by `tests/make_fixtures.py`,
using a real stochastic-volatility process (EWMA vol + Student-t
innovations), not plain GBM -- specifically so the fixture itself carries
fat tails and volatility clustering, since `validate_stats.py`'s acceptance
thresholds would otherwise fail in CI on every run regardless of whether
the generator code is correct. Re-run `tests/make_fixtures.py` (fixed seed,
deterministic output) to regenerate the fixture after a real schema change.
Credentialed reproduction against the live R2 bucket (the numbers quoted
throughout this README) remains available locally and is how this was
actually validated -- see Usage below -- but is optional for CI to pass.

## Known limitations

- **Intraday chain shape is data-starved.** `collector.py` has only been
  running since ~July, so the time-of-day OI/spread/volume/premium-decay
  ramp is calibrated from however many real intraday days exist in R2 at
  generation time -- could be a handful, and the premium-decay curve in
  particular showed a couple of noisy buckets (e.g. one bucket landing at
  1.0x instead of the surrounding ~8x) on a 15-day sample, most likely from
  low sample count in early-morning buckets on thin-quote days.
  `day_generator.py` prints how much real intraday coverage it found; treat
  the ramp as low-confidence until that grows. `options_0dte` EOD history
  (since 2025-04) has much deeper coverage and drives the premium/OI-shape
  donor, which is the more load-bearing piece.
- **Kurtosis is still real-vs-synthetic gap, not just measurement noise.**
  12.8 vs. real's 53, even after fixing the resolution-matching bug --
  moving block bootstrap smooths some of the most extreme single-bar jumps'
  exact timing; a smaller block size trades that off against more
  block-boundary artifacts (see next point).
- **Independently-drawn blocks introduce a mild return-autocorrelation
  artifact** at block boundaries, since consecutive blocks aren't serially
  linked -- visible in `validate_stats.py`'s ACF(returns) output.
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
- **`backtest_bridge.py` bypasses the mandatory EOD flatten.** This repo
  also has `crassus/flatten.py` (a mandatory, not opt-in, force-close
  control the real `Runner` checks before every strategy call -- see the
  PR introducing it), but it doesn't exist on this branch's tree yet
  (`synthetic_days` branched off `master` before that PR merged), so there
  is nothing for the harness to import and wire in. Flagged in review as a
  real gap: until that PR merges, a backtested strategy can hold a position
  past what the real deployed runner would ever allow. Once it does merge,
  `run_strategy_on_day` should check `flatten.maybe_flatten` before the
  strategy's own decision each cycle, mirroring `Runner._run_account`'s own
  ordering, rather than calling the strategy unconditionally as it does now.

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

To run against the checked-in hermetic fixture instead (no R2 creds, what CI does):

```bash
export SYNTHETIC_DAYS_CACHE=./tests/fixtures/r2_cache
python day_generator.py --n-days 10 --seed 3 --years 2024 --out ./tests/out --max-donor-days 10 --max-intraday-days 2 --intraday-samples-per-day 14
python validate_stats.py --years 2024 --days ./tests/out
python backtest_bridge.py --strategy max_pain_qqq --days ./tests/out
```

`backtest_bridge.py --strategy` accepts any registered `crassus` strategy_id
(`canopus_down_day_14`, `max_pain_qqq`, `put_call_ratio_qqq`, `oi_skew_qqq`,
`momentum_qqq`, ...) -- it imports the real strategy module unmodified from
`../crassus/crassus/strategies/`.
