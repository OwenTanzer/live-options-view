"""OA-242 synthetic resilience run (no network, synthetic fixtures only).

One run, two claims:
  1. Live polling keeps going while a replay backfill is in progress.
  2. A live slice whose download fails survives a restart and is recovered
     exactly once while the index keeps answering 304 (unchanged).

Usage: python -m market_data.credit_tape.synthetic_resilience [--out DIR]
Prints a JSON timeline and verdicts; exit 0 only if both claims hold.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import zipfile

from market_data.credit_tape import capture, slices

START = "2099-01-01T12:00:00"


def _zip(ids):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["Dissemination Identifier", "Action type", "Notional amount-Leg 1"])
    w.writeheader()
    for i in ids:
        w.writerow({"Dissemination Identifier": i, "Action type": "NEWT", "Notional amount-Leg 1": "5,000,000+"})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("SYNTHETIC.csv", buf.getvalue())
    return out.getvalue()


def _meta(agency, n, sid, dissem):
    name = f"{agency.upper()}_SLICE_CREDITS_2099_01_01_{n}.zip"
    return {"sliceId": sid, "fileName": name, "rowCount": 1, "dissemDTM": dissem,
            "startTs": dissem, "endTs": dissem, "fullFilePath": f"https://{slices.SLICE_HOST}/{agency}/slices/{name}"}


class _Resp(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SyntheticDTCC:
    """Stands in for the S3 index and slice files. Thread-safe request log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.index = {"CFTC_CR": [], "SEC_CR": []}
        self.etag = '"v1"'
        self.files: dict[str, bytes] = {}
        self.fail: set[str] = set()
        self.block: dict[str, threading.Event] = {}
        self.log: list[tuple[float, str, str]] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url == slices.INDEX_URL:
            if req.get_header("If-none-match") == self.etag:
                self._rec(url, "304")
                raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
            self._rec(url, "200")
            return _Resp(json.dumps(self.index).encode(), {"ETag": self.etag, "Last-Modified": "synthetic"})
        if url in self.block:
            self._rec(url, "blocked")
            self.block[url].wait(10)
        if url in self.fail:
            self._rec(url, "fail")
            raise urllib.error.URLError("synthetic outage")
        self._rec(url, "200")
        return _Resp(self.files[url], {})

    def _rec(self, url, what):
        with self.lock:
            self.log.append((time.monotonic(), url.rsplit("/", 1)[-1], what))


