"""Bounded best-effort QQQ display projection of archived stream observations.

Two products share one signing pass:
* ``rows``: up to 2,048 recent option prints for the plot (older prints drop off in bursts);
* ``session``: per-contract accumulated inferred-buy, inferred-sell and unknown contracts
  for the whole session, independent of the display cap. It survives stream reconnects
  (same process) and collector restarts (``rebuild`` replays the session's archived
  segments, deduplicating overlaps by provider identity). Reconnect outages and any
  unarchived restart interval are published as gaps, never as zero flow.
"""
from collections import deque
from datetime import datetime, timezone
import gzip
import json
import logging
import re
from threading import Lock
from zoneinfo import ZoneInfo

KEY = "order-flow/qqq/v1/recent.json"
OPTION = re.compile(r"QQQ\d{6}([CP])(\d{8})")
ET = ZoneInfo("America/New_York")
SIDES = ("buy", "sell", "unknown")


def _ms(stamp):
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


class SeqSet:
    """Exact per-symbol set of seen sequence numbers, compacted to a contiguous floor."""

    def __init__(self):
        self.low = self.floor = None  # every seq in [low, floor] has been seen
        self.other = set()            # seen seqs outside that run

    def __len__(self):
        return (0 if self.low is None else self.floor - self.low + 1) + len(self.other)

    def add(self, seq):
        """Return False when ``seq`` was already seen."""
        if self.low is None:
            self.low = self.floor = seq
            return True
        if self.low <= seq <= self.floor or seq in self.other:
            return False
        self.other.add(seq)
        while self.floor + 1 in self.other:
            self.floor += 1
            self.other.remove(self.floor)
        return True


class Session:
    def __init__(self, day):
        self.day = day
        self.contracts = {}
        self.seen = {}
        self.first = self.last = None
        self.gaps = []
        self.rebuilt = None
        self.prints = 0

    def accept(self, symbol, seq):
        return self.seen.setdefault(symbol, SeqSet()).add(seq)

    def add(self, row):
        c = self.contracts.setdefault(row["symbol"], [0, 0, 0, 0])
        c[SIDES.index(row["side"])] += row["size"]
        c[3] += 1
        self.prints += 1
        self.first = row["t"] if self.first is None else min(self.first, row["t"])
        self.last = row["t"] if self.last is None else max(self.last, row["t"])

    def snapshot(self):
        return dict(day=self.day, first=self.first, last=self.last, prints=self.prints,
                    contracts={s: dict(zip(("B", "S", "U", "prints"), v)) for s, v in sorted(self.contracts.items())},
                    gaps=[dict(g) for g in self.gaps], rebuilt=dict(self.rebuilt) if self.rebuilt else None)


class RecentPrints:
    def __init__(self):
        self.quotes = {}
        self.previous = {}
        self.rows = deque(maxlen=2048)
        self.session = None
        self.open_gap = None
        self.lock = Lock()
        self.version = 0

    def observe(self, event):
        try:
            self._observe(event)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            return

    def _session_for(self, ts):
        day = datetime.fromtimestamp(ts / 1000, ET).date().isoformat()
        if self.session is None or self.session.day != day:
            if self.session is not None and day < self.session.day:
                return None
            self.session = Session(day)
            self.previous = {}
        return self.session

    def _observe(self, event):
        receipt = event.get("collector_receipt_timestamp")
        if self.open_gap is not None and receipt and (self.open_gap["start"] is None
                                                      or _ms(receipt) > self.open_gap["start"]):
            with self.lock:
                self.open_gap["end"] = _ms(receipt)
                self.open_gap = None
                self.version += 1
        if event.get("type") == "gap":
            end = _ms(event["receipt_timestamp"])
            if self.session is not None:
                with self.lock:
                    self.session.gaps.append(dict(start=end - round(float(event["outage_seconds"]) * 1000), end=end,
                                                  reason=event.get("reason", "stream_gap")))
                    self.version += 1
            return
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
        seq = event["seq"]
        if seq is None or size <= 0 or ts <= 0 or price <= 0:
            return
        session = self._session_for(ts)
        if session is None or not session.accept(symbol, int(seq)):
            return
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
                   strike=int(option[2])/1000, type=option[1], side=side, seq=seq)
        with self.lock:
            self.rows.append(row)
            session.add(row)
            self.version += 1

    def rebuild(self, client, bucket, prefix, local_segments=(), now=None):
        """Replay this session's archived segments (R2, then unuploaded local spool) before capture.

        Overlapping events are dropped by identity. Anything between the last archived
        receipt and the first live event is recorded as a restart gap. A failure
        leaves the session marked incomplete; capture proceeds either way.
        """
        keys, token, last_receipt, segments = [], None, None, 0
        try:
            while True:
                kwargs = {"Bucket": bucket, "Prefix": f"{prefix}/"}
                if token:
                    kwargs["ContinuationToken"] = token
                listing = client.list_objects_v2(**kwargs)
                keys += [item["Key"] for item in listing.get("Contents", []) if item.get("Key", "").endswith(".ndjson.gz")]
                token = listing.get("NextContinuationToken") if listing.get("IsTruncated") else None
                if not token:
                    break
            sources = [lambda k=k: client.get_object(Bucket=bucket, Key=k)["Body"].read() for k in sorted(keys)]
            sources += [lambda p=p: p.read_bytes() for p in sorted(local_segments)]
            for read in sources:
                for line in gzip.decompress(read()).splitlines():
                    event = json.loads(line)
                    self.observe(event)
                    receipt = event.get("collector_receipt_timestamp")
                    if receipt and (last_receipt is None or receipt > last_receipt):
                        last_receipt = receipt
                segments += 1
            status = "rebuilt"
        except Exception as exc:  # incomplete history must stay visible, never fatal to capture
            logging.warning("Order-flow session rebuild failed: %s", type(exc).__name__)
            status = "rebuild_failed"
        now = now or datetime.now(timezone.utc)
        with self.lock:
            if self.session is None:
                self.session = Session(now.astimezone(ET).date().isoformat())
            start = _ms(last_receipt) if last_receipt else None
            self.session.rebuilt = dict(status=status, segments=segments, through=start, at=int(now.timestamp() * 1000))
            if status != "rebuilt" or start is not None:
                # Closed by the first live event's receipt; open (null end) until then.
                self.open_gap = dict(start=start, end=None, reason=f"collector_restart:{status}")
                self.session.gaps.append(self.open_gap)
            self.version += 1

    def payload(self):
        with self.lock:
            rows = list(self.rows)
            session = self.session.snapshot() if self.session else None
        return dict(lastReceived=rows[-1]["t"] if rows else None, cap=self.rows.maxlen, rows=rows, session=session)

    def publish(self, client, bucket, stop, lost):
        sent = 0
        while not stop.wait(3) and not lost.is_set():
            with self.lock:
                version = self.version
            if version == sent:
                continue
            body = self.payload()
            if not body["rows"] and not body["session"]:
                continue
            try:
                client.put_object(Bucket=bucket, Key=KEY, ContentType="application/json",
                                  CacheControl="no-store", Body=json.dumps(body, separators=(",", ":")).encode())
                sent = version
            except Exception as exc:
                logging.warning("Recent QQQ snapshot failed: %s", type(exc).__name__)
