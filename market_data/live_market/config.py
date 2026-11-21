"""Collector configuration: constants, logging, session clock.

WHY this module: every subsystem reads the same service constants (tickers,
cadences, R2 keys, EIA/STEO and VWAP/RVOL tuning) and the same ET session
clock, so they live in one place with no dependency on any other collector
module. Nothing here imports any sibling module.
"""

import logging
import os
import sys
from datetime import datetime

import pytz

# Force UTF-8 stdout to avoid UnicodeEncodeError on non-UTF-8 terminals/Railway
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("collector")


# -- config -------------------------------------------------------------------

ET              = pytz.timezone("America/New_York")
ONE_CLOCK_PUBLICATIONS = os.getenv("ONE_CLOCK_PUBLICATIONS", "disabled") == "enabled"
TASTY_BASE      = "https://api.tastyworks.com"
TICKER          = "QQQ"
# Underlyings with their own 0DTE chain, snapshot, indicators and archive
# (#121). Only TICKER (QQQ) gets the nearest-weekly archive; SPY is 0DTE only,
# to bound subscriptions and writes.
CHAIN_UNDERLYINGS = ("QQQ", "SPY", "IBIT")
# Opt-in only after provider and release acceptance; no new connection.
COLLECT_IBIT = os.environ.get("COLLECT_IBIT", "0") == "1"
COLLECT_SPY     = os.environ.get("COLLECT_SPY", "1") != "0"
STRIKE_WINDOW   = 33
SPY_MAX_STRIKES = 67
SNAPSHOT_SECS   = 60
WEEKLY_SNAPSHOT_SECS = 60 * 60
PRICES_SECS     = 10
HEALTH_SECS     = 15
PREMARKET_HOUR  = 6
STOP_HOUR       = 16
STOP_MIN        = 15
STALE_FEED_SECS = 120   # warn if no feed event for this many seconds
LIVE_QUOTE_PORT = int(os.environ.get("PORT", "8080"))
MAX_LIVE_QUOTE_SYMBOLS = 100
R2_BUCKET       = os.environ.get("R2_BUCKET_NAME", "pub-4d5c916b8cb74ffb8c0abd7dfadb02cf")
WEEKLY_R2_PREFIX = "raw/weekly"

# -- continuous price lane (#178) ---------------------------------------------
# The lane publishes prices outside the QQQ/SPY chain session. Setting
# PRICE_LANE_ENABLED=0 restores the legacy in-session publisher; both paths are
# mutually exclusive, so prices.json always has exactly one writer.
PRICE_LANE_ENABLED = os.environ.get("PRICE_LANE_ENABLED", "1") != "0"
# Trade prints for extended-hours equity prices, on a separate DXLink channel so
# a field/event rejection cannot disturb the accepted quote/trade feed. Set 0 to
# disable the probe without reverting the release.
PRICE_LANE_TIMESALE = os.environ.get("PRICE_LANE_TIMESALE", "1") != "0"
PRICE_LANE_HEALTH_KEY = "intraday/price_lane_health.json"
PRICE_LANE_HEALTH_SECS = 30
PRICE_LANE_MIN_BACKOFF_SECS = 60
PRICE_LANE_MAX_BACKOFF_SECS = 1800
PRICE_LANE_MAX_AUTH_CYCLES = 4          # per retry window, then a long sleep
PRICE_LANE_RETRY_WINDOW_SECS = 3600
PRICE_LANE_CONTRACT_REFRESH_SECS = 6 * 3600  # daily/roll reconciliation while healthy
# A failed or partial contract resolution must retry long before the six-hour
# cadence: the live November 9 failure left every futures row frozen for the
# rest of the evening because the next attempt was six hours away.
CONTRACT_RESOLVE_RETRY_MIN_SECS = 60
CONTRACT_RESOLVE_RETRY_MAX_SECS = 900
ROLLING_REFERENCE_KEY = "intraday/price_references.json"
# Extended-session equity selection (#178 repair). A stale regular-session
# trade may not stand in for an active premarket/after-hours market: when the
# feed carries a live two-sided quote and the last trade predates the current
# session, the quote midpoint is the current price. The bounds reject
# half-updated and wide quotes (IGV's 13% after-hours spread is not a price).
EXTENDED_QUOTE_MAX_AGE_SECS = STALE_FEED_SECS
EXTENDED_QUOTE_MAX_SPREAD_PCT = 2.0

