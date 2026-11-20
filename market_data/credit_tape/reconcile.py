"""OA-242 DTCC coverage reconciliation for one completed UTC day, per channel.

The live tape is fed only by intraday slices. This tool compares what the tape
logged for a completed day against DTCC's published cumulative file for that
day. Every gap, correction, repeat and disagreement is made explicit, and
anything not proven is reported as unknown. The cumulative file is used ONLY
to reconcile: nothing it contains is written to events.jsonl or
alerts_outbox.jsonl, and it never creates a panel row.

The report keeps three things apart:

  archive_audit   check_archive runs first. Logged rows count as VERIFIED only
                  if the audit is clean and the row's slice has a completed,
                  validated commit (original ZIP sha256, committed lines,
                  re-decoded rows). Otherwise the rows are counted but
                  reported as unverified, and the verdict cannot be complete.
  slice_coverage  numbered-slice coverage for the day: which slice numbers the
                  tape captured and which it did not. Optionally
                  (--recover-gaps) uncaptured numbers are fetched from DTCC's
                  numbered intraday endpoint. Those ZIPs are REPORT-ONLY: kept
                  as originals under dtcc-reconcile/<day>/report-only-slices/,
                  never logged to the tape, never counted as tape rows.
                  Restoring them to the tape is a separate decision.
                  An unavailable response (404, error, non-ZIP) is UNKNOWN.
                  The tail probe never claims a day boundary.
  parity          against the SAVED cumulative file (stored as an original
                  under dtcc-reconcile/<day>/cumulative/, sha256 recorded):
                    tape_parity         verified tape rows vs cumulative
                    with_report_only    tape rows + report-only slices vs
                                        cumulative (explains gaps; not tape
                                        parity)
                    unverified_view     only when some logged rows are not
                                        verified: all logged rows +
                                        report-only vs cumulative, labelled
                                        unverified, never in the verdict

Parity is full-row multiset equality per Dissemination Identifier. Each side
is a multiset of complete rows (all columns). Matched occurrences are paired
one to one. Every unmatched occurrence is listed on its side, even when the
two sides have the same total count for that identifier. Nothing is
deduplicated, merged or overwritten. Per identifier the state is one of:
  matched             identical row multisets
  occurrence_mismatch both sides have the ID, but some occurrences are unpaired
                      (`same_total_count` says whether only the content differs)
  captured_only       only the tape has the ID
  cumulative_only     only the cumulative file has the ID

Lifecycle links: every record with an Original Dissemination Identifier is
kept as its own record. Its original is either observed in this interval
(either side) or `original_not_observed`. Not observing it is unknown: it is
NOT treated as proof that the original is on an earlier day.

Interval: a DTCC slice day, which is the UTC calendar day. Slice numbering
restarts at _1 just after 00:00Z. The day must be complete in UTC.

Usage:
  python -m market_data.credit_tape.reconcile --archive <dir> --day YYYY-MM-DD \
      [--channel CFTC_CR|SEC_CR|both] [--volume <mount>] [--recover-gaps] \
      [--fetch-cumulative | --cumulative-dir <dir>] [--out report.json]
Exit 0 only if every requested channel's tape parity is "complete".
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

from market_data.credit_tape import check_archive, slices, volume

PPD = "https://pddata.dtcc.com/ppd/api/report"
AGENCY = {"CFTC_CR": "cftc", "SEC_CR": "sec"}
SOURCE = {"CFTC_CR": "dtcc-cftc-slices", "SEC_CR": "dtcc-sec-slices"}
UA = slices.UA
ID, ORIG, ACTION = "Dissemination Identifier", "Original Dissemination Identifier", "Action type"
LIST_MAX = 200
TAIL_PROBE_MAX = 50  # slice numbers probed past the last captured one; observations only


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


def _fetch(opener, url: str):
    """(blob, status). Anything but a readable ZIP is 'unavailable' or 'error': unknown, never proof."""
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            blob = r.read()
    except urllib.error.HTTPError as exc:
        return None, f"unavailable_http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"error_{type(exc).__name__}"
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        return None, "unavailable_not_a_zip"
    return blob, "ok"


def _canon(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


# -- inputs -------------------------------------------------------------------------------------

def logged_rows(archive: str, channel: str, day: str, audit: dict):
    """Rows the tape logged for this channel/day; verified only under a clean audit + validated commit."""
    tag = day.replace("-", "_")
    prefix = f"{AGENCY[channel].upper()}_SLICE_CREDITS_{tag}_"
    cdir = os.path.join(archive, "dtcc-slices", "commits")
    ddir = os.path.join(archive, "dtcc-slices", "done")
    with open(os.path.join(archive, "events.jsonl"), encoding="utf-8") as fh:
        events = [json.loads(l) for l in fh if l.strip()]
    captured, rows = {}, []
    for e in events:
        if e.get("source") != SOURCE[channel] or e.get("kind") != "slice":
            continue
        meta = e.get("slice") or {}
        name = meta.get("fileName") or ""
        if not name.startswith(prefix):
            continue
        key_file = slices.SliceRoute.commit_key(channel, meta.get("sliceId")).replace("/", "__") + ".json"
        committed = os.path.isfile(os.path.join(cdir, key_file)) and os.path.isfile(os.path.join(ddir, key_file))
        captured[name] = {"number": _slice_no(name), "replay": bool(e.get("replay")),
                          "startTs": meta.get("startTs"), "endTs": meta.get("endTs"),
                          "verified": bool(audit.get("clean")) and committed}
    for e in events:
        if e.get("source") != SOURCE[channel] or e.get("kind") != "publication":
            continue
        name = (e.get("slice") or {}).get("fileName") or ""
        if name in captured:
            rows.append({"row": e.get("record") or {}, "slice": name, "slice_no": _slice_no(name),
                         "row_index": e.get("row_index"), "origin": "replay" if e.get("replay") else "live",
                         "verified": captured[name]["verified"]})
    return captured, rows


# -- comparison ---------------------------------------------------------------------------------

def compare(tape_rows: list[dict], cumulative: list[dict]) -> dict:
    """Full-row multiset comparison per Dissemination Identifier; unmatched occurrences on both sides."""
    cap = collections.defaultdict(list)
    for r in tape_rows:
        cap[r["row"].get(ID)].append(r)
    cum = collections.defaultdict(list)
    for i, r in enumerate(cumulative):
        cum[r.get(ID)].append((i, r))
    states = collections.Counter()
    lists = collections.defaultdict(list)
    matched_occ = un_cap = un_cum = 0
    for did in sorted(set(cap) | set(cum), key=str):
        c_rows, k_rows = cap.get(did, []), cum.get(did, [])
        c_count = collections.Counter(_canon(r["row"]) for r in c_rows)
        k_count = collections.Counter(_canon(r) for _, r in k_rows)
        pair = c_count & k_count
        left, right = c_count - pair, k_count - pair  # unmatched occurrences, per side
        matched_occ += sum(pair.values())
        un_cap += sum(left.values())
        un_cum += sum(right.values())
        if len(c_rows) > 1 or len(k_rows) > 1:
            lists["repeated_reports"].append({"id": did, "tape": len(c_rows), "cumulative": len(k_rows)})
        if not left and not right:
            states["matched"] += 1
            continue
        if not k_rows:
            state = "captured_only"
        elif not c_rows:
            state = "cumulative_only"
        else:
            state = "occurrence_mismatch"
        states[state] += 1
        entry = {"id": did, "state": state, "tape_total": len(c_rows), "cumulative_total": len(k_rows),
                 "same_total_count": len(c_rows) == len(k_rows),
                 "unmatched_tape": [], "unmatched_cumulative": []}
        for canon, n in left.items():
            src = [r for r in c_rows if _canon(r["row"]) == canon]
            entry["unmatched_tape"].append({"count": n, "slices": sorted({r["slice"] for r in src}),
                                            "origins": sorted({r["origin"] for r in src}),
                                            "action": src[0]["row"].get(ACTION)})
        for canon, n in right.items():
            src = [i for i, r in k_rows if _canon(r) == canon]
            entry["unmatched_cumulative"].append({"count": n, "cumulative_rows": src[:10],
                                                  "action": json.loads(canon).get(ACTION)})
        if sum(left.values()) == 1 and sum(right.values()) == 1:  # one-to-one: name the differing fields
            a, b = json.loads(next(iter(left))), json.loads(next(iter(right)))
            entry["differing_fields"] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        lists["unmatched"].append(entry)
    return {"ids": dict(states), "occurrences": {"matched": matched_occ, "unmatched_tape": un_cap,
                                                 "unmatched_cumulative": un_cum},
            "tape_rows": len(tape_rows), "cumulative_rows": len(cumulative),
            "unmatched_total": len(lists["unmatched"]), "unmatched": lists["unmatched"][:LIST_MAX],
            "repeated_reports_total": len(lists["repeated_reports"]),
            "repeated_reports": lists["repeated_reports"][:LIST_MAX],
            "parity": un_cap == 0 and un_cum == 0}


# -- per channel ---------------------------------------------------------------------------------

def reconcile_channel(archive: str, channel: str, day: str, audit: dict, recover_gaps=False,
                      fetch_cumulative=False, cumulative_dir: str | None = None,
                      opener=urllib.request.urlopen) -> dict:
    ag, tag = AGENCY[channel], day.replace("-", "_")
    base = os.path.join(archive, "dtcc-reconcile", day)
    captured, tape = logged_rows(archive, channel, day, audit)
    numbers = sorted(v["number"] for v in captured.values() if v["number"])
    top = numbers[-1] if numbers else 0
    uncaptured = sorted(set(range(1, top + 1)) - set(numbers))

    # Numbered-slice coverage. Report-only recovery; every non-ZIP answer is unknown.
    fetch_status, report_only = {}, []
    tail = {"last_captured_number": top or None, "probes": [], "boundary": "unknown"}
    if recover_gaps:
        for n in uncaptured:
            name = f"{ag.upper()}_SLICE_CREDITS_{tag}_{n}.zip"
            blob, status = _fetch(opener, f"{PPD}/intraday/{ag}/{name}")
            fetch_status[n] = status
            if blob is not None:
                report_only.append((n, name, blob))
        n = top + 1
        while n <= top + TAIL_PROBE_MAX:  # observations past the last captured slice; never a boundary claim
            name = f"{ag.upper()}_SLICE_CREDITS_{tag}_{n}.zip"
            blob, status = _fetch(opener, f"{PPD}/intraday/{ag}/{name}")
            tail["probes"].append({"number": n, "status": status})
            if blob is None:
                break
            report_only.append((n, name, blob))
            fetch_status[n] = status
            n += 1
    ro_rows, ro_meta, problems = [], [], []
    for n, name, blob in sorted(report_only):
        path = os.path.join(base, "report-only-slices", name)
        sha = _store(path, blob)
        ro_meta.append({"number": n, "file": os.path.relpath(path, archive), "sha256": sha, "report_only": True})
        try:
            for i, row in enumerate(slices.decode_rows(blob)):
                ro_rows.append({"row": row, "slice": name, "slice_no": n, "row_index": i,
                                "origin": "report_only", "verified": False})
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            problems.append({"slice": name, "problem": "report_only_slice_decode_failure", "error": repr(exc)[:200]})
    if ro_meta:
        _store(os.path.join(base, "report-only-slices", f"{channel}.REPORT_ONLY.json"),
               json.dumps({"report_only": True, "channel": channel, "day": day,
                           "note": "Fetched for reconciliation only; not part of the tape; never logged.",
                           "slices": ro_meta}, indent=1).encode())

    # Saved cumulative file (original first).
    cum_name = f"{ag.upper()}_CUMULATIVE_CREDITS_{tag}.zip"
    cum_path = os.path.join(base, "cumulative", cum_name)
    cum_blob, cum_status = None, "not_requested"
    if fetch_cumulative:
        cum_blob, cum_status = _fetch(opener, f"{PPD}/cumulative/{ag}/{cum_name}")
        if cum_blob is not None:
            _store(cum_path, cum_blob)
    elif cumulative_dir and os.path.isfile(os.path.join(cumulative_dir, cum_name)):
        cum_blob, cum_status = open(os.path.join(cumulative_dir, cum_name), "rb").read(), "ok_local_copy"
        _store(cum_path, cum_blob)
    elif os.path.isfile(cum_path):
        cum_blob, cum_status = open(cum_path, "rb").read(), "ok_saved"

    verified_rows = [r for r in tape if r["verified"]]
    covered = sorted(captured.values(), key=lambda v: v["number"] or 0)
    report = {
        "channel": channel, "day": day, "interval_utc": [f"{day}T00:00:00Z", f"{_next(day)}T00:00:00Z"],
        "slice_coverage": {
            "captured": len(captured), "captured_live": sum(1 for v in captured.values() if not v["replay"]),
            "captured_replay": sum(1 for v in captured.values() if v["replay"]),
            "first_captured": covered[0] if covered else None, "last_captured": covered[-1] if covered else None,
            "uncaptured_numbers": uncaptured,
            "report_only_recovered": sorted(n for n, _, _ in report_only),
            "unknown_numbers": sorted(n for n in uncaptured if fetch_status.get(n, "not_fetched") != "ok"),
            "fetch_status": {str(k): v for k, v in sorted(fetch_status.items())},
            "tail": tail,
            "report_only_slices": ro_meta,
        },
        "tape_rows": {"logged": len(tape), "verified": len(verified_rows), "unverified": len(tape) - len(verified_rows)},
        "cumulative_file": {"name": cum_name, "status": cum_status,
                            "sha256": _sha(cum_blob) if cum_blob is not None else None,
                            "saved": os.path.relpath(cum_path, archive) if cum_blob is not None else None},
        "problems": problems,
    }
    if cum_blob is None:
        report["tape_parity"] = None
        report["verdict"] = {"tape_parity": "unknown", "reasons": [f"cumulative file {cum_name}: {cum_status}"]}
        return report
    cumulative = slices.decode_rows(cum_blob)
    report["tape_parity"] = compare(verified_rows, cumulative)
    if ro_rows:
        report["with_report_only"] = compare(verified_rows + ro_rows, cumulative)
    if len(verified_rows) < len(tape):
        # Informational only, never part of the verdict: every logged row (verified or not) plus
        # report-only slices against the saved file, so an unaudited archive still shows its shape.
        report["unverified_view"] = {"note": "includes rows that failed or skipped the archive audit; not verified",
                                     **compare(tape + ro_rows, cumulative)}
    # Lifecycle links over everything observed in the interval (tape, report-only, cumulative).
    observed = {r["row"].get(ID) for r in tape + ro_rows} | {r.get(ID) for r in cumulative}
    links, not_observed = collections.Counter(), []
    seen = set()
    for r in [x["row"] for x in tape + ro_rows] + cumulative:
        did, orig = r.get(ID), r.get(ORIG)
        if not orig or did in seen:
            continue
        seen.add(did)
        if orig in observed:
            links["original_observed_in_interval"] += 1
        else:
            links["original_not_observed"] += 1  # unknown: not proof of an earlier-day record
            not_observed.append({"id": did, "original_id": orig, "action": r.get(ACTION)})
    report["lifecycle_links"] = dict(links)
    report["original_not_observed"] = not_observed[:LIST_MAX]
    report["actions"] = {"tape": dict(collections.Counter(r["row"].get(ACTION) for r in tape)),
                         "cumulative": dict(collections.Counter(r.get(ACTION) for r in cumulative))}
    reasons = []
    if not audit.get("clean"):
        reasons.append("archive audit not clean: logged rows are unverified")
    elif report["tape_rows"]["unverified"]:
        reasons.append(f"{report['tape_rows']['unverified']} logged rows lack a validated commit")
    if not report["tape_parity"]["parity"]:
        reasons.append("tape rows and the saved cumulative file have unmatched occurrences")
    if problems:
        reasons.append("report-only slice problems")
    if not audit.get("clean") or report["tape_rows"]["unverified"]:
        state = "unverified"
    elif report["tape_parity"]["parity"] and not problems:
        state = "complete"
    else:
        state = "incomplete"
    report["verdict"] = {"tape_parity": state, "reasons": reasons,
                         "unknowns": {"slice_numbers": report["slice_coverage"]["unknown_numbers"],
                                      "tail_boundary": "unknown",
                                      "originals_not_observed": links.get("original_not_observed", 0)}}
    return report


def _next(day: str) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


def run(archive: str, day: str, channels, volume_root=None, recover_gaps=False, fetch_cumulative=False,
        cumulative_dir=None, opener=urllib.request.urlopen, is_mount=os.path.ismount) -> dict:
    audit = check_archive.check(archive, volume_root, is_mount=is_mount)
    summary = {k: audit.get(k) for k in ("clean", "archive_id", "volume", "commits_completed_validated",
                                         "originals_rechecked", "commit_problems", "error")}
    result = {"day": day, "archive_audit": summary,
              "channels": {c: reconcile_channel(archive, c, day, audit, recover_gaps, fetch_cumulative,
                                                cumulative_dir, opener) for c in channels}}
    result["complete"] = all(r["verdict"]["tape_parity"] == "complete" for r in result["channels"].values())
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.environ.get("CREDIT_TAPE_ARCHIVE"))
    ap.add_argument("--day", required=True, help="completed UTC day, YYYY-MM-DD")
    ap.add_argument("--channel", default="both", choices=["CFTC_CR", "SEC_CR", "both"])
    ap.add_argument("--volume", default=volume.volume_root(), help="persistent volume the archive must be inside")
    ap.add_argument("--recover-gaps", action="store_true", help="fetch uncaptured numbered slices (report-only)")
    ap.add_argument("--fetch-cumulative", action="store_true")
    ap.add_argument("--cumulative-dir", help="directory already holding the cumulative ZIP(s)")
    ap.add_argument("--out", help="write the JSON report here (default: stdout only)")
    a = ap.parse_args(argv)
    if not a.archive or not os.path.isfile(os.path.join(a.archive, "events.jsonl")):
        ap.error("--archive must be a credit-tape archive with events.jsonl")
    if dt.date.fromisoformat(a.day) >= dt.datetime.now(dt.timezone.utc).date():
        ap.error("--day must be a completed UTC day (before today, UTC)")
    chans = ["CFTC_CR", "SEC_CR"] if a.channel == "both" else [a.channel]
    result = run(a.archive, a.day, chans, a.volume, a.recover_gaps, a.fetch_cumulative, a.cumulative_dir)
    text = json.dumps(result, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
