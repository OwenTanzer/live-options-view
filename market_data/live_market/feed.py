"""DXLink websocket feed: the single live market-data subscription.

WHY this module first: DXLinkFeed is the largest single unit in the
collector (340 lines) and a clean leaf -- it talks to the tastytrade
DXLink websocket, keeps per-symbol quote state, and exposes it to the
snapshot/price/health loops. It reads no other collector subsystem, only
the shared log, so it moves out with no import churn elsewhere.
"""

import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import websocket

from .config import log

# -- DXLink websocket feed ----------------------------------------------------

class DXLinkFeed:
    _DXLINK_VERSION = "0.1-js/1.0.0"
    # Channel 1 carries the existing quote/trade/greeks events. Time and sale
    # gets its own channel and its own FEED_SETUP so a field or event-type
    # rejection there cannot disturb the accepted market feed. DXLink requires
    # *odd* numbers for service channels, so the second feed channel is 3.
    _TIMESALE_CHANNEL = 3
    # TimeAndSale flags bit layout (dxFeed): type in bits 0-1 (NEW=0,
    # CORRECTION=1, CANCEL=2), a valid-intraday-tick bit, an extended-hours bit
    # and a spread-leg bit.
    _TS_FLAG_TYPE_MASK = 0x3
    _TS_FLAG_VALID_TICK = 1 << 2
    _TS_FLAG_ETH = 1 << 3
    _TS_FLAG_SPREAD_LEG = 1 << 4

    def __init__(self, url: str, token: str):
        self._url   = url
        self._token = token
        self._state: dict[str, dict] = {}
        self._lock  = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ready = threading.Event()
        self._subs: list[dict] = []
        self._pending_option_subs: list[dict] = []
        self._subscription_error: Optional[str] = None
        self._subscribed          = False
        self._timesale_symbols: list[str] = []
        self._ts_subscribed       = False
        self._ts_error: Optional[str] = None
        self._ts_counts = {"received": 0, "accepted": 0, "corrected": 0,
                           "cancelled": 0, "out_of_order": 0, "invalid": 0,
                           "unmatched": 0, "invalidated": 0, "duplicate": 0}
        self._data_logged         = False
        # lifecycle telemetry
        self._connected           = False
        self._authorized          = False
        self._channel_open        = False
        self._reconnect_count     = 0
        self._first_connect_seen  = False
        self._auth_fail_count     = 0
        self._stopped             = False
        self._last_error: Optional[str]  = None
        self._last_close_code: Optional[int] = None
        self._last_event_time: Optional[datetime] = None
        # Per-event-type evidence. The November 9 repair could not tell whether
        # extended trades were absent, unsubscribed or unpaid for; counting what
        # the provider actually sends makes that answer observable.
        self._event_counts: dict[str, int] = {}
        self._quote_time_counts = {"with_provider_time": 0, "without_provider_time": 0}
        # Observed field names per event type: the deployment's answer to "what
        # does this feed actually send", instead of an inference from absence.
        self._event_fields: dict[str, list[str]] = {}

    def set_subscriptions(self, option_symbols: list[str], price_symbols: list[str],
                          timesale_symbols: list[str] | None = None):
        self._subs = []
        for sym in option_symbols:
            for event_type in ("Quote", "Summary", "Trade", "Greeks"):
                self._subs.append({"type": event_type, "symbol": sym})
        for sym in price_symbols:
            for event_type in ("Quote", "Trade", "TradeETH", "Summary"):
                self._subs.append({"type": event_type, "symbol": sym})
        # Held separately: sent on its own channel with its own FEED_SETUP.
        self._timesale_symbols = [sym for sym in (timesale_symbols or []) if sym]

    def add_option_subscriptions(self, option_symbols: list[str]):
        """Add the fixed SPY window after a fresh spot arrives; retain it on reconnect."""
        with self._lock:
            known = {(entry["type"], entry["symbol"]) for entry in self._subs}
            additions = []
            for sym in option_symbols:
                for event_type in ("Quote", "Summary", "Trade", "Greeks"):
                    key = (event_type, sym)
                    if key not in known:
                        additions.append({"type": event_type, "symbol": sym})
                        known.add(key)
            self._subs.extend(additions)
            self._pending_option_subs.extend(additions)
            if self._subscribed:
                self._flush_pending_option_subscriptions_locked()
        return len(additions)

    def _flush_pending_option_subscriptions_locked(self):
        while self._pending_option_subs:
            batch = self._pending_option_subs[:200]
            try:
                self._send({"type": "FEED_SUBSCRIPTION", "channel": 1,
                            "reset": False, "add": batch})
            except Exception as exc:
                self._subscription_error = str(exc)
                log.warning(f"SPY subscription delivery interrupted; {len(self._pending_option_subs)} "
                            f"event/symbol pairs pending retry: {exc}")
                return
            del self._pending_option_subs[:len(batch)]
        self._subscription_error = None

    def retry_pending_option_subscriptions(self):
        with self._lock:
            if self._subscribed:
                self._flush_pending_option_subscriptions_locked()

    def get_subscription_delivery(self) -> dict:
        with self._lock:
            return {"pending_event_pairs": len(self._pending_option_subs),
                    "last_error": self._subscription_error}

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
                "event_counts": dict(self._event_counts),
                "quote_time_counts": dict(self._quote_time_counts),
                "event_fields": {name: list(fields)
                                 for name, fields in self._event_fields.items()},
                "timesale": {
                    "requested": bool(self._timesale_symbols),
                    "subscribed": self._ts_subscribed,
                    "error": self._ts_error,
                    "counts": dict(self._ts_counts),
                },
            }

    # Three rejected AUTHs on fresh connections mean the streamer token is bad.
    _AUTH_FAIL_THRESHOLD = 3

    def needs_reauth(self) -> bool:
        with self._lock:
            return self._auth_fail_count >= self._AUTH_FAIL_THRESHOLD

    def _record_auth_rejection(self, message: str) -> None:
        """Count a DXLink AUTH rejection (an ERROR frame on channel 0).

        websocket-client's ``run_forever(reconnect=...)`` redials on a dropped
        or server-closed connection WITHOUT calling ``on_close`` or
        ``on_error``. Counting auth failures only in ``_on_close`` therefore
        never fired in production: on November 13 the price lane's streamer
        token expired at 00:23:29Z and the lane redialled every ~5 s with the
        same expired token (11,579 reconnects, ``needs_reauth()`` False) while
        publishing the last pre-expiry prices. The provider's own rejection is
        the reliable signal, so it is counted here. An explicit expiry needs a
        new token immediately; a bare rejection still waits for the threshold.
        """
        expired = "expired" in message.lower()
        with self._lock:
            self._authorized = False
            self._channel_open = False
            self._last_error = f"dxlink_auth_rejected: {message}"[:200]
            if expired:
                self._auth_fail_count = max(self._auth_fail_count + 1,
                                            self._AUTH_FAIL_THRESHOLD)
            else:
                self._auth_fail_count += 1
            count = self._auth_fail_count
        log.error(f"DXLink AUTH rejected (count {count}"
                  f"{', token expired' if expired else ''}): {message}")

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
        with self._lock:
            self._stopped = True
        if self._ws:
            self._ws.close()

    def _send(self, msg: dict):
        if self._ws:
            self._ws.send(json.dumps(msg))

    def _on_open(self, ws):
        with self._lock:
            stopped = self._stopped
        if stopped:
            # A redial already scheduled by run_forever can land after stop();
            # never authenticate a connection nobody owns any more.
            try:
                ws.close()
            except Exception:
                pass
            return
        with self._lock:
            self._connected = True
            # A silent redial skips _on_close, so clear the previous connection's
            # auth/channel state here; health must not report a dead login.
            self._authorized = False
            self._channel_open = False
            self._subscribed  = False  # reset so FEED_CONFIG re-subscribes after reconnect
            self._ts_subscribed = False
            self._ts_error = None
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

    def _send_timesale_setup(self):
        """Channel-2 FEED_SETUP: trade prints with provider time and session flag."""
        if not self._timesale_symbols:
            return
        self._send({
            "type": "FEED_SETUP", "channel": self._TIMESALE_CHANNEL,
            "acceptDataFormat": "FULL",
            "acceptEventFields": {
                "TimeAndSale": ["eventType", "eventSymbol", "time", "sequence", "price",
                                "size", "tradeId", "flags", "extendedTradingHours",
                                "validTick", "spreadLeg", "type"],
            },
        })

    def _subscribe_timesale(self):
        with self._lock:
            if self._ts_subscribed:
                return
            self._ts_subscribed = True
            symbols = list(self._timesale_symbols)
        subscriptions = [{"type": "TimeAndSale", "symbol": sym} for sym in symbols]
        for i in range(0, len(subscriptions), 200):
            batch = subscriptions[i:i + 200]
            self._send({
                "type": "FEED_SUBSCRIPTION", "channel": self._TIMESALE_CHANNEL,
                "reset": i == 0, "add": batch,
            })
        log.info(f"subscribed to {len(subscriptions)} TimeAndSale symbols "
                 f"on channel {self._TIMESALE_CHANNEL}")

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
            channel = msg.get("channel")
            if channel == self._TIMESALE_CHANNEL:
                log.info(f"DXLink channel {channel} open -- sending TimeAndSale FEED_SETUP")
                self._send_timesale_setup()
                return
            with self._lock:
                self._channel_open = True
            log.info("DXLink channel 1 open -- sending FEED_SETUP")
            self._send({
                "type": "FEED_SETUP", "channel": 1,
                "acceptDataFormat": "FULL",
                "acceptEventFields": {
                    "Quote":    ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidTime", "askTime"],
                    "Summary":  ["eventType", "eventSymbol", "openInterest", "prevDayClosePrice", "dayOpenPrice", "prevDayId"],
                    "Trade":    ["eventType", "eventSymbol", "dayVolume", "price", "time"],
                    "TradeETH": ["eventType", "eventSymbol", "price", "time"],
                    "Greeks":   ["eventType", "eventSymbol", "volatility", "delta", "gamma", "theta", "vega"],
                },
            })
            if self._timesale_symbols:
                self._send({
                    "type": "CHANNEL_REQUEST", "channel": self._TIMESALE_CHANNEL,
                    "service": "FEED", "parameters": {"contract": "AUTO"},
                })

        elif mtype == "FEED_CONFIG":
            channel = msg.get("channel")
            if channel == self._TIMESALE_CHANNEL:
                self._subscribe_timesale()
                return
            # Server acknowledged FEED_SETUP. Subscribe once only — server
            # may send multiple FEED_CONFIGs (one per batch ack), so guard
            # with a flag to avoid repeated resets.
            with self._lock:
                if self._subscribed:
                    return
                self._subscribed = True
                subscriptions = list(self._subs)
                log.info("DXLink feed configured -- sending subscriptions")
                if subscriptions:
                    batch_size = 200
                    for i in range(0, len(subscriptions), batch_size):
                        batch = subscriptions[i:i + batch_size]
                        self._send({
                            "type": "FEED_SUBSCRIPTION", "channel": 1,
                            "reset": i == 0, "add": batch,
                        })
                    log.info(f"subscribed to {len(subscriptions)} event/symbol pairs ({batch_size}/batch)")
                self._pending_option_subs.clear()
                self._subscription_error = None
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
            if msg.get("channel", 0) == 0 and str(msg.get("error") or "").upper() == "UNAUTHORIZED":
                self._record_auth_rejection(str(msg.get("message") or "UNAUTHORIZED"))
            elif msg.get("channel") == self._TIMESALE_CHANNEL:
                with self._lock:
                    self._ts_error = str(msg.get("error") or msg.get("message")
                                         or "timesale_channel_error")[:200]

    @staticmethod
    def _to_int(val):
        """Convert val to int, returning None for NaN / non-numeric strings."""
        try:
            f = float(val)
            import math
            return int(f) if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_exact_int(val):
        """Exact integer identity: no float round-trip, no fractional ids.

        DXLink identity fields (index, sequence, tradeId) can exceed 2**53, where
        a float round-trip silently merges adjacent events. Integers and integer
        strings are preserved exactly; a float that is fractional or too large to
        be exact is rejected rather than rounded into another event's identity.
        """
        if isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            if not math.isfinite(val) or not val.is_integer():
                return None
            if abs(val) > 9_007_199_254_740_992:  # 2**53, exclusive
                return None
            return int(val)
        text = str(val).strip()
        if not text or not re.fullmatch(r"[+-]?\d+", text):
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(val):
        """Convert val to float, returning None for NaN / non-numeric strings."""
        try:
            import math
            f = float(val)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_time(value):
        """dxFeed time fields are Unix milliseconds, never collector receipt."""
        try:
            millis = float(value)
            if not math.isfinite(millis) or millis <= 0:
                return None
            return datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    @staticmethod
    def _day_id(value):
        """Summary day IDs are calendar days since 1970-01-01, not times."""
        try:
            days = float(value)
            if not math.isfinite(days) or not days.is_integer() or days <= 0:
                return None
            return (datetime(1970, 1, 1) + timedelta(days=int(days))).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _to_bool(value):
        """Tri-state boolean: True/False, or None when the field is absent/invalid."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            try:
                if float(value) == 1.0:
                    return True
                if float(value) == 0.0:
                    return False
            except (TypeError, ValueError):
                return None
            return None
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "y"):
            return True
        if text in ("false", "0", "no", "n"):
            return False
        return None

    @staticmethod
    def _print_key(*, index, observed, sequence, trade_id):
        """Provider identity for correction/cancellation matching.

        dxFeed's unique per-symbol index is time plus sequence, and `tradeId` is
        0 when unavailable. An event with no index, no sequence and no trade id
        has no identity, so a correction or cancellation for it cannot be
        matched to anything we track.
        """
        if index is not None:
            return f"i:{index}"
        if observed is not None and sequence is not None:
            return f"t:{observed}:{sequence}"
        if trade_id is not None:
            return f"id:{trade_id}"
        return None

    @staticmethod
    def _is_later(observed_dt, sequence, stored_dt, stored_sequence):
        """Strict (time, sequence) ordering for a NEW print.

        dxFeed orders time and sales by the unique index composed of time and
        sequence, so a later print in the same millisecond is still later. When
        either side lacks a sequence, an equal-time event cannot be proven later
        and is refused (fail closed) rather than guessed at.
        """
        if stored_dt is None:
            return True
        if observed_dt > stored_dt:
            return True
        if observed_dt < stored_dt:
            return False
        if sequence is None or stored_sequence is None:
            return False
        return sequence > stored_sequence

    def _ingest_time_and_sale(self, s, event):
        """One trade print with documented transaction semantics.

        Identity is dxFeed's unique per-symbol index (time plus sequence) or a
        non-zero tradeId; tradeId 0 means unavailable and never matches. A NEW
        print moves the price only forward. A CORRECTION applies only to the
        print it identifies, so an unrelated or older correction cannot rewind
        the latest. A CANCEL removes the print it identifies even when it
        carries no price. Non-valid ticks, spread legs and unknown transactional
        types are rejected; the receipt clock is never substituted.
        """
        self._ts_counts["received"] += 1
        flags = self._to_exact_int(event.get("flags"))
        if flags is not None and flags < 0:
            flags = None
        explicit_type = str(event.get("type") or "").strip().upper()
        if explicit_type:
            if explicit_type not in ("NEW", "CORRECTION", "CANCEL"):
                self._ts_counts["invalid"] += 1
                return
            kind = explicit_type
        elif flags is not None:
            kind = {0: "NEW", 1: "CORRECTION", 2: "CANCEL"}.get(
                flags & self._TS_FLAG_TYPE_MASK)
            if kind is None:
                self._ts_counts["invalid"] += 1
                return
        else:
            kind = "NEW"
        observed = self._event_time(event.get("time"))
        sequence = self._to_exact_int(event.get("sequence"))
        index = self._to_exact_int(event.get("index"))
        trade_id = self._to_exact_int(event.get("tradeId"))
        if trade_id is not None and trade_id <= 0:
            trade_id = None  # 0 means unavailable, not an identity
        price = self._to_float(event.get("price"))
        size = self._to_float(event.get("size"))
        extended = self._to_bool(event.get("extendedTradingHours"))
        if extended is None and flags is not None:
            extended = bool(flags & self._TS_FLAG_ETH)
        valid = self._to_bool(event.get("validTick"))
        if valid is None and flags is not None:
            valid = bool(flags & self._TS_FLAG_VALID_TICK)
        spread_leg = self._to_bool(event.get("spreadLeg"))
        if spread_leg is None and flags is not None:
            spread_leg = bool(flags & self._TS_FLAG_SPREAD_LEG)
        key = self._print_key(index=index, observed=observed,
                              sequence=sequence, trade_id=trade_id)
        stored_key = s.get("ts_key")
        stored_time = s.get("ts_time")
        stored_sequence = s.get("ts_sequence")

        if kind == "CANCEL":
            self._ts_counts["cancelled"] += 1
            if key is not None and key == stored_key:
                self._remove_print(s)
            else:
                self._ts_counts["unmatched"] += 1
            return

        if kind == "CORRECTION":
            if key is None or stored_key is None or key != stored_key:
                self._ts_counts["unmatched"] += 1
                return
            if (observed is None or price is None or price <= 0
                    or valid is False or spread_leg):
                # A correction we cannot accept still supersedes the print it
                # identifies. Keeping the old price would present superseded
                # evidence, so the print is invalidated instead.
                self._remove_print(s)
                self._ts_counts["invalidated"] += 1
                return
            self._ts_counts["corrected"] += 1
            self._store_print(
                s, price=price, observed=observed, size=size,
                trade_id=trade_id if trade_id is not None else s.get("ts_trade_id"),
                index=index if index is not None else s.get("ts_index"),
                key=key,
                sequence=sequence if sequence is not None else s.get("ts_sequence"),
                extended=extended, valid=valid)
            return

        if (observed is None or price is None or price <= 0
                or valid is False or spread_leg):
            self._ts_counts["invalid"] += 1
            return

        # NEW: a duplicate identity is not a later print, and only a genuinely
        # later (time, sequence) event may replace the latest.
        if stored_key is not None and key is not None and key == stored_key:
            self._ts_counts["duplicate"] += 1
            return
        if stored_time is not None:
            try:
                stored_dt = datetime.fromisoformat(stored_time)
            except (TypeError, ValueError):
                stored_dt = None
            if not self._is_later(datetime.fromisoformat(observed), sequence,
                                  stored_dt, stored_sequence):
                self._ts_counts["out_of_order"] += 1
                return
        self._ts_counts["accepted"] += 1
        self._store_print(s, price=price, observed=observed, size=size,
                          trade_id=trade_id, index=index, key=key,
                          sequence=sequence, extended=extended, valid=valid)

    @staticmethod
    def _remove_print(s):
        for name in ("ts_price", "ts_time", "ts_size", "ts_trade_id",
                     "ts_index", "ts_key", "ts_sequence", "ts_extended",
                     "ts_valid", "ts_received_at"):
            s.pop(name, None)

    def _store_print(self, s, *, price, observed, size, trade_id, index, key,
                     sequence, extended, valid):
        s["ts_price"] = price
        s["ts_time"] = observed
        if size is not None:
            s["ts_size"] = size
        s["ts_trade_id"] = trade_id
        s["ts_index"] = index
        s["ts_key"] = key
        s["ts_sequence"] = sequence
        s["ts_extended"] = extended
        s["ts_valid"] = valid
        s["ts_received_at"] = datetime.now(timezone.utc).isoformat()

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
                if et:
                    self._event_counts[et] = self._event_counts.get(et, 0) + 1
                    if et not in self._event_fields:
                        self._event_fields[et] = sorted(
                            key for key in event.keys()
                            if key not in ("eventType", "eventSymbol"))[:40]
                if et == "Quote":
                    b = self._to_float(event.get("bidPrice"))
                    a = self._to_float(event.get("askPrice"))
                    bid_time = self._event_time(event.get("bidTime"))
                    ask_time = self._event_time(event.get("askTime"))
                    if bid_time or ask_time:
                        self._quote_time_counts["with_provider_time"] += 1
                    else:
                        self._quote_time_counts["without_provider_time"] += 1
                    if b is not None:
                        s["bid"] = b
                        s["bid_ts"] = now.isoformat()
                        s["bid_observed_at"] = bid_time
                    if a is not None:
                        s["ask"] = a
                        s["ask_ts"] = now.isoformat()
                        s["ask_observed_at"] = ask_time
                elif et == "Summary":
                    oi = self._to_int(event.get("openInterest"))
                    if oi is not None:
                        s["oi"] = oi
                    pc = self._to_float(event.get("prevDayClosePrice"))
                    if pc is not None:
                        s["prev_close"] = pc
                        s["prev_close_date"] = self._day_id(event.get("prevDayId"))
                        s["summary_ts"] = now.isoformat()
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
                        s["last_observed_at"] = self._event_time(event.get("time"))
                        s["last_session"] = "extended" if et == "TradeETH" else "regular"
                elif et == "TradeETH":
                    px = self._to_float(event.get("price"))
                    if px is not None:
                        s["last"] = px
                        s["last_ts"] = now.isoformat()
                        s["last_observed_at"] = self._event_time(event.get("time"))
                        s["last_session"] = "extended" if et == "TradeETH" else "regular"
                elif et == "TimeAndSale":
                    self._ingest_time_and_sale(s, event)
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

__all__ = ["DXLinkFeed"]
