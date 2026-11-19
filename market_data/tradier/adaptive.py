"""Grow the opening QQQ subscription on the existing Tradier socket."""
from contextlib import contextmanager
import json
import math

import requests

from ._probe import parse_epoch_ms


class AdaptiveQQQ:
    def __init__(self, router, primary, recent, count, stream_info):
        self.router = router
        self.primary = primary
        self.recent = recent
        self.count = count
        self.stream_info = stream_info
        self.pairs = {float(strike): legs for strike, legs in primary.universe["listed_qqq_pairs"].items()}
        self.next_check = 0
        self.socket = None
        restored = recent.session.added if recent.session else {}
        self.register(restored)

    def register(self, symbols):
        for symbol in symbols:
            if symbol not in self.router.by_symbol:
                self.router.by_symbol[symbol] = self.primary
                self.router.symbols.append(symbol)
        self.stream_info["subscribed_symbols"] = len(self.router.symbols)

    def subscribe(self):
        self.socket.send(json.dumps(dict(sessionid=self.session_id, symbols=self.router.symbols,
                                         filter=["quote", "timesale"], validOnly=False, advancedDetails=True)))

    @contextmanager
    def open(self, session_id):
        import websocket
        self.session_id = session_id
        try:
            self.socket = websocket.create_connection("wss://ws.tradier.com/v1/markets/events", timeout=10)
            self.subscribe()
            yield self.lines()
        except websocket.WebSocketException as exc:
            raise requests.ConnectionError("Tradier market socket interrupted") from exc
        finally:
            if self.socket is not None:
                self.socket.close()
                self.socket = None

    def lines(self):
        while True:
            frame = self.socket.recv()
            if not frame:
                raise requests.ConnectionError("Tradier market socket closed")
            for line in frame.splitlines():
                # Reject a provider error before the capture loop can close an outage.
                if '"error"' in (line.decode() if isinstance(line, bytes) else line):
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        pass  # Capture retains malformed lines in the archive.
                    else:
                        if isinstance(payload, dict) and "error" in payload:
                            raise requests.ConnectionError("Tradier rejected the market subscription")
                yield line

    def follow(self, event, now):
        if event.get("symbol") != "QQQ" or event.get("type") != "quote" or now < self.next_check:
            return
        stamps = [parse_epoch_ms(event.get(key)) for key in ("biddate", "askdate")]
        try:
            bid, ask = float(event["bid"]), float(event["ask"])
        except (KeyError, TypeError, ValueError):
            return
        if not (0 < bid <= ask and math.isfinite(ask)) or any(
            t is None or not 0 <= now - t <= 120000 for t in stamps
        ):
            return
        self.next_check = now + 5000
        spot = (bid + ask) / 2
        nearest = sorted(self.pairs, key=lambda k: (abs(k - spot), k))[:self.count]
        new = [s for k in nearest for s in self.pairs[k] if s not in self.router.by_symbol]
        if not new:
            return
        for symbol in new:
            marker = dict(type="subscription_added", symbol=symbol,
                          collector_receipt_timestamp=event["collector_receipt_timestamp"])
            self.primary.spool.write(marker)
            self.recent.observe(marker)
        self.register(new)
        self.subscribe()
