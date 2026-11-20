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
* Publishing is a durable, idempotent commit keyed by (source, channel,
  sliceId): (1) the complete raw ZIP is written to a temp file, fsynced,
  hash-checked and renamed into place; (2) a commit record holding every event
  and alert line plus the pre-append offsets of both logs is written
  atomically; (3) the lines are appended, each tagged with its commit key and
  line number; (4) a done marker is written. On restart, any commit without a
  done marker is re-applied from its recorded offsets: lines already present
  are skipped, a torn trailing line is trimmed, missing lines are appended,
  then the marker is written. Re-applying is idempotent, so a stop at any
  point yields no loss and no duplicate.
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


def decode_rows(blob: bytes) -> list[dict]:
    """Decode a slice ZIP (first member, UTF-8 CSV). check_archive re-uses this to re-verify commits."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))))


class SimulatedStop(Exception):
    """Raised only by synthetic crash hooks to model a process stop."""


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
        os.makedirs(os.path.join(self.dir, "commits"), exist_ok=True)
        self.crash_hook = None  # synthetic tests only: callable(stage) that may raise
        self.backfill_state, self.backfill_error = "not_started", None
        self.state_path = os.path.join(self.dir, "checkpoint.json")
        self.pending_path = os.path.join(self.dir, "pending.json")
        st = self._load(self.state_path, {})
        # Slice IDs are only unique within a channel (CFTC and SEC can publish the same
        # number), so pending and seen are keyed "CHANNEL:sliceId". Legacy bare-int
        # entries are dropped: completion is already recorded by channel-aware done markers.
        self.seen: set[str] = {k for k in st.get("seen_slice_keys", []) if isinstance(k, str)}
        self.etag: str | None = st.get("index_etag")
        self.started: str = st.get("started_at") or now()
        self.polls_ok, self.polls_err = st.get("polls_ok", 0), st.get("polls_err", 0)
        self.not_modified, self.last_index_at = st.get("not_modified", 0), st.get("last_index_at")
        self.index_backoff, self.index_retry_at = 0.0, 0.0
        # Per-channel status for health checks: listing in the last full index, last completed slice.
        self.channels: dict[str, dict] = {k: dict((st.get("channels") or {}).get(k) or {}) for k in CHANNELS}
        # pending: "CHANNEL:sliceId" -> {"key", "meta", "attempts", "retry_at", "last_error"}
        self.pending: dict[str, dict] = {}
        for k, v in self._load(self.pending_path, {}).items():
            sid = v.get("meta", {}).get("sliceId")
            if v.get("key") in CHANNELS and type(sid) is int:
                self.pending[self.pkey(v["key"], sid)] = v  # also migrates legacy bare-int keys
        self.recovered = self._recover()  # finish any commit interrupted before its done marker
        for pk in list(self.pending):
            p = self.pending[pk]
            if self._done(p["key"], p["meta"]["sliceId"]):
                del self.pending[pk]
                self.seen.add(pk)
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
            _atomic_json(self.pending_path, dict(self.pending))
            _atomic_json(self.state_path, {
                "seen_slice_keys": sorted(self.seen, key=self._order)[-50000:], "index_etag": self.etag, "started_at": self.started,
                "polls_ok": self.polls_ok, "polls_err": self.polls_err, "not_modified": self.not_modified,
                "last_index_at": self.last_index_at, "channels": self.channels})

    @staticmethod
    def pkey(channel: str, sid: int) -> str:
        return f"{channel}:{sid}"

    @staticmethod
    def _order(pk: str):
        channel, sid = pk.rsplit(":", 1)
        return int(sid), channel

    @staticmethod
    def commit_key(channel: str, sid: int) -> str:
        return f"{CHANNELS[channel]['source']}/{channel}/{sid}"

    def _key_file(self, kind: str, channel: str, sid: int) -> str:
        return os.path.join(self.dir, kind, self.commit_key(channel, sid).replace("/", "__") + ".json")

    def _done(self, channel: str, sid: int) -> bool:
        return os.path.exists(self._key_file("done", channel, sid))

    # -- durable idempotent publish -------------------------------------------
    @staticmethod
    def _trim_torn_tail(path: str, floor: int) -> None:
        """Drop an incomplete trailing line written after `floor` (a stop mid-append)."""
        if not os.path.exists(path):
            return
        with open(path, "rb+") as fh:
            size = fh.seek(0, os.SEEK_END)
            if size <= floor:
                return
            fh.seek(size - 1)
            if fh.read(1) == b"\n":
                return
            fh.seek(floor)
            tail = fh.read()
            cut = tail.rfind(b"\n")
            fh.truncate(floor + cut + 1 if cut >= 0 else floor)
            fh.flush()
            os.fsync(fh.fileno())

    def _apply(self, commit: dict) -> dict:
        """Append whatever part of the commit is missing; safe to repeat."""
        added = {}
        for name in ("events", "alerts"):
            path = os.path.join(self.out, commit[name + "_path"])
            floor = commit[name + "_offset"]
            self._trim_torn_tail(path, floor)
            present = set()
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    fh.seek(floor)
                    for raw in fh:
                        try:
                            obj = json.loads(raw)
                        except ValueError:
                            continue
                        if obj.get("commit_key") == commit["key"]:
                            present.add(obj.get("commit_line"))
            missing = [ln for ln in commit[name] if ln["commit_line"] not in present]
            if missing:
                lines = [json.dumps(o, separators=(",", ":")) + "\n" for o in missing]
                with open(path, "a", encoding="utf-8") as fh:
                    if self.crash_hook and len(lines) > 1 and self.crash_hook("tear_" + name):
                        fh.write("".join(lines[: len(lines) // 2]))
                        fh.write(lines[len(lines) // 2][:20])  # torn line
                        fh.flush()
                        raise SimulatedStop("mid_" + name)
                    fh.write("".join(lines))
                    fh.flush()
                    os.fsync(fh.fileno())
            added[name] = len(missing)
        return added

    def _recover(self) -> list:
        done = []
        with self.lock:
            cdir = os.path.join(self.dir, "commits")
            for fn in sorted(os.listdir(cdir)):
                if not fn.endswith(".json"):
                    continue
                commit = self._load(os.path.join(cdir, fn), None)
                if not commit or self._done(commit["channel"], commit["sliceId"]):
                    continue
                pk = self.pkey(commit["channel"], commit["sliceId"])
                if not self._raw_ok(commit):
                    # Missing or hash-invalid original: keep the slice pending (re-download),
                    # never mark it complete. The commit is finished later only with bytes
                    # whose sha256 equals the one recorded at publish time.
                    if pk not in self.pending and isinstance(commit.get("meta"), dict):
                        self.pending[pk] = {"key": commit["channel"], "meta": commit["meta"], "attempts": 0,
                                            "retry_at": 0.0, "enqueued_at": self.now(), "enqueued_ts": self.clock(),
                                            "last_error": None}
                    if pk in self.pending:
                        self.pending[pk]["last_error"] = "raw missing or hash-invalid; awaiting re-download"
                    self.seen.add(pk)
                    done.append({"key": commit["key"], "status": "awaiting_raw", "requeued": pk in self.pending})
                    continue
                added = self._apply(commit)
                _atomic_json(self._key_file("done", commit["channel"], commit["sliceId"]),
                             {"at": self.now(), "recovered": True, "added": added, "raw_intact": True})
                self.pending.pop(pk, None)
                self.seen.add(pk)
                done.append({"key": commit["key"], "status": "completed", "added": added})
        return done

    def _raw_ok(self, commit) -> bool:
        if not commit.get("raw_ref"):
            return True  # rejected slices have no original
        path = os.path.join(self.out, commit["raw_ref"])
        if not os.path.exists(path):
            return False
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest() == commit.get("raw_sha256")

    def _write_raw(self, path, blob, sha) -> None:
        """Complete original before anything is published: .part, fsync, re-hash, rename."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != sha:
                raise OSError("raw write verification failed")
        os.replace(tmp, path)

    def _retry_later(self, pk, p, error, t_req, name, replay):
        with self.lock:
            p["attempts"] += 1
            p["last_error"] = error[:300]
            p["retry_at"] = self.clock() + min(RETRY_MAX, RETRY_MIN * 2 ** (p["attempts"] - 1))
            self._save()
        self.append(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "ok": False, "what": "slice",
                                                           "sliceId": p["meta"].get("sliceId"), "channel": p["key"],
                                                           "file": name, "attempt": p["attempts"],
                                                           "error": p["last_error"], "replay": replay})
        return 0

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
            now = self.clock()
            live = sum(1 for p in self.pending.values() if not self.is_replay(p["meta"]))
            chans = {}
            for key in CHANNELS:
                mine = [p for p in self.pending.values() if p["key"] == key]
                lv = [p for p in mine if not self.is_replay(p["meta"])]
                chans[key] = {**self.channels[key],
                              "pending_live": len(lv), "pending_backfill": len(mine) - len(lv),
                              "oldest_live_pending_s": round(max((now - p.get("enqueued_ts", now) for p in lv), default=0.0), 1),
                              "max_attempts": max((p["attempts"] for p in mine), default=0)}
            return {"polls_ok": self.polls_ok, "polls_err": self.polls_err, "not_modified": self.not_modified,
                    "slices_seen": len(self.seen), "pending_live": live, "pending_backfill": len(self.pending) - live,
                    "last_index_at": self.last_index_at, "channels": chans,
                    "backfill_worker": {"state": self.backfill_state, "error": self.backfill_error}}

    def check_workers(self) -> None:
        """Raise if the replay worker died, so the collector (and the service) stop visibly."""
        if self.backfill_state == "failed":
            raise RuntimeError(f"replay backfill worker failed: {self.backfill_error}")

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
                self.channels[key].update({"listed_in_index": isinstance(lst, list),
                                           "listed_slices": len(lst) if isinstance(lst, list) else 0,
                                           "listing_at": t_recv})
                if not isinstance(lst, list):
                    self.append(events_path, {"source": ch["source"], "kind": "index_channel_missing", "first_seen": t_recv,
                                              "channel": key, "publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"]})
                    continue
                for meta in lst:
                    sid = meta.get("sliceId") if isinstance(meta, dict) else None
                    if type(sid) is not int or self.pkey(key, sid) in self.seen or self._done(key, sid):
                        continue
                    self.pending[self.pkey(key, sid)] = {"key": key, "meta": meta, "attempts": 0, "retry_at": 0.0,
                                         "enqueued_at": t_recv, "enqueued_ts": self.clock(), "last_error": None}
                    self.seen.add(self.pkey(key, sid))
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
            return [pk for pk in sorted(self.pending, key=self._order)
                    if self.is_replay(self.pending[pk]["meta"]) == replay and self.pending[pk]["retry_at"] <= now]

    def drain_live(self, events_path, alerts_path, budget: int = 50) -> int:
        """Fetch due live slices; called on the live loop every pass (200 or 304 alike)."""
        n = 0
        for pk in self._due(replay=False)[:budget]:
            n += self._process(pk, events_path, alerts_path)
        return n

    def drain_backfill(self, events_path, alerts_path, budget: int = 25, pause: float = 0.0, stop=None) -> int:
        n = 0
        for pk in self._due(replay=True)[:budget]:
            if stop is not None and stop.is_set():
                break
            n += self._process(pk, events_path, alerts_path)
            if pause:
                time.sleep(pause)
        return n

    def start_backfill_thread(self, events_path, alerts_path, idle: float = 2.0, pause: float = 0.05):
        stop = threading.Event()
        self.backfill_state, self.backfill_error = "running", None

        def run():
            try:
                while not stop.is_set():
                    if not self.drain_backfill(events_path, alerts_path, pause=pause, stop=stop):
                        stop.wait(idle)
                self.backfill_state = "stopped"
            except BaseException as exc:  # noqa: BLE001 - surfaced through health() and check_workers()
                self.backfill_error = f"{type(exc).__name__}: {exc}"[:500]
                self.backfill_state = "failed"

        t = threading.Thread(target=run, name="credit-tape-backfill", daemon=True)
        t.start()
        return t, stop

    def _process(self, pk, events_path, alerts_path) -> int:
        with self.lock:
            p = self.pending.get(pk)
            if p is None or self._done(p["key"], p["meta"]["sliceId"]):
                self.pending.pop(pk, None)
                return 0
            key, meta = p["key"], p["meta"]
            sid = meta["sliceId"]
        ch = CHANNELS[key]
        url, name = meta.get("fullFilePath") or "", meta.get("fileName") or ""
        u = urlsplit(url)
        restored = p.get("restored")  # restore.py: a saved original re-entering through this same path
        replay = self.is_replay(meta) or bool(restored)  # restored slices are always backfill (no alerts)
        slice_info = {k: meta.get(k) for k in ("fileName", "sliceId", "dissemDTM", "startTs", "endTs", "rowCount")}
        prov = {"publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"], "route": "slice-index"}
        if (u.scheme != "https" or u.hostname != SLICE_HOST or not u.path.startswith(ch["prefix"])
                or not name.endswith(".zip") or "/" in name or u.path.rsplit("/", 1)[-1] != name):
            self._finish(sid, key, events_path, alerts_path, [{"source": ch["source"], "kind": "slice_rejected", "first_seen": self.now(),
                                             "slice": slice_info, "replay": replay, **prov}], [], {"rejected": True})
            return 1
        t_req = self.now()
        try:
            with self.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                blob = r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return self._retry_later(pk, p, repr(exc), t_req, name, replay)
        t_recv = self.now()
        sha = hashlib.sha256(blob).hexdigest()
        prior = self._load(self._key_file("commits", key, sid), None)
        if prior and not self._done(key, sid):
            # A commit exists whose original was missing or hash-invalid at recovery.
            if prior.get("raw_sha256") != sha:
                return self._retry_later(pk, p, "downloaded bytes differ from the committed sha256", t_req, name, replay)
            try:
                self._write_raw(os.path.join(self.out, prior["raw_ref"]), blob, sha)
            except OSError as exc:
                return self._retry_later(pk, p, repr(exc), t_req, name, replay)
            with self.lock:
                added = self._apply(prior)
                _atomic_json(self._key_file("done", key, sid), {"at": self.now(), "recovered": True, "added": added,
                                                                "raw_restored": True})
                self.pending.pop(pk, None)
                self._save()
            return 1
        rel = f"dtcc-slices/raw/{(meta.get('dissemDTM') or t_recv)[:10]}/{name}"
        try:
            self._write_raw(os.path.join(self.out, rel), blob, sha)
        except OSError as exc:  # stays pending; nothing published without an intact original
            return self._retry_later(pk, p, repr(exc), t_req, name, replay)
        if self.crash_hook:
            self.crash_hook("after_raw")
        base = {"first_seen": t_recv, "raw_ref": rel, "slice": slice_info, "replay": replay,
                "attempts": p["attempts"] + 1, **prov, **({"restored": restored} if restored else {})}
        try:
            rows = decode_rows(blob)
        except Exception as exc:
            ev = {"source": ch["source"], "kind": "decode_failure", "error": repr(exc)[:300], **base}
            self._finish(sid, key, events_path, alerts_path, [ev], [ev], {"decode_failure": True}, rel, sha)
            return 1
        match = len(rows) == meta.get("rowCount")
        evs = [{"source": ch["source"], "kind": "slice", "requested_at": t_req,
                "raw_sha256": sha, "decoded_rows": len(rows), "rowcount_match": match, **base}]
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
        self._finish(sid, key, events_path, alerts_path, evs, alerts, {"rows": len(rows)}, rel, sha)
        return 1

    def _finish(self, sid, channel, events_path, alerts_path, evs, alerts, info, raw_ref=None, raw_sha256=None):
        key = self.commit_key(channel, sid)
        with self.lock:
            if self._done(channel, sid):  # another drain finished it first
                self.pending.pop(self.pkey(channel, sid), None)
                return
            stamp = self.now()
            ev_lines = [{**e, "commit_key": key, "commit_line": i} for i, e in enumerate(evs)]
            al_lines = [{**a, "alert_written": stamp, "commit_key": key, "commit_line": i} for i, a in enumerate(alerts)]
            sizes = {}
            for name, path in (("events", events_path), ("alerts", alerts_path)):
                sizes[name] = os.path.getsize(path) if os.path.exists(path) else 0
            pend = self.pending.get(self.pkey(channel, sid)) or {}
            commit = {"key": key, "channel": channel, "sliceId": sid, "source": CHANNELS[channel]["source"],
                      "meta": pend.get("meta"),
                      "raw_ref": raw_ref, "raw_sha256": raw_sha256, "committed_at": stamp,
                      "events_path": os.path.relpath(events_path, self.out), "events_offset": sizes["events"],
                      "alerts_path": os.path.relpath(alerts_path, self.out), "alerts_offset": sizes["alerts"],
                      "events": ev_lines, "alerts": al_lines}
            _atomic_json(self._key_file("commits", channel, sid), commit)
            if self.crash_hook:
                self.crash_hook("after_commit_record")
            self._apply(commit)
            if self.crash_hook:
                self.crash_hook("after_output")  # Chej's case: output written, completion not yet recorded
            done_at = self.now()
            _atomic_json(self._key_file("done", channel, sid), {"at": done_at, **info})
            self.pending.pop(self.pkey(channel, sid), None)
            meta = (evs[0].get("slice") if evs else None) or {}
            which = "last_backfill_completed" if evs and evs[0].get("replay") else "last_live_completed"
            self.channels[channel][which] = {"sliceId": sid, "dissemDTM": meta.get("dissemDTM"), "at": done_at,
                                             "outcome": next(iter(info), "rows")}
            self._save()
