#!/usr/bin/env python3
"""
collector.py -- QQQ 0DTE live chain snapshot service.

Authenticates with tastytrade, subscribes to the QQQ 0DTE option chain via
DXLink websocket, and uploads snapshots to R2 every minute.

R2 output:
  intraday/YYYYMMDD/snapshot_HHMMSSffffff.csv  -- archived snapshots (microsecond key)
  intraday/latest.json                   -- live feed for the web viewer
  intraday/prices.json                   -- macro price strip (every 10s; yfinance fill cached 60s)
  intraday/health.json                   -- lifecycle telemetry (every 15s)
  macro/eia_steo.json                    -- EIA STEO crude calibration + OVX regime
                                             (every EIA_STEO_POLL_SECS, optional feature --
                                             see crude_calibration.py; skipped if EIA_API_KEY unset)
  baselines/eia_steo_vintages.json       -- rolling log of past STEO releases this
                                             collector has observed, for vintage-over-vintage diffs

Environment variables (set in Railway dashboard):
  TASTY_LOGIN            tastytrade username
  TASTY_PASSWORD         tastytrade password
  R2_ACCOUNT_ID          Cloudflare account ID
  R2_ACCESS_KEY_ID       R2 access key
  R2_SECRET_ACCESS_KEY   R2 secret key
  R2_BUCKET_NAME         bucket name (default: pub-4d5c916b8cb74ffb8c0abd7dfadb02cf)
  LIVE_QUOTE_KEY         shared key for the Worker's read-only quote proxy
  PORT                   Railway HTTP port (default: 8080 locally)
  EIA_API_KEY            optional -- enables the macro/eia_steo.json feed (see
                          crude_calibration.py); free signup at eia.gov/opendata.
                          Feature is silently skipped (logged once) if unset.
"""

import io
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

import boto3
import pandas as pd
import pytz
import requests
import websocket
import yfinance as yf

import crude_calibration as cc
import market_signals as ms

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
TASTY_BASE      = "https://api.tastyworks.com"
TICKER          = "QQQ"
STRIKE_WINDOW   = 33
SNAPSHOT_SECS   = 60
PRICES_SECS     = 10
HEALTH_SECS     = 15
PREMARKET_HOUR  = 6
STOP_HOUR       = 16
STOP_MIN        = 15
STALE_FEED_SECS = 120   # warn if no feed event for this many seconds
LIVE_QUOTE_PORT = int(os.environ.get("PORT", "8080"))
MAX_LIVE_QUOTE_SYMBOLS = 100
R2_BUCKET       = os.environ.get("R2_BUCKET_NAME", "pub-4d5c916b8cb74ffb8c0abd7dfadb02cf")

# EIA STEO crude calibration feed (see crude_calibration.py). Optional --
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

# VWAP/RVOL (see market_signals.py and docs/plans/2026-07-vwap-rvol.md).
# Bucket/lookback/min-days are not derived from anything about QQQ itself --
# reasonable defaults chosen to balance per-bucket sample density against how
# fast RVOL exits "insufficient_history" after this feature first deploys.
RVOL_BUCKET_MINUTES     = 5
RVOL_LOOKBACK_DAYS      = 20
RVOL_MIN_DAYS_REQUIRED  = 5
RVOL_BASELINE_KEY       = "baselines/qqq_rvol_buckets.json"

# Time-series momentum (display/log only, see market_signals.py's module
# docstring and docs/plans/2026-07-momentum-indicator.md) -- same shape as
# momentum_qqq's own default params (crassus/crassus/strategies/momentum_qqq.py),
# but this is a reference signal for the UI/log, not a change to what any
# account actually trades on.
MOMENTUM_LOOKBACK_MINUTES          = 60.0
MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN  = 10.0
MOMENTUM_NEUTRAL_BAND_PCT          = 0.05  # display-only up/down/flat threshold
MOMENTUM_RETAIN_MINUTES            = 24 * 60.0  # generous, decoupled from the configured lookback

PRICE_TICKERS: dict[str, str] = {
    "QQQ":     "QQQ",
    "USO":     "USO",
    "VIX":     "$VIX.X",
    # Cboe Crude Oil ETF Volatility Index. Same unverified-guess situation as
    # KOSPI below: tastytrade/dxFeed may or may not carry this index symbol
    # at all. If the guess is wrong this ticker just never gets DXLink data
    # and always falls through to the yfinance path (a real, working ^OVX
    # symbol) -- same graceful degradation every ticker here already has
    # pre-market.
    "OVX":     "$OVX.X",
    "SMH":     "SMH",
    "IGV":     "IGV",
    "10Y":     "$TNX.X",      # CBOE 10-year Treasury yield index (value = yield × 10)
    "JPY/USD": "/6J:XCME",    # CME yen futures, USD-per-JPY; inverted for display
    # KOSPI Composite Index — tastytrade/dxFeed is a US-brokerage feed and may
    # not carry this at all; unverified guess at the dxFeed index-symbol
    # convention (cf. $VIX.X/$TNX.X above). If it's wrong this ticker simply
    # never gets DXLink data and always falls through to the yfinance path
    # below, which is a real, working symbol — same graceful-degradation
    # behavior every ticker already has pre-market.
    "KOSPI":   "$KOSPI.X",
    "BTC/USD": "BTC/USD:CXERX",
    "META":    "META",
    "GOOGL":   "GOOGL",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
    "MU":      "MU",
    "SPCX":    "SPCX",
    "AAPL":    "AAPL",
}

TICKER_CLASSES: dict[str, str] = {
    "QQQ": "equity", "USO": "equity", "SMH": "equity", "IGV": "equity",
    "META": "equity", "GOOGL": "equity", "AMZN": "equity", "TSLA": "equity",
    "MU": "equity", "SPCX": "equity", "AAPL": "equity",
    "VIX": "index", "OVX": "index", "10Y": "yield", "JPY/USD": "futures",
    "KOSPI": "international", "BTC/USD": "crypto",
}

# Yahoo Finance symbols for the same tickers (fallback when DXLink has no data)
YF_SYMBOL_MAP: dict[str, str] = {
    "QQQ":     "QQQ",
    "USO":     "USO",
    "VIX":     "^VIX",       # pre-market: None expected (CBOE only calculates at open)
    "OVX":     "^OVX",       # pre-market: same CBOE-open-only caveat as VIX above
    "SMH":     "SMH",
    "IGV":     "IGV",
    "10Y":     "^TNX",       # yields the rate directly (e.g. 4.485), NOT × 10
    "JPY/USD": "JPYUSD=X",
    "KOSPI":   "^KS11",      # KOSPI Composite Index
    "BTC/USD": "BTC-USD",
    "META":    "META",
    "GOOGL":   "GOOGL",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
    "MU":      "MU",
    "SPCX":    "SPCX",
    "AAPL":    "AAPL",
}


# -- upload counters ----------------------------------------------------------

class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self.prices_ok   = 0
        self.snapshot_ok = 0
        self.csv_ok      = 0
        self.failures    = 0
        self.last_price_time    = None
        self.last_snapshot_time = None

    def inc_prices(self, ts: str):
        with self._lock:
            self.prices_ok += 1
            self.last_price_time = ts

    def inc_snapshot(self, ts: str):
        with self._lock:
            self.snapshot_ok += 1
            self.last_snapshot_time = ts

    def inc_csv(self):
        with self._lock:
            self.csv_ok += 1

    def inc_failure(self):
        with self._lock:
            self.failures += 1

    def get(self) -> dict:
        with self._lock:
            return {
                "prices_ok":          self.prices_ok,
                "snapshot_ok":        self.snapshot_ok,
                "csv_ok":             self.csv_ok,
                "failures":           self.failures,
                "last_price_time":    self.last_price_time,
                "last_snapshot_time": self.last_snapshot_time,
            }


# -- snapshot cadence tracker -------------------------------------------------

class SnapshotTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.seq                             = 0
        self.expected_next: Optional[datetime] = None
        self.missed                          = 0

    def record(self):
        with self._lock:
            self.seq += 1
            self.expected_next = datetime.now(timezone.utc) + timedelta(seconds=SNAPSHOT_SECS)

    def check_missed(self):
        with self._lock:
            if (self.expected_next is not None and
                    datetime.now(timezone.utc) > self.expected_next + timedelta(seconds=60)):
                self.missed += 1
                log.warning(f"missed snapshot (expected by {self.expected_next.isoformat()})")
                self.expected_next = None

    def get(self) -> dict:
        with self._lock:
            return {
                "snapshot_sequence":          self.seq,
                "expected_next_snapshot_time": (self.expected_next.isoformat()
                                                if self.expected_next else None),
                "missed_snapshot_count":      self.missed,
            }


# -- tastytrade auth ----------------------------------------------------------

R2_REMEMBER_TOKEN_KEY = "auth/remember_token.json"


def _load_remember_token(s3) -> str | None:
    try:
        body = s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=R2_REMEMBER_TOKEN_KEY)["Body"].read()
        return json.loads(body)["remember_token"]
    except Exception:
        pass
    return os.environ.get("TASTY_REMEMBER_TOKEN")


def _save_remember_token(s3, token: str):
    s3.put_object(
        Bucket=os.environ["R2_BUCKET_NAME"],
        Key=R2_REMEMBER_TOKEN_KEY,
        Body=json.dumps({"remember_token": token, "updated_at": datetime.now(timezone.utc).isoformat()}).encode(),
        ContentType="application/json",
    )
    log.info("remember-token rotated and saved to R2")


def _complete_device_challenge(login: str, password: str, challenge_token: str) -> requests.Response:
    import pyotp
    requests.post(
        f"{TASTY_BASE}/device-challenge",
        headers={"Content-Type": "application/json", "X-Tastyworks-Challenge-Token": challenge_token},
        timeout=10,
    )
    otp = pyotp.TOTP(os.environ["TASTY_TOTP_SECRET"]).now()
    log.info("device challenge: submitting TOTP")
    return requests.post(
        f"{TASTY_BASE}/sessions",
        json={"login": login, "password": password, "remember-me": True},
        headers={
            "Content-Type": "application/json",
            "X-Tastyworks-Challenge-Token": challenge_token,
            "X-Tastyworks-OTP": otp,
        },
        timeout=15,
    )


