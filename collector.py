#!/usr/bin/env python3
"""
collector.py -- QQQ 0DTE live chain snapshot service.

Authenticates with tastytrade, subscribes to the QQQ 0DTE option chain via
DXLink websocket, and uploads snapshots to R2 every 11 minutes.

R2 output:
  intraday/YYYYMMDD/snapshot_HHMMSSffffff.csv  -- archived snapshots (microsecond key)
  intraday/latest.json                   -- live feed for the web viewer
  intraday/prices.json                   -- macro price strip (every 30s)
  intraday/health.json                   -- lifecycle telemetry (every 15s)

Environment variables (set in Railway dashboard):
  TASTY_LOGIN            tastytrade username
  TASTY_PASSWORD         tastytrade password
  R2_ACCOUNT_ID          Cloudflare account ID
  R2_ACCESS_KEY_ID       R2 access key
  R2_SECRET_ACCESS_KEY   R2 secret key
  R2_BUCKET_NAME         bucket name (default: pub-4d5c916b8cb74ffb8c0abd7dfadb02cf)
"""

import io
import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import boto3
import pandas as pd
import pytz
import requests
import websocket
import yfinance as yf

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
SNAPSHOT_SECS   = 11 * 60
PRICES_SECS     = 30
HEALTH_SECS     = 15
PREMARKET_HOUR  = 6
STOP_HOUR       = 16
STOP_MIN        = 15
STALE_FEED_SECS = 120   # warn if no feed event for this many seconds
R2_BUCKET       = os.environ.get("R2_BUCKET_NAME", "pub-4d5c916b8cb74ffb8c0abd7dfadb02cf")

PRICE_TICKERS: dict[str, str] = {
    "QQQ":     "QQQ",
    "USO":     "USO",
    "VIX":     "$VIX.X",
    "SMH":     "SMH",
    "IGV":     "IGV",
    "JPY/USD": "/6J:XCME",    # CME yen futures, USD-per-JPY; inverted for display
    "BTC/USD": "BTC/USD:CXERX",
    "META":    "META",
    "GOOGL":   "GOOGL",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
}

