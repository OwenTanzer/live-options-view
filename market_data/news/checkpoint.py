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


def _listing(store) -> dict[str, dict[str, str]] | None:
    lister = getattr(store, "list_meta", None)
    if lister is None:
        return None
    meta = lister(RUNS_PREFIX)
    return {prefix: {name: meta[prefix + name] for name in sorted(names)}
            for prefix, names in group_run_keys(meta).items()}


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


def read_history(store, clock=None, *, use_checkpoint: bool = True, max_age_s: int | None = FULL_VERIFY_S) -> HistoryScan:
    """Committed history, equal to scan_history(store), read through the checkpoint when it is usable.

    clock is given by writers: it dates a full verification and enforces max_age_s. Readers omit it.
    """
    now = clock() if clock is not None else None
    if not use_checkpoint:
        return _full(store, _listing(store), "disabled", now)
    listing = _listing(store)
    if listing is None:
        return _full(store, None, "store has no listing metadata", now)
    head, body, reason = load_checkpoint(store)
    if body is not None and now is not None and max_age_s is not None:
        verified = parse_iso(body["full_verified_at"])
        if now - verified > timedelta(seconds=max_age_s):
            body, reason = None, f"stale: last full verification {body['full_verified_at']} is over {max_age_s}s old"
    if body is None:
        return _full(store, listing, reason, now)

    entries = {e["prefix"]: e for e in body["runs"]}
    scan, reverified, read, matched = HistoryScan(), [], 0, 0
    for prefix in sorted(listing):
        e = entries.get(prefix)
        if e is not None and e["listing"] == listing[prefix]:
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
    for prefix in sorted(set(entries) - set(listing)):
        scan.problems.append({"run_id": entries[prefix]["manifest"]["run_id"], "prefix": prefix,
                              "detail": "checkpointed run is missing from the store"})
    scan.checkpoint = {"mode": "checkpoint", "reason": "", "checkpoint_key": head["key"],
                       "runs_from_checkpoint": matched, "runs_read": read, "reverified": reverified, "full_verified_at": body["full_verified_at"]}
    return sort_runs(scan)


def save_checkpoint(store, scan: HistoryScan, clock) -> dict:
    """Write a checkpoint of every settled run, after a commit, under the writer lease.

    scan is the history this cycle read; runs whose listing has not changed since are taken from it,
    anything else (the run just committed, runs settled this cycle) is read and verified again.
    """
    listing = _listing(store)
    if listing is None:
        raise ArchiveError("store has no listing metadata; the checkpoint is not used")
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
            "runs": entries}
    data = encode_body(body)
    digest = sha256_hex(data)
    key = f"{BODY_PREFIX}{digest}.json.gz"
    current = store.get(HEAD_KEY)
    try:
        previous = json.loads(current) if current is not None else {}
    except ValueError:
        previous = {}
    previous_key = previous.get("key") if isinstance(previous, dict) else None
    if previous_key == key and previous.get("sha256") == digest and store.get(key) == data:
        return {"key": key, "runs": len(entries), "written": False}
    store.put(key, data, "application/gzip")
    if store.get(key) != data:
        raise ArchiveIntegrityError(f"{key} did not read back as written")
    head = encode_json({"schema_version": SCHEMA, "compatibility": compatibility(), "key": key, "bytes": len(data),
                        "sha256": digest, "runs": len(entries), "full_verified_at": full_verified_at,
                        "built_at": iso(clock()), "previous_key": previous_key})
    store.put(HEAD_KEY, head, "application/json")
    if store.get(HEAD_KEY) != head:
        raise ArchiveIntegrityError(f"{HEAD_KEY} did not read back as written")
    for old in store.list(BODY_PREFIX):
        if old not in (key, previous_key):
            store.delete(old)
    return {"key": key, "runs": len(entries), "written": True}


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
        out.update({k: head.get(k) for k in ("key", "bytes", "runs", "full_verified_at", "built_at")})
    return out


def compare(store) -> dict:
    """The reference check: checkpoint-plus-tail against full replay, run by run."""
    full = scan_history(store)
    fast = read_history(store)

    def shape(scan):
        return ([(r.manifest_key, r.manifest_sha256, r.manifest, r.observations, r.classifications) for r in scan.runs],
                scan.problems, scan.uncommitted)

    a, b = shape(full), shape(fast)
    return {"equal": a == b, "runs": len(full.runs), "corrupt_runs": len(full.problems), "read": fast.checkpoint,
            "differences": [] if a == b else [name for name, x, y in zip(("runs", "problems", "uncommitted"), a, b)
                                              if x != y]}