def tasty_auth(login: str, s3) -> dict:
    remember_token = _load_remember_token(s3)
    if remember_token:
        log.info("tasty_auth -- trying remember-token")
        resp = requests.post(
            f"{TASTY_BASE}/sessions",
            json={"login": login, "remember-token": remember_token, "remember-me": True},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 201:
            data      = resp.json()["data"]
            new_token = data.get("remember-token")
            log.info("tastytrade session established via remember-token")
            if new_token:
                _save_remember_token(s3, new_token)
            resp2 = requests.get(
                f"{TASTY_BASE}/api-quote-tokens",
                headers={"Authorization": data["session-token"]},
                timeout=10,
            )
            resp2.raise_for_status()
            d = resp2.json()["data"]
            streamer_token = d["token"]
            streamer_url   = (d.get("dxlink-url") or d.get("websocket-url") or
                              "wss://tasty-openapi-ws.dxfeed.com/realtime")
            log.info(f"streamer token obtained  url={streamer_url}")
            return {
                "session_token":  data["session-token"],
                "streamer_token": streamer_token,
                "streamer_url":   streamer_url,
            }
        log.warning(f"remember-token rejected ({resp.status_code}), falling back to password+TOTP")

    password = os.environ["TASTY_PASSWORD"]
    log.info("tasty_auth -- using password")
    resp = requests.post(
        f"{TASTY_BASE}/sessions",
        json={"login": login, "password": password, "remember-me": True},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code == 403:
        challenge_token = resp.headers.get("X-Tastyworks-Challenge-Token")
        if not challenge_token:
            resp.raise_for_status()
        log.info("device challenge required -- completing automatically")
        resp = _complete_device_challenge(login, password, challenge_token)

    resp.raise_for_status()
    data          = resp.json()["data"]
    session_token = data["session-token"]
    new_token     = data.get("remember-token")
    log.info("tastytrade session established")

    if new_token:
        _save_remember_token(s3, new_token)

    resp2 = requests.get(
        f"{TASTY_BASE}/api-quote-tokens",
        headers={"Authorization": session_token},
        timeout=10,
    )
    resp2.raise_for_status()
    d = resp2.json()["data"]
    streamer_token = d["token"]
    streamer_url   = (d.get("dxlink-url") or d.get("websocket-url") or
                      "wss://tasty-openapi-ws.dxfeed.com/realtime")
    log.info(f"streamer token obtained  url={streamer_url}")
    return {
        "session_token":  session_token,
        "streamer_token": streamer_token,
        "streamer_url":   streamer_url,
    }


# -- option chain structure ---------------------------------------------------

def _strike_str(strike: float) -> str:
    """dxFeed strike format: 713.0 -> '713', 713.5 -> '71350'."""
    if strike == int(strike):
        return str(int(strike))
    return f"{strike * 100:.0f}".rstrip("0")


def _dxlink_symbol(occ_symbol: str) -> str:
    """Convert OCC symbol to dxFeed streamer format.
    QQQ260623C00713000 -> .QQQ260623C713
    The OCC strike field is 8 digits representing price * 1000.
    """
    occ = occ_symbol.replace(" ", "")
    i = 0
    while i < len(occ) and not occ[i].isdigit():
        i += 1
    underlying = occ[:i]
    date_part  = occ[i:i+6]
    side       = occ[i+6]
    strike     = int(occ[i+7:]) / 1000.0
    return f".{underlying}{date_part}{side}{_strike_str(strike)}"


def _build_symbol(strike: float, exp_date: str, option_type: str) -> str:
    yy, mm, dd = exp_date[2:4], exp_date[5:7], exp_date[8:10]
    side = "C" if option_type.lower() == "call" else "P"
    return f".{TICKER}{yy}{mm}{dd}{side}{_strike_str(strike)}"


def load_chain(session_token: str, today: date) -> tuple[list[dict], str]:
    resp = requests.get(
        f"{TASTY_BASE}/option-chains/{TICKER}/nested",
        headers={"Authorization": session_token},
        timeout=30,
    )
    resp.raise_for_status()

    items = resp.json().get("data", {}).get("items", [])
    if not items:
        raise RuntimeError("empty option chain response")

    today_str   = today.isoformat()
    expirations = items[0].get("expirations", [])

    target = None
    for exp in sorted(expirations, key=lambda e: e.get("expiration-date", "")):
        if exp.get("expiration-date", "") >= today_str:
            target = exp
            break
    if target is None:
        raise RuntimeError(f"no upcoming expiration found in chain for {today_str}")

    exp_date = target["expiration-date"]
    log.info(f"chain expiration: {exp_date}  ({len(target.get('strikes', []))} strikes)")

    strikes = []
    for s in target.get("strikes", []):
        strike = float(s.get("strike-price", 0))
        c = s.get("call", {})
        p = s.get("put",  {})
        if isinstance(c, str):
            call_occ = c.replace(" ", "")
            call_sym = _dxlink_symbol(call_occ) if call_occ else _build_symbol(strike, exp_date, "call")
        else:
            call_occ = c.get("symbol", "")
            call_sym = (c.get("streamer-symbol") or
                        (_dxlink_symbol(call_occ) if call_occ else _build_symbol(strike, exp_date, "call")))
        if isinstance(p, str):
            put_occ = p.replace(" ", "")
            put_sym = _dxlink_symbol(put_occ) if put_occ else _build_symbol(strike, exp_date, "put")
        else:
            put_occ  = p.get("symbol", "")
            put_sym  = (p.get("streamer-symbol") or
                        (_dxlink_symbol(put_occ) if put_occ else _build_symbol(strike, exp_date, "put")))
        strikes.append({
            "strike":   strike,
            "call_sym": call_sym,
            "put_sym":  put_sym,
            "call_occ": call_occ,
            "put_occ":  put_occ,
        })

    return strikes, exp_date


# -- DXLink websocket feed ----------------------------------------------------

class DXLinkFeed:
    _DXLINK_VERSION = "0.1-js/1.0.0"

    def __init__(self, url: str, token: str):
        self._url   = url
        self._token = token
        self._state: dict[str, dict] = {}
        self._lock  = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ready = threading.Event()
        self._subs: list[dict] = []
        self._subscribed          = False
        self._data_logged         = False
        # lifecycle telemetry
        self._connected           = False
        self._authorized          = False
        self._channel_open        = False
        self._reconnect_count     = 0
        self._first_connect_seen  = False
        self._auth_fail_count     = 0
        self._last_error: Optional[str]  = None
        self._last_close_code: Optional[int] = None
        self._last_event_time: Optional[datetime] = None

    def set_subscriptions(self, option_symbols: list[str], price_symbols: list[str]):
        self._subs = []
        for sym in option_symbols:
            for event_type in ("Quote", "Summary", "Trade", "Greeks"):
                self._subs.append({"type": event_type, "symbol": sym})
        for sym in price_symbols:
            for event_type in ("Quote", "Trade", "TradeETH", "Summary"):
                self._subs.append({"type": event_type, "symbol": sym})

    def get_state(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def get_health(self) -> dict:
        with self._lock:
            return {
                "connected":            self._connected,
                "authorized":           self._authorized,
                "channel_open":         self._channel_open,
                "reconnect_count":      self._reconnect_count,
                "last_error":           self._last_error,
                "last_close_code":      self._last_close_code,
                "last_feed_event_time": self._last_event_time,
            }

    def needs_reauth(self) -> bool:
        with self._lock:
            return self._auth_fail_count >= 3

    def update_token(self, new_token: str):
        """Replace the streamer token. The next reconnect will use it automatically."""
        with self._lock:
            self._token = new_token
            self._auth_fail_count = 0

    def restart_if_dead(self):
        """Restart the WS thread if run_forever exited (e.g. after a ws.close() call)."""
        if self._thread is None or not self._thread.is_alive():
            log.warning("DXLink thread is dead -- restarting")
            self._start_thread()

    def wait_ready(self, timeout: float = 60.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def wait_first_data(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._last_event_time is not None:
                    return True
            time.sleep(0.5)
        return False

    def _start_thread(self):
        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, kwargs={"reconnect": 5}, daemon=True)
        self._thread.start()
        log.info("DXLink feed thread started")

    def start(self):
        self._thread: Optional[threading.Thread] = None
        self._start_thread()

    def stop(self):
        if self._ws:
            self._ws.close()

    def _send(self, msg: dict):
        if self._ws:
            self._ws.send(json.dumps(msg))

    def _on_open(self, ws):
        with self._lock:
            self._connected = True
            self._subscribed  = False  # reset so FEED_CONFIG re-subscribes after reconnect
            self._data_logged = False
            if self._first_connect_seen:
                self._reconnect_count += 1
            self._first_connect_seen = True
        log.info("DXLink connected -- sending SETUP")
        self._send({
            "type": "SETUP", "channel": 0,
            "version": self._DXLINK_VERSION,
            "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60,
        })

    def _on_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        mtype = msg.get("type")

        if mtype == "SETUP":
            self._send({"type": "AUTH", "channel": 0, "token": self._token})

        elif mtype == "AUTH_STATE":
            state = msg.get("state")
            if state == "AUTHORIZED":
                with self._lock:
                    self._authorized = True
                    self._auth_fail_count = 0
                log.info("DXLink authorized -- requesting channel")
                self._send({
                    "type": "CHANNEL_REQUEST", "channel": 1,
                    "service": "FEED",
                    "parameters": {"contract": "AUTO"},
                })
            else:
                # dxLink sends AUTH_STATE:UNAUTHORIZED unprompted right after SETUP to
                # say it is awaiting credentials -- it is not a rejection, and it races
                # with the AUTH we send from the SETUP handler. A genuine auth failure
                # is a connection that closes having never reached AUTHORIZED, which
                # _on_close counts instead.
                log.debug(f"DXLink awaiting auth: {msg}")

        elif mtype == "CHANNEL_OPENED":
            with self._lock:
                self._channel_open = True
            log.info("DXLink channel 1 open -- sending FEED_SETUP")
            self._send({
                "type": "FEED_SETUP", "channel": 1,
                "acceptDataFormat": "FULL",
                "acceptEventFields": {
                    "Quote":    ["eventType", "eventSymbol", "bidPrice", "askPrice"],
                    "Summary":  ["eventType", "eventSymbol", "openInterest", "prevDayClosePrice", "dayOpenPrice"],
                    "Trade":    ["eventType", "eventSymbol", "dayVolume", "price"],
                    "TradeETH": ["eventType", "eventSymbol", "price"],
                    "Greeks":   ["eventType", "eventSymbol", "volatility", "delta", "gamma", "theta", "vega"],
                },
            })

        elif mtype == "FEED_CONFIG":
            # Server acknowledged FEED_SETUP. Subscribe once only — server
            # may send multiple FEED_CONFIGs (one per batch ack), so guard
            # with a flag to avoid repeated resets.
            if self._subscribed:
                return
            self._subscribed = True
            log.info("DXLink feed configured -- sending subscriptions")
            if self._subs:
                batch_size = 200
                for i in range(0, len(self._subs), batch_size):
                    batch = self._subs[i:i + batch_size]
                    self._send({
                        "type": "FEED_SUBSCRIPTION", "channel": 1,
                        "reset": i == 0, "add": batch,
                    })
                log.info(f"subscribed to {len(self._subs)} event/symbol pairs ({batch_size}/batch)")
            self._ready.set()

        elif mtype == "FEED_DATA":
            data = msg.get("data", [])
            with self._lock:
                if not self._data_logged:
                    self._data_logged = True
                    log.info(f"FEED_DATA sample (first message): {str(data[:3])[:500]}")
            self._ingest(data)

        elif mtype == "KEEPALIVE":
            self._send({"type": "KEEPALIVE", "channel": 0})

        elif mtype == "ERROR":
            log.error(f"DXLink server error: {msg}")

    @staticmethod
    def _to_int(val):
        """Convert val to int, returning None for NaN / non-numeric strings."""
        try:
            f = float(val)
            import math
            return None if math.isnan(f) else int(f)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(val):
        """Convert val to float, returning None for NaN / non-numeric strings."""
        try:
            import math
            f = float(val)
            return None if math.isnan(f) else f
        except (TypeError, ValueError):
            return None

    def _ingest(self, data):
        if not isinstance(data, list):
            return
        now = datetime.now(timezone.utc)
        for event in data:
            if not isinstance(event, dict):
                continue
            et  = event.get("eventType")
            sym = event.get("eventSymbol")
            if not sym:
                continue
            with self._lock:
                self._last_event_time = now
                s = self._state.setdefault(sym, {})
                if et == "Quote":
                    b = self._to_float(event.get("bidPrice"))
                    a = self._to_float(event.get("askPrice"))
                    if b is not None:
                        s["bid"] = b
                        s["bid_ts"] = now.isoformat()
                    if a is not None:
                        s["ask"] = a
                        s["ask_ts"] = now.isoformat()
                elif et == "Summary":
                    oi = self._to_int(event.get("openInterest"))
                    if oi is not None:
                        s["oi"] = oi
                    pc = self._to_float(event.get("prevDayClosePrice"))
                    if pc is not None:
                        s["prev_close"] = pc
                    do = self._to_float(event.get("dayOpenPrice"))
                    if do is not None:
                        s["day_open"] = do
                elif et == "Trade":
                    vol = self._to_int(event.get("dayVolume"))
                    if vol is not None:
                        s["volume"] = vol
                    px = self._to_float(event.get("price"))
                    if px is not None:
                        s["last"] = px
                        s["last_ts"] = now.isoformat()
                elif et == "TradeETH":
                    px = self._to_float(event.get("price"))
                    if px is not None:
                        s["last"] = px
                        s["last_ts"] = now.isoformat()
                elif et == "Greeks":
                    for field in ("volatility", "delta", "gamma", "theta", "vega"):
                        v = self._to_float(event.get(field))
                        if v is not None:
                            s[field] = v

    def _on_error(self, ws, error):
        with self._lock:
            self._last_error = str(error)
        log.error(f"DXLink error: {error}")

    def _on_close(self, ws, code, msg):
        with self._lock:
            never_authorized   = not self._authorized
            self._connected    = False
            self._authorized   = False
            self._channel_open = False
            self._last_close_code = code
            if never_authorized:
                self._auth_fail_count += 1
                fail_count = self._auth_fail_count
        if never_authorized:
            log.error(f"DXLink closed without authorizing (attempt {fail_count}): code={code}")
        else:
            log.warning(f"DXLink closed: code={code}")
        self._ready.clear()


# -- ephemeral live quote HTTP service ----------------------------------------

class LiveQuoteRegistry:
    """Thread-safe session pointer behind the process-lifetime HTTP server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._feed: Optional[DXLinkFeed] = None
        self._contracts: dict[str, dict] = {}
        self._ticker_fallbacks: dict[str, dict] = {}

    def set_session(self, feed: DXLinkFeed, contracts: dict[str, dict]):
        with self._lock:
            self._feed = feed
            self._contracts = dict(contracts)

    def clear_session(self):
        with self._lock:
            self._feed = None
            self._contracts = {}

    def update_ticker_fallbacks(self, prices: dict[str, dict], observed_at: str):
        with self._lock:
            self._ticker_fallbacks = {
                symbol: {**quote, "quote_ts": quote.get("quote_ts")}
                for symbol, quote in prices.items()
            }

    def health(self) -> dict:
        with self._lock:
            feed = self._feed
        now = datetime.now(timezone.utc)
        if feed is None:
            return {"state": "offline", "last_quote_at": None, "server_ts": now.isoformat()}
        feed_health = feed.get_health()
        last_event = feed_health["last_feed_event_time"]
        if not (feed_health["connected"] and feed_health["authorized"] and feed_health["channel_open"]):
            state = "connecting"
        elif last_event is None:
            state = "connecting"
        elif (now - last_event).total_seconds() > STALE_FEED_SECS:
            state = "stale"
        else:
            state = "live"
        return {
            "state": state,
            "last_quote_at": last_event.isoformat() if last_event else None,
            "server_ts": now.isoformat(),
        }

    def quote_payload(self, symbols: list[str]) -> dict:
        with self._lock:
            feed = self._feed
            contracts = dict(self._contracts)
            ticker_fallbacks = dict(self._ticker_fallbacks)
        health = self.health()
        state = feed.get_state() if feed is not None else {}
        quotes = []
        for symbol in symbols:
            if symbol in PRICE_TICKERS:
                raw = state.get(PRICE_TICKERS[symbol], {})
                bid, ask, last = raw.get("bid"), raw.get("ask"), raw.get("last")
                mid = (bid + ask) / 2 if bid is not None and ask is not None else None
                price = last if last is not None else mid
                observed = [ts for ts in (raw.get("last_ts"), raw.get("bid_ts"), raw.get("ask_ts")) if ts]
                if price is not None and observed:
                    prev = raw.get("prev_close")
                    quotes.append({
                        "kind": "ticker", "symbol": symbol, "price": price,
                        "bid": bid, "ask": ask, "prev_close": prev,
                        "chg_pct": round((price - prev) / prev * 100, 2) if prev else None,
                        "quote_ts": max(observed),
                        "bid_ts": raw.get("bid_ts"), "ask_ts": raw.get("ask_ts"),
                        "source": "dxlink",
                        "instrument_class": TICKER_CLASSES[symbol],
                    })
                elif ticker_fallbacks.get(symbol, {}).get("price") is not None:
                    quotes.append({
                        **ticker_fallbacks[symbol], "kind": "ticker", "symbol": symbol,
                        "instrument_class": TICKER_CLASSES[symbol],
                    })
                continue
            contract = contracts.get(symbol)
            if not contract:
                continue
            quote = state.get(contract["streamer_symbol"], {})
            bid = quote.get("bid")
            ask = quote.get("ask")
            bid_ts = quote.get("bid_ts")
            ask_ts = quote.get("ask_ts")
            observed = [ts for ts in (bid_ts, ask_ts) if ts]
            if bid is None or ask is None or not observed:
                continue
            quotes.append({
                "kind": "option",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "bid_ts": bid_ts,
                "ask_ts": ask_ts,
                "quote_ts": max(observed),
                "strike": contract["strike"],
                "type": contract["type"],
                "exp": contract["exp"],
            })
        return {
            "quotes": quotes,
            "requested": len(symbols),
            "returned": len(quotes),
            "server_ts": health["server_ts"],
            "health": health,
        }


def start_live_quote_server(registry: LiveQuoteRegistry):
    access_key = os.environ.get("LIVE_QUOTE_KEY", "")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, registry.health())
                return
            if parsed.path != "/live-quotes":
                self._json(404, {"error": "Not found"})
                return
            if not access_key or self.headers.get("X-Live-Quote-Key") != access_key:
                self._json(403, {"error": "Forbidden"})
                return
            raw_symbols = parse_qs(parsed.query).get("symbols", [""])[0]
            symbols = list(dict.fromkeys(
                symbol.strip().replace(" ", "")
                for symbol in raw_symbols.split(",")
                if symbol.strip()
            ))
            if not symbols or len(symbols) > MAX_LIVE_QUOTE_SYMBOLS:
                self._json(400, {"error": f"Request 1-{MAX_LIVE_QUOTE_SYMBOLS} symbols"})
                return
            health = registry.health()
            has_tickers = any(symbol in PRICE_TICKERS for symbol in symbols)
            if health["state"] in {"offline", "connecting"} and not has_tickers:
                self._json(
                    503,
                    {"error": "Live quote feed unavailable", "health": health},
                    {"Retry-After": "5"},
                )
                return
            self._json(200, registry.quote_payload(symbols))

        def _json(self, status: int, value: dict, headers: Optional[dict[str, str]] = None):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            log.debug("live quote HTTP: " + format, *args)

    server = ThreadingHTTPServer(("0.0.0.0", LIVE_QUOTE_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, name="live-quote-http", daemon=True)
    thread.start()
    log.info(f"live quote HTTP service started on port {LIVE_QUOTE_PORT}")
    return server


# -- tier classification (mirrors oi_viewer.py) -------------------------------

# The window must reach back far enough to cover the current month's monthly-opex
# Friday, which classify_tier looks up and which is already in the past for most of
# the second half of any month.
_CALENDAR_LOOKBACK_DAYS = 45
_CALENDAR_LOOKAHEAD_DAYS = 90


def _load_calendar():
    try:
        import pandas_market_calendars as mcal
        nyse  = mcal.get_calendar("NYSE")
        start = date.today() - timedelta(days=_CALENDAR_LOOKBACK_DAYS)
        end   = date.today() + timedelta(days=_CALENDAR_LOOKAHEAD_DAYS)
        return {d.date() for d in nyse.valid_days(start_date=start.isoformat(),
                                                    end_date=end.isoformat())}
    except Exception as exc:
        log.warning(f"trading calendar unavailable ({exc}) -- tier falls back to 0DTE_Regular")
        return set()


DEFAULT_TIER = "0DTE_Regular"

# Bound on how far the trading-day walks may step before giving up. The calendar
# window is finite, so an unbounded walk off either end runs to date.min/date.max
# and raises OverflowError -- which used to kill the whole collector session.
_TD_WALK_LIMIT = 30


class _CalendarLookupError(Exception):
    """A trading-day walk ran past the edge of the loaded calendar window."""


def classify_tier(today: date) -> str:
    import calendar as _cal

    valid = _load_calendar()
    if not valid:
        return DEFAULT_TIER

    def prior_td(d):
        for _ in range(_TD_WALK_LIMIT):
            if d in valid:
                return d
            d -= timedelta(days=1)
        raise _CalendarLookupError(f"no trading day at or before {d} within calendar window")

    def next_td(d):
        for _ in range(_TD_WALK_LIMIT):
            d += timedelta(days=1)
            if d in valid:
                return d
        raise _CalendarLookupError(f"no trading day after {d} within calendar window")

    def nominal_fri(d):
        return d + timedelta(days=(4 - d.weekday()) % 7)

    try:
        eow    = prior_td(nominal_fri(today))
        plus1d = next_td(today)
        if plus1d != eow:
            return DEFAULT_TIER

        count, opex = 0, None
        for day in range(1, _cal.monthrange(plus1d.year, plus1d.month)[1] + 1):
            if date(plus1d.year, plus1d.month, day).weekday() == 4:
                count += 1
                if count == 3:
                    opex = prior_td(date(plus1d.year, plus1d.month, day))
                    break
        return "0DTE_Monthly" if plus1d == opex else "0DTE_Weekly"
    except _CalendarLookupError as exc:
        # Tier is a labelling concern; never let it take down market-data streaming.
        log.warning(f"tier classification fell back to {DEFAULT_TIER}: {exc}")
        return DEFAULT_TIER


# -- R2 client ----------------------------------------------------------------

def make_s3():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


# -- startup classification ---------------------------------------------------

def _classify_startup(s3, process_start: datetime) -> str:
    """Read prior health.json to classify this startup."""
    try:
        resp  = s3.get_object(Bucket=R2_BUCKET, Key="intraday/health.json")
        prior = json.loads(resp["Body"].read())
    except Exception:
        return "clean_start"

    if prior.get("collector", {}).get("past_stop", False):
        return "clean_start"

    prior_updated = prior.get("updated_at")
    if prior_updated:
        try:
            prior_dt  = datetime.fromisoformat(prior_updated.replace("Z", "+00:00"))
            gap_mins  = (process_start - prior_dt).total_seconds() / 60
            return "recovery_after_crash" if gap_mins < 120 else "recovery_after_gap"
        except Exception:
            pass

    return "unknown"


# -- ticker health diagnostics ------------------------------------------------

def _log_ticker_health(feed: DXLinkFeed):
    state = feed.get_state()
    dead  = []
    log.info("-- price ticker health check ------------------------------------")
    for label, dxlink_sym in PRICE_TICKERS.items():
        d     = state.get(dxlink_sym, {})
        price = d.get("last") or (
            round((d["bid"] + d["ask"]) / 2, 4)
            if d.get("bid") is not None and d.get("ask") is not None else None
        )
        if price is not None:
            log.info(f"  OK    {label:<10} ({dxlink_sym})  price={price}")
        else:
            log.warning(f"  WARN  {label:<10} ({dxlink_sym})  NO DATA -- symbol may be wrong")
            dead.append(label)
    if dead:
        log.warning(f"  {len(dead)} ticker(s) with no data: {', '.join(dead)}")
    else:
        log.info("  all price tickers returning data")
    log.info("-----------------------------------------------------------------")


# -- prices.json upload (every PRICES_SECS) -----------------------------------

# yfinance can degrade to ~10s per symbol when Yahoo's API is down, turning one
# fill into a multi-minute stall of the prices loop. Bound each fill with a
# timeout and back off for a cooldown after a failure. Successful results are
# cached so Yahoo is hit at most once per YF_MIN_INTERVAL_SECS even though the
# prices loop runs faster.
YF_FETCH_TIMEOUT_SECS   = 20
YF_COOLDOWN_SECS        = 300
YF_MIN_INTERVAL_SECS    = 60
_yf_cooldown_until: list = [None]   # [datetime|None]
_yf_cache: dict = {"ts": None, "data": None}


def fetch_yf_prices() -> dict[str, Optional[float]]:
    """Fetch current prices from Yahoo Finance. Supports pre/post-market."""
    result: dict[str, Optional[float]] = {k: None for k in YF_SYMBOL_MAP}
    try:
        tickers = yf.Tickers(" ".join(YF_SYMBOL_MAP.values()))
        for label, sym in YF_SYMBOL_MAP.items():
            try:
                fi = tickers.tickers[sym].fast_info
                price = None
                for attr in ("pre_market_price", "last_price", "post_market_price"):
                    val = getattr(fi, attr, None)
                    if val is not None and float(val) > 0:
                        price = float(val)
                        break
                result[label] = price
            except Exception:
                pass
    except Exception as e:
        log.warning(f"yfinance fetch failed: {e}")
    return result


def fetch_yf_prices_bounded() -> dict[str, Optional[float]]:
    """fetch_yf_prices with a result cache, hard deadline, and outage cooldown.

    Returns the cached result if fetched within YF_MIN_INTERVAL_SECS. Returns
    all-None immediately while in cooldown, or if the fetch exceeds
    YF_FETCH_TIMEOUT_SECS / returns no data (both open a new cooldown).
    """
    empty: dict[str, Optional[float]] = {k: None for k in YF_SYMBOL_MAP}
    now = datetime.now(timezone.utc)
    if (_yf_cache["ts"] is not None and
            (now - _yf_cache["ts"]).total_seconds() < YF_MIN_INTERVAL_SECS):
        return _yf_cache["data"]
    until = _yf_cooldown_until[0]
    if until is not None and now < until:
        return empty
    box: list = [None]

    def _worker():
        try:
            box[0] = fetch_yf_prices()
        except Exception as e:
            log.warning(f"yfinance fetch failed: {e}")

    # daemon thread (not ThreadPoolExecutor) so a hung Yahoo call can't block
    # interpreter shutdown; a stranded thread just dies with the process
    t = threading.Thread(target=_worker, name="yf-fetch", daemon=True)
    t.start()
    t.join(timeout=YF_FETCH_TIMEOUT_SECS)
    if t.is_alive():
        _yf_cooldown_until[0] = now + timedelta(seconds=YF_COOLDOWN_SECS)
        log.warning(f"yfinance timed out after {YF_FETCH_TIMEOUT_SECS}s -- "
                    f"cooling down for {YF_COOLDOWN_SECS}s")
        return empty
    result = box[0]
    if result is None or all(v is None for v in result.values()):
        _yf_cooldown_until[0] = now + timedelta(seconds=YF_COOLDOWN_SECS)
        log.warning(f"yfinance returned no data -- cooling down for {YF_COOLDOWN_SECS}s")
    else:
        _yf_cooldown_until[0] = None
        _yf_cache["ts"] = now
        _yf_cache["data"] = result
    return result if result is not None else empty


def push_prices(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None):
    state      = feed.get_state()
    fh         = feed.get_health()
    ts_et      = datetime.now(ET)
    ts_utc     = datetime.now(timezone.utc)

    last_event = fh["last_feed_event_time"]
    feed_stale = (last_event is None or
                  (ts_utc - last_event).total_seconds() > STALE_FEED_SECS)
    if feed_stale:
        log.warning(f"prices.json -- feed stale (last event: {last_event})")

    prices = {}
    for label, dxlink_sym in PRICE_TICKERS.items():
        d       = state.get(dxlink_sym, {})
        bid     = d.get("bid")
        ask     = d.get("ask")
        last    = d.get("last")
        mid     = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
        price   = last or mid
        prev    = d.get("prev_close")
        chg_pct = None
        if price and prev and prev != 0:
            chg_pct = round((price - prev) / prev * 100, 2)
        prices[label] = {
            "price":      price,
            "bid":        bid,
            "ask":        ask,
            "prev_close": prev,
            "chg_pct":    chg_pct,
            "volume":     d.get("volume"),
            "source":     "dxlink" if price is not None else None,
            "quote_ts":   max([v for v in (d.get("last_ts"), d.get("bid_ts"), d.get("ask_ts")) if v],
                              default=None),
        }

    # yfinance fallback for any tickers DXLink didn't populate
    yf_missing = [lbl for lbl, d in prices.items() if d["price"] is None]
    if yf_missing:
        yf_data = fetch_yf_prices_bounded()
        filled = []
        for lbl in yf_missing:
            yf_price = yf_data.get(lbl)
            if yf_price is not None:
                prices[lbl]["price"] = yf_price
                prices[lbl]["source"] = "yfinance"
                # yfinance's simple last-price response has no trustworthy
                # provider event timestamp. Retrieval time must not masquerade
                # as market observation time.
                prices[lbl]["quote_ts"] = None
                filled.append(f"{lbl}={yf_price}")
        if filled:
            log.info(f"prices -- yfinance filled: {', '.join(filled)}")
        qqq_yf = yf_data.get("QQQ")
        if qqq_yf is not None:
            _last_spot[0] = qqq_yf
            # Same reasoning as quote_ts above: yfinance has no trustworthy
            # provider event timestamp, so _last_spot[1] must not carry over
            # whatever (older, DXLink-sourced) timestamp was there before --
            # take_snapshot's freshness check needs spot_ts=None here to
            # correctly report this fallback as not "live".
            _last_spot[1] = None

    for lbl, d in prices.items():
        if d["price"] is not None:
            _last_prices[lbl] = {
                "price": d["price"],
                "quote_ts": d.get("quote_ts"),
            }

    # last-known-value fallback for anything still missing
    stale_filled = []
    for lbl, d in prices.items():
        remembered = _last_prices.get(lbl)
        if d["price"] is None and remembered is not None:
            d["price"] = remembered["price"]
            d["stale"] = True
            d["source"] = "last-known"
            d["quote_ts"] = remembered.get("quote_ts")
            stale_filled.append(lbl)
    if stale_filled:
        log.warning(f"prices -- serving last-known values for: {', '.join(stale_filled)}")

    dead = [label for label, d in prices.items() if d["price"] is None]
    if dead:
        log.warning(f"prices.json -- no data for: {', '.join(dead)}")

    if quote_registry is not None:
        quote_registry.update_ticker_fallbacks(prices, ts_utc.isoformat())

    payload = json.dumps({
        "timestamp":     ts_utc.isoformat(),
        "snapshot_time": ts_et.strftime("%H:%M ET"),
        "feed_stale":    feed_stale,
        "prices":        prices,
    }, default=str)

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key="intraday/prices.json",
            Body=payload.encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        counters.inc_prices(ts_utc.isoformat())
    except Exception as e:
        counters.inc_failure()
        raise


def prices_loop(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None):
    while not past_stop():
        try:
            push_prices(s3, feed, counters, quote_registry)
        except Exception as e:
            log.error(f"prices.json error: {e}")
        time.sleep(PRICES_SECS)
    log.info("prices loop stopped")


# -- eia_steo.json upload (every EIA_STEO_POLL_SECS, optional feature) -------


def fetch_eia_steo_rows(api_key: str) -> list[dict]:
    """Raw rows from EIA's v2 STEO API for the series crude_calibration.py
    tracks (see BRENT_SERIES_ID/WTI_SERIES_ID/BALANCE_SERIES_ID there),
    most-recent-period-first. Network I/O only -- parsing/normalization is
    crude_calibration.parse_steo_rows.

    Series IDs and the response shape assumed here (`response.data`, each row
    a flat dict with `period`/`seriesId`/`value`) should be verified against
    a live call before this is trusted -- see crude_calibration.py's module
    docstring.

    An earlier version passed `length=36` intending "36 periods of history
    per series" -- but EIA v2's `length` caps *total rows in the response*,
    and with 3 series requested together that capped the combined response
    at 36 rows total (~12 periods across 3 series), silently truncating
    history without any error. Flagged in review. Fixed two ways: request a
    `length` (`EIA_STEO_PAGE_LENGTH`) with real margin over what 3 series x
    36 periods needs, and verify the server's own `response.total` count
    against what was actually returned -- raising rather than silently
    returning a truncated page if EIA's per-request cap or a future
    additional series ever exceeds it. `total` is only checked when present;
    an unexpected response schema degrades to "can't verify," not a crash.
    """
    params = {
        "api_key": api_key,
        "frequency": "monthly",
        "data[0]": "value",
        "facets[seriesId][]": [cc.BRENT_SERIES_ID, cc.WTI_SERIES_ID, cc.BALANCE_SERIES_ID],
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": EIA_STEO_PAGE_LENGTH,
    }
    resp = requests.get(EIA_STEO_URL, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()["response"]
    data = payload["data"]
    total = payload.get("total")
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        # A missing/malformed `total` means we cannot verify the response
        # wasn't truncated -- fail closed rather than silently persisting
        # unverifiable partial data as a new vintage. Flagged in review: the
        # original `if total is not None:` guard skipped verification
        # entirely (and swallowed unparseable values into a no-op) whenever
        # EIA's response didn't carry a clean int `total`, which is exactly
        # the case this check exists to catch.
        raise RuntimeError(
            f"EIA STEO response missing or malformed 'total' field ({total!r}) -- cannot "
            f"verify the {len(data)} returned row(s) are complete, refusing to treat them "
            f"as a trustworthy vintage."
        )
    if total_int > len(data):
        raise RuntimeError(
            f"EIA STEO response truncated: server reports {total_int} total row(s) but "
            f"only {len(data)} were returned (length={EIA_STEO_PAGE_LENGTH}). Increase "
            f"EIA_STEO_PAGE_LENGTH or add real offset-based pagination rather than "
            f"silently using a partial history."
        )
    return data


def load_steo_vintage_log(s3) -> list[dict]:
    """Rolling log of past STEO releases this collector has itself observed
    -- EIA's live API serves the current published values, not a queryable
    history of what a *prior* release forecast for the same future month, so
    that vintage-over-vintage comparison has to be built up locally over
    time, one release per stored entry (same reasoning as RVOL's own
    baselines/ history, see market_signals.py).

    Raises on anything other than the log genuinely never having been
    written yet (`NoSuchKey` -- a fresh deployment/bucket). A transient R2
    read failure, an auth error, or corrupt JSON must NOT be silently
    treated as "no history exists": `push_eia_steo` appends to whatever
    this returns and overwrites the R2 object with the result, so treating
    a *read* failure as an empty log would destroy every real prior
    vintage on the next write. Flagged in review; let the caller's own
    exception handling (`eia_steo_loop`'s per-cycle try/except) skip this
    poll and retry later instead.
    """
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=EIA_STEO_LOG_KEY)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return []
    return json.loads(body)


def save_steo_vintage_log(s3, entries: list[dict]) -> None:
    payload = json.dumps(entries[-EIA_STEO_MAX_VINTAGES:]).encode()
    s3.put_object(
        Bucket=R2_BUCKET, Key=EIA_STEO_LOG_KEY,
        Body=payload, ContentType="application/json",
    )


def push_eia_steo(s3) -> None:
    rows = fetch_eia_steo_rows(EIA_API_KEY)
    # EIA's v2 API doesn't hand back "which monthly release this row came
    # from" directly, so the calendar month we happen to be polling in is
    # used as the vintage *label* -- but whether to mint a new vintage entry
    # at all is decided by whether the fetched data actually changed from
    # the most recently logged entry, not by the calendar alone. Labeling by
    # calendar month unconditionally (the original approach) creates a
    # phantom release right at a boundary: a poll in the first few days of a
    # month, before that month's STEO has actually been published, would log
    # a new entry under the new month's label containing last month's
    # unchanged data -- and anything reading macro/eia_steo.json in that
    # window (or a vintage-over-vintage comparison run against it) would see
    # a fabricated "this month's release" that was never actually published.
    # Flagged in review. Content-equality against the last logged entry is
    # the real signal for "did a new release land"; the calendar month is
    # only the label attached once that's true.
    release_period = datetime.now(timezone.utc).strftime("%Y-%m")
    current = cc.parse_steo_rows(rows, release_period=release_period)
    current_points_payload = [
        {"period": p.period, "brent": p.brent, "wti": p.wti, "balance": p.balance}
        for p in current.points
    ]

    entries = load_steo_vintage_log(s3)  # raises on a real read failure -- see that function's docstring
    if entries and entries[-1].get("points") == current_points_payload:
        # Byte-identical to the most recent logged vintage: EIA hasn't
        # actually published a new STEO since we last saw one, even if the
        # calendar month has rolled over. Reuse the existing label instead
        # of minting a phantom one, and skip the R2 write entirely.
        release_period = entries[-1]["release_period"]
        # Flagged in review: `current` was built above with the pre-
        # canonicalization calendar guess, so leaving it as-is would let
        # compare_vintages() below stamp each SteoRevision's current_release
        # with that stale guess while the top-level payload's current_release
        # uses the canonicalized label -- the two would disagree. Rebuild
        # `current` with the final label before it's used for anything else.
        current = cc.SteoVintage(release_period=release_period, points=current.points)
    else:
        entries = [e for e in entries if e.get("release_period") != release_period]
        entries.append({"release_period": release_period, "points": current_points_payload})
        save_steo_vintage_log(s3, entries)

    prior_vintage = None
    if len(entries) >= 2:
        prior_entry = entries[-2]
        prior_vintage = cc.SteoVintage(
            release_period=prior_entry["release_period"],
            points=tuple(cc.SteoPricePoint(**p) for p in prior_entry["points"]),
        )

    revisions = []
    if prior_vintage is not None:
        for point in current.points:
            revision = cc.compare_vintages(prior_vintage, current, point.period)
            if revision is not None:
                revisions.append(revision.__dict__)

    ovx_value = _last_prices.get("OVX", {}).get("price")

    payload = json.dumps({
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "current_release": release_period,
        "prior_release":   prior_vintage.release_period if prior_vintage else None,
        "points":          [
            {"period": p.period, "brent": p.brent, "wti": p.wti, "balance": p.balance}
            for p in current.points
        ],
        "revisions":       revisions,
        "ovx": {
            "value":  ovx_value,
            "regime": cc.classify_ovx_regime(ovx_value),
        },
    }, default=str).encode()

    s3.put_object(
        Bucket=R2_BUCKET, Key=EIA_STEO_KEY,
        Body=payload, ContentType="application/json",
        CacheControl="no-cache, max-age=300",
    )


def eia_steo_loop(s3) -> None:
    if not EIA_API_KEY:
        log.info("EIA_API_KEY not set -- skipping macro/eia_steo.json (crude calibration) feed")
        return
    while True:
        try:
            push_eia_steo(s3)
        except Exception as e:
            log.warning(f"eia_steo.json error: {e}")
        time.sleep(EIA_STEO_POLL_SECS)


# -- health.json upload (every 15s) ------------------------------------------

def push_health(s3, feed: DXLinkFeed, counters: Counters, tracker: SnapshotTracker,
                run_id: str, process_start: datetime, classification: str, today: date):
    fh   = feed.get_health()
    ctr  = counters.get()
    trk  = tracker.get()
    now  = datetime.now(timezone.utc)

    last_event = fh["last_feed_event_time"]
    feed_stale = (last_event is None or (now - last_event).total_seconds() > STALE_FEED_SECS)

    state     = feed.get_state()
    no_data   = [label for label, sym in PRICE_TICKERS.items()
                 if state.get(sym, {}).get("last") is None and state.get(sym, {}).get("bid") is None]
    with_data = len(PRICE_TICKERS) - len(no_data)

    payload = json.dumps({
        "run_id":             run_id,
        "trade_date":         today.isoformat(),
        "process_start_time": process_start.isoformat(),
        "updated_at":         now.isoformat(),
        "classification":     classification,
        "collector": {
            "past_stop":      past_stop(),
            "loop_alive":     True,
            "last_loop_time": now.isoformat(),
        },
        "feed": {
            "connected":            fh["connected"],
            "authorized":           fh["authorized"],
            "channel_open":         fh["channel_open"],
            "reconnect_count":      fh["reconnect_count"],
            "last_feed_event_time": last_event.isoformat() if last_event else None,
            "feed_stale":           feed_stale,
            "last_error":           fh["last_error"],
            "last_close_code":      fh["last_close_code"],
        },
        "uploads": {
            "prices_success_count":      ctr["prices_ok"],
            "snapshot_success_count":    ctr["snapshot_ok"],
            "csv_success_count":         ctr["csv_ok"],
            "failure_count":             ctr["failures"],
            "last_price_upload_time":    ctr["last_price_time"],
            "last_snapshot_upload_time": ctr["last_snapshot_time"],
        },
        "cadence": trk,
        "symbols": {
            "expected_price_symbols":  len(PRICE_TICKERS),
            "price_symbols_with_data": with_data,
            "no_data_symbols":         no_data,
        },
    }, default=str)

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key="intraday/health.json",
            Body=payload.encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.error(f"health.json upload failed: {e}")
        counters.inc_failure()


def health_loop(s3, feed: DXLinkFeed, counters: Counters, tracker: SnapshotTracker,
                run_id: str, process_start: datetime, classification: str, today: date):
    while not past_stop():
        try:
            push_health(s3, feed, counters, tracker, run_id, process_start, classification, today)
        except Exception as e:
            log.error(f"health loop error: {e}")
        time.sleep(HEALTH_SECS)
    log.info("health loop stopped")


# -- snapshot upload ----------------------------------------------------------

def _fmt_oi(v: int) -> str:
    if v == 0:    return ""
    if v < 1000:  return str(v)
    if v < 10000: return f"{v/1000:.1f}K"
    return f"{v//1000}K"


_prev_vol: dict[str, int] = {}    # persists across calls to compute per-minute delta
_last_spot: list = [None, None]   # [price|None, observed_at_iso|None] — yfinance/CSV fallback for underlying price
_last_prices: dict[str, dict] = {}  # label -> {price, quote_ts}; timestamp never refreshed by fallback
_first_snapshot_written: bool = False  # guard so first.csv is only written once per session

_vwap_state: "ms.VwapState" = ms.VwapState()  # session-scoped VWAP accumulator, see market_signals.py
_rvol_baseline: dict = {}         # loaded once per session from RVOL_BASELINE_KEY; {"buckets": {...}, ...}
_rvol_today: dict[str, int] = {}  # bucket_label -> latest session cum_volume seen in that bucket this session

# Rolling window of recent spot observations for the display-only momentum
# reading (see market_signals.py's compute_time_series_momentum). Unlike
# _vwap_state, this needs no restart-recovery persistence: it's a bounded
# real-time window, not a session-cumulative sum, so it self-heals within
# ~MOMENTUM_LOOKBACK_MINUTES of any restart on its own.
_momentum_history: list = []  # list[ms.SpotPoint]


def restore_state(s3, today: date) -> None:
    """Seed _prev_vol, _last_spot, and _last_prices from the most recent R2 snapshot.

    Called at session start so a redeploy doesn't blank VolDelta for one beat
    or lose price context.
    """
    date_str = today.strftime("%Y%m%d")
    try:
        resp = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=f"intraday/{date_str}/")
        csvs = sorted(
            [o for o in resp.get("Contents", []) if o["Key"].endswith(".csv")],
            key=lambda o: o["Key"],
        )
        if not csvs:
            log.info("restore_state: no snapshots found for today, starting fresh")
            return

        global _first_snapshot_written
        _first_snapshot_written = True  # prior snapshots exist; first.csv already written

        latest_key = csvs[-1]["Key"]
        body = s3.get_object(Bucket=R2_BUCKET, Key=latest_key)["Body"].read().decode()
        df = pd.read_csv(io.StringIO(body))

        # Restore _prev_vol from Volume column keyed by OptionSymbol
        if "OptionSymbol" in df.columns and "Volume" in df.columns:
            for _, row in df.iterrows():
                sym = row.get("OptionSymbol")
                vol = row.get("Volume")
                if sym and pd.notna(vol):
                    # Convert OCC symbol back to dxFeed format used as state key
                    try:
                        dx_sym = _dxlink_symbol(str(sym))
                        _prev_vol[dx_sym] = int(vol)
                    except Exception:
                        pass

        # Restore _last_spot, paired with the timestamp encoded in the
        # snapshot's own filename (snapshot_HHMMSSffffff.csv) -- not
        # datetime.now() at restore time, which would misrepresent how old
        # this recovered price actually is.
        if "UnderlyingPrice" in df.columns:
            spot = df["UnderlyingPrice"].dropna().iloc[-1] if not df["UnderlyingPrice"].dropna().empty else None
            if spot:
                _last_spot[0] = float(spot)
                _last_spot[1] = None
                name_match = re.search(r"snapshot_(\d{6})(\d{6})\.csv$", latest_key)
                if name_match:
                    hms, micro = name_match.groups()
                    try:
                        spot_dt = ET.localize(datetime(
                            today.year, today.month, today.day,
                            int(hms[0:2]), int(hms[2:4]), int(hms[4:6]), int(micro),
                        ))
                        _last_spot[1] = spot_dt.astimezone(timezone.utc).isoformat()
                    except ValueError:
                        pass

        # Restore _last_prices from price columns (any col not in core option fields)
        core_cols = {"TradeDate","Expiration","Strike","Type","OptionSymbol","DTE",
                     "OpenInterest","Volume","VolDelta","Bid","Mid","Ask","Last",
                     "IV","Delta","Gamma","Theta","Vega","UnderlyingPrice"}
        for col in df.columns:
            if col not in core_cols:
                val = df[col].dropna().iloc[-1] if not df[col].dropna().empty else None
                if val is not None:
                    label = col.replace("_", "/") if col in ("JPY_USD", "BTC_USD") else col
                    _last_prices[label] = {"price": float(val), "quote_ts": None}

        _restore_vwap_state(s3, today, date_str)

        log.info(
            f"restore_state: loaded {latest_key.split('/')[-1]} -- "
            f"vol_keys={len(_prev_vol)}  spot={_last_spot[0]}  prices={len(_last_prices)}"
        )
    except Exception as e:
        log.warning(f"restore_state failed (non-fatal): {e}")


def _vwap_state_key(date_str: str) -> str:
    return f"intraday/{date_str}/vwap_state.json"


def _restore_vwap_state(s3, today: date, date_str: str) -> None:
    """Best-effort recovery of `_vwap_state`'s running sums after a mid-session
    restart. Written every snapshot cycle purely as a restart-recovery aid --
    nothing else reads this object. Without this, a Railway restart mid-day
    would silently reset VWAP to a fresh (lower-sample) accumulation with no
    indication in the payload that this happened.
    """
    global _vwap_state
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=_vwap_state_key(date_str))["Body"].read()
        data = json.loads(body)
        if data.get("session_date") != today.isoformat():
            return  # stale leftover from a prior day; a fresh accumulator is correct
        session_started_at = data.get("session_started_at")
        last_observed_at = data.get("last_observed_at")
        _vwap_state = ms.VwapState(
            session_date=today,
            session_started_at=datetime.fromisoformat(session_started_at) if session_started_at else None,
            last_observed_at=datetime.fromisoformat(last_observed_at) if last_observed_at else None,
            cum_pv=data.get("cum_pv", 0.0),
            cum_vol=data.get("cum_vol", 0),
            last_dayvolume=data.get("last_dayvolume"),
            vwap=data.get("vwap"),
            vwap_ts=data.get("vwap_ts"),
        )
        log.info(f"restore_state: recovered vwap_state -- cum_vol={_vwap_state.cum_vol}  vwap={_vwap_state.vwap}")
    except Exception as e:
        log.info(f"restore_vwap_state: nothing to recover ({e})")


def _persist_vwap_state(s3, date_str: str) -> None:
    """Write `_vwap_state`'s running sums so a mid-session restart can recover
    them via `_restore_vwap_state`. Best-effort -- a failure here must never
    interrupt the snapshot it's piggybacking on.
    """
    try:
        data = {
            "session_date": _vwap_state.session_date.isoformat() if _vwap_state.session_date else None,
            "session_started_at": _vwap_state.session_started_at.isoformat() if _vwap_state.session_started_at else None,
            "last_observed_at": _vwap_state.last_observed_at.isoformat() if _vwap_state.last_observed_at else None,
            "cum_pv": _vwap_state.cum_pv,
            "cum_vol": _vwap_state.cum_vol,
            "last_dayvolume": _vwap_state.last_dayvolume,
            "vwap": _vwap_state.vwap,
            "vwap_ts": _vwap_state.vwap_ts,
        }
        s3.put_object(
            Bucket=R2_BUCKET, Key=_vwap_state_key(date_str),
            Body=json.dumps(data).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.warning(f"vwap_state.json upload failed (non-fatal): {e}")


def load_rvol_baseline(s3) -> dict:
    """Best-effort load of the cross-day RVOL baseline. Missing file (first
    deploy of this feature) is not an error -- every bucket simply reports
    "insufficient_history"/"no_data" until enough sessions have run.
    """
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=RVOL_BASELINE_KEY)["Body"].read()
        data = json.loads(body)
        log.info(f"load_rvol_baseline: loaded, updated_through={data.get('updated_through')}")
        return data
    except Exception as e:
        log.info(f"load_rvol_baseline: no baseline yet ({e}) -- starting fresh")
        return {
            "symbol": TICKER,
            "bucket_minutes": RVOL_BUCKET_MINUTES,
            "lookback_days": RVOL_LOOKBACK_DAYS,
            "min_days_required": RVOL_MIN_DAYS_REQUIRED,
            "updated_through": None,
            "buckets": {},
        }


def finalize_rvol_baseline(s3, today: date) -> None:
    """Fold today's completed-session bucket readings into the baseline.

    Called once, at end-of-session -- never mid-session -- so the baseline
    file only ever reflects complete trading days; a mid-session crash before
    this runs simply means today's readings are lost for the baseline (not
    corrupted into it), the same "prefer a clean miss over corrupt state"
    tradeoff `restore_state` already makes for CSV-derived state.
    """
    if not _rvol_today:
        log.info("finalize_rvol_baseline: no bucket readings this session, skipping")
        return
    try:
        baseline = load_rvol_baseline(s3)
        buckets = baseline.setdefault("buckets", {})
        for bucket, cum_volume in _rvol_today.items():
            samples = buckets.get(bucket, {}).get("samples", [])
            samples = ms.prune_baseline_samples(samples, today, baseline.get("lookback_days", RVOL_LOOKBACK_DAYS))
            samples = ms.append_session_reading(samples, today, cum_volume)
            buckets[bucket] = {"samples": samples}
        baseline["updated_through"] = today.isoformat()
        baseline["bucket_minutes"] = RVOL_BUCKET_MINUTES
        baseline["lookback_days"] = RVOL_LOOKBACK_DAYS
        baseline["min_days_required"] = RVOL_MIN_DAYS_REQUIRED
        s3.put_object(
            Bucket=R2_BUCKET, Key=RVOL_BASELINE_KEY,
            Body=json.dumps(baseline).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        log.info(f"finalize_rvol_baseline: wrote {len(_rvol_today)} bucket readings for {today.isoformat()}")
    except Exception as e:
        log.warning(f"finalize_rvol_baseline failed (non-fatal): {e}")


def _momentum_log_key(date_str: str) -> str:
    return f"intraday/{date_str}/momentum_log.jsonl"


def _log_momentum_reading(s3, date_str: str, entry: dict) -> None:
    """Append one momentum reading to a rolling per-day JSONL log in R2 --
    a public, queryable historical record of the display signal, independent
    of the private crassus trading bot's own decision ledger (which is a
    local file wherever the runner executes, never published -- see
    docs/plans/2026-07-momentum-indicator.md). Read-modify-write is fine at
    this volume: at most one line per SNAPSHOT_SECS-second cycle, capped at
    roughly 390 short lines/day. Best-effort -- a failure here must never
    interrupt the snapshot it's piggybacking on.
    """
    try:
        key = _momentum_log_key(date_str)
        try:
            existing = s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read().decode()
        except Exception:
            existing = ""
        s3.put_object(
            Bucket=R2_BUCKET, Key=key,
            Body=(existing + json.dumps(entry) + "\n").encode(),
            ContentType="application/x-ndjson",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.warning(f"momentum_log.jsonl append failed (non-fatal): {e}")


def _compute_underlying_market(s3, qqq: dict, underlying: float | None, spot_ts_str: str | None,
                                ts_et: datetime, ts_utc: datetime, today: date) -> dict:
    """VWAP/RVOL for the `underlying_market` block of `intraday/latest.json`.

    Approximation, not tick-accurate VWAP: weights the snapshot's own spot
    price by the volume delta since the last snapshot, not by each
    individual trade's own price (see market_signals.py's module docstring).

    `spot_ts_str` must already be paired with `underlying` by the caller
    (see take_snapshot's spot-resolution block) -- this function does not
    re-derive it from `qqq`, so a value that fell back to a non-DXLink source
    can't end up stamped with an unrelated DXLink timestamp.
    """
    global _vwap_state

    _vwap_state = ms.reset_if_new_session(_vwap_state, today)

    raw_day_volume = qqq.get("volume")
    spot_observed_at = datetime.fromisoformat(spot_ts_str) if spot_ts_str else None

    _vwap_state = ms.accumulate_vwap(
        _vwap_state, price=underlying, raw_day_volume=raw_day_volume, observed_at=spot_observed_at,
    )
    _persist_vwap_state(s3, today.strftime("%Y%m%d"))

    bucket = ms.bucket_label(ts_et, RVOL_BUCKET_MINUTES)
    if raw_day_volume is not None:
        _rvol_today[bucket] = raw_day_volume

    baseline_samples = _rvol_baseline.get("buckets", {}).get(bucket, {}).get("samples", [])
    rvol = ms.compute_rvol(raw_day_volume, baseline_samples, min_days_required=RVOL_MIN_DAYS_REQUIRED)

    price_vs_vwap_abs = price_vs_vwap_pct = None
    if underlying is not None and _vwap_state.vwap:
        price_vs_vwap_abs = round(underlying - _vwap_state.vwap, 4)
        price_vs_vwap_pct = round((underlying - _vwap_state.vwap) / _vwap_state.vwap * 100, 4)

    freshness = ms.classify_freshness(
        inside_session_window=True,  # take_snapshot only ever runs inside the session window
        spot_ts=spot_observed_at, now=ts_utc, stale_after_s=STALE_FEED_SECS,
    )

    session_start, _ = _session_bounds(ts_et)
    vwap_partial_session = ms.is_partial_session(_vwap_state, session_start)

    momentum = _compute_and_log_momentum(s3, underlying, spot_observed_at, ts_utc, today)

    return {
        "symbol": TICKER,
        "spot": underlying,
        "spot_ts": spot_ts_str,
        "vwap": _vwap_state.vwap,
        "vwap_ts": _vwap_state.vwap_ts,
        "vwap_session_date": _vwap_state.session_date.isoformat() if _vwap_state.session_date else None,
        "vwap_session_started_at": _vwap_state.session_started_at.isoformat() if _vwap_state.session_started_at else None,
        "vwap_partial_session": vwap_partial_session,
        "price_vs_vwap_abs": price_vs_vwap_abs,
        "price_vs_vwap_pct": price_vs_vwap_pct,
        "session_volume": raw_day_volume,
        "session_volume_ts": spot_ts_str,
        "rvol": {
            "status": rvol.status,
            "multiple": rvol.multiple,
            "bucket_label": bucket,
            "baseline_volume": rvol.baseline_volume,
            "baseline_days_used": rvol.baseline_days_used,
            "baseline_lookback_days": _rvol_baseline.get("lookback_days", RVOL_LOOKBACK_DAYS),
            "baseline_updated_through": _rvol_baseline.get("updated_through"),
        },
        "momentum": momentum,
        "source": "dxlink",
        "freshness": freshness,
    }


def _compute_and_log_momentum(
    s3, underlying: float | None, spot_observed_at: datetime | None, ts_utc: datetime, today: date,
) -> dict:
    """Display/log-only time-series momentum -- see market_signals.py's
    module docstring for why this is a separate computation from
    momentum_qqq's own live trading signal, not a shared one.
    """
    global _momentum_history

    if underlying is not None and spot_observed_at is not None:
        _momentum_history.append(ms.SpotPoint(observed_at=spot_observed_at, price=underlying))
    _momentum_history = ms.prune_spot_history(_momentum_history, ts_utc, MOMENTUM_RETAIN_MINUTES)

    reading = ms.compute_time_series_momentum(
        _momentum_history, ts_utc,
        lookback_minutes=MOMENTUM_LOOKBACK_MINUTES,
        max_anchor_overshoot_minutes=MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN,
        neutral_band_pct=MOMENTUM_NEUTRAL_BAND_PCT,
    )

    log.info(
        f"momentum: status={reading.status} return_pct={reading.return_pct} "
        f"direction={reading.direction} sample_count={reading.sample_count}"
    )
    _log_momentum_reading(s3, today.strftime("%Y%m%d"), {
        "ts": ts_utc.isoformat(),
        "status": reading.status,
        "return_pct": reading.return_pct,
        "lookback_minutes": reading.lookback_minutes,
        "anchor_age_minutes": reading.anchor_age_minutes,
        "sample_count": reading.sample_count,
        "direction": reading.direction,
    })

    return {
        "status": reading.status,
        "return_pct": reading.return_pct,
        "lookback_minutes": reading.lookback_minutes,
        "anchor_age_minutes": reading.anchor_age_minutes,
        "sample_count": reading.sample_count,
        "direction": reading.direction,
    }


def _resolve_underlying_spot(
    qqq: dict, last_spot_price: float | None, last_spot_ts: str | None,
) -> tuple[float | None, str | None]:
    """Resolve the current spot price and its paired observation timestamp
    together, branch for branch, so the two always describe the same
    observation. Previously `underlying` could fall through to the
    yfinance-backed `_last_spot` fallback while its timestamp was derived
    independently from `qqq`'s DXLink state -- pairing a feed-derived
    timestamp with a value that didn't actually come from the feed.
    `last_spot_ts` is `None` whenever `last_spot_price` came from yfinance
    (see `push_prices()`), which has no trustworthy provider event
    timestamp -- that correctly makes this fallback classify as "stale",
    never "live".
    """
    bid, ask = qqq.get("bid"), qqq.get("ask")
    if bid and ask:
        return round((bid + ask) / 2, 2), max([v for v in (qqq.get("bid_ts"), qqq.get("ask_ts")) if v], default=None)
    if qqq.get("last"):
        return qqq.get("last"), qqq.get("last_ts")
    if last_spot_price is not None:
        return round(last_spot_price, 2), last_spot_ts
    return None, None


def take_snapshot(s3, feed: DXLinkFeed, strikes: list[dict],
                  exp_date: str, tier: str, today: date,
                  counters: Counters, tracker: SnapshotTracker):
    state  = feed.get_state()
    ts_et  = datetime.now(ET)
    ts_utc = datetime.now(timezone.utc)

    qqq = state.get(TICKER, {})
    underlying, spot_ts_str = _resolve_underlying_spot(qqq, _last_spot[0], _last_spot[1])
    atm = round(underlying) if underlying else None

    underlying_market = _compute_underlying_market(s3, qqq, underlying, spot_ts_str, ts_et, ts_utc, today)

    rows = []
    for s in strikes:
        strike = s["strike"]
        if atm is not None and abs(strike - atm) > STRIKE_WINDOW:
            continue
        for option_type, sym_key, occ_key in (
            ("call", "call_sym", "call_occ"),
            ("put",  "put_sym",  "put_occ"),
        ):
            sym  = s[sym_key]
            data = state.get(sym, {})
            b    = data.get("bid")
            a    = data.get("ask")
            mid  = round((b + a) / 2, 4) if b is not None and a is not None else None
            vol  = data.get("volume", 0) or 0
            vol_delta = max(0, vol - _prev_vol.get(sym, vol))
            _prev_vol[sym] = vol
            price_cols = {
                lbl.replace("/", "_"): remembered.get("price")
                for lbl, remembered in _last_prices.items()
            }
            rows.append({
                "TradeDate":       today.isoformat(),
                "Expiration":      exp_date,
                "Strike":          strike,
                "Type":            option_type,
                "OptionSymbol":    s[occ_key],
                "DTE":             0,
                "OpenInterest":    data.get("oi", 0) or 0,
                "Volume":          vol,
                "VolDelta":        vol_delta,
                "Bid":             b,
                "Mid":             mid,
                "Ask":             a,
                "Last":            data.get("last"),
                "IV":              data.get("volatility"),
                "Delta":           data.get("delta"),
                "Gamma":           data.get("gamma"),
                "Theta":           data.get("theta"),
                "Vega":            data.get("vega"),
                "UnderlyingPrice": underlying,
                **price_cols,
            })

    if not rows:
        log.warning("snapshot empty -- state not populated yet")
        return

    bid_count = sum(1 for r in rows if r.get("Bid") is not None)

    df      = pd.DataFrame(rows)
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)

    date_str = today.strftime("%Y%m%d")
    time_str = ts_et.strftime("%H%M%S%f")   # microsecond precision prevents overwrite on rapid restart
    csv_key  = f"intraday/{date_str}/snapshot_{time_str}.csv"

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key=csv_key,
            Body=csv_buf.getvalue().encode(),
            ContentType="text/csv",
        )
        counters.inc_csv()
        log.info(f"-> {csv_key}  ({len(rows)} rows,  underlying={underlying},  bids={bid_count})")
    except Exception as e:
        log.error(f"CSV upload failed: {e}")
        counters.inc_failure()
        raise

    global _first_snapshot_written
    if not _first_snapshot_written:
        first_key = f"intraday/{date_str}/first.csv"
        try:
            s3.put_object(
                Bucket=R2_BUCKET, Key=first_key,
                Body=csv_buf.getvalue().encode(),
                ContentType="text/csv",
            )
            _first_snapshot_written = True
            log.info(f"-> {first_key}  (session-open snapshot, mirrors {csv_key})")
        except Exception as e:
            log.warning(f"first.csv upload failed (non-fatal): {e}")

    if bid_count == 0:
        log.warning("latest.json NOT updated -- no option data (DXLink feed down)")
        return

    payload = {
        "timestamp":        ts_utc.isoformat(),
        "snapshot_time":    ts_et.strftime("%H:%M ET"),
        "date":             today.isoformat(),
        "expiration":       exp_date,
        "tier":             tier,
        "underlying_price": underlying,
        "snapshot_key":     csv_key,
        "rows":             rows,
        "underlying_market": underlying_market,
    }

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key="intraday/latest.json",
            Body=json.dumps(payload, default=str).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        counters.inc_snapshot(ts_utc.isoformat())
        tracker.record()
        log.info("-> intraday/latest.json updated")
    except Exception as e:
        log.error(f"latest.json upload failed: {e}")
        counters.inc_failure()
        raise


# -- session lifecycle --------------------------------------------------------

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


def _next_session_start(et: datetime) -> datetime:
    start, stop = _session_bounds(et)
    if et < stop:
        return start
    next_day = et.date() + timedelta(days=1)
    return ET.localize(datetime(
        next_day.year, next_day.month, next_day.day,
        PREMARKET_HOUR, 0, 0,
    ))


def wait_for_premarket():
    """Block until inside the valid session window (06:00-16:15 ET).
    If called post-close, sleeps until next day to prevent Railway restart-loops."""
    while True:
        et = datetime.now(ET)
        if _inside_session_window(et):
            return
        base = _next_session_start(et)
        delay = (base - et).total_seconds()
        log.info(
            f"outside trading window -- sleeping "
            f"{int(delay // 3600)}h {int((delay % 3600) // 60)}m "
            f"until {base.strftime('%Y-%m-%d %H:%M ET')}"
        )
        time.sleep(min(delay, 3600))


def _run_session(login: str, quote_registry: LiveQuoteRegistry):
    run_id        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    process_start = datetime.now(timezone.utc)
    log.info(f"session start  run_id={run_id}")

    s3             = make_s3()
    classification = _classify_startup(s3, process_start)
    log.info(f"startup classification: {classification}")

    today   = date.today()
    restore_state(s3, today)

    global _rvol_baseline, _rvol_today
    _rvol_baseline = load_rvol_baseline(s3)
    _rvol_today = {}

    auth    = tasty_auth(login, s3)
    tier    = classify_tier(today)
    log.info(f"session date={today}  tier={tier}")

    strikes, exp_date = load_chain(auth["session_token"], today)

    option_syms = []
    contracts = {}
    for s in strikes:
        option_syms.append(s["call_sym"])
        option_syms.append(s["put_sym"])
        if s["call_occ"]:
            contracts[s["call_occ"].replace(" ", "")] = {
                "streamer_symbol": s["call_sym"], "strike": s["strike"],
                "type": "call", "exp": exp_date,
            }
        if s["put_occ"]:
            contracts[s["put_occ"].replace(" ", "")] = {
                "streamer_symbol": s["put_sym"], "strike": s["strike"],
                "type": "put", "exp": exp_date,
            }

    price_syms = list(PRICE_TICKERS.values())
    log.info(f"subscribing to {len(option_syms)} option symbols + {len(price_syms)} price tickers")
    for label, sym in PRICE_TICKERS.items():
        log.info(f"  price ticker  {label:<10} -> {sym}")

    feed = DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions(option_syms, price_syms)
    quote_registry.set_session(feed, contracts)
    feed.start()

    if not feed.wait_ready(timeout=30):
        log.warning("DXLink channel not open after 30s -- proceeding anyway")

    log.info("waiting for first option data event (up to 15s)...")
    if feed.wait_first_data(timeout=15):
        log.info("option data flowing -- proceeding to snapshot")
    else:
        log.warning("no feed data within 15s -- proceeding anyway")

    _log_ticker_health(feed)

    counters = Counters()
    tracker  = SnapshotTracker()

    prices_thread = threading.Thread(
        target=prices_loop, args=(s3, feed, counters, quote_registry), daemon=True)
    prices_thread.start()
    log.info(f"prices thread started (every {PRICES_SECS}s)")

    health_thread = threading.Thread(
        target=health_loop,
        args=(s3, feed, counters, tracker, run_id, process_start, classification, today),
        daemon=True,
    )
    health_thread.start()
    log.info(f"health thread started (every {HEALTH_SECS}s)")

    log.info(f"snapshot loop started (every {SNAPSHOT_SECS}s, stop {STOP_HOUR:02d}:{STOP_MIN:02d} ET)")

    while not past_stop():
        if feed.needs_reauth():
            log.warning("DXLink auth failed 3+ times -- re-fetching streamer token")
            try:
                new_auth = tasty_auth(login, s3)
                feed.update_token(new_auth["streamer_token"])
                log.info("streamer token refreshed")
            except Exception as e:
                log.error(f"token refresh failed: {e}")
        feed.restart_if_dead()

        tracker.check_missed()
        try:
            take_snapshot(s3, feed, strikes, exp_date, tier, today, counters, tracker)
        except Exception as e:
            log.error(f"snapshot error: {e}")
        time.sleep(SNAPSHOT_SECS)

    trk = tracker.get()
    ctr = counters.get()
    log.info(
        f"session complete  run_id={run_id}  "
        f"snapshots={trk['snapshot_sequence']}  "
        f"missed={trk['missed_snapshot_count']}  "
        f"failures={ctr['failures']}"
    )

    # Write final health.json with past_stop=True so next startup classifies as clean_start
    try:
        push_health(s3, feed, counters, tracker, run_id, process_start, classification, today)
    except Exception:
        pass

    # Fold today's RVOL bucket readings into the cross-day baseline. Done here
    # (once, at confirmed session end) rather than incrementally, so a
    # mid-session crash can never leave the baseline holding a partial day.
    finalize_rvol_baseline(s3, today)

    feed.stop()
    quote_registry.clear_session()


def main():
    quote_registry = LiveQuoteRegistry()
    start_live_quote_server(quote_registry)
    login = os.environ["TASTY_LOGIN"]

    # Independent of the QQQ market session (unlike prices_loop/health_loop,
    # started fresh inside every _run_session): EIA STEO has nothing to do
    # with market hours, so this runs continuously off its own s3 client for
    # the life of the process. No-ops immediately if EIA_API_KEY is unset.
    steo_thread = threading.Thread(target=eia_steo_loop, args=(make_s3(),), daemon=True)
    steo_thread.start()

    while True:
        wait_for_premarket()
        try:
            _run_session(login, quote_registry)
        except Exception as e:
            quote_registry.clear_session()
            log.error(f"session failed: {e}", exc_info=True)
            time.sleep(60)
        # After session end or crash, wait_for_premarket() handles sleeping until
        # the next window -- process never exits, Railway never restart-loops


if __name__ == "__main__":
    main()
