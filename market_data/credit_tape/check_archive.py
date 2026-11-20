"""OA-242 archive integrity check (read-only). Run before and after a restart.

Proves it is reading the actual persistent archive, then re-verifies it:
  * volume: with --volume (default CREDIT_TAPE_VOLUME / RAILWAY_VOLUME_MOUNT_PATH)
    the archive must sit inside that path and the path must be a mount point
  * the archive exists, is a credit-tape archive, and carries its one-time
    identity (archive.json); --compare requires the SAME archive_id before and
    after, so a restart that came up on a fresh or temporary directory fails
  * every completed commit record is validated against
      - its original ZIP: present and byte-identical (sha256) to what was committed
      - the logs: each committed event and alert line present, identical
      - the ZIP content: re-decoded rows match the committed publications
        (count, row order, record, Dissemination Identifier); a recorded
        decode failure must still fail to decode
  * no logged commit line without a commit record; no duplicate
    (commit_key, commit_line); no torn or unreadable lines; every commit has
    its done marker; no leftover .part files
  * no ticker output of any kind (the service is slice-only)
  * reports publication counts and pending live/backfill per channel

Usage: python -m market_data.credit_tape.check_archive --archive <dir> [--volume <mount>] [--compare before.json]
Prints JSON; exit 0 only if clean (and, with --compare, if the archive_id is
unchanged, no channel lost publications, completed commits did not fall and
the ticker count did not grow).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

from market_data.credit_tape import slices, volume

CHANNEL_SOURCES = {"dtcc-cftc-slices": "CFTC_CR", "dtcc-sec-slices": "SEC_CR"}


def _scan(path):
    torn, unreadable, index, keys, rows = False, 0, {}, Counter(), []
    if not os.path.exists(path):
        return torn, unreadable, index, keys, rows
    with open(path, "rb") as fh:
        data = fh.read()
    torn = bool(data) and not data.endswith(b"\n")
    for raw in data.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            unreadable += 1
            continue
        rows.append(obj)
        if obj.get("commit_key") is not None:
            k = (obj["commit_key"], obj.get("commit_line"))
            keys[k] += 1
            index.setdefault(k, obj)
    return torn, unreadable, index, keys, rows


TICKER_SOURCES = ("dtcc-cftc-credits", "dtcc-sec-credits")
MAX_LISTED = 50


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _validate_commit(archive, commit, ev_index, al_index):
    """Problems for one completed commit: original ZIP, committed lines, and ZIP content vs lines."""
    key, problems = commit.get("key"), []
    add = lambda what, **kw: problems.append({"commit": key, "problem": what, **kw})
    blob = None
    if commit.get("raw_ref"):
        path = os.path.join(archive, commit["raw_ref"])
        if not os.path.exists(path):
            add("original_missing", raw_ref=commit["raw_ref"])
        else:
            with open(path, "rb") as fh:
                blob = fh.read()
            if hashlib.sha256(blob).hexdigest() != commit.get("raw_sha256"):
                add("original_hash_mismatch", raw_ref=commit["raw_ref"])
                blob = None
    for name, index in (("events", ev_index), ("alerts", al_index)):
        for line in commit.get(name) or []:
            logged = index.get((key, line.get("commit_line")))
            if logged is None:
                add(f"committed_{name[:-1]}_line_missing", commit_line=line.get("commit_line"), kind=line.get("kind"))
            elif logged != line:
                add(f"committed_{name[:-1]}_line_differs", commit_line=line.get("commit_line"), kind=line.get("kind"))
    if blob is not None:
        evs = commit.get("events") or []
        kinds = {e.get("kind") for e in evs}
        try:
            rows = slices.decode_rows(blob)
        except Exception:
            rows = None
        if "decode_failure" in kinds:
            if rows is not None:
                add("decode_failure_recorded_but_original_decodes")
        elif rows is None:
            add("original_does_not_decode")
        else:
            pubs = [e for e in evs if e.get("kind") == "publication"]
            sl = next((e for e in evs if e.get("kind") == "slice"), None)
            if sl is not None and sl.get("decoded_rows") != len(rows):
                add("slice_row_count_differs_from_original", committed=sl.get("decoded_rows"), original=len(rows))
            if len(pubs) != len(rows):
                add("publication_count_differs_from_original", committed=len(pubs), original=len(rows))
            for e in pubs:
                i = e.get("row_index")
                if not isinstance(i, int) or not 0 <= i < len(rows) or e.get("record") != rows[i] \
                        or e.get("id") != rows[i].get("Dissemination Identifier"):
                    add("publication_differs_from_original_row", row_index=i)
    return problems


def check(archive: str, volume_root: str | None = None, is_mount=os.path.ismount) -> dict:
    base = {"archive": os.path.abspath(archive)}
    if volume_root:
        try:
            volume.require_volume(os.path.abspath(archive), {"CREDIT_TAPE_VOLUME": volume_root}, is_mount)
            base["volume"] = {"root": volume_root, "mounted_and_contains_archive": True}
        except ValueError as exc:
            return {**base, "clean": False, "volume": {"root": volume_root, "mounted_and_contains_archive": False},
                    "error": str(exc)}
    else:
        base["volume"] = None  # not checked: pass --volume (or set CREDIT_TAPE_VOLUME / RAILWAY_VOLUME_MOUNT_PATH)
    if not os.path.isdir(archive) or not os.path.isdir(os.path.join(archive, "dtcc-slices")):
        # A missing, mistyped or unmounted archive must never read as "clean".
        return {**base, "clean": False, "error": "archive missing or not a credit-tape archive (no dtcc-slices/)"}
    ident = volume.read_identity(archive)
    sdir = os.path.join(archive, "dtcc-slices")
    cdir, ddir = os.path.join(sdir, "commits"), os.path.join(sdir, "done")
    names = {f for f in os.listdir(cdir) if f.endswith(".json")} if os.path.isdir(cdir) else set()
    done = set(os.listdir(ddir)) if os.path.isdir(ddir) else set()
    open_commits = sorted(c for c in names if c not in done)
    problems, records = [], {}
    for fn in sorted(names):
        try:
            with open(os.path.join(cdir, fn), encoding="utf-8") as fh:
                records[fn] = json.load(fh)
        except (OSError, ValueError):
            problems.append({"commit": fn, "problem": "commit_record_unreadable"})
    # Scan the standard logs plus every log a commit names (validation follows the commit's own paths).
    logs = {"events.jsonl", "alerts_outbox.jsonl"}
    for c in records.values():
        for name in ("events_path", "alerts_path"):
            rel = c.get(name)
            if isinstance(rel, str) and not os.path.isabs(rel) and ".." not in rel.replace("\\", "/").split("/"):
                logs.add(rel)
    scans = {rel: _scan(os.path.join(archive, rel)) for rel in sorted(logs)}
    events = scans["events.jsonl"][4]
    pubs = Counter(e.get("source") for e in events if e.get("kind") == "publication")
    validated = raw_checked = 0
    for fn, commit in records.items():
        if fn in done:
            ev_index = scans.get(commit.get("events_path"), scans["events.jsonl"])[2]
            al_index = scans.get(commit.get("alerts_path"), scans["alerts_outbox.jsonl"])[2]
            problems += _validate_commit(archive, commit, ev_index, al_index)
            validated += 1
            raw_checked += bool(commit.get("raw_ref"))
    known_keys = {c.get("key") for c in records.values()}
    orphans = sorted({k for sc in scans.values() for k, _ in sc[3] if k not in known_keys})
    for k in orphans:
        problems.append({"commit": k, "problem": "logged_lines_without_commit_record"})
    parts = [os.path.join(r, f) for r, _, fs in os.walk(archive) for f in fs if f.endswith(".part")]
    pending = {}
    try:
        with open(os.path.join(sdir, "pending.json"), encoding="utf-8") as fh:
            for v in json.load(fh).values():
                ch = v.get("key", "?")
                pending[ch] = pending.get(ch, 0) + 1
    except (OSError, ValueError):
        pass
    dups = {rel: sum(n - 1 for n in sc[3].values() if n > 1) for rel, sc in scans.items()}
    torn = {rel: sc[0] for rel, sc in scans.items()}
    unreadable = {rel: sc[1] for rel, sc in scans.items()}
    ticker_pubs = sum(n for s, n in pubs.items() if s in TICKER_SOURCES)
    # Any ticker output at all (events of any kind, or a ticker archive directory) fails a slice-only check.
    ticker_events = sum(1 for e in events if e.get("source") in TICKER_SOURCES)
    ticker_dirs = [d for d in TICKER_SOURCES if os.path.exists(os.path.join(archive, d))]
    result = {
        **base,
        "archive_id": ident.get("archive_id") if ident else None,
        "archive_created_at": ident.get("created_at") if ident else None,
        "publications": {CHANNEL_SOURCES.get(s, s): n for s, n in sorted(pubs.items(), key=lambda kv: str(kv[0]))},
        "ticker_publications": ticker_pubs,
        "ticker_events": ticker_events,
        "ticker_dirs": ticker_dirs,
        "commits_completed_validated": validated,
        "originals_rechecked": raw_checked,
        "commit_problems": len(problems),
        "commit_problem_list": problems[:MAX_LISTED],
        "logs_checked": sorted(scans),
        "duplicate_lines": dups,
        "torn_tail": torn,
        "unreadable_lines": unreadable,
        "commits_without_done_marker": open_commits,
        "part_files": parts,
        "pending_by_channel": pending,
    }
    result["clean"] = not (ident is None or problems or any(dups.values()) or any(torn.values())
                           or any(unreadable.values()) or open_commits or parts or ticker_events or ticker_dirs)
    if ident is None:
        result["error"] = "archive has no identity (archive.json); not written by the collector"
    return result


def compare(before: dict, after: dict) -> dict:
    if "publications" not in before or "publications" not in after:
        return {"ok": False, "error": "a compared check has no archive"}
    same = bool(before.get("archive_id")) and before.get("archive_id") == after.get("archive_id")
    lost = {ch: [before["publications"].get(ch, 0), after["publications"].get(ch, 0)]
            for ch in set(before["publications"]) | set(after["publications"])
            if after["publications"].get(ch, 0) < before["publications"].get(ch, 0)}
    fewer = after.get("commits_completed_validated", 0) < before.get("commits_completed_validated", 0)
    return {"same_archive_id": same, "publications_lost": lost, "completed_commits_fell": fewer,
            "ticker_publications_grew": after["ticker_publications"] > before["ticker_publications"],
            "ok": same and not lost and not fewer and after["ticker_publications"] <= before["ticker_publications"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.environ.get("CREDIT_TAPE_ARCHIVE"))
    ap.add_argument("--volume", default=volume.volume_root(),
                    help="persistent volume mount the archive must be inside (default: CREDIT_TAPE_VOLUME or "
                         "RAILWAY_VOLUME_MOUNT_PATH)")
    ap.add_argument("--compare", help="JSON written by an earlier run of this check")
    a = ap.parse_args()
    if not a.archive:
        ap.error("--archive or CREDIT_TAPE_ARCHIVE required")
    result = check(a.archive, a.volume)
    ok = result["clean"]
    if a.compare:
        with open(a.compare, encoding="utf-8") as fh:
            result["vs_before"] = compare(json.load(fh), result)
        ok = ok and result["vs_before"]["ok"]
    print(json.dumps(result, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
