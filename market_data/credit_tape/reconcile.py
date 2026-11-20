"""OA-242 DTCC coverage reconciliation for one completed UTC day, per channel.

The live tape is fed only by intraday slices. This tool compares what the tape
captured for a completed day against DTCC's published cumulative file for the
same day, and makes every gap, correction, repeat and disagreement explicit.
The cumulative file is used ONLY to reconcile: nothing it contains is written
to events.jsonl or alerts_outbox.jsonl, and it never creates a panel row.

Interval: a DTCC slice day, which is the UTC calendar day. Slice numbering
restarts at _1 just after 00:00Z, e.g. CFTC_SLICE_CREDITS_2026_11_20_1 started
00:01:14Z. The day must be complete, i.e. strictly before today in UTC.

Sources compared (per channel):
  * captured: slice publications in the archive's events.jsonl for that day.
    Each original ZIP is re-hashed against the slice event's raw_sha256, and
    rows are kept with (slice file, slice number, row index, live/replay).
  * gap recovery (optional, --recover-gaps): slice numbers missing from the
    capture are fetched from DTCC's numbered intraday endpoint
    (/ppd/api/report/intraday/...). These are intraday slices, not the daily
    file. Originals are stored under dtcc-reconcile/<day>/gap-slices/ before
    decoding and are labelled "gap_recovery", not "live".
  * cumulative: <AGENCY>_CUMULATIVE_CREDITS_<day>.zip from
    /ppd/api/report/cumulative/..., stored under dtcc-reconcile/<day>/cumulative/
    before decoding (--fetch-cumulative or --cumulative-dir).

Comparison is a multiset per Dissemination Identifier, using full 110-column
row equality. Nothing is deduplicated or overwritten:
  matched                    same rows, same multiplicity
  matched_with_differences   same ID, field values differ; both versions kept
  repeat_count_mismatch      the ID appears a different number of times on each side
  repeated_reports           IDs reported more than once (consistent or not)
  captured_only              in the capture but absent from the cumulative file
                             (unresolved)
  missing_in_uncaptured_slice  in the cumulative file, not captured, while some
                             slice numbers are still uncaptured. Unresolved:
                             the cumulative file is not in slice order, so the
                             record cannot be pinned to one slice; recovering
                             the gap slices resolves it
  missing_unexplained        in the cumulative file but in no captured or
                             recovered slice
Corrections and lifecycle events (CORR, MODI, TERM, EROR, REVI) are kept as
records. Each record with an Original Dissemination Identifier is linked:
  original_in_interval   the original record is in this day
  original_before_interval  it points to an earlier day; listed explicitly
                         and not resolvable within this interval

Usage:
  python -m market_data.credit_tape.reconcile --archive <dir> --day 2026-11-19 \
      [--channel CFTC_CR|SEC_CR|both] [--recover-gaps] [--fetch-cumulative] [--out report.json]
Exit 0 only if nothing is captured_only, missing_* (after recovery),
differing or repeat-mismatched.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
import io

from market_data.credit_tape import slices

PPD = "https://pddata.dtcc.com/ppd/api/report"
AGENCY = {"CFTC_CR": "cftc", "SEC_CR": "sec"}
SOURCE = {"CFTC_CR": "dtcc-cftc-slices", "SEC_CR": "dtcc-sec-slices"}
UA = slices.UA
ID, ORIG, ACTION = "Dissemination Identifier", "Original Dissemination Identifier", "Action type"
LIST_MAX = 200
TAIL_PROBE_MAX = 500  # never probe more than this many slice numbers past the last captured one


def _slice_no(name: str) -> int | None:
    m = re.search(r"_(\d+)\.zip$", name or "")
    return int(m.group(1)) if m else None


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _store(path: str, blob: bytes) -> str:
    """Original bytes first (temp, fsync, verify, rename), like the live route."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    with open(tmp, "rb") as fh:
        if _sha(fh.read()) != _sha(blob):
            raise OSError("write verification failed")
    os.replace(tmp, path)
    return _sha(blob)


