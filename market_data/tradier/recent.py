"""Bounded best-effort QQQ display projection of archived stream observations.

Two products share one signing pass:
* ``rows``: up to 2,048 recent option prints for the plot (older prints drop off in bursts);
* ``session``: per-contract accumulated inferred-buy, inferred-sell and unknown contracts
  for the whole session, independent of the display cap. It survives stream reconnects
  (same process) and collector restarts (``rebuild`` replays the session's archived
  segments, deduplicating overlaps by provider identity). Reconnect outages and any
  unarchived restart interval are published as gaps, never as zero flow.

Each print also carries a second, tiered signing (``label`` + ``basis``) that leaves
``side`` and the B/S/U totals untouched. It uses only what was known before the print:
* ``q1``/``q5``/``q30``: quote rule against the *prevailing* quote, i.e. the latest quote
  in a short per-contract history whose receipt and bid/ask dates both precede the
  print's provider time, by the older side's age (<=1 s, <=5 s, <=30 s);
* ``tick``/``zero_tick``: tick test against the previous print in sequence order, so
  same-millisecond sweep legs are compared rather than reset to unknown;
* ``mid``/``locked``/``none``: still unsigned (midpoint, locked/crossed quote, nothing).
Session totals per contract add ``label`` = {q1: [B, S], q5: [B, S], low: [B, S], U}, where
``low`` pools q30/tick/zero_tick, which agreed with the provider's own trade-time bid/ask
little better than chance in the 2026-11-23 audit and should not be read as signal.
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
QUOTE_TIERS = ((1000, "q1"), (5000, "q5"), (30000, "q30"))
QUOTE_HISTORY = 32
LABEL_SLOTS = {"q1": 0, "q5": 2, "q30": 4, "tick": 4, "zero_tick": 4}  # buy slot; sell = +1; else U at 6


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
        c = self.contracts.setdefault(row["symbol"], [0, 0, 0, 0, [0] * 7])
        c[SIDES.index(row["side"])] += row["size"]
        c[3] += 1
        slot = LABEL_SLOTS.get(row.get("basis"))
        c[4][6 if slot is None else slot + (row["label"] == "sell")] += row["size"]
        self.prints += 1
        self.first = row["t"] if self.first is None else min(self.first, row["t"])
        self.last = row["t"] if self.last is None else max(self.last, row["t"])

    def snapshot(self):
        return dict(day=self.day, first=self.first, last=self.last, prints=self.prints,
                    contracts={s: dict(zip(("B", "S", "U", "prints"), v[:4]),
                                       label=dict(q1=v[4][0:2], q5=v[4][2:4], low=v[4][4:6], U=v[4][6]))
                               for s, v in sorted(self.contracts.items())},
                    gaps=[dict(g) for g in self.gaps], rebuilt=dict(self.rebuilt) if self.rebuilt else None)


class RecentPrints:
    def __init__(self, universe=None):
        universe = universe or {}
        self.opening = dict.fromkeys(universe.get("option_symbols", ()))
        prior = universe.get("previous_selection")
        self.widening = ({"selected_at": universe["selected_at"],
                          "previous_symbols": prior["option_symbols"]} if prior else None)
        self.quotes = {}
        self.previous = {}
        self.history = {}   # symbol -> deque of (receipt_ms, oldest_date, newest_date, bid, ask), ten-thousandths
        self.sequence = {}  # symbol -> (seq, provider_ms, price, last nonzero tick side)
        self.rows = deque(maxlen=2048)
        self.session = None
        self.open_gap = None
        self.lock = Lock()
        self.version = 0

    def observe(self, event):
        try:
            return self._observe(event)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            return

    def _session_for(self, ts):
        day = datetime.fromtimestamp(ts / 1000, ET).date().isoformat()
        if self.session is None or self.session.day != day:
            if self.session is not None and day < self.session.day:
                return None
            self.session = Session(day)
            self.previous = {}
            self.sequence = {}
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
        if receipt and symbol in self.opening and self.opening[symbol] is None and event["type"] in ("quote", "timesale"):
            with self.lock:
                self.opening[symbol] = _ms(receipt)
                self.version += 1
        if event["type"] == "quote":
            self.quotes[symbol] = event
            self._remember(symbol, event)
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
        if symbol not in self.history and event.get("preceding_quote_context"):
            self._remember(symbol, event["preceding_quote_context"])
        label, basis = self._label(symbol, ts, price, int(seq))
        row = dict(t=ts, size=size, price=price/10000, symbol=symbol,
                   strike=int(option[2])/1000, type=option[1], side=side, seq=seq, label=label, basis=basis)
        with self.lock:
            self.rows.append(row)
            session.add(row)
            self.version += 1

        return row

    def _remember(self, symbol, quote):
        try:
            dates = int(quote["biddate"]), int(quote["askdate"])
            entry = (_ms(quote["collector_receipt_timestamp"]), min(dates), max(dates),
                     round(float(quote["bid"]) * 10000), round(float(quote["ask"]) * 10000))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return
        self.history.setdefault(symbol, deque(maxlen=QUOTE_HISTORY)).append(entry)

    def _label(self, symbol, ts, price, seq):
        """Tiered side for one print from information that preceded it (see module doc)."""
        label = basis = None
        for receipt, oldest, newest, bid, ask in reversed(self.history.get(symbol, ())):
            if receipt < ts and newest < ts:
                tier = next((name for limit, name in QUOTE_TIERS if ts - oldest <= limit), None)
                if tier:
                    delta = 2 * price - bid - ask
                    if not 0 < bid < ask:
                        basis = "locked"
                    elif delta:
                        label, basis = ("buy" if delta > 0 else "sell"), tier
                    else:
                        basis = "mid"
                break
        previous = self.sequence.get(symbol)
        if previous is None:
            self.sequence[symbol] = (seq, ts, price, None)
        else:
            previous_seq, previous_ts, previous_price, tick = previous
            if seq > previous_seq and ts >= previous_ts:
                if price != previous_price:
                    tick = "buy" if price > previous_price else "sell"
                if label is None and tick:
                    label, basis = tick, "tick" if price != previous_price else "zero_tick"
                self.sequence[symbol] = (seq, ts, price, tick)
        return label or "unknown", basis if label else (basis or "none")

    def rebuild(self, client, bucket, prefix, local_segments=(), now=None, session_open=None):
        """Replay this session's archived segments (R2, then unuploaded local spool) before capture.

        Overlapping events are dropped by identity. When capture starts after the open,
        the interval from the later of the open and the last archived receipt to the
        first live event is recorded as a restart gap, including a late start with no
        archived history at all. A failure leaves the session marked incomplete;
        capture proceeds either way.
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
        local_now = now.astimezone(ET)
        opened = session_open or local_now.replace(hour=9, minute=30, second=0, microsecond=0)
        open_ms, now_ms = int(opened.timestamp() * 1000), int(now.timestamp() * 1000)
        with self.lock:
            if self.session is None:
                self.session = Session(local_now.date().isoformat())
            through = _ms(last_receipt) if last_receipt else None
            self.session.rebuilt = dict(status=status, segments=segments, through=through, at=now_ms,
                                        session_open=open_ms)
            start = max(open_ms, through) if through is not None else open_ms
            if status != "rebuilt" or now_ms > open_ms:
                # Closed by the first live event's receipt; open (null end) until then.
                self.open_gap = dict(start=start, end=None, reason=f"collector_restart:{status}")
                self.session.gaps.append(self.open_gap)
            self.version += 1

    def payload(self):
        with self.lock:
            rows = list(self.rows)
            session = self.session.snapshot() if self.session else None
            if session is not None and self.opening:
                session["opening"] = self.opening.copy()
                if self.widening:
                    session["widening"] = self.widening
                    session["gaps"].append(dict(start=session["rebuilt"]["session_open"],
                                                end=_ms(self.widening["selected_at"]),
                                                reason="partial_strike_coverage_before_widening"))
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
