"""OA-242 synthetic crash-safety run for slice publishing (no network).

For each stop point, one live slice with one row is published, the process is
"stopped" at that point (SimulatedStop), and a fresh SliceRoute is created on
the same directory (restart). Then the index keeps answering 304 and the live
loop runs twice more. Expected outcome at every stop point: one intact
original ZIP (hash equal to the published bytes, no .part leftovers), one
publication event, one slice event, one alert, no torn lines, nothing pending.

Usage: python -m market_data.credit_tape.synthetic_crash
Prints JSON results; exit 0 only if every stop point passes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

from market_data.credit_tape import capture, slices
from market_data.credit_tape.synthetic_resilience import START, SyntheticDTCC, _meta, _zip

STOP_POINTS = ["after_raw", "after_commit_record", "tear_events", "after_output"]


def _hook(stop_at):
    state = {"fired": False}

    def hook(stage):
        if state["fired"]:
            return False
        if stage == stop_at:
            state["fired"] = True
            if stage.startswith("tear_"):
                return True  # slices._apply writes half the lines plus a torn one, then stops
            raise slices.SimulatedStop(stage)
        return False

    return hook, state


def run_one(out: str, stop_at: str) -> dict:
    events, alerts = os.path.join(out, "events.jsonl"), os.path.join(out, "alerts_outbox.jsonl")
    # A ticker line already in the log, to show recovery never touches other writers' lines.
    capture.append_jsonl(events, {"source": "dtcc-cftc-credits", "kind": "publication", "id": "SYN-TICKER-1"})
    dtcc = SyntheticDTCC()
    m = _meta("cftc", 7, 707, "2999-01-01T00:00:07Z")
    blob = _zip(["SYN-LIVE-707"])
    dtcc.files[m["fullFilePath"]] = blob
    dtcc.index = {"CFTC_CR": [m], "SEC_CR": []}

    route = slices.SliceRoute(out, capture.now, capture.append_jsonl, opener=dtcc, lock=capture.WRITE_LOCK)
    route.started = START + "Z"
    route.crash_hook, fired = _hook(stop_at)
    route.poll_index(events)
    stopped = None
    try:
        route.drain_live(events, alerts)
    except slices.SimulatedStop as exc:
        stopped = str(exc)
    del route  # process gone

    torn_before = _torn(events) or _torn(alerts)
    restarted = slices.SliceRoute(out, capture.now, capture.append_jsonl, opener=dtcc, lock=capture.WRITE_LOCK)
    recovered = restarted.recovered
    for _ in range(2):
        restarted.poll_index(events)  # unchanged index -> 304
        restarted.drain_live(events, alerts)

    ev = [json.loads(l) for l in open(events, encoding="utf-8")]
    al = [json.loads(l) for l in open(alerts, encoding="utf-8")] if os.path.exists(alerts) else []
    raw_path = os.path.join(out, "dtcc-slices", "raw", "2999-01-01", m["fileName"])
    raw_ok = os.path.exists(raw_path) and hashlib.sha256(open(raw_path, "rb").read()).hexdigest() == hashlib.sha256(blob).hexdigest()
    parts = [f for _, _, fs in os.walk(out) for f in fs if f.endswith(".part")]
    counts = {
        "publication_events": sum(1 for e in ev if e.get("kind") == "publication" and e.get("source") == "dtcc-cftc-slices"),
        "slice_events": sum(1 for e in ev if e.get("kind") == "slice"),
        "alerts": len(al),
        "ticker_lines_kept": sum(1 for e in ev if e.get("id") == "SYN-TICKER-1"),
    }
    fetches = [w for _, f, w in dtcc.log if f == m["fileName"]]
    ok = (stopped == stop_at.replace("tear_", "mid_") and raw_ok and not parts and not _torn(events) and not _torn(alerts)
          and counts == {"publication_events": 1, "slice_events": 1, "alerts": 1, "ticker_lines_kept": 1}
          and restarted._done("CFTC_CR", 707) and not restarted.pending)
    return {"stop_at": stop_at, "stopped": stopped, "torn_line_present_at_stop": torn_before,
            "recovered_on_restart": recovered, "slice_downloads": fetches, "raw_intact": raw_ok,
            "part_files": parts, "counts": counts, "pending_after": sorted(restarted.pending), "pass": ok}


def _torn(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.endswith(b"\n"):
        return True
    for line in data.splitlines():
        try:
            json.loads(line)
        except ValueError:
            return True
    return False


def run() -> dict:
    results = []
    for stop_at in STOP_POINTS:
        with tempfile.TemporaryDirectory() as d:
            results.append(run_one(d, stop_at))
    return {"results": results, "all_pass": all(r["pass"] for r in results)}


def main():
    result = run()
    print(json.dumps(result, indent=1))
    return 0 if result["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
