"""Bounded best-effort QQQ display projection of archived stream observations."""
from collections import deque
from datetime import datetime
import json
import logging
import re
from threading import Lock

KEY = "order-flow/qqq/v1/recent.json"
OPTION = re.compile(r"QQQ\d{6}([CP])(\d{8})")


class RecentPrints:
    def __init__(self):
        self.quotes = {}
        self.previous = {}
        self.identities = set()
        self.history = deque()
        self.rows = deque(maxlen=2048)
        self.lock = Lock()
        self.version = 0

    def observe(self, event):
        try:
            self._observe(event)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            return

    def _observe(self, event):
        symbol = event.get("symbol", "")
        option = OPTION.fullmatch(symbol)
        if not option:
            return
        if event["type"] == "quote":
            self.quotes[symbol] = event
            return
        if event["type"] != "timesale" or any(event.get(k) for k in ("duplicate_in_run", "correction", "cancel")):
            return
        ts, size = int(event["date"]), int(event["size"])
        price = round(float(event["last"]) * 10000)
        identity = (symbol, event["seq"])
        if identity[1] is None or identity in self.identities or size <= 0 or ts <= 0 or price <= 0:
            return
        self.identities.add(identity)
        self.history.append(identity)
        if len(self.history) > 16384:
            self.identities.remove(self.history.popleft())
        previous = self.previous.get(symbol)
        tick = "unknown"
        if previous and ts > previous[0]:
            tick = "buy" if price > previous[1] else "sell" if price < previous[1] else previous[2]
        if not previous or ts >= previous[0]:
            self.previous[symbol] = (ts, price, tick)
        side = tick
        quote = self.quotes.get(symbol) or event.get("preceding_quote_context")
        if quote:
            try:
                bid, ask = (round(float(quote[k])*10000) for k in ("bid", "ask"))
                dates = [int(quote[k]) for k in ("biddate", "askdate")]
                seen = quote["collector_receipt_timestamp"]
                if (quote["symbol"] == symbol and seen < event["collector_receipt_timestamp"]
                        and datetime.fromisoformat(seen).timestamp()*1000 < ts
                        and ts-1000 <= min(dates) <= max(dates) < ts and 0 < bid < ask):
                    delta = 2*price-bid-ask
                    side = "buy" if delta > 0 else "sell" if delta < 0 else tick
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                pass
        row = dict(t=ts, size=size, price=price/10000, symbol=symbol,
                   strike=int(option[2])/1000, type=option[1], side=side, seq=identity[1])
        with self.lock:
            self.rows.append(row)
            self.version += 1

    def publish(self, client, bucket, stop, lost):
        sent = 0
        while not stop.wait(3) and not lost.is_set():
            with self.lock:
                version = self.version
                if version == sent:
                    continue
                rows = list(self.rows)
            try:
                client.put_object(Bucket=bucket, Key=KEY, ContentType="application/json",
                                  CacheControl="no-store", Body=json.dumps(dict(
                                      lastReceived=rows[-1]["t"], rows=rows),
                                      separators=(",", ":")).encode())
                sent = version
            except Exception as exc:
                logging.warning("Recent QQQ snapshot failed: %s", type(exc).__name__)
