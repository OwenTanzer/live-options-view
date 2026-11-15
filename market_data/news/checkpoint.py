"""History checkpoint: a disposable speed-up over full replay, never a second source of truth.

Full replay (archive.scan_history) reads and verifies every committed run on every read. The
checkpoint holds the verified contents of *settled* runs (manifest read back and commit.json
recorded), so a read lists the run prefix once, reads the checkpoint, and fully reads only:

  - runs not in the checkpoint (the tail: new, availability-pending, uncommitted or corrupt runs);
  - checkpointed runs whose listing changed: any object added, removed or rewritten (the listing's
    size and ETag locally size and mtime differ from the ones the run was verified against).

Layout (private store, outside news/v1/runs/, so the archive itself is never touched or pruned):

  news/v1/checkpoint/history/{sha256}.json.gz   content-addressed body (all checkpointed runs)
  news/v1/checkpoint/head.json                  pointer: key, bytes, sha256, run count, times

The body is written and read back before the head points at it; the current and previous bodies
are kept, so a reader that read the old head still finds its body. A crash between the two leaves
the old head (and an orphan body that the next save removes).

The checkpoint is used only when it is present, intact and compatible, and it is refused when it is
stale. Anything else falls back to full replay, which also rebuilds it on the next writer save:

  missing        no head
  corrupt        head or body unreadable, body missing, size/digest/shape mismatch
  incompatible   different checkpoint schema, readable manifest schemas, commit schema or objects
  stale          (writers only) its last full verification is older than FULL_VERIFY_S; or a
                 checkpointed run is missing from the store, which fails visibly as corrupt history
                 (a full replay alone could not notice that a run had disappeared)

Only writers (collect_once, enrich_pending, `checkpoint rebuild`) save it, under the writer lease.
Readers (views, indexes, query, join) use it read-only and never write.

Bounded listing (the frontier). Raw runs are immutable and never pruned, so listing all of
news/v1/runs/ on every read would grow without bound. The head records a frontier: the last run
prefix (in key order) such that every run prefix at or before it was settled and is in the body.
Reads list only the keys after the frontier (ListObjectsV2 StartAfter) and take the runs at or before
it from the body. Three tiers keep this correct:

  every read      list after the frontier; runs at or before it come from the body. A run that is
                  not settled (uncommitted, availability pending, corrupt) is never passed by the
                  frontier, so it is always listed and re-read until it settles.
  every FULL_VERIFY_S (writers)
                  list all of news/v1/runs/ and compare every run's listing tags (size, ETag) with the
                  body: a changed run is re-read and verified, a missing one fails visibly. This is
                  a daily metadata check. Writers also replay raw bytes in quarter-hour cohorts,
                  verifying each settled run daily while spreading GETs across scheduled cycles.
  full replay     reading every object again only when the checkpoint is missing, corrupt or
                  incompatible, when disabled, and for `checkpoint rebuild` / `compare`.

A writer that is about to upload a run whose prefix is at or before the frontier (a spool recovered
under its original, older run_id, or a clock that went backwards) first clears the frontier durably
(guard_frontier), before any upload. A crash after that upload cannot hide the run: the next read has
no frontier and lists everything, and the next save sets a new frontier from that full listing.
A change to a run at or before the frontier (an object rewritten or deleted outside the writer) is
detected at the next full listing, at most FULL_VERIFY_S later, not on every read.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta

from . import config
from .archive import (ArchiveError, ArchiveIntegrityError, CommittedRun, HistoryScan, OBJECT_NAMES, RUNS_PREFIX,
                      encode_json, group_run_keys, scan_history, scan_one, sort_runs)
from .normalize import iso, parse_iso, sha256_hex

CHECKPOINT_PREFIX = f"{config.NAMESPACE}/checkpoint/"
HEAD_KEY = CHECKPOINT_PREFIX + "head.json"
BODY_PREFIX = CHECKPOINT_PREFIX + "history/"
SCHEMA = "news-history-checkpoint/v1"
FULL_VERIFY_S = 24 * 3600   # writers replay everything at least this often
RUN_NAMES = frozenset({"manifest.json", "commit.json"} | {f"{n}.jsonl.gz" for n in OBJECT_NAMES})
_ENTRY_KEYS = {"prefix", "manifest_key", "manifest_sha256", "manifest", "listing", *OBJECT_NAMES}


def compatibility() -> str:
    """Everything a checkpoint's contents depend on. A change makes every older checkpoint incompatible."""
    return sha256_hex(json.dumps({"checkpoint": SCHEMA, "manifests": list(config.READABLE_MANIFEST_SCHEMAS),
                                  "commit": config.COMMIT_SCHEMA, "objects": list(OBJECT_NAMES),
                                  "run_names": sorted(RUN_NAMES)}, sort_keys=True).encode())


