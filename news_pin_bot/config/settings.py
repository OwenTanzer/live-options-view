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

# --- Unexplained-move (anomaly) scanner ---
ANOMALY_BASELINE_WINDOW_MIN = float(os.environ.get("ANOMALY_BASELINE_WINDOW_MIN", "30"))
ANOMALY_CHECK_INTERVAL_SECS = float(os.environ.get("ANOMALY_CHECK_INTERVAL_SECS", "15"))
ANOMALY_ZSCORE_THRESHOLD = float(os.environ.get("ANOMALY_ZSCORE_THRESHOLD", "3.0"))
