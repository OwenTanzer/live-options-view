"""OA-242 local credit-tape capture (prototype, local only).

Polls the two DTCC public price dissemination credit tickers, persists every
distinct original response BEFORE decoding, then records each newly seen
publication as an event with first-seen time and a reference back to the raw
archive. Alerts go to a local outbox until the Options View alert path is
decided; no production storage is touched.

Usage: python -m market_data.credit_tape.capture --out <dir> [--interval 20]
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

SOURCES = {
    "dtcc-cftc-credits": {
        "url": "https://pddata.dtcc.com/ppd/api/ticker/CFTC/CREDITS",
        "publisher": "DTCC Data Repository (U.S.) LLC",
        "jurisdiction": "US-CFTC",
    },
    "dtcc-sec-credits": {
        "url": "https://pddata.dtcc.com/ppd/api/ticker/SEC/CREDITS",
        "publisher": "DTCC Data Repository (U.S.) LLC",
        "jurisdiction": "US-SEC",
    },
}
UA = "Mozilla/5.0 (credit-tape research client)"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


WRITE_LOCK = threading.RLock()  # ticker loop and slice backfill thread share the event log


def append_jsonl(path: str, obj: dict) -> None:
    with WRITE_LOCK, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class Source:
    def __init__(self, name: str, cfg: dict, out: str):
        self.name, self.cfg = name, cfg
        self.dir = os.path.join(out, name)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "checkpoint.json")
        st = {}
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        self.seen: set[str] = set(st.get("seen", []))
        self.last_sha: str | None = st.get("last_sha")
        self.polls_ok = st.get("polls_ok", 0)
        self.polls_err = st.get("polls_err", 0)
        self.started = st.get("capture_started_at") or now()

    def save_state(self, extra: dict) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"seen": sorted(self.seen)[-20000:], "last_sha": self.last_sha,
                       "polls_ok": self.polls_ok, "polls_err": self.polls_err,
                       "capture_started_at": self.started, **extra}, fh)
        os.replace(tmp, self.state_path)

    def poll(self, alerts_path: str, events_path: str) -> None:
        req = urllib.request.Request(self.cfg["url"], headers={"User-Agent": UA, "Accept": "application/json"})
        t_req = now()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                status = resp.status
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.polls_err += 1
            append_jsonl(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "ok": False, "error": repr(exc)})
            self.save_state({"last_error_at": t_req})
            return
        t_recv = now()
        sha = hashlib.sha256(body).hexdigest()
        self.polls_ok += 1
        raw_ref = None
        if sha != self.last_sha:
            day = t_recv[:10]
            os.makedirs(os.path.join(self.dir, "raw", day), exist_ok=True)
            raw_ref = f"{self.name}/raw/{day}/{t_recv.replace(':', '').replace('-', '')}_{sha[:12]}.json.gz"
            with gzip.open(os.path.join(os.path.dirname(self.dir), raw_ref), "wb") as fh:
                fh.write(body)  # original bytes, persisted before any decoding
            self.last_sha = sha
        append_jsonl(os.path.join(self.dir, "polls.jsonl"),
                     {"t": t_req, "recv": t_recv, "ok": True, "http": status, "bytes": len(body),
                      "sha256": sha, "raw_ref": raw_ref})
        if raw_ref is None:
            self.save_state({"last_ok_at": t_recv})
            return
        try:
            rows = json.loads(body)["tradeList"] or []
        except Exception as exc:  # decode failure still yields a raw event
            ev = {"source": self.name, "kind": "decode_failure", "first_seen": t_recv,
                  "raw_ref": raw_ref, "error": repr(exc), **self._prov()}
            append_jsonl(events_path, ev)
            append_jsonl(alerts_path, {**ev, "alert_written": now()})
            self.save_state({"last_ok_at": t_recv})
            return
        had_seen = bool(self.seen)
        overlap = 0
        new = []
        for idx, row in enumerate(rows):
            key = row.get("disseminationIdentifier") if isinstance(row, dict) else None
            if key is None:
                key = "sha:" + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            if key in self.seen:
                overlap += 1
                continue
            new.append((idx, key, row))
        # Ticker returns newest 100; zero overlap after warm-up means a possible gap.
        if had_seen and rows and overlap == 0:
            append_jsonl(events_path, {"source": self.name, "kind": "possible_gap", "first_seen": t_recv,
                                       "raw_ref": raw_ref, "rows": len(rows), **self._prov()})
        for idx, key, row in reversed(new):  # oldest first
            self.seen.add(key)
            ev = {"source": self.name, "kind": "publication", "id": key,
                  "original_id": row.get("originalDisseminationIdentifier"),
                  "action": row.get("actionType"), "execution_ts": row.get("executionTimestamp"),
                  "event_ts": row.get("eventTimestamp"), "dissemination_ts": row.get("disseminationTimestamp"),
                  "first_seen": t_recv, "raw_ref": raw_ref, "row_index": idx,
                  "warmup": not had_seen,
                  # published before this capture began -> backfill, not a live first sighting
                  "backfill": bool(row.get("disseminationTimestamp")) and row["disseminationTimestamp"] < self.started[:19] + "Z",
                  **self._prov(), "record": row}
            append_jsonl(events_path, ev)
            if had_seen:  # don't alert the initial backlog
                append_jsonl(alerts_path, {k: v for k, v in ev.items() if k != "record"} | {"alert_written": now()})
        self.save_state({"last_ok_at": t_recv})

    def _prov(self) -> dict:
        return {"publisher": self.cfg["publisher"], "jurisdiction": self.cfg["jurisdiction"]}


def write_health(out: str, health: dict, extra: dict | None = None) -> None:
    """Atomic health.json (the panel and /healthz/collector read it concurrently)."""
    path = os.path.join(out, "health.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"t": now(), "sources": health, **(extra or {})}, fh)
    os.replace(tmp, path)


def run(out: str, interval: float = 20.0, ticker: bool = True, slices: bool = True,
        once: bool = False, stop: threading.Event | None = None, opener=None) -> int:
    """Collector loop. `stop` (optional) ends it after the current pass. `opener`: tests only."""
    os.makedirs(out, exist_ok=True)
    srcs = [Source(n, c, out) for n, c in SOURCES.items()] if ticker else []
    from market_data.credit_tape.slices import SliceRoute
    kw = {"opener": opener} if opener else {}
    route = SliceRoute(out, now, append_jsonl, lock=WRITE_LOCK, **kw) if slices else None
    events, alerts = os.path.join(out, "events.jsonl"), os.path.join(out, "alerts_outbox.jsonl")
    bf_stop = None
    if route and not once:
        _, bf_stop = route.start_backfill_thread(events, alerts)  # replay never blocks the live loop
    mode = {"ticker": ticker, "slices": slices, "interval_s": interval}
    try:
        while True:
            for s in srcs:
                s.poll(alerts, events)
            if route:
                route.poll_index(events)
                route.drain_live(events, alerts)  # runs whether the index answered 200 or 304
                if once:
                    route.drain_backfill(events, alerts, budget=10**6)
            health = {s.name: {"polls_ok": s.polls_ok, "polls_err": s.polls_err, "seen": len(s.seen)} for s in srcs}
            if route:
                health["dtcc-slice-index"] = route.health()
            write_health(out, health, {"collector": mode})
            if route:
                try:
                    route.check_workers()  # a dead replay worker stops the collector (service exits 1)
                except RuntimeError:
                    health["dtcc-slice-index"] = route.health()
                    write_health(out, health, {"collector": mode})  # leave the failure visible on disk
                    raise
            if once or (stop is not None and stop.wait(interval)):
                return 0
            if stop is None:
                time.sleep(interval)
    finally:
        if bf_stop is not None:
            bf_stop.set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-ticker", action="store_true", help="slice-index route only")
    ap.add_argument("--no-slices", action="store_true", help="ticker route only")
    a = ap.parse_args()
    return run(a.out, a.interval, ticker=not a.no_ticker, slices=not a.no_slices, once=a.once)


if __name__ == "__main__":
    sys.exit(main())
