# news_pin_bot

Local-first Discord bot that pins market-moving headlines to the actual
price reaction, plus flags price moves that have no news behind them yet.
Proposed as a new, standalone addition alongside `crassus/` -- it's a
monitoring/alerting layer, not wired into `crassus`'s account runner or
order execution. Runs entirely on free-tier-forever services (no card
required anywhere in the chain); see "Data sources" below.

## Why this exists

- **FinancialJuice's Discord bot** (what prompted this): scores headlines
  with a fixed keyword list against a 1-10 volatility score, no price
  verification. This bot uses a local LLM (Ollama) to judge
  magnitude/surprise instead, and actually checks whether price moved
  afterward rather than just delivering the headline.
- **SpotGamma's Tape/HIRO/TRACE**: infers price direction from options
  positioning/dealer hedging flow. Out of scope for v1 (needs a funded
  brokerage relationship for real options-chain data to stay unambiguously
  free) -- see "Not yet built" below.
- **`crassus/` in this repo**: `crassus/crassus/sentiment.py` and
  `trump_sentiment.py` already do VADER sentiment over Reddit + Trump's
  Truth Social feed for the QQQ-only strategies, with a well-built
  dedup/novelty filter (`_is_duplicate`, fuzzy match) this bot's
  `ingest/dedup.py` reuses the same pattern for. What this adds on top:
  a real news wire (Alpaca/Finnhub, not just Reddit/one Truth Social
  feed), a magnitude-aware local LLM score instead of generic VADER
  sentiment, multi-ticker instead of QQQ-only, and the unexplained-move
  scanner (`correlate/anomaly.py`), which nothing else in the repo does.

## Data sources (all free-forever, no card required)

| Source | What | Cost |
|---|---|---|
| Alpaca (paper account) | real-time news websocket, pre-tagged with tickers; real-time IEX trade websocket | free |
| Finnhub | general market news, REST polled well under the 60/min free limit | free (optional) |
| Local Ollama | headline impact scoring | free, local |
| SQLite | all storage | free, local |

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in ALPACA_*, DISCORD_*, adjust WATCHLIST
ollama pull mistral-nemo   # or whatever OLLAMA_MODEL you set
python main.py
```

## How it works

1. `ingest/alpaca_stream.py` + `ingest/free_wires.py` push/poll headlines.
2. `ingest/dedup.py` drops re-reported/near-identical headlines (fuzzy
   text match, not exact) so one story doesn't count as N signals.
3. `score/impact_scorer.py` asks a local Ollama model to score 0-10
   expected volatility impact; falls back to a VADER-magnitude score if
   Ollama's unreachable/slow.
4. A score >= 5.0 opens a "pin" (`correlate/pin_engine.py`): snapshot
   price now, wait `PIN_POST_SECONDS`, check whether price actually moved
   >= `PIN_MOVE_THRESHOLD_PCT` on >= `PIN_VOLUME_RATIO_THRESHOLD`x normal
   volume. Only confirmed pins post to Discord.
5. Independently, `correlate/anomaly.py` sweeps every watched symbol every
   `ANOMALY_CHECK_INTERVAL_SECS` for a price z-score beyond
   `ANOMALY_ZSCORE_THRESHOLD` with no matching headline in the last 5
   minutes, and posts those as "unexplained move, watching for news."
6. Everything lands in `db/market_pin_bot.sqlite3` -- `Storage.accuracy_stats()`
   gives a running hit-rate so scoring/thresholds can be tuned against
   real outcomes instead of guessed once and left alone.

## Not yet built (documented gaps, not silent ones)

- **Options-flow/gamma layer** (SpotGamma-style dealer positioning): needs
  a real options-chain data source. crassus's `collector.py` uses
  tastytrade's DXLink feed via a funded/live account -- deliberately left
  out of v1 since that's not unambiguously "free forever." Revisit if
  you open a tastytrade account specifically for this.
- **SEC EDGAR / GDELT supplementary wires**: noted in
  `ingest/free_wires.py` as a natural next source, same shape as the
  Finnhub poller, not wired up yet.
- **Self-tuning weights**: outcomes are logged (`accuracy_stats()`) but
  nothing yet auto-adjusts the impact threshold or model prompt from that
  data -- it's there to look at, not acted on automatically.