def run(out: str) -> dict:
    events, alerts = os.path.join(out, "events.jsonl"), os.path.join(out, "alerts_outbox.jsonl")
    clock = {"t": 1000.0}
    now_iso = lambda: capture.now()  # real wall time for receipts
    dtcc = SyntheticDTCC()
    timeline = []
    note = lambda what, **kw: timeline.append({"step": len(timeline) + 1, "what": what, **kw})

    # Replay backlog: 3 CFTC slices disseminated before the route started.
    replay = [_meta("cftc", n, 100 + n, f"2000-01-01T00:00:0{n}Z") for n in (1, 2, 3)]
    for m in replay:
        dtcc.files[m["fullFilePath"]] = _zip([f"SYN-REPLAY-{m['sliceId']}"])
    # Live slices: SEC #1 will fail first, CFTC #4 arrives during the backfill.
    sec_live = _meta("sec", 1, 201, "2999-01-01T00:00:01Z")
    cftc_live = _meta("cftc", 4, 104, "2999-01-01T00:00:02Z")
    for m in (sec_live, cftc_live):
        dtcc.files[m["fullFilePath"]] = _zip([f"SYN-LIVE-{m['sliceId']}"])

    route = slices.SliceRoute(out, now_iso, capture.append_jsonl, opener=dtcc, clock=lambda: clock["t"],
                              lock=capture.WRITE_LOCK)
    route.started = START + "Z"

    # --- Claim 1: live loop keeps moving while backfill is blocked mid-slice.
    dtcc.index = {"CFTC_CR": replay, "SEC_CR": [sec_live]}
    dtcc.fail.add(sec_live["fullFilePath"])
    dtcc.block[replay[0]["fullFilePath"]] = gate = threading.Event()
    route.poll_index(events)
    note("index v1 (200): enqueued 3 replay + 1 live SEC", health=route.health())
    t, stop = route.start_backfill_thread(events, alerts, idle=0.05, pause=0.0)
    deadline = time.monotonic() + 5
    while not any(w == "blocked" for _, _, w in dtcc.log) and time.monotonic() < deadline:
        time.sleep(0.01)
    note("backfill thread is inside replay slice #1 (blocked)")
    live_polls = 0
    for _ in range(3):
        route.poll_index(events)
        route.drain_live(events, alerts)
        live_polls += 1
    note("3 live passes ran while backfill was blocked; SEC live download failed (synthetic outage)",
         sec_attempts=route.pending["SEC_CR:201"]["attempts"], health=route.health())
    dtcc.index = {"CFTC_CR": replay + [cftc_live], "SEC_CR": [sec_live]}
    dtcc.etag = '"v2"'
    route.poll_index(events)
    route.drain_live(events, alerts)
    live_polls += 1
    cftc_live_done = route._done("CFTC_CR", 104)
    replay_done_while_blocked = sum(route._done("CFTC_CR", m["sliceId"]) for m in replay)
    note("index v2 (200): live CFTC #4 fetched while backfill still blocked",
         cftc_live_done=cftc_live_done, replay_done=replay_done_while_blocked)
    gate.set()
    deadline = time.monotonic() + 5
    while route.health()["pending_backfill"] and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(2)
    note("backfill released and finished", health=route.health())

    # --- Claim 2: restart, index unchanged (304), failed SEC slice recovered exactly once.
    del route
    dtcc.fail.clear()
    restarted = slices.SliceRoute(out, now_iso, capture.append_jsonl, opener=dtcc, clock=lambda: clock["t"],
                                  lock=capture.WRITE_LOCK)
    note("restart: pending reloaded from pending.json", pending=sorted(restarted.pending), etag=restarted.etag)
    restarted.poll_index(events)  # same ETag -> 304
    restarted.drain_live(events, alerts)
    note("pass before retry time: index 304, SEC not yet due", not_modified=restarted.not_modified,
         sec_pending="SEC_CR:201" in restarted.pending)
    clock["t"] += slices.RETRY_MAX + 1
    for _ in range(3):
        restarted.poll_index(events)
        restarted.drain_live(events, alerts)
    note("3 passes after retry time, index 304 each time", not_modified=restarted.not_modified,
         sec_pending="SEC_CR:201" in restarted.pending)

    evs = [json.loads(l) for l in open(events, encoding="utf-8")]
    pubs = [e for e in evs if e["kind"] == "publication"]
    count = lambda sid: sum(1 for e in pubs if e["slice"]["sliceId"] == sid)
    sec_fetches = [w for _, f, w in dtcc.log if f == sec_live["fileName"]]
    index_304 = sum(1 for _, f, w in dtcc.log if f == "Slice.json" and w == "304")
    verdicts = {
        "live_polling_during_backfill": live_polls == 4 and cftc_live_done and replay_done_while_blocked == 0,
        "failed_slice_recovered_exactly_once_on_304": (
            count(201) == 1 and "SEC_CR:201" not in restarted.pending and index_304 >= 4 and sec_fetches.count("200") == 1),
        "replays_emitted_once_each": all(count(m["sliceId"]) == 1 for m in replay),
        "live_cftc_emitted_once": count(104) == 1,
    }
    return {"timeline": timeline, "sec_slice_requests": sec_fetches, "index_304_responses": index_304,
            "publications": [{"sliceId": e["slice"]["sliceId"], "id": e["id"], "replay": e["replay"],
                              "attempts": e["attempts"]} for e in pubs],
            "verdicts": verdicts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        result = run(a.out)
    else:
        with tempfile.TemporaryDirectory() as d:
            result = run(d)
    print(json.dumps(result, indent=1))
    return 0 if all(result["verdicts"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