def encode_body(body: dict) -> bytes:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return gzip.compress(raw, mtime=0)


def _after(frontier: str) -> str:
    """The StartAfter key that skips the frontier run and everything before it. Run prefixes have a fixed
    length (date/run_id/), and every object name sorts before '~', so the next run's keys all sort after."""
    return frontier + "~"


def _listing(store, frontier: str | None = None) -> dict[str, dict[str, str]] | None:
    """Run prefix -> {object name: listing tag}; only the runs after frontier when one is given."""
    lister = getattr(store, "list_meta", None)
    if lister is None:
        return None
    meta = lister(RUNS_PREFIX, start_after=_after(frontier)) if frontier else lister(RUNS_PREFIX)
    return {prefix: {name: meta[prefix + name] for name in sorted(names)}
            for prefix, names in group_run_keys(meta).items()}


def _usable_frontier(store, head: dict | None, body: dict | None) -> str | None:
    """The head's frontier, only when the store can list after a key and the frontier names a body run."""
    if not head or body is None or not getattr(store, "supports_start_after", False):
        return None
    frontier = head.get("frontier")
    if not isinstance(frontier, str) or not frontier.startswith(RUNS_PREFIX):
        return None
    return frontier if any(e["prefix"] == frontier for e in body["runs"]) else None


def frontier_of(listed_prefixes, checkpointed) -> str | None:
    """The last prefix, in key order, before which every listed run is checkpointed (None if the first is not)."""
    frontier = None
    for prefix in sorted(listed_prefixes):
        if prefix not in checkpointed:
            break
        frontier = prefix
    return frontier


def guard_frontier(store, prefixes) -> dict | None:
    """Before uploading runs at these prefixes: if any is at or before the head's frontier, clear the
    frontier durably first (reads then list everything until the next save). Returns what was cleared."""
    raw = store.get(HEAD_KEY)
    if raw is None:
        return None
    try:
        head = json.loads(raw)
    except ValueError:
        return None
    frontier = head.get("frontier") if isinstance(head, dict) else None
    if not isinstance(frontier, str):
        return None
    behind = sorted(p for p in prefixes if p <= frontier)
    if not behind:
        return None
    cleared = dict(head, frontier=None, frontier_cleared={"was": frontier, "for": behind})
    data = encode_json(cleared)
    store.put(HEAD_KEY, data, "application/json")
    if store.get(HEAD_KEY) != data:
        raise ArchiveIntegrityError(f"{HEAD_KEY} did not read back after clearing the frontier")
    return {"was": frontier, "for": behind}


def _body_problem(head: dict, body) -> str:
    if not isinstance(body, dict):
        return "body is not an object"
    if body.get("schema_version") != SCHEMA or body.get("compatibility") != compatibility():
        return "body schema does not match its head"
    runs = body.get("runs")
    if not isinstance(runs, list) or len(runs) != head.get("runs"):
        return "body run count does not match its head"
    if body.get("full_verified_at") != head.get("full_verified_at") or parse_iso(body.get("full_verified_at")) is None:
        return "body verification time does not match its head"
    seen = set()
    for e in runs:
        if not isinstance(e, dict) or set(e) != _ENTRY_KEYS:
            return "body entry has the wrong fields"
        m = e["manifest"]
        if (not isinstance(m, dict) or not m.get("committed_at") or not isinstance(e["listing"], dict)
                or set(e["listing"]) != RUN_NAMES or e["manifest_key"] != e["prefix"] + "manifest.json"
                or not e["prefix"].startswith(RUNS_PREFIX) or e["prefix"] in seen):
            return f"body entry {e.get('prefix')!r} is malformed"
        for name in OBJECT_NAMES:
            meta = (m.get("objects") or {}).get(name) or {}
            if not isinstance(e[name], list) or len(e[name]) != meta.get("records"):
                return f"body entry {e['prefix']!r} {name} count does not match its manifest"
        seen.add(e["prefix"])
    return ""