def _fetch(opener, url: str) -> bytes | None:
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            blob = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 403, 404):
            return None
        raise
    return blob if zipfile.is_zipfile(io.BytesIO(blob)) else None  # an error page is not a slice


def captured_rows(archive: str, channel: str, day: str):
    """Rows the live tape published for this channel/day, each original re-verified."""
    tag = day.replace("-", "_")
    prefix = f"{AGENCY[channel].upper()}_SLICE_CREDITS_{tag}_"
    slices_seen, rows, problems = {}, [], []
    with open(os.path.join(archive, "events.jsonl"), encoding="utf-8") as fh:
        events = [json.loads(l) for l in fh if l.strip()]
    for e in events:
        if e.get("source") != SOURCE[channel] or e.get("kind") != "slice":
            continue
        name = (e.get("slice") or {}).get("fileName") or ""
        if not name.startswith(prefix):
            continue
        path = os.path.join(archive, e.get("raw_ref") or "")
        ok = os.path.isfile(path) and _sha(open(path, "rb").read()) == e.get("raw_sha256")
        if not ok:
            problems.append({"slice": name, "problem": "captured_original_missing_or_hash_mismatch"})
        slices_seen[name] = {"number": _slice_no(name), "replay": bool(e.get("replay")), "original_ok": ok,
                             "startTs": e["slice"].get("startTs"), "endTs": e["slice"].get("endTs")}
    for e in events:
        if e.get("source") != SOURCE[channel] or e.get("kind") != "publication":
            continue
        name = (e.get("slice") or {}).get("fileName") or ""
        if name in slices_seen:
            rows.append({"row": e.get("record") or {}, "slice": name, "slice_no": _slice_no(name),
                         "row_index": e.get("row_index"), "origin": "replay" if e.get("replay") else "live"})
    return slices_seen, rows, problems


