"""DTCC public slice-index route for the credit tape (OA-242).

The PPD dashboard publishes a public index (Slice.json) listing every intraday
slice file per channel with sliceId, dissemDTM, startTs/endTs and rowCount.
For CFTC_CR and SEC_CR we fetch every unseen slice, persist its original
bytes before decoding, emit one event per decoded row and compare the decoded
row count with the published rowCount. The index is fetched conditionally
(If-None-Match) with exponential backoff on errors.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
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


class SliceRoute:
    def __init__(self, out: str, now, append_jsonl, opener=urllib.request.urlopen, sleep=time.sleep):
        self.out, self.now, self.append, self.open, self.sleep = out, now, append_jsonl, opener, sleep
        self.dir = os.path.join(out, "dtcc-slices")
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "checkpoint.json")
        st = {}
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        self.seen: set[int] = set(st.get("seen_slice_ids", []))
        self.etag: str | None = st.get("index_etag")
        self.started: str = st.get("started_at") or now()
        self.polls_ok, self.polls_err, self.not_modified = st.get("polls_ok", 0), st.get("polls_err", 0), st.get("not_modified", 0)
        self.backoff_until = 0.0
        self.backoff = 0.0
        self.last_index_at = st.get("last_index_at")

    # -- state ---------------------------------------------------------------
    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"seen_slice_ids": sorted(self.seen)[-50000:], "index_etag": self.etag,
                       "started_at": self.started, "polls_ok": self.polls_ok, "polls_err": self.polls_err,
                       "not_modified": self.not_modified, "last_index_at": self.last_index_at}, fh)
        os.replace(tmp, self.state_path)

    def health(self):
        return {"polls_ok": self.polls_ok, "polls_err": self.polls_err, "not_modified": self.not_modified,
                "slices_seen": len(self.seen), "last_index_at": self.last_index_at}

    def _fail(self, what, exc):
        self.polls_err += 1
        self.backoff = min(300.0, max(5.0, self.backoff * 2))
        self.backoff_until = time.monotonic() + self.backoff
        self.append(os.path.join(self.dir, "polls.jsonl"), {"t": self.now(), "ok": False, "what": what,
                                                           "error": repr(exc)[:300], "backoff_s": self.backoff})

    # -- polling -------------------------------------------------------------
    def poll(self, events_path: str, alerts_path: str) -> None:
        if time.monotonic() < self.backoff_until:
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
                self.not_modified += 1
                self.last_index_at = t_req
                self.append(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "ok": True, "http": 304})
                self.save()
                return
            return self._fail("index", exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return self._fail("index", exc)
        t_recv = self.now()
        self.polls_ok += 1
        self.backoff = 0.0
        try:
            index = json.loads(body)
        except ValueError as exc:
            return self._fail("index_decode", exc)
        self.etag, self.last_index_at = etag, t_recv
        # The full index is ~3.6 MB and changes every minute; keep the credit entries verbatim per change.
        day = t_recv[:10]
        os.makedirs(os.path.join(self.dir, "index", day), exist_ok=True)
        cr = {k: index.get(k) for k in CHANNELS}
        with open(os.path.join(self.dir, "index", day, f"{t_recv.replace(':', '')}_{(etag or '').strip(chr(34))[:12]}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": t_recv, "etag": etag, "last_modified": lastmod,
                       "index_sha256": hashlib.sha256(body).hexdigest(), "entries": cr}, fh)
        self.append(os.path.join(self.dir, "polls.jsonl"), {"t": t_req, "recv": t_recv, "ok": True, "http": 200,
                                                           "etag": etag, "last_modified": lastmod, "bytes": len(body)})
        for key, ch in CHANNELS.items():
            entries = index.get(key)
            if not isinstance(entries, list):
                self.append(events_path, {"source": ch["source"], "kind": "index_channel_missing", "first_seen": t_recv,
                                          "channel": key, "publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"]})
                continue
            for meta in sorted((e for e in entries if isinstance(e, dict)), key=lambda e: e.get("sliceId") or 0):
                sid = meta.get("sliceId")
                if type(sid) is not int or sid in self.seen:
                    continue
                if not self._fetch_slice(key, ch, meta, events_path, alerts_path):
                    break  # retry the remainder next poll after backoff, preserving order
        self.save()

    def _fetch_slice(self, key, ch, meta, events_path, alerts_path) -> bool:
        url = meta.get("fullFilePath") or ""
        u = urlsplit(url)
        name = (meta.get("fileName") or "")
        if (u.scheme != "https" or u.hostname != SLICE_HOST or not u.path.startswith(ch["prefix"])
                or not name.endswith(".zip") or "/" in name or u.path.rsplit("/", 1)[-1] != name):
            self.seen.add(meta["sliceId"])
            self.append(events_path, {"source": ch["source"], "kind": "slice_rejected", "first_seen": self.now(),
                                      "slice": meta, "publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"]})
            return True
        t_req = self.now()
        try:
            with self.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                blob = r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._fail("slice " + name, exc)
            return False
        t_recv = self.now()
        day = (meta.get("dissemDTM") or t_recv)[:10]
        rel = f"dtcc-slices/raw/{day}/{name}"
        path = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:  # original bytes before decoding
            fh.write(blob)
        self.seen.add(meta["sliceId"])
        replay = bool(meta.get("dissemDTM")) and meta["dissemDTM"] < self.started[:19] + "Z"
        prov = {"publisher": PUBLISHER, "jurisdiction": ch["jurisdiction"], "route": "slice-index"}
        slice_info = {k: meta.get(k) for k in ("fileName", "sliceId", "dissemDTM", "startTs", "endTs", "rowCount")}
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                members = z.namelist()
                text = z.read(members[0]).decode("utf-8")
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception as exc:
            ev = {"source": ch["source"], "kind": "decode_failure", "first_seen": t_recv, "raw_ref": rel,
                  "slice": slice_info, "replay": replay, "error": repr(exc)[:300], **prov}
            self.append(events_path, ev)
            self.append(alerts_path, {**ev, "alert_written": self.now()})
            return True
        match = len(rows) == meta.get("rowCount")
        self.append(events_path, {"source": ch["source"], "kind": "slice", "first_seen": t_recv, "requested_at": t_req,
                                  "raw_ref": rel, "raw_sha256": hashlib.sha256(blob).hexdigest(), "slice": slice_info,
                                  "decoded_rows": len(rows), "rowcount_match": match, "replay": replay, **prov})
        if not match:
            ev = {"source": ch["source"], "kind": "rowcount_mismatch", "first_seen": t_recv, "raw_ref": rel,
                  "slice": slice_info, "decoded_rows": len(rows), "replay": replay, **prov}
            self.append(events_path, ev)
            self.append(alerts_path, {**ev, "alert_written": self.now()})
        for idx, row in enumerate(rows):
            ev = {"source": ch["source"], "kind": "publication", "id": row.get("Dissemination Identifier"),
                  "original_id": row.get("Original Dissemination Identifier") or None,
                  "action": row.get("Action type"), "execution_ts": row.get("Execution Timestamp"),
                  "event_ts": row.get("Event timestamp"), "dissemination_ts": meta.get("dissemDTM"),
                  "first_seen": t_recv, "raw_ref": rel, "row_index": idx, "slice": slice_info,
                  "replay": replay, "backfill": replay, **prov, "record": row}
            self.append(events_path, ev)
            if not replay:
                self.append(alerts_path, {k: v for k, v in ev.items() if k != "record"} | {"alert_written": self.now()})
        return True
