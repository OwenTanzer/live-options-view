"""Central config, all sourced from environment variables / .env.

Every external dependency here is free-tier-forever: Alpaca (paper account,
no card), Finnhub (free key), unauthenticated public feeds (SEC EDGAR RSS,
GDELT, Reddit .json, trumpstruth.org), and a local Ollama instance for
scoring. Nothing in this file should ever require a paid plan.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
# All durable state (headlines, pins, unexplained moves, replay evidence)
# lives in this one file. It is the only thing that needs backing up to
# preserve history across a redeploy -- a periodic copy (e.g. a scheduled
# `cp` to another disk/volume before restart) is sufficient; there is no
# server or external dependency to snapshot alongside it.
DB_PATH = ROOT_DIR / "db" / "market_pin_bot.sqlite3"

# --- Alpaca (free paper account: https://app.alpaca.markets/signup) ---
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
ALPACA_IEX_WS_URL = "wss://stream.data.alpaca.markets/v2/iex"

# --- Finnhub (free key: https://finnhub.io/register) ---
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
FINNHUB_NEWS_POLL_SECS = float(os.environ.get("FINNHUB_NEWS_POLL_SECS", "20"))

# --- Discord ---
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or 0)

# --- Local scoring (Ollama) ---
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral-nemo:latest")
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT_S", "12"))

# --- Watchlist: tickers this bot actively pins. Keep it small -- Alpaca free
# IEX websocket and Finnhub's free websocket both comfortably handle this. ---
WATCHLIST = tuple(
    s.strip().upper()
    for s in os.environ.get("WATCHLIST", "SPY,QQQ,NVDA,AAPL,MSFT,TSLA").split(",")
    if s.strip()
)

# --- Pinning window ---
PIN_PRE_SECONDS = float(os.environ.get("PIN_PRE_SECONDS", "30"))
PIN_POST_SECONDS = float(os.environ.get("PIN_POST_SECONDS", "300"))
PIN_MOVE_THRESHOLD_PCT = float(os.environ.get("PIN_MOVE_THRESHOLD_PCT", "0.5"))
PIN_VOLUME_RATIO_THRESHOLD = float(os.environ.get("PIN_VOLUME_RATIO_THRESHOLD", "2.0"))
# How far back (seconds, ending at window_start) the pre-headline baseline
# return is measured -- used to classify a pin as "already moving before"
# the headline vs. "moved only after" it. MOO-170 finding 2.
PIN_BASELINE_LOOKBACK_SECS = float(os.environ.get("PIN_BASELINE_LOOKBACK_SECS", "300"))

# --- Unexplained-move (anomaly) scanner ---
ANOMALY_BASELINE_WINDOW_MIN = float(os.environ.get("ANOMALY_BASELINE_WINDOW_MIN", "30"))
ANOMALY_CHECK_INTERVAL_SECS = float(os.environ.get("ANOMALY_CHECK_INTERVAL_SECS", "15"))
ANOMALY_ZSCORE_THRESHOLD = float(os.environ.get("ANOMALY_ZSCORE_THRESHOLD", "3.0"))
# Once a symbol is flagged, suppress re-flagging it again until this many
# seconds have passed -- otherwise one sustained anomalous stretch produces
# a new row (and Discord post) every ANOMALY_CHECK_INTERVAL_SECS. Persisted
# in storage (not just in-process) so a restart mid-move doesn't re-flood.
ANOMALY_COOLDOWN_SECS = float(os.environ.get("ANOMALY_COOLDOWN_SECS", "300"))
# The single fixed interval used BOTH to z-score "is this move unusual" AND
# to report the displayed pct_move -- MOO-170 finding 3 requires these be
# the same interval, never a last-tick score paired with a longer displayed
# window.
ANOMALY_RETURN_INTERVAL_SECS = float(os.environ.get("ANOMALY_RETURN_INTERVAL_SECS", "60"))
# The "now" endpoint of a return must have a trade at least this recent, or
# the read is stale and reported as insufficient evidence rather than a
# confident-looking number computed off an old print.
ANOMALY_MAX_STALENESS_SECS = float(os.environ.get("ANOMALY_MAX_STALENESS_SECS", "30"))
# How many recent fixed-interval returns must exist before a z-score is
# trusted -- guards against a short/newly-warmed-up history producing a
# score off a handful of samples.
ANOMALY_MIN_RETURN_SAMPLES = int(os.environ.get("ANOMALY_MIN_RETURN_SAMPLES", "10"))
# How far around a flagged anomaly (seconds, before AND after) candidate
# headlines are considered for relevance matching -- MOO-170 finding 4.
ANOMALY_HEADLINE_MATCH_WINDOW_SECS = float(os.environ.get("ANOMALY_HEADLINE_MATCH_WINDOW_SECS", "600"))
# How long a later-arriving headline can still be reconciled against an
# already-logged unexplained move.
ANOMALY_RECONCILE_WINDOW_SECS = float(os.environ.get("ANOMALY_RECONCILE_WINDOW_SECS", "1800"))

# --- Volume-history warm-up (MOO-170 finding 5) ---
# baseline_volume_rate() must see trade history covering at least this
# fraction of the requested window, or it reports "not enough data" (None)
# instead of a precise-looking rate computed off a short span.
VOLUME_MIN_COVERAGE_FRACTION = float(os.environ.get("VOLUME_MIN_COVERAGE_FRACTION", "0.8"))
VOLUME_MIN_TRADE_COUNT = int(os.environ.get("VOLUME_MIN_TRADE_COUNT", "20"))

# --- Evaluation (MOO-170 findings 6-7) ---
# Below IMPACT_POST_THRESHOLD (see main.py) but at/above this floor, a
# headline still gets a non-posting "shadow" observation window so the
# evaluation denominator covers more than just score>=5 headlines.
SHADOW_SCORE_FLOOR = float(os.environ.get("SHADOW_SCORE_FLOOR", "0.0"))
# A same-symbol, same-time-of-day control window this far before each pin's
# window_start, used as an approximate no-news comparison (never a true
# randomized control -- see docs/moo170_evaluation_protocol.md). Skipped
# (control_pct_move stays NULL) if a headline is found near the shifted
# window, since that would no longer be a no-news comparison.
CONTROL_LOOKBACK_SECS = float(os.environ.get("CONTROL_LOOKBACK_SECS", str(24 * 3600)))
