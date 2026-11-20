"""OA-242 archive integrity check (read-only). Run before and after a restart.

Checks the private archive the service writes:
  * no torn or unreadable lines in events.jsonl / alerts_outbox.jsonl
  * no duplicate (commit_key, commit_line) in either log, i.e. no slice
    published twice
  * every slice commit has its done marker (nothing half-published)
  * every slice raw ZIP referenced by an event exists and matches the
    sha256 recorded when it was published; no leftover .part files
  * no ticker output of any kind (events or ticker archive directories):
    the service is slice-only
  * the archive exists and is a credit-tape archive (a missing or unmounted
    path fails instead of reading as clean)
  * reports publication counts and pending live/backfill per channel

Usage: python -m market_data.credit_tape.check_archive --archive <dir> [--compare before.json]
Prints JSON; exit 0 only if clean (and, with --compare, if no channel lost
publications and the ticker count did not grow).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

CHANNEL_SOURCES = {"dtcc-cftc-slices": "CFTC_CR", "dtcc-sec-slices": "SEC_CR"}


def _scan(path):
    torn, unreadable, keys, rows = False, 0, Counter(), []
    if not os.path.exists(path):
        return torn, unreadable, keys, rows
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
            keys[(obj["commit_key"], obj.get("commit_line"))] += 1
    return torn, unreadable, keys, rows


TICKER_SOURCES = ("dtcc-cftc-credits", "dtcc-sec-credits")


def check(archive: str) -> dict:
    if not os.path.isdir(archive) or not os.path.isdir(os.path.join(archive, "dtcc-slices")):
        # A missing, mistyped or unmounted archive must never read as "clean".
        return {"archive": os.path.abspath(archive), "clean": False,
                "error": "archive missing or not a credit-tape archive (no dtcc-slices/)"}
    ev_torn, ev_bad, ev_keys, events = _scan(os.path.join(archive, "events.jsonl"))
    al_torn, al_bad, al_keys, _ = _scan(os.path.join(archive, "alerts_outbox.jsonl"))
    pubs = Counter(e.get("source") for e in events if e.get("kind") == "publication")
    raw_sha = {}
    for e in events:
        if e.get("kind") == "slice" and e.get("raw_ref"):
            raw_sha[e["raw_ref"]] = e.get("raw_sha256")
    raw_missing, raw_mismatch = [], []
    for ref, sha in raw_sha.items():
        p = os.path.join(archive, ref)
        if not os.path.exists(p):
            raw_missing.append(ref)
        elif sha and hashlib.sha256(open(p, "rb").read()).hexdigest() != sha:
            raw_mismatch.append(ref)
    sdir = os.path.join(archive, "dtcc-slices")
    commits = set(os.listdir(os.path.join(sdir, "commits"))) if os.path.isdir(os.path.join(sdir, "commits")) else set()
    done = set(os.listdir(os.path.join(sdir, "done"))) if os.path.isdir(os.path.join(sdir, "done")) else set()
    open_commits = sorted(c for c in commits if c.endswith(".json") and c not in done)
    parts = [os.path.join(r, f) for r, _, fs in os.walk(archive) for f in fs if f.endswith(".part")]
    pending = {}
    try:
        with open(os.path.join(sdir, "pending.json"), encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                ch = v.get("key", "?")
                pending.setdefault(ch, 0)
                pending[ch] += 1
    except (OSError, ValueError):
        pass
    dup_events = sum(n - 1 for n in ev_keys.values() if n > 1)
    dup_alerts = sum(n - 1 for n in al_keys.values() if n > 1)
    ticker_pubs = sum(n for s, n in pubs.items() if s in TICKER_SOURCES)
    # Any ticker output at all (events of any kind, or a ticker archive directory) fails a slice-only check.
    ticker_events = sum(1 for e in events if e.get("source") in TICKER_SOURCES)
    ticker_dirs = [d for d in TICKER_SOURCES if os.path.exists(os.path.join(archive, d))]
    result = {
        "archive": os.path.abspath(archive),
        "publications": {CHANNEL_SOURCES.get(s, s): n for s, n in sorted(pubs.items(), key=lambda kv: str(kv[0]))},
        "ticker_publications": ticker_pubs,
        "ticker_events": ticker_events,
        "ticker_dirs": ticker_dirs,
        "slices_published": len(raw_sha),
        "duplicate_event_lines": dup_events,
        "duplicate_alert_lines": dup_alerts,
        "torn_tail": {"events": ev_torn, "alerts": al_torn},
        "unreadable_lines": {"events": ev_bad, "alerts": al_bad},
        "commits_without_done_marker": open_commits,
        "raw_missing": raw_missing,
        "raw_hash_mismatch": raw_mismatch,
        "part_files": parts,
        "pending_by_channel": pending,
    }
    result["clean"] = not (dup_events or dup_alerts or ev_torn or al_torn or ev_bad or al_bad
                           or open_commits or raw_missing or raw_mismatch or parts
                           or ticker_events or ticker_dirs)
    return result


def compare(before: dict, after: dict) -> dict:
    if "publications" not in before or "publications" not in after:
        return {"ok": False, "error": "a compared check has no archive"}
    lost = {ch: [before["publications"].get(ch, 0), after["publications"].get(ch, 0)]
            for ch in set(before["publications"]) | set(after["publications"])
            if after["publications"].get(ch, 0) < before["publications"].get(ch, 0)}
    return {"publications_lost": lost,
            "ticker_publications_grew": after["ticker_publications"] > before["ticker_publications"],
            "ok": not lost and after["ticker_publications"] <= before["ticker_publications"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.environ.get("CREDIT_TAPE_ARCHIVE"))
    ap.add_argument("--compare", help="JSON written by an earlier run of this check")
    a = ap.parse_args()
    if not a.archive:
        ap.error("--archive or CREDIT_TAPE_ARCHIVE required")
    result = check(a.archive)
    ok = result["clean"]
    if a.compare:
        with open(a.compare, encoding="utf-8") as fh:
            result["vs_before"] = compare(json.load(fh), result)
        ok = ok and result["vs_before"]["ok"]
    print(json.dumps(result, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