# Yahoo Finance symbols for the same tickers (fallback when DXLink has no data)
YF_SYMBOL_MAP: dict[str, str] = {
    "QQQ":     "QQQ",
    "USO":     "USO",
    "VIX":     "^VIX",       # pre-market: None expected (CBOE only calculates at open)
    "SMH":     "SMH",
    "IGV":     "IGV",
    "JPY/USD": "JPYUSD=X",
    "BTC/USD": "BTC-USD",
    "META":    "META",
    "GOOGL":   "GOOGL",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
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

def _dxlink_symbol(occ_symbol: str) -> str:
    return "." + occ_symbol.replace(" ", "")


def _build_symbol(strike: float, exp_date: str, option_type: str) -> str:
    yy, mm, dd = exp_date[2:4], exp_date[5:7], exp_date[8:10]
    side = "C" if option_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f".{TICKER}{yy}{mm}{dd}{side}{strike_int:08d}"


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
        # lifecycle telemetry
        self._connected           = False
        self._authorized          = False
        self._channel_open        = False
        self._reconnect_count     = 0
        self._first_connect_seen  = False
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

    def wait_ready(self, timeout: float = 60.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def start(self):
        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        t = threading.Thread(target=self._ws.run_forever, kwargs={"reconnect": 5}, daemon=True)
        t.start()
        log.info("DXLink feed thread started")

    def stop(self):
        if self._ws:
            self._ws.close()

    def _send(self, msg: dict):
        if self._ws:
            self._ws.send(json.dumps(msg))

    def _on_open(self, ws):
        with self._lock:
            self._connected = True
            self._subscribed = False  # reset so FEED_CONFIG re-subscribes after reconnect
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
                log.info("DXLink authorized -- requesting channel")
                self._send({
                    "type": "CHANNEL_REQUEST", "channel": 1,
                    "service": "FEED",
                    "parameters": {"contract": "AUTO"},
                })
            else:
                log.error(f"DXLink auth failed: {msg}")

        elif mtype == "CHANNEL_OPENED":
            with self._lock:
                self._channel_open = True
            log.info("DXLink channel 1 open -- sending FEED_SETUP")
            self._send({
                "type": "FEED_SETUP", "channel": 1,
                "acceptDataFormat": "FULL",
                "acceptEventFields": {
                    "Quote":    ["eventSymbol", "bidPrice", "askPrice"],
                    "Summary":  ["eventSymbol", "openInterest", "prevDayClosePrice", "dayOpenPrice"],
                    "Trade":    ["eventSymbol", "dayVolume", "price"],
                    "TradeETH": ["eventSymbol", "price"],
                    "Greeks":   ["eventSymbol", "volatility", "delta", "gamma", "theta", "vega"],
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
            self._ingest(msg.get("data", []))

        elif mtype == "KEEPALIVE":
            self._send({"type": "KEEPALIVE", "channel": 0})

        elif mtype == "ERROR":
            log.error(f"DXLink server error: {msg}")

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
                    if event.get("bidPrice") is not None:
                        s["bid"] = event["bidPrice"]
                    if event.get("askPrice") is not None:
                        s["ask"] = event["askPrice"]
                elif et == "Summary":
                    if event.get("openInterest") is not None:
                        s["oi"] = int(event["openInterest"])
                    if event.get("prevDayClosePrice") is not None:
                        s["prev_close"] = event["prevDayClosePrice"]
                    if event.get("dayOpenPrice") is not None:
                        s["day_open"] = event["dayOpenPrice"]
                elif et == "Trade":
                    if event.get("dayVolume") is not None:
                        s["volume"] = int(event["dayVolume"])
                    if event.get("price") is not None:
                        s["last"] = event["price"]
                elif et == "TradeETH":
                    # Extended-hours last trade price; only overwrite if no regular last
                    if event.get("price") is not None and s.get("last") is None:
                        s["last"] = event["price"]
                elif et == "Greeks":
                    for field in ("volatility", "delta", "gamma", "theta", "vega"):
                        if event.get(field) is not None:
                            s[field] = event[field]

    def _on_error(self, ws, error):
        with self._lock:
            self._last_error = str(error)
        log.error(f"DXLink error: {error}")

    def _on_close(self, ws, code, msg):
        with self._lock:
            self._connected    = False
            self._authorized   = False
            self._channel_open = False
            self._last_close_code = code
        log.warning(f"DXLink closed: code={code}")
        self._ready.clear()


# -- tier classification (mirrors oi_viewer.py) -------------------------------

def _load_calendar():
    try:
        import pandas_market_calendars as mcal
        nyse  = mcal.get_calendar("NYSE")
        start = date.today()
        end   = start + timedelta(days=90)
        return {d.date() for d in nyse.valid_days(start_date=start.isoformat(),
                                                    end_date=end.isoformat())}
    except Exception:
        return set()


def classify_tier(today: date) -> str:
    import calendar as _cal

    valid = _load_calendar()

    def prior_td(d):
        while d not in valid:
            d -= timedelta(days=1)
        return d

    def next_td(d):
        d += timedelta(days=1)
        while d not in valid:
            d += timedelta(days=1)
        return d

    def nominal_fri(d):
        return d + timedelta(days=(4 - d.weekday()) % 7)

    eow    = prior_td(nominal_fri(today))
    plus1d = next_td(today)
    if plus1d != eow:
        return "0DTE_Regular"

    count, opex = 0, None
    for day in range(1, _cal.monthrange(plus1d.year, plus1d.month)[1] + 1):
        if date(plus1d.year, plus1d.month, day).weekday() == 4:
            count += 1
            if count == 3:
                opex = prior_td(date(plus1d.year, plus1d.month, day))
                break
    return "0DTE_Monthly" if plus1d == opex else "0DTE_Weekly"


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


# -- prices.json upload (every 30s) ------------------------------------------

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


def push_prices(s3, feed: DXLinkFeed, counters: Counters):
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
        }

    # yfinance fallback for any tickers DXLink didn't populate
    yf_missing = [lbl for lbl, d in prices.items() if d["price"] is None]
    if yf_missing:
        yf_data = fetch_yf_prices()
        filled = []
        for lbl in yf_missing:
            yf_price = yf_data.get(lbl)
            if yf_price is not None:
                prices[lbl]["price"] = yf_price
                filled.append(f"{lbl}={yf_price}")
        if filled:
            log.info(f"prices -- yfinance filled: {', '.join(filled)}")

    dead = [label for label, d in prices.items() if d["price"] is None]
    if dead:
        log.warning(f"prices.json -- no data for: {', '.join(dead)}")

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


def prices_loop(s3, feed: DXLinkFeed, counters: Counters):
    while not past_stop():
        try:
            push_prices(s3, feed, counters)
        except Exception as e:
            log.error(f"prices.json error: {e}")
        time.sleep(PRICES_SECS)
    log.info("prices loop stopped")


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


def take_snapshot(s3, feed: DXLinkFeed, strikes: list[dict],
                  exp_date: str, tier: str, today: date,
                  counters: Counters, tracker: SnapshotTracker):
    state  = feed.get_state()
    ts_et  = datetime.now(ET)
    ts_utc = datetime.now(timezone.utc)

    qqq = state.get(TICKER, {})
    bid, ask = qqq.get("bid"), qqq.get("ask")
    underlying = round((bid + ask) / 2, 2) if bid and ask else (qqq.get("last") or None)
    atm = round(underlying) if underlying else None

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
            rows.append({
                "TradeDate":       today.isoformat(),
                "Expiration":      exp_date,
                "Strike":          strike,
                "Type":            option_type,
                "OptionSymbol":    s[occ_key],
                "DTE":             0,
                "OpenInterest":    data.get("oi", 0) or 0,
                "Volume":          data.get("volume", 0) or 0,
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
            })

    if not rows:
        log.warning("snapshot empty -- state not populated yet")
        return

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
        log.info(f"-> {csv_key}  ({len(rows)} rows,  underlying={underlying})")
    except Exception as e:
        log.error(f"CSV upload failed: {e}")
        counters.inc_failure()
        raise

    payload = {
        "timestamp":        ts_utc.isoformat(),
        "snapshot_time":    ts_et.strftime("%H:%M ET"),
        "date":             today.isoformat(),
        "expiration":       exp_date,
        "tier":             tier,
        "underlying_price": underlying,
        "snapshot_key":     csv_key,
        "rows":             rows,
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


def _run_session(login: str):
    run_id        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    process_start = datetime.now(timezone.utc)
    log.info(f"session start  run_id={run_id}")

    s3             = make_s3()
    classification = _classify_startup(s3, process_start)
    log.info(f"startup classification: {classification}")

    auth    = tasty_auth(login, s3)
    today   = date.today()
    tier    = classify_tier(today)
    log.info(f"session date={today}  tier={tier}")

    strikes, exp_date = load_chain(auth["session_token"], today)

    option_syms = []
    for s in strikes:
        option_syms.append(s["call_sym"])
        option_syms.append(s["put_sym"])

    price_syms = list(PRICE_TICKERS.values())
    log.info(f"subscribing to {len(option_syms)} option symbols + {len(price_syms)} price tickers")
    for label, sym in PRICE_TICKERS.items():
        log.info(f"  price ticker  {label:<10} -> {sym}")

    feed = DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions(option_syms, price_syms)
    feed.start()

    if not feed.wait_ready(timeout=30):
        log.warning("DXLink channel not open after 30s -- proceeding anyway")

    log.info("waiting for initial data flush (up to 90s)...")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(5)
        qqq_d = feed.get_state().get(TICKER, {})
        if qqq_d.get("bid") is not None or qqq_d.get("last") is not None:
            elapsed = 90 - max(0, deadline - time.monotonic())
            log.info(f"QQQ price data received after {elapsed:.0f}s -- proceeding")
            break
    else:
        log.warning("QQQ price data not received within 90s -- proceeding anyway")

    _log_ticker_health(feed)

    counters = Counters()
    tracker  = SnapshotTracker()

    prices_thread = threading.Thread(target=prices_loop, args=(s3, feed, counters), daemon=True)
    prices_thread.start()
    log.info(f"prices thread started (every {PRICES_SECS}s)")

    health_thread = threading.Thread(
        target=health_loop,
        args=(s3, feed, counters, tracker, run_id, process_start, classification, today),
        daemon=True,
    )
    health_thread.start()
    log.info(f"health thread started (every {HEALTH_SECS}s)")

    log.info(f"snapshot loop started (every {SNAPSHOT_SECS // 60}m, stop {STOP_HOUR:02d}:{STOP_MIN:02d} ET)")

    while not past_stop():
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

    feed.stop()


def main():
    login = os.environ["TASTY_LOGIN"]

    while True:
        wait_for_premarket()
        try:
            _run_session(login)
        except Exception as e:
            log.error(f"session failed: {e}", exc_info=True)
            time.sleep(60)
        # After session end or crash, wait_for_premarket() handles sleeping until
        # the next window -- process never exits, Railway never restart-loops


if __name__ == "__main__":
    main()