# Keep the weekly archive byte-for-byte compatible with qqq-data-pipeline's
# chain CSV field set and order.  The live 0DTE CSV intentionally retains its
# additional VolDelta and macro columns.
PIPELINE_CHAIN_COLUMNS = [
    "TradeDate", "Expiration", "Strike", "Type", "OptionSymbol", "DTE",
    "OpenInterest", "Volume", "Bid", "Mid", "Ask", "Last", "IV", "Delta",
    "Gamma", "Theta", "Vega", "UnderlyingPrice",
]

# EIA STEO crude calibration feed (see market_data/live_market/crude_calibration.py). Optional --
# skipped entirely if EIA_API_KEY isn't set, same soft-dependency shape as
# the KOSPI/OVX DXLink-symbol guesses degrading to yfinance rather than
# failing the whole collector. Runs independent of the market session (unlike
# prices_loop/health_loop, which only run inside _run_session) since STEO has
# nothing to do with QQQ market hours -- started once from main().
EIA_API_KEY          = os.environ.get("EIA_API_KEY")
EIA_STEO_URL          = "https://api.eia.gov/v2/steo/data/"
EIA_STEO_POLL_SECS    = 6 * 3600   # STEO itself updates ~monthly; polling a few times a
                                    # day just catches a fresh release promptly without a cron
EIA_STEO_KEY          = "macro/eia_steo.json"
EIA_STEO_LOG_KEY      = "baselines/eia_steo_vintages.json"
EIA_STEO_MAX_VINTAGES = 6
# EIA v2's `length` caps *total rows returned*, not periods-per-series --
# with 3 series requested together (facets[seriesId][] has 3 values), the
# old length=36 capped the combined response at 36 rows total (effectively
# ~12 periods across 3 series, not 36 periods per series as the docstring
# claimed), silently truncating history. 5000 is comfortably above
# 3 series x 36 periods = 108 rows with margin for future series/window
# growth, and is at/near EIA v2's own per-request row cap. See
# fetch_eia_steo_rows's truncation check for the actual safety net --
# this constant alone doesn't guarantee nothing is ever dropped.
EIA_STEO_PAGE_LENGTH = 5000

# VWAP/RVOL (see market_data/live_market/market_signals.py and docs/plans/2026-07-vwap-rvol.md).
# Bucket/lookback/min-days are not derived from anything about QQQ itself --
# reasonable defaults chosen to balance per-bucket sample density against how
# fast RVOL exits "insufficient_history" after this feature first deploys.
RVOL_BUCKET_MINUTES     = 5
RVOL_LOOKBACK_DAYS      = 20
RVOL_MIN_DAYS_REQUIRED  = 5
RVOL_BASELINE_KEY       = "baselines/qqq_rvol_buckets.json"

# Time-series momentum (display/log only, see market_data/live_market/market_signals.py's module
# docstring and docs/plans/2026-07-momentum-indicator.md) -- same shape as
# momentum_qqq's own default params (crassus/crassus/strategies/momentum_qqq.py),
# but this is a reference signal for the UI/log, not a change to what any
# account actually trades on.
MOMENTUM_LOOKBACK_MINUTES          = 60.0
MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN  = 10.0
MOMENTUM_NEUTRAL_BAND_PCT          = 0.05  # display-only up/down/flat threshold
MOMENTUM_RETAIN_MINUTES            = 24 * 60.0  # generous, decoupled from the configured lookback

# TICKER_CLASSES is derived from the canonical exact-100 universe below
# (market_data/overview/universe.json). It was hand-written (17 entries) and
# did not cover 82 of the 99 PRICE_TICKERS labels, so a priced ticker
# crashed the quote handler with KeyError and the Worker returned HTTP 502
# (NVDA, 2026-11-10). See _INSTRUMENT_TYPE_CLASSES.

# Canonical release-gated registry owns producer requests. SPY retains its
# separate existing subscription/tile path; disabled candidates request nothing.
from market_data.overview import INSTRUMENTS, producer_maps
PRICE_TICKERS, YF_SYMBOL_MAP = producer_maps()

# Display class per instrument_type (the universe's most specific field),
# grounded in the pre-existing hand-written entries:
#   stock / equity_etf / bond_etf / commodity_pool_etp / bullion_trust_etp
#       -> "equity"   (exchange-traded equity securities; USO, a commodity
#          pool ETP, was already "equity")
#   volatility_index -> "index"   (VIX, OVX)
#   cash_index / cash_index_gross_return -> "international"   (KOSPI; all are
#          non-US indices: FTSE100, HSI, NIKKEI225, DAX)
#   yield_index / daily_yield_series -> "yield"   (10Y)
#   futures_active_contract_series -> "futures"   (JPY/USD)
#   crypto_spot -> "crypto"   (BTC/USD)
#   spot_fx -> "international"   (cross-border currency pairs; no live DXLink
#          price, so they report ticker_unavailable unless a dated fallback
#          provides a price)
_INSTRUMENT_TYPE_CLASSES = {
    "stock": "equity",
    "equity_etf": "equity",
    "bond_etf": "equity",
    "commodity_pool_etp": "equity",
    "bullion_trust_etp": "equity",
    "volatility_index": "index",
    "cash_index": "international",
    "cash_index_gross_return": "international",
    "yield_index": "yield",
    "daily_yield_series": "yield",
    "futures_active_contract_series": "futures",
    "crypto_spot": "crypto",
    "spot_fx": "international",
}
_missing_types = {r["instrument_type"] for r in INSTRUMENTS} - set(_INSTRUMENT_TYPE_CLASSES)
if _missing_types:
    raise ValueError(f"universe instrument_type has no ticker class: {sorted(_missing_types)}")
