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

1. `main.ingest_loop` (one per source: `ingest/alpaca_stream.py` +
   `ingest/free_wires.py`) parses, timestamps and durably records every
   headline immediately, then hands it to `main.score_loop` over a queue --
   a slow/backlogged scorer delays only scoring, never parsing the next
   headline off the source. `ingest/dedup.py` drops re-reported/
   near-identical headlines (fuzzy text match, not exact) so one story
   doesn't count as N signals; untagged (general-wire) headlines are
   matched to watchlist tickers with a word-boundary check, not raw
   substring containment.
2. `score/impact_scorer.py` asks a local Ollama model to score 0-10
   expected volatility impact; falls back to a VADER-magnitude score if
   Ollama's unreachable/slow. The two are never pooled downstream -- the
   stored `scorer` identity (e.g. `mistral-nemo:latest@v1` vs.
   `vader-fallback`) keeps them in separate evaluation strata.
3. A score >= 5.0 opens a "pin" (`correlate/pin_engine.py`): anchor price
   at the original headline ingest time, wait until that time plus
   `PIN_POST_SECONDS`, check whether price actually moved
   >= `PIN_MOVE_THRESHOLD_PCT` on >= `PIN_VOLUME_RATIO_THRESHOLD`x IEX
   baseline volume. Scoring delays do not move either endpoint; later ticks
   are excluded. A pre-headline baseline return additionally classifies the
   pin as `already_moving_before`, `subsequent_move`, `no_qualifying_move`,
   or `insufficient_evidence` -- an "associated" move is an observation,
   never a causal claim. A below-threshold headline still opens a
   non-posting "shadow" pin for evaluation coverage. Only confirmed,
   non-shadow pins post to Discord.
4. Independently, `correlate/anomaly.py` sweeps every watched symbol every
   `ANOMALY_CHECK_INTERVAL_SECS` for a z-scored `ANOMALY_RETURN_INTERVAL_SECS`
   return (the same fixed interval is both scored and displayed -- never a
   last-tick score paired with a longer reported window) beyond
   `ANOMALY_ZSCORE_THRESHOLD`, on a fresh, adequately-warmed-up read. A
   nearby headline is attached as advisory evidence (`matched_headline_id`,
   dated `preceding`/`following`), never a blanket suppressor -- the move
   is always logged, and `run_reconciliation_pass` links a headline that
   arrives *after* the move was flagged. The per-symbol cooldown persists in
   `storage`, so a restart mid-move doesn't immediately re-flag.
5. Everything lands in `db/market_pin_bot.sqlite3` -- `Storage.evaluation_report()`
   (see `scripts/daily_review.py`) gives a stratified, control-aware summary
   -- not just a single hit-rate off already-posted alerts -- so
   scoring/thresholds can be evaluated against real outcomes instead of
   guessed once and left alone.

Restart loses the in-memory tick history. Open observations from the previous
run are therefore closed with `incomplete_reason=restart_lost_price_history`;
missing endpoint prices or baseline volume also produce explicit incomplete
observations. `price_observations` persists sparse (ts, price, cum_volume)
samples for every resolved pin/anomaly so its return can be replayed without
depending on the in-memory ring buffer, which evicts. These observations have
no invented return, are not posted as confirmed, and are excluded from
accuracy statistics. Existing databases receive an additive column
migration; completed historical outcomes are preserved.

See `docs/moo170_evaluation_protocol.md` for the frozen evaluation rules and
the proposed bounded market trial.

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
