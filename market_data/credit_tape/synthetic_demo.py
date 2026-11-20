"""OA-242 runnable local demo: slice-only recent-trades panel (no network).

Public slices are the only publication source in this run (no ticker). A
CFTC and an SEC fixture slice carry the SAME numeric sliceId; each holds one
synthetic trade. The run:

  1. publishes both from a synthetic Slice.json, stopping the process after the
     SEC lines are written but before its done marker;
  2. restarts (recovery finishes the SEC commit) and polls twice (index 304);
  3. restarts again and polls once more;
  4. starts the real panel server (dev-local, loopback, ephemeral port) and
     checks over HTTP: two publication rows, one per channel, same sliceId,
     each linked to its own original ZIP, byte-identical to what was published.

Usage:
  python -m market_data.credit_tape.synthetic_demo --out <new empty dir> [--serve]
With --serve the panel then stays up at http://127.0.0.1:8790 (PORT to change)
on the demo archive until Ctrl+C. Exit 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from market_data.credit_tape import capture, slices
from market_data.credit_tape import serve_private as sp
from market_data.credit_tape.synthetic_resilience import START, SyntheticDTCC

SHARED_ID = 900000000000000101  # synthetic; same number on both channels
FIELDS = ["Dissemination Identifier", "Action type", "Execution Timestamp", "Notional amount-Leg 1",
          "Notional currency-Leg 1", "Underlier ID-Leg 1", "UPI FISN", "Expiration Date"]


def fixture(agency: str):
    name = f"{agency.upper()}_SLICE_CREDITS_2999_01_01_1.zip"
    row = {"Dissemination Identifier": f"SYN-{agency.upper()}-0001", "Action type": "NEWT",
           "Execution Timestamp": "2999-01-01T00:00:00Z", "Notional amount-Leg 1": "5,000,000+",
           "Notional currency-Leg 1": "USD", "Underlier ID-Leg 1": f"SYNTHETIC-{agency.upper()}-REF",
           "UPI FISN": "SYNTHETIC/CDS", "Expiration Date": "3004-01-01"}
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerow(row)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name.replace(".zip", ".csv"), buf.getvalue())
    meta = {"sliceId": SHARED_ID, "fileName": name, "rowCount": 1, "dissemDTM": "2999-01-01T00:00:01Z",
            "startTs": "2999-01-01T00:00:00Z", "endTs": "2999-01-01T00:00:01Z",
            "fullFilePath": f"https://{slices.SLICE_HOST}/{agency}/slices/{name}"}
    return meta, out.getvalue()


def _route(out, dtcc):
    r = slices.SliceRoute(out, capture.now, capture.append_jsonl, opener=dtcc, lock=capture.WRITE_LOCK)
    r.started = START + "Z"  # fixtures are disseminated after this, so they are live (alerting) rows
    return r


def _health(out, route):
    with open(os.path.join(out, "health.json"), "w", encoding="utf-8") as fh:
        json.dump({"t": capture.now(), "sources": {"dtcc-slice-index": route.health()}}, fh)


def _get(port, path):
    c = HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def run(out: str) -> dict:
    events, alerts = os.path.join(out, "events.jsonl"), os.path.join(out, "alerts_outbox.jsonl")
    dtcc = SyntheticDTCC()
    published = {}
    for agency, key in (("cftc", "CFTC_CR"), ("sec", "SEC_CR")):
        meta, blob = fixture(agency)
        dtcc.files[meta["fullFilePath"]] = blob
        dtcc.index[key] = [meta]
        published[f"dtcc-{agency}-slices"] = {"fileName": meta["fileName"], "sha256": hashlib.sha256(blob).hexdigest(),
                                              "bytes": blob}
    steps = []

    # 1. First run: stop after the SEC output, before its done marker.
    r1 = _route(out, dtcc)
    calls = {"n": 0}

    def hook(stage):
        if stage == "after_output":
            calls["n"] += 1
            if calls["n"] == 2:  # CFTC finished first (same id, sorted by channel); stop inside SEC
                raise slices.SimulatedStop("sec_after_output")
        return False

    r1.crash_hook = hook
    r1.poll_index(events)
    steps.append({"step": "index 200, both channels enqueued", "pending": sorted(r1.pending)})
    try:
        r1.drain_live(events, alerts)
        stopped = None
    except slices.SimulatedStop as exc:
        stopped = str(exc)
    steps.append({"step": "process stopped", "at": stopped,
                  "done_markers": {k: r1._done(k, SHARED_ID) for k in slices.CHANNELS}})
    del r1

    # 2. Restart: recovery, then two passes on an unchanged (304) index.
    r2 = _route(out, dtcc)
    steps.append({"step": "restart 1", "recovered": r2.recovered})
    for _ in range(2):
        r2.poll_index(events)
        r2.drain_live(events, alerts)
    steps.append({"step": "2 passes", "index_not_modified": r2.not_modified, "pending": sorted(r2.pending)})
    del r2

    # 3. Second restart, one more pass.
    r3 = _route(out, dtcc)
    r3.poll_index(events)
    r3.drain_live(events, alerts)
    _health(out, r3)
    steps.append({"step": "restart 2 + 1 pass", "recovered": r3.recovered, "pending": sorted(r3.pending)})

    # 4. Real panel server (dev-local) over HTTP.
    settings = sp.Settings.from_env({"CREDIT_TAPE_ARCHIVE": str(Path(out).resolve()), "CREDIT_TAPE_DEV_LOCAL": "1"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), sp.make_handler(settings))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_port
        st, body = _get(port, "/api/trades?after=0")
        rows = json.loads(body)["rows"] if st == 200 else []
        pubs = [e for e in rows if e.get("kind") == "publication"]
        panel_rows, raw_checks = [], []
        for e in pubs:
            rs, rb = _get(port, "/api/raw?ref=" + quote(e["raw_ref"], safe=""))
            want = published.get(e["source"], {})
            ok = rs == 200 and hashlib.sha256(rb).hexdigest() == want.get("sha256") and rb == want.get("bytes")
            raw_checks.append({"source": e["source"], "raw_ref": e["raw_ref"], "http": rs, "matches_published_zip": ok})
            panel_rows.append({"seq": e["seq"], "source": e["source"], "jurisdiction": e["jurisdiction"],
                               "id": e["id"], "sliceId": e["slice"]["sliceId"], "file": e["slice"]["fileName"],
                               "raw_ref": e["raw_ref"], "notional": e["summary"].get("notionalAmountLeg1"),
                               "live": not e["replay"]})
    finally:
        srv.shutdown()
        srv.server_close()

    al = [json.loads(l) for l in open(alerts, encoding="utf-8")]
    al_pubs = [a for a in al if a["kind"] == "publication"]
    slice_downloads = [(f, w) for _, f, w in dtcc.log if f.endswith(".zip")]
    checks = {
        "trades_api_http_200": st == 200,
        "two_publication_rows": len(pubs) == 2,
        "one_row_per_channel": sorted(p["source"] for p in panel_rows) == ["dtcc-cftc-slices", "dtcc-sec-slices"],
        "same_numeric_slice_id": {p["sliceId"] for p in panel_rows} == {SHARED_ID},
        "distinct_raw_refs": len({p["raw_ref"] for p in panel_rows}) == 2,
        "each_row_links_its_own_original_zip": len(raw_checks) == 2 and all(c["matches_published_zip"] for c in raw_checks),
        "no_duplicate_after_restarts": len(pubs) == 2 and len(al_pubs) == 2,
        "slices_only_source": all(e["source"].endswith("-slices") for e in rows if e.get("kind") == "publication")
                              and not any(e.get("source", "").endswith("-credits") for e in rows),
        "each_zip_downloaded_once": sorted(slice_downloads) == sorted((v["fileName"], "200") for v in published.values()),
        "nothing_pending": not r3.pending,
    }
    return {"steps": steps, "panel_rows": panel_rows, "raw_checks": raw_checks,
            "alerts_outbox_publications": len(al_pubs), "slice_downloads": slice_downloads,
            "checks": checks, "all_pass": all(checks.values())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="new or empty directory for the demo archive (default: temporary)")
    ap.add_argument("--serve", action="store_true", help="keep the panel up on the demo archive afterwards")
    a = ap.parse_args()
    if a.serve and not a.out:
        ap.error("--serve needs --out")
    if a.out:
        if os.path.exists(a.out) and os.listdir(a.out):
            ap.error("--out must be a new or empty directory")
        os.makedirs(a.out, exist_ok=True)
        result = run(a.out)
    else:
        with tempfile.TemporaryDirectory() as d:
            result = run(d)
    print(json.dumps(result, indent=1))
    if not result["all_pass"]:
        return 1
    if a.serve:
        os.environ.update({"CREDIT_TAPE_ARCHIVE": str(Path(a.out).resolve()), "CREDIT_TAPE_DEV_LOCAL": "1"})
        sp.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