TICKER_CLASSES: dict[str, str] = {
    r["label"]: _INSTRUMENT_TYPE_CLASSES[r["instrument_type"]]
    for r in INSTRUMENTS
}

# -- session clock ------------------------------------------------------------
# Pure ET wall-clock helpers on the constants above. They lived in the
# sessions block of the flat file; they belong here because they depend only
# on config values, and the market/prices/health modules need them without
# importing the session-eligibility machinery.

def past_stop() -> bool:
    et = datetime.now(ET)
    return (et.hour, et.minute) >= (STOP_HOUR, STOP_MIN)

def _session_bounds(et: datetime) -> tuple[datetime, datetime]:
    """Return the session start/stop bounds for the ET date of ``et``."""
    session_date = et.date()
    start = ET.localize(datetime(
        session_date.year, session_date.month, session_date.day,
        PREMARKET_HOUR, 0, 0,
    ))
    stop = ET.localize(datetime(
        session_date.year, session_date.month, session_date.day,
        STOP_HOUR, STOP_MIN, 0,
    ))
    return start, stop


def _inside_session_window(et: datetime) -> bool:
    start, stop = _session_bounds(et)
    return start <= et < stop



__all__ = [
    "ET",
    "TASTY_BASE",
    "TICKER",
    "CHAIN_UNDERLYINGS",
    "COLLECT_IBIT",
    "COLLECT_SPY",
    "STRIKE_WINDOW",
    "SPY_MAX_STRIKES",
    "SNAPSHOT_SECS",
    "WEEKLY_SNAPSHOT_SECS",
    "PRICES_SECS",
    "HEALTH_SECS",
    "PREMARKET_HOUR",
    "STOP_HOUR",
    "STOP_MIN",
    "STALE_FEED_SECS",
    "LIVE_QUOTE_PORT",
    "MAX_LIVE_QUOTE_SYMBOLS",
    "R2_BUCKET",
    "WEEKLY_R2_PREFIX",
    "PRICE_LANE_ENABLED",
    "PRICE_LANE_TIMESALE",
    "PRICE_LANE_HEALTH_KEY",
    "PRICE_LANE_HEALTH_SECS",
    "PRICE_LANE_MIN_BACKOFF_SECS",
    "PRICE_LANE_MAX_BACKOFF_SECS",
    "PRICE_LANE_MAX_AUTH_CYCLES",
    "PRICE_LANE_RETRY_WINDOW_SECS",
    "PRICE_LANE_CONTRACT_REFRESH_SECS",
    "CONTRACT_RESOLVE_RETRY_MIN_SECS",
    "CONTRACT_RESOLVE_RETRY_MAX_SECS",
    "EXTENDED_QUOTE_MAX_AGE_SECS",
    "EXTENDED_QUOTE_MAX_SPREAD_PCT",
    "ROLLING_REFERENCE_KEY",
    "PIPELINE_CHAIN_COLUMNS",
    "EIA_API_KEY",
    "EIA_STEO_URL",
    "EIA_STEO_POLL_SECS",
    "EIA_STEO_KEY",
    "EIA_STEO_LOG_KEY",
    "EIA_STEO_MAX_VINTAGES",
    "EIA_STEO_PAGE_LENGTH",
    "RVOL_BUCKET_MINUTES",
    "RVOL_LOOKBACK_DAYS",
    "RVOL_MIN_DAYS_REQUIRED",
    "RVOL_BASELINE_KEY",
    "MOMENTUM_LOOKBACK_MINUTES",
    "MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN",
    "MOMENTUM_NEUTRAL_BAND_PCT",
    "MOMENTUM_RETAIN_MINUTES",
    "PRICE_TICKERS",
    "TICKER_CLASSES",
    "YF_SYMBOL_MAP",
    "log",
    "past_stop",
    "_session_bounds",
    "_inside_session_window",
]