def load_checkpoint(store) -> tuple[dict | None, dict | None, str]:
    """(head, body, why not usable). Two reads: the head and the body it points at."""
    raw = store.get(HEAD_KEY)
    if raw is None:
        return None, None, "missing"
    try:
        head = json.loads(raw)
    except ValueError:
        return None, None, "corrupt: head unreadable"
    if not isinstance(head, dict):
        return None, None, "corrupt: head is not an object"
    if head.get("schema_version") != SCHEMA or head.get("compatibility") != compatibility():
        return head, None, (f"incompatible: checkpoint {head.get('schema_version')!r} was built for different "
                            "manifest, commit or object schemas")
    key = head.get("key")
    if not isinstance(key, str) or not key.startswith(BODY_PREFIX):
        return head, None, "corrupt: head points outside the checkpoint prefix"
    data = store.get(key)
    if data is None:
        return head, None, "corrupt: body missing"
    if len(data) != head.get("bytes") or sha256_hex(data) != head.get("sha256"):
        return head, None, "corrupt: body size/digest mismatch"
    try:
        body = json.loads(gzip.decompress(data))
    except (OSError, ValueError, EOFError):
        return head, None, "corrupt: body undecodable"
    problem = _body_problem(head, body)
    if problem:
        return head, None, f"corrupt: {problem}"
    return head, body, ""


def _full(store, listing, reason: str, now) -> HistoryScan:
    scan = HistoryScan()
    if listing is None:
        scan = scan_history(store)
    else:
        for prefix in sorted(listing):
            if scan_one(store, scan, prefix, listing[prefix]):
                scan.listing[prefix] = listing[prefix]
        sort_runs(scan)
    scan.checkpoint = {"mode": "full_replay", "reason": reason, "runs_from_checkpoint": 0,
                       "runs_read": len(listing) if listing is not None else None, "reverified": [],
                       "full_verified_at": iso(now) if now is not None else None}
    return scan