def reconcile_channel(archive: str, channel: str, day: str, recover_gaps=False, fetch_cumulative=False,
                      cumulative_dir: str | None = None, opener=urllib.request.urlopen) -> dict:
    ag, tag = AGENCY[channel], day.replace("-", "_")
    base = os.path.join(archive, "dtcc-reconcile", day)
    captured, rows, problems = captured_rows(archive, channel, day)
    numbers = sorted(v["number"] for v in captured.values() if v["number"])
    top = numbers[-1] if numbers else 0
    gaps = sorted(set(range(1, top + 1)) - set(numbers))
    recovered, still_missing_slices, tail_probe = {}, [], None
    if recover_gaps:
        n = top + 1  # also probe past the last captured slice until the endpoint has no more
        candidates = list(gaps)
        while n <= top + TAIL_PROBE_MAX:
            name = f"{ag.upper()}_SLICE_CREDITS_{tag}_{n}.zip"
            blob = _fetch(opener, f"{PPD}/intraday/{ag}/{name}")
            if blob is None:
                tail_probe = {"first_absent_slice": n}
                break
            candidates.append(n)
            gaps.append(n)
            recovered[n] = (name, blob)
            n += 1
        for n in candidates:
            if n in recovered:
                continue
            name = f"{ag.upper()}_SLICE_CREDITS_{tag}_{n}.zip"
            blob = _fetch(opener, f"{PPD}/intraday/{ag}/{name}")
            if blob is None:
                still_missing_slices.append(n)
            else:
                recovered[n] = (name, blob)
        for n, (name, blob) in sorted(recovered.items()):
            sha = _store(os.path.join(base, "gap-slices", name), blob)
            try:
                for i, row in enumerate(slices.decode_rows(blob)):
                    rows.append({"row": row, "slice": name, "slice_no": n, "row_index": i, "origin": "gap_recovery",
                                 "sha256": sha})
            except Exception as exc:  # noqa: BLE001 - recorded, never hidden
                problems.append({"slice": name, "problem": "gap_slice_decode_failure", "error": repr(exc)[:200]})
    else:
        still_missing_slices = list(gaps)

    cum_name = f"{ag.upper()}_CUMULATIVE_CREDITS_{tag}.zip"
    cum_blob, cum_path = None, os.path.join(base, "cumulative", cum_name)
    if fetch_cumulative:
        cum_blob = _fetch(opener, f"{PPD}/cumulative/{ag}/{cum_name}")
        if cum_blob is not None:
            _store(cum_path, cum_blob)
    elif cumulative_dir and os.path.isfile(os.path.join(cumulative_dir, cum_name)):
        cum_blob = open(os.path.join(cumulative_dir, cum_name), "rb").read()
        _store(cum_path, cum_blob)
    elif os.path.isfile(cum_path):
        cum_blob = open(cum_path, "rb").read()
    if cum_blob is None:
        return {"channel": channel, "day": day, "error": f"cumulative file {cum_name} not available",
                "complete": False}
    cumulative = slices.decode_rows(cum_blob)

    cap_by = collections.defaultdict(list)
    for r in rows:
        cap_by[r["row"].get(ID)].append(r)
    cum_by = collections.defaultdict(list)
    for i, r in enumerate(cumulative):
        cum_by[r.get(ID)].append((i, r))
    recovered_nos = set(recovered)
    out = collections.defaultdict(list)
    for did in sorted(set(cap_by) | set(cum_by), key=str):
        cap, cum = cap_by.get(did, []), cum_by.get(did, [])
        if len(cap) > 1 or len(cum) > 1:
            out["repeated_reports"].append({"id": did, "captured": len(cap), "cumulative": len(cum),
                                            "slices": [c["slice"] for c in cap]})
        if cap and not cum:
            out["captured_only"].append({"id": did, "slices": [c["slice"] for c in cap],
                                         "action": cap[0]["row"].get(ACTION)})
            continue
        if cum and not cap:
            # The cumulative file is not in slice order (checked on 2026-11-19 data), so a record
            # cannot be placed in a particular uncaptured slice by position; it is either within
            # the known uncaptured slice numbers (unresolved) or unexplained.
            cat = "missing_in_uncaptured_slice" if still_missing_slices else "missing_unexplained"
            out[cat].append({"id": did, "cumulative_row": cum[0][0], "action": cum[0][1].get(ACTION),
                             **({"uncaptured_slice_numbers": sorted(still_missing_slices)} if still_missing_slices else {})})
            continue
        cum_rows = [r for _, r in cum]
        differing = [c for c in cap if c["row"] not in cum_rows]
        if len(cap) != len(cum):  # every occurrence is kept; the counts disagree, so not "matched"
            out["repeat_count_mismatch"].append({"id": did, "captured": len(cap), "cumulative": len(cum),
                                                 "rows_identical": not differing})
        if differing:
            fields = sorted({k for c in differing for k in set(c["row"]) | set(cum_rows[0])
                             if c["row"].get(k) != cum_rows[0].get(k)})
            out["matched_with_differences"].append({
                "id": did, "fields": fields, "captured_versions": [{k: c["row"].get(k) for k in fields} for c in differing],
                "cumulative_version": {k: cum_rows[0].get(k) for k in fields}, "slices": [c["slice"] for c in differing]})
        elif len(cap) == len(cum):
            out["matched"].append(did)
            if any(c["origin"] == "gap_recovery" for c in cap):
                out["matched_via_gap_recovery"].append(did)

    # Lifecycle links: never overwrite; resolve within the interval or list as cross-day.
    in_day = set(cap_by) | set(cum_by)
    links = collections.Counter()
    before = []
    for did, occ in list(cum_by.items()) + [(d, [(None, c["row"]) for c in v]) for d, v in cap_by.items() if d not in cum_by]:
        row = occ[0][1]
        orig = row.get(ORIG)
        if not orig:
            continue
        if orig in in_day:
            links["original_in_interval"] += 1
        else:
            links["original_before_interval"] += 1
            before.append({"id": did, "original_id": orig, "action": row.get(ACTION)})
    actions = {"captured": dict(collections.Counter(r["row"].get(ACTION) for r in rows)),
               "cumulative": dict(collections.Counter(r.get(ACTION) for r in cumulative))}
    unresolved = {k: len(out[k]) for k in ("captured_only", "missing_in_uncaptured_slice", "missing_unexplained",
                                           "matched_with_differences", "repeat_count_mismatch")}
    covered = sorted(captured.values(), key=lambda v: v["number"] or 0)
    report = {
        "channel": channel, "day": day, "interval_utc": [f"{day}T00:00:00Z", f"{_next(day)}T00:00:00Z"],
        "slices": {"captured": len(captured), "captured_live": sum(1 for v in captured.values() if not v["replay"]),
                   "captured_replay": sum(1 for v in captured.values() if v["replay"]),
                   "first_captured": covered[0] if covered else None, "last_captured": covered[-1] if covered else None,
                   "uncaptured_numbers": gaps, "recovered_numbers": sorted(recovered_nos),
                   "still_missing_numbers": sorted(still_missing_slices), "tail_probe": tail_probe},
        "rows": {"captured": sum(1 for r in rows if r["origin"] != "gap_recovery"),
                 "gap_recovered": sum(1 for r in rows if r["origin"] == "gap_recovery"),
                 "cumulative": len(cumulative), "distinct_ids_captured": len(cap_by), "distinct_ids_cumulative": len(cum_by)},
        "cumulative_file": {"name": cum_name, "sha256": _sha(cum_blob), "stored": os.path.relpath(cum_path, archive)},
        "counts": {"matched": len(out["matched"]), "matched_via_gap_recovery": len(out["matched_via_gap_recovery"]),
                   "repeated_reports": len(out["repeated_reports"]), **unresolved},
        "actions": actions, "lifecycle_links": dict(links),
        "original_before_interval": before[:LIST_MAX], "original_before_interval_total": len(before),
        "problems": problems,
        **{k: out[k][:LIST_MAX] for k in ("captured_only", "missing_in_uncaptured_slice", "missing_unexplained",
                                         "matched_with_differences", "repeat_count_mismatch", "repeated_reports")},
    }
    report["complete"] = not any(unresolved.values()) and not still_missing_slices and not problems
    return report


