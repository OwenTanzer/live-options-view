"""OA-242 offline tape-restoration demo (synthetic fixtures, disposable archive, no network).

Scenario (CFTC_CR, completed day 2001-02-03):
  1. The collector sees slices #1-#3 in the index; #2's live download keeps failing, so the
     tape misses it (it stays pending). #4 is never listed in any index the tape saw.
  2. Reconciliation saves #2 and #4 as report-only ZIPs (offline fake endpoint), recording
     their sha256. #2 holds a legitimately repeated row (R twice) and a correction (C of A).
  3. restore: a tampered copy of #2 -> unresolved (sha256 mismatch); #4 -> unresolved (no
     saved index entry, so its identity is unknown); #2 -> restored via the commit path.
  4. Rerun of restore, a restart, and a later replay of #2 (index changed, S3 now answers):
     nothing is published twice.
  5. Panel (real handler, dev-local): #2's rows show as backfill/restored, each linked to an
     original ZIP byte-identical to the saved one. Strict archive check is clean.
     Reconciliation now matches #2's rows; #4's row stays cumulative_only.

Usage: python -m market_data.credit_tape.synthetic_restore
Prints JSON evidence; exit 0 only if every check holds.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from market_data.credit_tape import capture, check_archive, reconcile, restore, slices
from market_data.credit_tape import serve_private as sp
from market_data.credit_tape.synthetic_resilience import SyntheticDTCC, _Resp

DAY, TAG = "2001-02-03", "2001_02_03"
FIELDS = ["Dissemination Identifier", "Original Dissemination Identifier", "Action type", "Notional amount-Leg 1"]


def row(did, action="NEWT", orig=""):
    return {"Dissemination Identifier": did, "Original Dissemination Identifier": orig, "Action type": action,
            "Notional amount-Leg 1": "5,000,000+"}


def zipped(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("X.csv", buf.getvalue())
    return out.getvalue()


SLICES = {1: [row("A"), row("B")],
          2: [row("R"), row("R"), row("C", "CORR", "A")],  # legitimate repeat + a correction
          3: [row("D")],
          4: [row("E")]}                                    # never listed in an index the tape saw


def meta(n):
    name = f"CFTC_SLICE_CREDITS_{TAG}_{n}.zip"
    return {"sliceId": 9100 + n, "fileName": name, "rowCount": len(SLICES[n]), "dissemDTM": f"{DAY}T00:0{n}:10Z",
            "startTs": f"{DAY}T00:0{n}:00Z", "endTs": f"{DAY}T00:0{n}:09Z",
            "fullFilePath": f"https://{slices.SLICE_HOST}/cftc/slices/{name}"}


class OfflinePPD:
    """Stands in for the numbered intraday + cumulative endpoints; never touches the network."""

    def __init__(self):
        self.files = {f"/intraday/cftc/CFTC_SLICE_CREDITS_{TAG}_{n}.zip": zipped(r) for n, r in SLICES.items()}
        self.files[f"/cumulative/cftc/CFTC_CUMULATIVE_CREDITS_{TAG}.zip"] = zipped(
            [r for n in sorted(SLICES) for r in SLICES[n]])
        self.requests = []

    def __call__(self, req, timeout=None):
        path = req.full_url.split("/ppd/api/report", 1)[1]
        self.requests.append(path)
        if path not in self.files:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return _Resp(self.files[path], {})


def pubs(archive, n):
    name = f"CFTC_SLICE_CREDITS_{TAG}_{n}.zip"
    with open(os.path.join(archive, "events.jsonl"), encoding="utf-8") as fh:
        evs = [json.loads(l) for l in fh]
    return [e for e in evs if e.get("kind") == "publication" and (e.get("slice") or {}).get("fileName") == name]


def run(archive: str) -> dict:
    ev = {}
    dtcc = SyntheticDTCC()
    for n in (1, 2, 3):
        dtcc.files[meta(n)["fullFilePath"]] = zipped(SLICES[n])
    dtcc.index = {"CFTC_CR": [meta(n) for n in (1, 2, 3)], "SEC_CR": []}
    dtcc.fail.add(meta(2)["fullFilePath"])  # the tape misses #2
    capture.run(archive, ticker=False, slices=True, once=True, opener=dtcc)
    ev["1_capture"] = {"published_per_slice": {n: len(pubs(archive, n)) for n in (1, 2, 3)},
                       "slice2_pending": "CFTC_CR:9102" in json.load(open(os.path.join(archive, "dtcc-slices", "pending.json")))}

    ppd = OfflinePPD()
    rec1 = reconcile.run(archive, DAY, ["CFTC_CR"], recover_gaps=True, fetch_cumulative=True, opener=ppd)
    cov = rec1["channels"]["CFTC_CR"]["slice_coverage"]
    ro = rec1["channels"]["CFTC_CR"]["report_only"]
    ro_dir = Path(archive) / "dtcc-reconcile" / DAY / "report-only-slices"
    ev["2_report_only"] = {"uncaptured": cov["uncaptured_numbers"], "saved": [s["number"] for s in ro["slices"]],
                           "verdict_before": rec1["channels"]["CFTC_CR"]["verdict"]["tape_parity"]}

    saved2 = ro_dir / f"CFTC_SLICE_CREDITS_{TAG}_2.zip"
    tampered = Path(archive).parent / "tampered" / saved2.name
    tampered.parent.mkdir(exist_ok=True)
    tampered.write_bytes(zipped([row("R"), row("R"), row("C", "CORR", "Z")]))  # same name/rowCount, other bytes
    r_tampered = restore.restore(archive, "CFTC_CR", str(tampered))
    r_four = restore.restore(archive, "CFTC_CR", str(ro_dir / f"CFTC_SLICE_CREDITS_{TAG}_4.zip"))
    r_two = restore.restore(archive, "CFTC_CR", str(saved2))
    ev["3_restore"] = {"tampered": [r_tampered["status"], r_tampered["reasons"]],
                       "slice4": [r_four["status"], r_four["reasons"]],
                       "slice2": {k: r_two.get(k) for k in ("status", "commit_key", "rows", "identity", "staged", "network_requests")}}
    after_restore = len(pubs(archive, 2))

    r_again = restore.restore(archive, "CFTC_CR", str(saved2))  # rerun
    restarted = slices.SliceRoute(archive, capture.now, capture.append_jsonl, opener=dtcc, lock=capture.WRITE_LOCK)
    restarted.poll_index(os.path.join(archive, "events.jsonl"))
    restarted.drain_live(os.path.join(archive, "events.jsonl"), os.path.join(archive, "alerts_outbox.jsonl"))
    restarted.drain_backfill(os.path.join(archive, "events.jsonl"), os.path.join(archive, "alerts_outbox.jsonl"))
    del restarted
    dtcc.fail.clear()  # later replay: #2 is listed again and S3 now answers
    dtcc.etag = '"replayed-index"'
    s3_fetches_before = sum(1 for _, f, w in dtcc.log if f == meta(2)["fileName"] and w == "200")
    capture.run(archive, ticker=False, slices=True, once=True, opener=dtcc)
    s3_fetches_after = sum(1 for _, f, w in dtcc.log if f == meta(2)["fileName"] and w == "200")
    p2 = pubs(archive, 2)
    ev["4_no_duplicates"] = {"rerun_status": r_again["status"], "slice2_rows_after_restore": after_restore,
                             "slice2_rows_after_rerun_restart_replay": len(p2),
                             "s3_downloads_of_slice2_on_replay": s3_fetches_after - s3_fetches_before,
                             "ids": sorted(e["id"] for e in p2)}

    settings = sp.Settings.from_env({"CREDIT_TAPE_ARCHIVE": str(Path(archive).resolve()), "CREDIT_TAPE_DEV_LOCAL": "1"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), sp.make_handler(settings))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
            c.request("GET", path)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body
        st, body = get("/api/trades?after=0")
        rows = [e for e in json.loads(body)["rows"] if e.get("kind") == "publication"
                and (e.get("slice") or {}).get("fileName") == saved2.name]
        raw_ok = []
        for e in rows:
            rs, rb = get("/api/raw?ref=" + quote(e["raw_ref"], safe=""))
            raw_ok.append(rs == 200 and rb == saved2.read_bytes())
    finally:
        srv.shutdown()
        srv.server_close()
    ev["5_panel"] = {"rows": [{"id": e["id"], "action": e["action"], "backfill": e["replay"],
                               "restored": bool(e.get("restored")), "raw_ref": e["raw_ref"]} for e in rows],
                     "original_zip_links_byte_identical": raw_ok}
    alerts = [json.loads(l) for l in open(os.path.join(archive, "alerts_outbox.jsonl"), encoding="utf-8")] \
        if os.path.exists(os.path.join(archive, "alerts_outbox.jsonl")) else []
    audit = check_archive.check(archive)
    rec2 = reconcile.run(archive, DAY, ["CFTC_CR"], fetch_cumulative=True, opener=OfflinePPD())
    tp = rec2["channels"]["CFTC_CR"]["tape_parity"]
    ev["6_after"] = {"strict_audit_clean": audit["clean"], "alerts_for_slice2": sum(
        1 for a in alerts if (a.get("slice") or {}).get("fileName") == saved2.name),
        "tape_parity_ids": tp["ids"], "unmatched": [(u["id"], u["state"]) for u in tp["unmatched"]],
        "verdict": rec2["channels"]["CFTC_CR"]["verdict"]["tape_parity"]}

    checks = {
        "tape_missed_slice2": ev["1_capture"]["published_per_slice"] == {1: 2, 2: 0, 3: 1} and ev["1_capture"]["slice2_pending"],
        "report_only_saved_2_and_4": ev["2_report_only"]["saved"] == [2, 4],
        "tampered_copy_unresolved": r_tampered["status"] == "unresolved",
        "unknown_identity_slice4_unresolved": r_four["status"] == "unresolved",
        "slice2_restored_via_commit_path": r_two["status"] == "restored" and r_two["commit_key"] == "dtcc-cftc-slices/CFTC_CR/9102",
        "restore_makes_no_network_request": r_two["network_requests"] == []
        and hashlib.sha256((Path(archive) / r_two["staged"]).read_bytes()).hexdigest() == r_two["sha256"],
        "repeats_and_correction_kept": sorted(e["id"] for e in p2) == ["C", "R", "R"],
        "rerun_is_noop": r_again["status"] == "already_complete",
        "no_duplicate_after_rerun_restart_replay": after_restore == 3 and len(p2) == 3,
        "replay_did_not_refetch": s3_fetches_after == s3_fetches_before,
        "panel_shows_backfill_restored": st == 200 and len(rows) == 3 and all(e["replay"] and e.get("restored") for e in rows),
        "panel_links_original_zip": len(raw_ok) == 3 and all(raw_ok),
        "no_alerts_for_backfill": ev["6_after"]["alerts_for_slice2"] == 0,
        "strict_audit_clean": audit["clean"],
        "slice4_still_unresolved_in_parity": ev["6_after"]["unmatched"] == [("E", "cumulative_only")],
    }
    return {"evidence": ev, "checks": checks, "all_pass": all(checks.values())}


def main():
    with tempfile.TemporaryDirectory() as d:  # disposable archive; nothing else is touched
        archive = os.path.join(d, "archive")
        result = run(archive)
    print(json.dumps(result, indent=1, default=str))
    return 0 if result["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
