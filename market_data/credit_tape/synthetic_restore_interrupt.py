"""OA-242 interrupted-restore safety demo (offline fixtures, disposable archives, no network).

Each scenario starts from the restoration setup of synthetic_restore.py: the tape missed
CFTC #2 of 2001-02-03, and reconciliation saved #2 as a report-only ZIP with its sha256.
In every restart below, S3 now answers #2 with REPLACEMENT bytes (a different row). The
ordinary collector must never publish those bytes under restored provenance.

  resume          restore stops after queueing, before committing; the ordinary collector
                  restarts and completes the restore from the staged, verified local bytes
  staged_missing  restore stops; the staged bytes are deleted; the collector restarts and
                  leaves the restore visibly unresolved (nothing fetched, nothing published);
                  rerunning restore with the verified saved ZIP then completes it
  staged_tampered as above, but the staged bytes are replaced with the replacement bytes
  stop_<stage>    restore stops inside the commit (after_raw, after_commit_record, mid-append);
                  the restart completes it once, from the same bytes

Usage: python -m market_data.credit_tape.synthetic_restore_interrupt
Prints JSON evidence; exit 0 only if every check holds.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from market_data.credit_tape import capture, check_archive, reconcile, restore, slices
from market_data.credit_tape import synthetic_restore as sr
from market_data.credit_tape.synthetic_resilience import SyntheticDTCC

REPLACEMENT = [sr.row("Q-REPLACEMENT")]


def setup(archive: str):
    dtcc = SyntheticDTCC()
    for n in (1, 2, 3):
        dtcc.files[sr.meta(n)["fullFilePath"]] = sr.zipped(sr.SLICES[n])
    dtcc.index = {"CFTC_CR": [sr.meta(n) for n in (1, 2, 3)], "SEC_CR": []}
    dtcc.fail.add(sr.meta(2)["fullFilePath"])
    capture.run(archive, ticker=False, slices=True, once=True, opener=dtcc)
    reconcile.run(archive, sr.DAY, ["CFTC_CR"], recover_gaps=True, fetch_cumulative=True, opener=sr.OfflinePPD())
    saved = Path(archive) / "dtcc-reconcile" / sr.DAY / "report-only-slices" / sr.meta(2)["fileName"]
    # From now on S3 answers #2, but with different bytes.
    dtcc.fail.clear()
    dtcc.files[sr.meta(2)["fullFilePath"]] = sr.zipped(REPLACEMENT)
    return dtcc, saved


def restart(archive: str, dtcc, tag: str):
    """The ordinary collector restarts (new route, changed index, S3 reachable)."""
    dtcc.etag = f'"restart-{tag}"'
    capture.run(archive, ticker=False, slices=True, once=True, opener=dtcc)


def state(archive: str, dtcc, saved: Path) -> dict:
    p2 = sr.pubs(archive, 2)
    raw = Path(archive) / "dtcc-slices" / "raw" / sr.DAY / sr.meta(2)["fileName"]
    health = json.loads((Path(archive) / "health.json").read_text(encoding="utf-8"))["sources"]["dtcc-slice-index"]
    return {"slice2_rows": sorted(e["id"] for e in p2),
            "restored_provenance": [bool(e.get("restored")) for e in p2],
            "raw_is_saved_bytes": raw.exists() and raw.read_bytes() == saved.read_bytes(),
            "s3_downloads_of_slice2": sum(1 for _, f, w in dtcc.log if f == sr.meta(2)["fileName"] and w == "200"),
            "health_restores": health.get("restores"),
            "restore_status": restore.status(archive)}


def scenario(name: str, d: str) -> dict:
    archive = os.path.join(d, name)
    dtcc, saved = setup(archive)
    out = {"scenario": name}
    stage = name[len("stop_"):] if name.startswith("stop_") else None
    if stage:
        def hook(s, fired={"done": False}):
            if not fired["done"] and s == stage:
                fired["done"] = True
                if s.startswith("tear_"):
                    return True
                raise slices.SimulatedStop(s)
            return False
        try:
            restore.restore(archive, "CFTC_CR", str(saved), _crash_hook=hook)
            out["stopped"] = None
        except slices.SimulatedStop as exc:
            out["stopped"] = str(exc)
    else:
        out["first"] = restore.restore(archive, "CFTC_CR", str(saved), _stop_before_commit=True)["status"]
        staged = Path(archive) / "dtcc-slices" / "restores" / "dtcc-cftc-slices__CFTC_CR__9102.zip"
        if name == "staged_missing":
            staged.unlink()
        elif name == "staged_tampered":
            staged.write_bytes(sr.zipped(REPLACEMENT))
    restart(archive, dtcc, "1")
    out["after_restart"] = state(archive, dtcc, saved)
    restart(archive, dtcc, "2")
    out["after_second_restart"] = state(archive, dtcc, saved)
    if name.startswith("staged_"):
        out["rerun"] = restore.restore(archive, "CFTC_CR", str(saved))["status"]
        restart(archive, dtcc, "3")
        out["after_rerun_and_restart"] = state(archive, dtcc, saved)
    final = out.get("after_rerun_and_restart") or out["after_second_restart"]
    out["strict_audit_clean"] = check_archive.check(archive)["clean"]
    good = (final["slice2_rows"] == ["C", "R", "R"] and all(final["restored_provenance"])
            and final["raw_is_saved_bytes"] and final["s3_downloads_of_slice2"] == 0
            and final["restore_status"]["unresolved"] == [] and out["strict_audit_clean"])
    if name.startswith("staged_"):
        mid = out["after_second_restart"]
        good = good and (mid["slice2_rows"] == [] and mid["s3_downloads_of_slice2"] == 0
                         and mid["restore_status"]["unresolved"] == ["CFTC_CR:9102"]
                         and mid["health_restores"]["unresolved"] == ["CFTC_CR:9102"]
                         and out["rerun"] == "restored")
    out["pass"] = good
    return out


SCENARIOS = ["resume", "staged_missing", "staged_tampered",
             "stop_after_raw", "stop_after_commit_record", "stop_tear_events"]


def run() -> dict:
    with tempfile.TemporaryDirectory() as d:  # disposable archives only
        results = [scenario(n, d) for n in SCENARIOS]
    return {"results": results, "all_pass": all(r["pass"] for r in results)}


def main():
    result = run()
    print(json.dumps(result, indent=1))
    return 0 if result["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
