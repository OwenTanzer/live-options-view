"""OA-242 tape restoration of a missed slice from an already saved numbered intraday ZIP.

Restores ONE slice the tape missed, through the existing commit path (SliceRoute._process
-> durable idempotent commit), so it appears in the panel as backfill with the original-ZIP
link. Makes no network request: the commit path's opener can serve only the saved file.
Daily cumulative files are never used here; they stay comparison-only.

Source identity must be established, or the slice stays unresolved:
  1. name: <AGENCY>_SLICE_CREDITS_<YYYY_MM_DD>_<n>.zip for the requested channel;
  2. index entry: the tape's own saved index snapshots (dtcc-slices/index/) must list this
     fileName for this channel with one consistent sliceId, rowCount and fullFilePath on the
     slice host. Without a saved entry the sliceId is unknown, so the slice stays unresolved;
  3. bytes: if a report-only marker (dtcc-reconcile/*/report-only-slices/*.REPORT_ONLY.json)
     lists the file, its recorded sha256 must equal the bytes; otherwise --sha256 must be
     given and match. A file with no recorded or supplied hash stays unresolved;
  4. content: it decodes, and the decoded row count equals the index entry's rowCount.

Then the slice is queued as pending with `restored` provenance and processed once:
  - the original is stored like any slice (raw ZIP, sha256);
  - every row is published, so legitimate repeated rows and corrections are kept;
  - rows are labelled backfill (replay=True, `restored`), and no alerts are raised.
Idempotency is the commit path's: a slice already completed (restored, captured or replayed)
is never published twice. Rerunning restore, restarting, or a later replay of the same slice
all find its done marker.

Run it with the collector stopped (it opens the archive's pending queue).

Usage: python -m market_data.credit_tape.restore --archive <dir> --channel CFTC_CR|SEC_CR --zip <path>
       [--sha256 <hex>]
Prints JSON; exit 0 if restored or already complete, 1 if unresolved.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import urllib.error
from urllib.parse import urlsplit

from market_data.credit_tape import capture, slices, volume

NAME = re.compile(r"^(CFTC|SEC)_SLICE_CREDITS_(\d{4})_(\d{2})_(\d{2})_(\d+)\.zip$")


class _LocalOnly:
    """Opener for the commit path: serves exactly one saved original; refuses everything else."""

    def __init__(self, url: str, blob: bytes):
        self.url, self.blob, self.requests = url, blob, []

    def __call__(self, req, timeout=None):
        self.requests.append(req.full_url)
        if req.full_url != self.url:
            raise urllib.error.URLError("restore is offline: only the saved original can be read")
        import io

        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R(self.blob)


def index_entries(archive: str, channel: str, file_name: str) -> list[dict]:
    """Every saved index snapshot entry the tape recorded for this channel/file."""
    found = []
    for path in sorted(glob.glob(os.path.join(archive, "dtcc-slices", "index", "*", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
        except (OSError, ValueError):
            continue
        for meta in (snap.get("entries") or {}).get(channel) or []:
            if isinstance(meta, dict) and meta.get("fileName") == file_name:
                found.append({**meta, "_snapshot": os.path.relpath(path, archive), "_fetched_at": snap.get("fetched_at")})
    return found


def recorded_sha(archive: str, channel: str, file_name: str) -> list[dict]:
    """sha256 values recorded for this file by report-only recovery."""
    out = []
    for path in glob.glob(os.path.join(archive, "dtcc-reconcile", "*", "report-only-slices", "*.REPORT_ONLY.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                marker = json.load(fh)
        except (OSError, ValueError):
            continue
        if marker.get("channel") != channel:
            continue
        for s in marker.get("slices") or []:
            if os.path.basename(s.get("file") or "") == file_name:
                out.append({"sha256": s.get("sha256"), "marker": os.path.relpath(path, archive)})
    return out


def restore(archive: str, channel: str, zip_path: str, sha256: str | None = None) -> dict:
    name = os.path.basename(zip_path)
    res = {"channel": channel, "file": name, "status": "unresolved", "reasons": []}
    unresolved = lambda why: {**res, "reasons": res["reasons"] + [why]}
    if not volume.read_identity(archive) or not os.path.isfile(os.path.join(archive, "events.jsonl")):
        return unresolved("not a credit-tape archive written by the collector (no identity or events)")
    m = NAME.match(name)
    agency = slices.CHANNELS.get(channel, {}).get("prefix", "")
    if not m or not agency or f"/{m.group(1).lower()}/" not in agency:
        return unresolved("file name is not a numbered intraday slice of this channel")
    try:
        with open(zip_path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        return unresolved(f"saved ZIP unreadable: {exc}")
    sha = hashlib.sha256(blob).hexdigest()
    res["sha256"] = sha

    # Identity 1: the tape's own saved index entry (sliceId, rowCount, source path).
    entries = index_entries(archive, channel, name)
    if not entries:
        return unresolved("no saved index entry for this slice: its sliceId and source path are unknown")
    keys = {(e.get("sliceId"), e.get("rowCount"), e.get("fullFilePath")) for e in entries}
    if len(keys) != 1:
        return unresolved("saved index entries disagree for this slice")
    meta = {k: v for k, v in entries[0].items() if not k.startswith("_")}
    sid = meta.get("sliceId")
    u = urlsplit(meta.get("fullFilePath") or "")
    if (type(sid) is not int or u.scheme != "https" or u.hostname != slices.SLICE_HOST
            or not u.path.startswith(slices.CHANNELS[channel]["prefix"]) or u.path.rsplit("/", 1)[-1] != name):
        return unresolved("saved index entry is not a valid slice of this channel")
    res["identity"] = {"sliceId": sid, "rowCount": meta.get("rowCount"), "index_snapshots": [e["_snapshot"] for e in entries]}

    # Identity 2: the bytes are the ones recorded when saved (or a supplied hash).
    recorded = recorded_sha(archive, channel, name)
    expected = {r["sha256"] for r in recorded} | ({sha256.lower()} if sha256 else set())
    if not expected:
        return unresolved("no recorded or supplied sha256 for this ZIP: bytes cannot be tied to the source")
    if expected != {sha}:
        return unresolved("ZIP bytes do not match the recorded sha256")
    res["identity"]["sha256_from"] = [r["marker"] for r in recorded] + (["--sha256"] if sha256 else [])

    # Identity 3: content agrees with the index entry.
    try:
        rows = slices.decode_rows(blob)
    except Exception as exc:  # noqa: BLE001
        return unresolved(f"ZIP does not decode: {exc!r}"[:200])
    if len(rows) != meta.get("rowCount"):
        return unresolved(f"decoded {len(rows)} rows, index entry says {meta.get('rowCount')}")

    events, alerts = os.path.join(archive, "events.jsonl"), os.path.join(archive, "alerts_outbox.jsonl")
    opener = _LocalOnly(meta["fullFilePath"], blob)
    route = slices.SliceRoute(archive, capture.now, capture.append_jsonl, opener=opener, lock=capture.WRITE_LOCK)
    pk = route.pkey(channel, sid)
    if route._done(channel, sid):
        route.pending.pop(pk, None)
        route._save()
        return {**res, "status": "already_complete", "commit_key": route.commit_key(channel, sid),
                "commit_path_reads_served_locally": opener.requests}
    with route.lock:
        route.pending[pk] = {"key": channel, "meta": meta, "attempts": 0, "retry_at": 0.0,
                             "enqueued_at": capture.now(), "enqueued_ts": route.clock(), "last_error": None,
                             "restored": {"source": "saved_numbered_intraday_zip", "sha256": sha,
                                          "identity": "saved index entry + recorded sha256 + rowCount"}}
        route.seen.add(pk)
        route._save()
    route._process(pk, events, alerts)
    if not route._done(channel, sid):
        p = route.pending.get(pk) or {}
        return unresolved(f"commit path did not complete: {p.get('last_error')}")
    return {**res, "status": "restored", "commit_key": route.commit_key(channel, sid), "rows": len(rows),
            "commit_path_reads_served_locally": opener.requests}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.environ.get("CREDIT_TAPE_ARCHIVE"), required=False)
    ap.add_argument("--channel", required=True, choices=sorted(slices.CHANNELS))
    ap.add_argument("--zip", required=True)
    ap.add_argument("--sha256")
    a = ap.parse_args(argv)
    if not a.archive:
        ap.error("--archive or CREDIT_TAPE_ARCHIVE required")
    result = restore(a.archive, a.channel, a.zip, a.sha256)
    print(json.dumps(result, indent=1))
    return 0 if result["status"] in ("restored", "already_complete") else 1


if __name__ == "__main__":
    sys.exit(main())