def read_history(store, clock=None, *, use_checkpoint: bool = True, max_age_s: int | None = FULL_VERIFY_S,
                 bounded: bool = True) -> HistoryScan:
    """Committed history, equal to scan_history(store), read through the checkpoint when it is usable.

    clock is given by writers: it dates a full listing verification and enforces max_age_s. Readers omit it.
    """
    now = clock() if clock is not None else None
    if not use_checkpoint:
        return _full(store, _listing(store), "disabled", now)
    head, body, reason = load_checkpoint(store)
    if body is None:
        listing = _listing(store)
        if listing is None:
            return _full(store, None, "store has no listing metadata", now)
        return _full(store, listing, reason, now)
    full_due = False
    if now is not None and max_age_s is not None:
        verified = parse_iso(body["full_verified_at"])
        full_due = now - verified > timedelta(seconds=max_age_s)
    frontier = None if (full_due or not bounded) else _usable_frontier(store, head, body)
    listing = _listing(store, frontier)
    if listing is None:
        return _full(store, None, "store has no listing metadata", now)

    entries = {e["prefix"]: e for e in body["runs"]}
    scan, reverified, read, matched, carried = HistoryScan(), [], 0, 0, 0
    # Spread one daily byte replay over the 96 ticks. Catch up missed slots after a
    # restart. An audit position is advanced only by saving a successful writer scan.
    prior_audit = parse_iso(body.get("audit_at") or body["full_verified_at"])
    audit_now = now if max_age_s is not None else None
    due = set()
    if audit_now is not None:
        tick = config.INTERVAL_S
        first = int(prior_audit.timestamp()) // tick
        last = int(audit_now.timestamp()) // tick
        slots = FULL_VERIFY_S // tick
        elapsed_slots = last - first
        for prefix, entry in entries.items():
            started = parse_iso(entry["manifest"]["started_at"])
            start_tick = int(started.timestamp()) // tick
            latest_due = last - (last - start_tick) % slots
            if elapsed_slots < 0 or latest_due > max(first, start_tick):
                due.add(prefix)
    if frontier is not None:
        # Runs at or before the frontier: settled and immutable, taken from the body unlisted.
        for prefix in sorted(p for p in entries if p <= frontier):
            e = entries[prefix]
            if prefix in due:
                scan_one(store, scan, prefix, e["listing"])
                scan.listing[prefix] = e["listing"]
                read += 1
                continue
            scan.runs.append(CommittedRun(dict(e["manifest"]), e["manifest_key"], e["manifest_sha256"],
                                          e["observations"], e["classifications"]))
            scan.listing[prefix] = e["listing"]
            carried += 1
    for prefix in sorted(listing):
        e = entries.get(prefix)
        if prefix not in due and e is not None and e["listing"] == listing[prefix]:
            scan.runs.append(CommittedRun(dict(e["manifest"]), e["manifest_key"], e["manifest_sha256"],
                                          e["observations"], e["classifications"]))
            scan.listing[prefix] = listing[prefix]
            matched += 1
            continue
        if e is not None:
            reverified.append(e["manifest"]["run_id"])
        read += 1
        if scan_one(store, scan, prefix, listing[prefix]):
            scan.listing[prefix] = listing[prefix]
    listed_range = {p for p in entries if frontier is None or p > frontier}
    for prefix in sorted(listed_range - set(listing)):
        scan.problems.append({"run_id": entries[prefix]["manifest"]["run_id"], "prefix": prefix,
                              "detail": "checkpointed run is missing from the store"})
    scan.checkpoint = {"mode": "checkpoint", "reason": "", "checkpoint_key": head["key"],
                       "listing": "after_frontier" if frontier is not None else "full",
                       "audit_at": iso(audit_now) if audit_now else iso(prior_audit),
                       "frontier": frontier, "runs_carried": carried, "runs_listed": len(listing),
                       "runs_from_checkpoint": matched + carried, "runs_read": read, "reverified": reverified,
                       "full_verified_at": iso(now) if full_due else body["full_verified_at"]}
    if full_due:
        scan.checkpoint["reason"] = (f"full listing verification: last one {body['full_verified_at']} is over "
                                     f"{max_age_s}s old")
    return sort_runs(scan)


