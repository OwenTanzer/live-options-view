"""DTCC public slice-index route for the credit tape (OA-242).

The PPD dashboard publishes a public index (Slice.json) listing every intraday
slice file per channel with sliceId, dissemDTM, startTs/endTs and rowCount.

Structure (so live collection keeps moving through replay and retries):

* ``poll_index`` fetches the index conditionally (If-None-Match) and only
  *enqueues* unseen CFTC_CR / SEC_CR slices into a durable pending store
  (``dtcc-slices/pending.json``), kept separately from the index ETag.
* ``drain_live`` runs on the live loop every pass, independent of whether the
  index answered 200 or 304, and fetches pending slices disseminated after the
  route started. Failed slices stay pending with a per-slice retry time.
* ``drain_backfill`` fetches replay slices (disseminated before start). Run it
  on its own thread (``start_backfill_thread``) so replay never blocks live
  polling.
* A slice is emitted exactly once: original bytes are written, its events are
  appended in one write, then a done marker is recorded before it leaves the
  pending store. On restart, pending slices that already have a marker are
  dropped without re-emitting.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlsplit

INDEX_URL = "https://kgc0418-tdw-data-0.s3.amazonaws.com/dashboard/Slice.json"
SLICE_HOST = "kgc0418-tdw-data-0.s3.amazonaws.com"
CHANNELS = {
    "CFTC_CR": {"source": "dtcc-cftc-slices", "jurisdiction": "US-CFTC", "prefix": "/cftc/slices/CFTC_SLICE_CREDITS_"},
    "SEC_CR": {"source": "dtcc-sec-slices", "jurisdiction": "US-SEC", "prefix": "/sec/slices/SEC_SLICE_CREDITS_"},
}
PUBLISHER = "DTCC Data Repository (U.S.) LLC"
UA = "Mozilla/5.0 (credit-tape research client)"
RETRY_MIN, RETRY_MAX = 5.0, 300.0


def _atomic_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class SliceRoute:
    def __init__(self, out: str, now, append_jsonl, opener=urllib.request.urlopen, clock=time.time, lock=None):
        self.out, self.now, self._append, self.open, self.clock = out, now, append_jsonl, opener, clock
        self.lock = lock or threading.RLock()  # pass the writer's lock when sharing an event log
        self.dir = os.path.join(out, "dtcc-slices")
        os.makedirs(os.path.join(self.dir, "done"), exist_ok=True)
        self.state_path = os.path.join(self.dir, "checkpoint.json")
        self.pending_path = os.path.join(self.dir, "pending.json")
        st = self._load(self.state_path, {})
        self.seen: set[int] = set(st.get("seen_slice_ids", []))  # enqueued or completed
        self.etag: str | None = st.get("index_etag")
        self.started: str = st.get("started_at") or now()
        self.polls_ok, self.polls_err = st.get("polls_ok", 0), st.get("polls_err", 0)
        self.not_modified, self.last_index_at = st.get("not_modified", 0), st.get("last_index_at")
        self.index_backoff, self.index_retry_at = 0.0, 0.0
        # pending: sliceId -> {"key", "meta", "attempts", "retry_at", "last_error"}
        self.pending: dict[int, dict] = {int(k): v for k, v in self._load(self.pending_path, {}).items()}
        for sid in list(self.pending):  # completed just before a crash: drop, don't re-emit
            if self._done(sid):
                del self.pending[sid]
                self.seen.add(sid)
        self._save()

    # -- persistence -----------------------------------------------------------
    @staticmethod
    def _load(path, default):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        return default

    def _save(self):
        with self.lock:
            _atomic_json(self.pending_path, {str(k): v for k, v in self.pending.items()})
            _atomic_json(self.state_path, {
                "seen_slice_ids": sorted(self.seen)[-50000:], "index_etag": self.etag, "started_at": self.started,
                "polls_ok": self.polls_ok, "polls_err": self.polls_err, "not_modified": self.not_modified,
                "last_index_at": self.last_index_at})

    def _done(self, sid: int) -> bool:
        return os.path.exists(os.path.join(self.dir, "done", str(sid)))

    def _mark_done(self, sid: int, info: dict):
        path = os.path.join(self.dir, "done", str(sid))
        _atomic_json(path, info)

    def append(self, path, obj):
        with self.lock:
            self._append(path, obj)

    def _append_many(self, path, objs):
        with self.lock, open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(o, separators=(",", ":")) + "\n" for o in objs))
            fh.flush()
            os.fsync(fh.fileno())

    def is_replay(self, meta) -> bool:
        return bool(meta.get("dissemDTM")) and meta["dissemDTM"] < self.started[:19] + "Z"

    def health(self):
        with self.lock:
            live = sum(1 for p in self.pending.values() if not self.is_replay(p["meta"]))
            return {"polls_ok": self.polls_ok, "polls_err": self.polls_err, "not_modified": self.not_modified,
                    "slices_seen": len(self.seen), "pending_live": live, "pending_backfill": len(self.pending) - live,
                    "last_index_at": self.last_index_at}

    # -- index -----------------------------------------------------------------
    def poll_index(self, events_path: str) -> None:
        """Conditional index fetch; enqueue unseen credit slices. Never fetches slices."""
        if self.clock() < self.index_retry_at:
            return
        headers = {"User-Agent": UA}
        if self.etag:
            headers["If-None-Match"] = self.etag
        t_req = self.now()
        try:
            with self.open(urllib.request.Request(INDEX_URL, headers=headers), timeout=60) as r:
                body, etag, lastmod = r.read(), r.headers.get("ETag"), r.headers.get("Last-Modified")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                with self.lock:
                    self.not_modified += 1
                    self.last_index_at = t_req
                self.append(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "ok": True, "http": 304})
                self._save()
                return
            return self._index_fail(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return self._index_fail(exc)
        t_recv = self.now()
        try:
            index = json.loads(body)
        except ValueError as exc:
            return self._index_fail(exc)
        day = t_recv[:10]
        os.makedirs(os.path.join(self.dir, "index", day), exist_ok=True)
        entries = {k: index.get(k) for k in CHANNELS}
        _atomic_json(os.path.join(self.dir, "index", day, f"{t_recv.replace(':', '')}_{(etag or '').strip(chr(34))[:12]}.json"),
                     {"fetched_at": t_recv, "etag": etag, "last_modified": lastmod,
                      "index_sha256": hashlib.sha256(body).hexdigest(), "entries": entries})
        self.append(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "recv": t_recv, "ok": True, "http": 200,
                                                           "etag": etag, "last_modified": lastmod, "bytes": len(body)})
        added = 0
        with self.lock:
            self.polls_ok += 1
            self.index_backoff = 0.0
            for key, ch in CHANNELS.items():
                lst = entries.get(key)
                if not isinstance(lst, list):
                    self.append(events_path, {"source": ch["source"], "kind": "index_channel_missing", "first_seen": t_recv,
                                              "channel": key, "publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"]})
                    continue
                for meta in lst:
                    sid = meta.get("sliceId") if isinstance(meta, dict) else None
                    if type(sid) is not int or sid in self.seen or self._done(sid):
                        continue
                    self.pending[sid] = {"key": key, "meta": meta, "attempts": 0, "retry_at": 0.0,
                                         "enqueued_at": t_recv, "last_error": None}
                    self.seen.add(sid)
                    added += 1
            # The ETag is only advanced after pending entries are durable.
            self.etag, self.last_index_at = etag, t_recv
            self._save()

    def _index_fail(self, exc):
        with self.lock:
            self.polls_err += 1
            self.index_backoff = min(RETRY_MAX, max(RETRY_MIN, self.index_backoff * 2))
            self.index_retry_at = self.clock() + self.index_backoff
        self.append(os.path.join(self.dir, "polls.jsonl"), {"t": self.now(), "ok": False, "what": "index",
                                                           "error": repr(exc)[:300], "backoff_s": self.index_backoff})

    # -- draining --------------------------------------------------------------
    def _due(self, replay: bool):
        now = self.clock()
        with self.lock:
            return [sid for sid, p in sorted(self.pending.items())
                    if self.is_replay(p["meta"]) == replay and p["retry_at"] <= now]

    def drain_live(self, events_path, alerts_path, budget: int = 50) -> int:
        """Fetch due live slices; called on the live loop every pass (200 or 304 alike)."""
        n = 0
        for sid in self._due(replay=False)[:budget]:
            n += self._process(sid, events_path, alerts_path)
        return n

    def drain_backfill(self, events_path, alerts_path, budget: int = 25, pause: float = 0.0, stop=None) -> int:
        n = 0
        for sid in self._due(replay=True)[:budget]:
            if stop is not None and stop.is_set():
                break
            n += self._process(sid, events_path, alerts_path)
            if pause:
                time.sleep(pause)
        return n

    def start_backfill_thread(self, events_path, alerts_path, idle: float = 2.0, pause: float = 0.05):
        stop = threading.Event()

        def run():
            while not stop.is_set():
                if not self.drain_backfill(events_path, alerts_path, pause=pause, stop=stop):
                    stop.wait(idle)

        t = threading.Thread(target=run, name="credit-tape-backfill", daemon=True)
        t.start()
        return t, stop

    def _process(self, sid, events_path, alerts_path) -> int:
        with self.lock:
            p = self.pending.get(sid)
            if p is None or self._done(sid):
                self.pending.pop(sid, None)
                return 0
            key, meta = p["key"], p["meta"]
        ch = CHANNELS[key]
        url, name = meta.get("fullFilePath") or "", meta.get("fileName") or ""
        u = urlsplit(url)
        replay = self.is_replay(meta)
        slice_info = {k: meta.get(k) for k in ("fileName", "sliceId", "dissemDTM", "startTs", "endTs", "rowCount")}
        prov = {"publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"], "route": "slice-index"}
        if (u.scheme != "https" or u.hostname != SLICE_HOST or not u.path.startswith(ch["prefix"])
                or not name.endswith(".zip") or "/" in name or u.path.rsplit("/", 1)[-1] != name):
            self._finish(sid, events_path, alerts_path, [{"source": ch["source"], "kind": "slice_rejected", "first_seen": self.now(),
                                             "slice": slice_info, "replay": replay, **prov}], [], {"rejected": True})
            return 1
        t_req = self.now()
        try:
            with self.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                blob = r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            with self.lock:
                p["attempts"] += 1
                p["last_error"] = repr(exc)[:300]
                p["retry_at"] = self.clock() + min(RETRY_MAX, RETRY_MIN * 2 ** (p["attempts"] - 1))
                self._save()
            self.append(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "ok": False, "what": "slice", "sliceId": sid,
                                                               "file": name, "attempt": p["attempts"],
                                                               "error": p["last_error"], "replay": replay})
            return 0
        t_recv = self.now()
        rel = f"dtcc-slices/raw/{(meta.get('dissemDTM') or t_recv)[:10]}/{name}"
        path = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:  # original bytes before decoding
            fh.write(blob)
        base = {"first_seen": t_recv, "raw_ref": rel, "slice": slice_info, "replay": replay,
                "attempts": p["attempts"] + 1, **prov}
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                rows = list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))))
        except Exception as exc:
            ev = {"source": ch["source"], "kind": "decode_failure", "error": repr(exc)[:300], **base}
            self._finish(sid, events_path, alerts_path, [ev], [ev], {"decode_failure": True})
            return 1
        match = len(rows) == meta.get("rowCount")
        evs = [{"source": ch["source"], "kind": "slice", "requested_at": t_req,
                "raw_sha256": hashlib.sha256(blob).hexdigest(), "decoded_rows": len(rows), "rowcount_match": match, **base}]
        alerts = []
        if not match:
            mm = {"source": ch["source"], "kind": "rowcount_mismatch", "decoded_rows": len(rows), **base}
            evs.append(mm)
            alerts.append(mm)
        for idx, row in enumerate(rows):
            ev = {"source": ch["source"], "kind": "publication", "id": row.get("Dissemination Identifier"),
                  "original_id": row.get("Original Dissemination Identifier") or None,
                  "action": row.get("Action type"), "execution_ts": row.get("Execution Timestamp"),
                  "event_ts": row.get("Event timestamp"), "dissemination_ts": meta.get("dissemDTM"),
                  "row_index": idx, "backfill": replay, **base, "record": row}
            evs.append(ev)
            if not replay:
                alerts.append({k: v for k, v in ev.items() if k != "record"})
        self._finish(sid, events_path, alerts_path, evs, alerts, {"rows": len(rows), "raw_ref": rel})
        return 1

    def _finish(self, sid, events_path, alerts_path, evs, alerts, info):
        with self.lock:
            if self._done(sid):  # another drain finished it first
                self.pending.pop(sid, None)
                return
            self._append_many(events_path, evs)
            if alerts:
                stamp = self.now()
                self._append_many(alerts_path, [{**a, "alert_written": stamp} for a in alerts])
            self._mark_done(sid, {"at": self.now(), **info})
            self.pending.pop(sid, None)
            self._save()