def _next(day: str) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.environ.get("CREDIT_TAPE_ARCHIVE"), required=False)
    ap.add_argument("--day", required=True, help="completed UTC day, YYYY-MM-DD")
    ap.add_argument("--channel", default="both", choices=["CFTC_CR", "SEC_CR", "both"])
    ap.add_argument("--recover-gaps", action="store_true")
    ap.add_argument("--fetch-cumulative", action="store_true")
    ap.add_argument("--cumulative-dir", help="directory already holding the cumulative ZIP(s)")
    ap.add_argument("--out", help="write the JSON report here (default: stdout only)")
    a = ap.parse_args(argv)
    if not a.archive or not os.path.isfile(os.path.join(a.archive, "events.jsonl")):
        ap.error("--archive must be a credit-tape archive with events.jsonl")
    if dt.date.fromisoformat(a.day) >= dt.datetime.now(dt.timezone.utc).date():
        ap.error("--day must be a completed UTC day (before today, UTC)")
    chans = ["CFTC_CR", "SEC_CR"] if a.channel == "both" else [a.channel]
    result = {"day": a.day, "channels": {c: reconcile_channel(a.archive, c, a.day, a.recover_gaps, a.fetch_cumulative,
                                                              a.cumulative_dir) for c in chans}}
    result["complete"] = all(r.get("complete") for r in result["channels"].values())
    text = json.dumps(result, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