def save_checkpoint(store, scan: HistoryScan, clock) -> dict:
    """Write a checkpoint of every settled run, after a commit, under the writer lease.

    scan is the history this cycle read; runs whose listing has not changed since are taken from it,
    anything else (the run just committed, runs settled this cycle) is read and verified again.
    """
    # The same bound the read used, unless the frontier was cleared since (guard_frontier): then all.
    frontier = scan.checkpoint.get("frontier")
    current = store.get(HEAD_KEY)
    try:
        current_head = json.loads(current) if current is not None else {}
    except ValueError:
        current_head = {}
    if not isinstance(current_head, dict) or current_head.get("frontier") != frontier:
        frontier = None
    listing = _listing(store, frontier)
    if listing is None:
        raise ArchiveError("store has no listing metadata; the checkpoint is not used")
    if frontier is not None:
        # Runs at or before the frontier were taken from the body by the read; carry them with their tags.
        carried = {p: scan.listing[p] for p in scan.listing if p <= frontier}
        listing = dict(carried, **listing)
    known = {run.manifest_key[:-len("manifest.json")]: run for run in scan.runs}
    reread = HistoryScan()
    entries = []
    for prefix in sorted(listing):
        if set(listing[prefix]) != RUN_NAMES:
            continue   # uncommitted, or availability not yet recorded: stays in the tail
        run = known.get(prefix)
        if run is None or scan.listing.get(prefix) != listing[prefix]:
            run = scan_one(store, reread, prefix, listing[prefix])
            if run is None:
                continue   # corrupt: left out, so every later read verifies it (and reports it)
        if run.committed_at is None:
            continue
        entries.append({"prefix": prefix, "manifest_key": run.manifest_key, "manifest_sha256": run.manifest_sha256,
                        "manifest": run.manifest, "listing": listing[prefix],
                        "observations": run.observations, "classifications": run.classifications})
    full_verified_at = scan.checkpoint.get("full_verified_at") or iso(clock())
    body = {"schema_version": SCHEMA, "compatibility": compatibility(), "full_verified_at": full_verified_at,
            "audit_at": scan.checkpoint.get("audit_at") or full_verified_at, "runs": entries}
    checkpointed = {e["prefix"] for e in entries}
    new_frontier = frontier_of(listing, checkpointed)
    # The first listed run the frontier could not pass (unsettled, corrupt, or with a stray object): it is
    # listed and read on every cycle until it settles, and later runs are listed too. Reported, never hidden.
    stopped_at = next((p for p in sorted(listing) if p not in checkpointed), None)
    data = encode_body(body)
    digest = sha256_hex(data)
    key = f"{BODY_PREFIX}{digest}.json.gz"
    previous = current_head if isinstance(current_head, dict) else {}
    previous_key = previous.get("key")
    if (previous_key == key and previous.get("sha256") == digest and previous.get("frontier") == new_frontier
            and previous.get("frontier_stopped_at") == stopped_at and store.get(key) == data):
        return {"key": key, "runs": len(entries), "written": False, "frontier": new_frontier}
    store.put(key, data, "application/gzip")
    if store.get(key) != data:
        raise ArchiveIntegrityError(f"{key} did not read back as written")
    head = encode_json({"schema_version": SCHEMA, "compatibility": compatibility(), "key": key, "bytes": len(data),
                        "sha256": digest, "runs": len(entries), "full_verified_at": full_verified_at,
                        "built_at": iso(clock()), "previous_key": previous_key, "frontier": new_frontier,
                        "frontier_stopped_at": stopped_at})
    store.put(HEAD_KEY, head, "application/json")
    if store.get(HEAD_KEY) != head:
        raise ArchiveIntegrityError(f"{HEAD_KEY} did not read back as written")
    for old in store.list(BODY_PREFIX):
        if old not in (key, previous_key):
            store.delete(old)
    return {"key": key, "runs": len(entries), "written": True, "frontier": new_frontier}


def rebuild(store, clock, *, lease_holder=None, break_stale_lease=False) -> dict:
    """Replay every run, then write a fresh checkpoint. Refused (nothing written) if history is corrupt."""
    from .archive import HistoryCorrupt, WriterLease

    with WriterLease(store, clock, lease_holder, break_stale=break_stale_lease):
        _, _, before = load_checkpoint(store)
        scan = read_history(store, clock, use_checkpoint=False)
        if scan.problems:
            raise HistoryCorrupt(scan.problems)
        saved = save_checkpoint(store, scan, clock)
    return {"previous": before or "usable", "runs": saved["runs"], "key": saved["key"], "written": saved["written"]}


def status(store) -> dict:
    head, body, why = load_checkpoint(store)
    out = {"usable": body is not None, "reason": why}
    if isinstance(head, dict):
        out.update({k: head.get(k) for k in ("key", "bytes", "runs", "full_verified_at", "built_at", "frontier",
                                            "frontier_stopped_at", "frontier_cleared")})
    return out


def compare(store) -> dict:
    """The reference check: checkpoint-plus-tail against full replay, run by run."""
    full = scan_history(store)
    fast = read_history(store, bounded=False)   # the operator check lists every run

    def shape(scan):
        return ([(r.manifest_key, r.manifest_sha256, r.manifest, r.observations, r.classifications) for r in scan.runs],
                scan.problems, scan.uncommitted)

    a, b = shape(full), shape(fast)
    return {"equal": a == b, "runs": len(full.runs), "corrupt_runs": len(full.problems), "read": fast.checkpoint,
            "differences": [] if a == b else [name for name, x, y in zip(("runs", "problems", "uncommitted"), a, b)
                                              if x != y]}
