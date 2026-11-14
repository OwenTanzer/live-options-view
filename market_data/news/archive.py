"""Durable, append-only news archive: spool -> immutable objects -> manifest last.

Layout under the news/v1/ namespace (separate from the market manifest):

  news/v1/runs/YYYY-MM-DD/{run_id}/observations.jsonl.gz     immutable
  news/v1/runs/YYYY-MM-DD/{run_id}/classifications.jsonl.gz  immutable
  news/v1/runs/YYYY-MM-DD/{run_id}/manifest.json             written after the objects = commit
  news/v1/runs/YYYY-MM-DD/{run_id}/commit.json               availability record (committed_at)
  news/v1/index/YYYY-MM-DD.json, news/v1/latest.json          derived, rebuildable
  news/v1/_writer/lease.json                                  single-writer lease

A run is committed only when its manifest exists and every object it names
reads back with the recorded size and SHA-256. Readers ignore anything else.
The partition date is the collector's observation date, never a claimed
publication date.

committed_at is not stored in the manifest, because a time written inside an
object always precedes that object's own durability. It is stamped only after
the manifest has been written and read back, and stored in commit.json, so it
is an upper bound on when the run became durable: an archive-as-of cutoff can
never include a run before its manifest was durable. A committed run whose
commit.json is missing (a crash between the two writes) is still history for
deduplication and collector-mode reads, but archive mode excludes it until the
next writer records its availability, stamped at that later time.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .adapters.rss import default_pool, fetch_all
from .classify import classify_all, classify_rules
from .normalize import iso, normalize_entry, parse_iso, sha256_hex

RUNS_PREFIX = f"{config.NAMESPACE}/runs/"
INDEX_PREFIX = f"{config.NAMESPACE}/index/"
LATEST_KEY = f"{config.NAMESPACE}/latest.json"
LEASE_KEY = f"{config.NAMESPACE}/_writer/lease.json"
OBJECT_NAMES = ("observations", "classifications")
MAX_ENTRY_FAILURES_LISTED = 20
_KEY_RE = re.compile(r"^[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*$")


class ArchiveError(Exception):
    pass


class ArchiveIntegrityError(ArchiveError):
    """An object or manifest did not read back as written, or conflicts with an existing one."""


class HistoryCorrupt(ArchiveError):
    """Committed history failed verification; collection stops instead of resetting dedup."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("committed history failed verification: " +
                         "; ".join(f"{p['run_id']}: {p['detail']}" for p in problems[:5]))


class WriterBusy(ArchiveError):
    pass


@dataclass(frozen=True)
class CasOutcome:
    """The explicit result of one conditional write (G6). Exactly one request was sent for it.

      applied    the store answered that the write committed; etag is the new ETag when the store returned one
      rejected   the store answered 412 PreconditionFailed: this request did not apply and cannot apply later
      ambiguous  anything else (other errors, timeouts, resets, no response): the write may still commit,
                 with no time bound. Callers must not treat it as either of the other two.
    """
    status: str
    etag: str | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status == "applied"

    @property
    def rejected(self) -> bool:
        return self.status == "rejected"

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"


def content_etag(data: bytes) -> str:
    """The ETag the local and in-memory stores give an object: a quoted MD5 of its bytes. Identical
    content gives an identical ETag (as the provider documents); callers that need a write to be
    distinguishable must make its bytes unique."""
    import hashlib

    return '"' + hashlib.md5(data).hexdigest() + '"'


# ── object stores ───────────────────────────────────────────────────────────

def check_key(key: str) -> str:
    if not _KEY_RE.match(key) or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"invalid object key {key!r}")
    return key


class LocalObjectStore:
    """Directory-backed store with the same semantics the bucket store provides."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*check_key(key).split("/"))

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream",
            cache_control: str | None = None) -> None:
        # A directory has no object metadata: cache_control is accepted (same signature as the bucket
        # store) and not stored. Tests that check it use a recording store.
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)

    def last_modified(self, key: str) -> datetime | None:
        """The store's own modification time for key (here: the file's mtime, from this machine's clock)."""
        path = self._path(key)
        if not path.is_file():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    def put_if_absent(self, key: str, data: bytes, content_type: str = "application/octet-stream",
                      cache_control: str | None = None) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return True

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def get_versioned(self, key: str) -> tuple[bytes, str] | None:
        """(bytes, ETag) of one object version, or None if absent."""
        body = self.get(key)
        return None if body is None else (body, content_etag(body))

    def put_if_match(self, key: str, data: bytes, etag: str | None, content_type: str = "application/octet-stream",
                     cache_control: str | None = None, write_id: str | None = None) -> CasOutcome:
        """Compare-and-swap: write only if the object's ETag is `etag` (None: only if the key is absent).

        A directory has no conditional write. The check and the replace here are two steps, so this is
        a single-process stand-in for the bucket's atomic condition, not a cross-process guarantee."""
        if etag is None:
            created = self.put_if_absent(key, data, content_type, cache_control=cache_control)
            return CasOutcome("applied", content_etag(data)) if created else CasOutcome("rejected", detail="key exists")
        current = self.get(key)
        if current is None or content_etag(current) != etag:
            return CasOutcome("rejected", detail="ETag differs" if current is not None else "key absent")
        self.put(key, data, content_type, cache_control=cache_control)
        return CasOutcome("applied", content_etag(data))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list(self, prefix: str) -> list[str]:
        base = self.root.joinpath(*[p for p in prefix.split("/") if p])
        if not base.is_dir():
            return []
        out = []
        for path in base.rglob("*"):
            if path.is_file() and not path.name.startswith(".tmp-"):
                out.append(path.relative_to(self.root).as_posix())
        return sorted(out)

    def list_meta(self, prefix: str) -> dict[str, str]:
        """key -> a change tag from the listing alone (size and modification time; no object reads)."""
        out = {}
        for key in self.list(prefix):
            st = self._path(key).stat()
            out[key] = f"{st.st_size}:{st.st_mtime_ns}"
        return out


class R2ObjectStore:
    """Cloudflare R2 through the S3 API. Only constructed by an explicit CLI opt-in."""

    def __init__(self, client, bucket: str, cas_client=None):
        self.client = client
        self.bucket = bucket
        # A second client that sends exactly one request per call (no SDK retries). Conditional index
        # and head writes go through it only: a silent retry would turn "the first try landed" into a
        # 412. Without it put_if_match refuses, so existing two-argument callers are unchanged.
        self.cas_client = cas_client

    @classmethod
    def from_env(cls, env=os.environ):
        missing = [k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME") if not env.get(k)]
        if missing:
            raise ArchiveError(f"bucket mode needs {', '.join(missing)}")
        import boto3

        from botocore.config import Config

        kwargs = dict(endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"], aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
        client = boto3.client("s3", **kwargs)
        # ASSUMPTION (SDK, not verified here): total_max_attempts=1 means one HTTP request and no retry.
        cas_client = boto3.client("s3", config=Config(retries={"total_max_attempts": 1}), **kwargs)
        return cls(client, env["R2_BUCKET_NAME"], cas_client=cas_client)

    @staticmethod
    def _code(exc) -> str:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        extra = {"CacheControl": cache_control} if cache_control else {}
        self.client.put_object(Bucket=self.bucket, Key=check_key(key), Body=data, ContentType=content_type, **extra)

    def last_modified(self, key):
        """The bucket's own LastModified for key (server clock), or None if absent."""
        try:
            return self.client.head_object(Bucket=self.bucket, Key=check_key(key))["LastModified"]
        except Exception as exc:
            if self._code(exc) in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
        extra = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        try:
            self.client.put_object(Bucket=self.bucket, Key=check_key(key), Body=data, IfNoneMatch="*", **extra)
        except Exception as exc:
            if self._code(exc) in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                return False
            raise
        return True

    def get(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=check_key(key))["Body"].read()
        except Exception as exc:
            if self._code(exc) in ("NoSuchKey", "404"):
                return None
            raise

    def get_versioned(self, key):
        """(bytes, ETag) from one GetObject response, or None if absent.
        ASSUMPTION (provider, not verified here): the body and the ETag header describe the same version."""
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=check_key(key))
        except Exception as exc:
            if self._code(exc) in ("NoSuchKey", "404"):
                return None
            raise
        return resp["Body"].read(), resp["ETag"]

    def put_if_match(self, key, data, etag, content_type="application/octet-stream", cache_control=None,
                     write_id=None) -> CasOutcome:
        """One single-part PutObject with If-Match: <etag> (etag None: If-None-Match: *), sent once.

        Documented (R2 conditional operations): the condition is evaluated when the write commits;
        a failed condition is 412 PreconditionFailed. Only that answer is "rejected"; every other
        failure is "ambiguous" (the write may still commit).
        ASSUMPTIONS (SDK/provider, not verified here): put_object accepts IfMatch/IfNoneMatch and
        returns the new ETag; the 412 surfaces with code PreconditionFailed or 412."""
        if self.cas_client is None:
            raise ArchiveError("conditional writes need a no-retry client (R2ObjectStore.from_env provides one)")
        extra = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        if write_id:
            extra["Metadata"] = {"write-id": write_id}
        if etag is None:
            extra["IfNoneMatch"] = "*"
        else:
            extra["IfMatch"] = etag
        try:
            resp = self.cas_client.put_object(Bucket=self.bucket, Key=check_key(key), Body=data, **extra)
        except Exception as exc:
            if self._code(exc) in ("PreconditionFailed", "412"):
                return CasOutcome("rejected", detail="412 PreconditionFailed")
            return CasOutcome("ambiguous", detail=f"{type(exc).__name__}: {exc}"[:300])
        return CasOutcome("applied", (resp or {}).get("ETag"))

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=check_key(key))

    def list(self, prefix):
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return sorted(keys)

    def list_meta(self, prefix):
        """key -> size and ETag from list_objects_v2 (no object reads). A rewrite changes the ETag."""
        out = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.update({o["Key"]: f"{o['Size']}:{o['ETag']}" for o in page.get("Contents", [])})
        return dict(sorted(out.items()))


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── encoding ────────────────────────────────────────────────────────────────

def encode_jsonl_gz(records: list[dict]) -> bytes:
    body = "".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for r in records)
    return gzip.compress(body.encode("utf-8"), mtime=0)


def decode_jsonl_gz(data: bytes) -> list[dict]:
    text = gzip.decompress(data).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def encode_json(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def run_prefix(observation_date: str, run_id: str) -> str:
    return f"{RUNS_PREFIX}{observation_date}/{run_id}/"


def new_run_id(started) -> str:
    return f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


# ── single writer ───────────────────────────────────────────────────────────

class WriterLease:
    """One writer per namespace, enforced by a create-if-absent lease object.

    An expired lease is not taken over silently: the operator must confirm no
    writer is running and pass break_stale=True.
    """

    def __init__(self, store, clock, holder: str | None = None, ttl_s: int = config.LEASE_TTL_S,
                 break_stale: bool = False, key: str = LEASE_KEY):
        self.store, self.clock, self.ttl_s, self.break_stale, self.key = store, clock, ttl_s, break_stale, key
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def __enter__(self):
        now = self.clock()
        body = encode_json({"holder": self.holder, "acquired_at": iso(now),
                            "expires_at": iso(now + timedelta(seconds=self.ttl_s))})
        if self.store.put_if_absent(self.key, body):
            return self
        raw = self.store.get(self.key)
        current = json.loads(raw) if raw else {}
        expired = bool(current) and parse_iso(current.get("expires_at")) <= now
        if expired and self.break_stale:
            self.store.delete(self.key)
            if self.store.put_if_absent(self.key, body):
                return self
        state = "expired (pass --break-stale-lease after confirming no writer runs)" if expired else "active"
        raise WriterBusy(f"writer lease held by {current.get('holder', '?')} until {current.get('expires_at', '?')}: {state}")

    def __exit__(self, *exc):
        raw = self.store.get(self.key)
        if raw and json.loads(raw).get("holder") == self.holder:
            self.store.delete(self.key)
        return False


# ── history scan and verification ───────────────────────────────────────────

@dataclass
class CommittedRun:
    manifest: dict          # the stored manifest plus committed_at from commit.json (None if not yet recorded)
    manifest_key: str
    manifest_sha256: str    # of the stored manifest bytes
    observations: list[dict]
    classifications: list[dict]

    @property
    def committed_at(self) -> str | None:
        return self.manifest["committed_at"]


@dataclass
class HistoryScan:
    runs: list[CommittedRun] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)      # corrupt committed runs
    uncommitted: list[dict] = field(default_factory=list)   # objects without a manifest
    # Filled only by checkpoint.read_history: run prefix -> {object name: change tag} from the listing
    # each run was verified (or matched) against, and a summary of how the history was read.
    listing: dict = field(default_factory=dict)
    checkpoint: dict = field(default_factory=dict)

    @property
    def availability_pending(self) -> list[str]:
        return [r.manifest["run_id"] for r in self.runs if r.committed_at is None]


def _read_commit_record(store, prefix: str, run_id: str, manifest_sha256: str) -> tuple[str | None, str]:
    """(committed_at or None, problem)."""
    raw = store.get(prefix + "commit.json")
    if raw is None:
        return None, ""
    try:
        rec = json.loads(raw)
    except ValueError:
        return None, "commit record unreadable"
    if (rec.get("schema_version") != config.COMMIT_SCHEMA or rec.get("run_id") != run_id
            or rec.get("manifest_sha256") != manifest_sha256 or not rec.get("committed_at")):
        return None, "commit record does not match its manifest"
    return rec["committed_at"], ""


def _verify_run(store, prefix: str, manifest_key: str) -> tuple[CommittedRun | None, str]:
    raw = store.get(manifest_key)
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError):
        return None, "manifest unreadable"
    if manifest.get("schema_version") not in config.READABLE_MANIFEST_SCHEMAS or "committed_at" in manifest:
        return None, "manifest schema invalid"
    loaded = {}
    for name in OBJECT_NAMES:
        meta = manifest.get("objects", {}).get(name)
        if not meta or meta.get("key") != f"{prefix}{name}.jsonl.gz":
            return None, f"{name}: manifest entry missing or outside run prefix"
        data = store.get(meta["key"])
        if data is None:
            return None, f"{name}: object missing"
        if len(data) != meta["bytes"] or sha256_hex(data) != meta["sha256"]:
            return None, f"{name}: size/digest mismatch"
        try:
            records = decode_jsonl_gz(data)
        except (OSError, ValueError, EOFError):
            return None, f"{name}: undecodable"
        if len(records) != meta["records"]:
            return None, f"{name}: record count mismatch"
        loaded[name] = records
    committed_at, problem = _read_commit_record(store, prefix, manifest.get("run_id"), sha256_hex(raw))
    if problem:
        return None, problem
    return CommittedRun(dict(manifest, committed_at=committed_at), manifest_key, sha256_hex(raw),
                        loaded["observations"], loaded["classifications"]), ""


def group_run_keys(keys) -> dict[str, set[str]]:
    """Run prefix -> object names, from a listing of RUNS_PREFIX."""
    groups: dict[str, set[str]] = {}
    for key in keys:
        parts = key[len(RUNS_PREFIX):].split("/")
        if len(parts) != 3:
            continue
        groups.setdefault(f"{RUNS_PREFIX}{parts[0]}/{parts[1]}/", set()).add(parts[2])
    return groups


def scan_one(store, scan: HistoryScan, prefix: str, names) -> CommittedRun | None:
    """Read one listed run prefix fully and record it as committed, corrupt or uncommitted."""
    run_id = prefix.rstrip("/").rsplit("/", 1)[-1]
    if "manifest.json" not in names:
        scan.uncommitted.append({"run_id": run_id, "prefix": prefix, "objects": sorted(names)})
        return None
    run, problem = _verify_run(store, prefix, prefix + "manifest.json")
    if run is None:
        scan.problems.append({"run_id": run_id, "prefix": prefix, "detail": problem})
    else:
        scan.runs.append(run)
    return run


def sort_runs(scan: HistoryScan) -> HistoryScan:
    scan.runs.sort(key=lambda r: (r.manifest["started_at"], r.manifest["run_id"]))
    scan.problems.sort(key=lambda p: p["prefix"])
    scan.uncommitted.sort(key=lambda u: u["prefix"])
    return scan


def scan_history(store) -> HistoryScan:
    """Full replay: read and verify every committed run. The reference for any checkpoint."""
    scan = HistoryScan()
    for prefix, names in sorted(group_run_keys(store.list(RUNS_PREFIX)).items()):
        scan_one(store, scan, prefix, names)
    return sort_runs(scan)


def identity_state(runs: list[CommittedRun]) -> dict[str, dict]:
    """item_id -> latest revision, payload digest and first sighting, from committed history."""
    state: dict[str, dict] = {}
    for run in runs:
        for obs in sorted(run.observations, key=lambda o: o["observed_at"]):
            prev = state.get(obs["item_id"])
            first = min(prev["first_seen_at"], obs["first_seen_at"]) if prev else obs["first_seen_at"]
            state[obs["item_id"]] = {"latest_revision_id": obs["revision_id"],
                                     "payload_digest": obs["payload_digest"], "first_seen_at": first}
    return state


# ── spool and commit ────────────────────────────────────────────────────────

def _spool_dir(spool_root, run_id) -> Path:
    return Path(spool_root) / run_id


def spool_run(spool_root, draft: dict, blobs: dict[str, bytes]) -> Path:
    """Durably write the run's objects and draft manifest locally before any upload."""
    d = _spool_dir(spool_root, draft["run_id"])
    d.mkdir(parents=True, exist_ok=True)
    for name, data in blobs.items():
        _atomic_write(d / f"{name}.jsonl.gz", data)
    _atomic_write(d / "manifest.draft.json", encode_json(draft))
    return d


def _record_commit(store, prefix: str, run_id: str, manifest_sha256: str, commit_clock) -> str:
    """Write commit.json once (create-if-absent), stamped after the manifest is durable."""
    committed_at, problem = _read_commit_record(store, prefix, run_id, manifest_sha256)
    if problem:
        raise ArchiveIntegrityError(f"{prefix}commit.json: {problem}")
    if committed_at:
        return committed_at
    body = encode_json({"schema_version": config.COMMIT_SCHEMA, "run_id": run_id,
                        "manifest_sha256": manifest_sha256, "committed_at": iso(commit_clock())})
    store.put_if_absent(prefix + "commit.json", body)  # losing a race to an identical record is fine
    committed_at, problem = _read_commit_record(store, prefix, run_id, manifest_sha256)
    if problem or not committed_at:
        raise ArchiveIntegrityError(f"{prefix}commit.json did not read back: {problem or 'missing'}")
    return committed_at


def _clear_spool(d: Path) -> None:
    # Objects first, the draft last: a crash part-way leaves either a draft
    # (recognized as committed on restart) or an inert directory.
    for path in sorted(d.iterdir(), key=lambda p: p.name == "manifest.draft.json"):
        path.unlink(missing_ok=True)
    d.rmdir()


def commit_spooled(store, spool_root, run_id: str, commit_clock) -> dict:
    """Upload, read back, then commit the manifest. Safe to repeat with the same run_id.

    A run that is already committed is recognized from the store before any
    spooled object is needed, so an interrupted cleanup (draft left, objects
    removed) finishes the cleanup instead of failing every restart.
    """
    d = _spool_dir(spool_root, run_id)
    draft = json.loads((d / "manifest.draft.json").read_bytes())
    prefix = run_prefix(draft["observation_date"], run_id)
    manifest_key = prefix + "manifest.json"
    existing = store.get(manifest_key)
    if existing is not None:
        if json.loads(existing) != draft:
            raise ArchiveIntegrityError(f"{manifest_key} exists with different content")
        run, problem = _verify_run(store, prefix, manifest_key)
        if run is None:
            raise ArchiveIntegrityError(f"{manifest_key} is committed but does not verify: {problem}")
        body = existing
    else:
        for name in OBJECT_NAMES:
            meta = draft["objects"][name]
            path = d / f"{name}.jsonl.gz"
            if not path.is_file():
                raise ArchiveIntegrityError(f"spooled {name} for uncommitted run {run_id} is missing")
            data = path.read_bytes()
            if sha256_hex(data) != meta["sha256"]:
                raise ArchiveIntegrityError(f"spooled {name} for {run_id} does not match its draft digest")
            stored = store.get(meta["key"])
            if stored is None:
                store.put(meta["key"], data, "application/gzip")
                stored = store.get(meta["key"])
            if stored is None or sha256_hex(stored) != meta["sha256"]:
                raise ArchiveIntegrityError(f"{meta['key']} did not read back with the spooled digest")
        body = encode_json(draft)
        store.put(manifest_key, body, "application/json")
        if store.get(manifest_key) != body:
            raise ArchiveIntegrityError(f"{manifest_key} did not read back as written")
    committed_at = _record_commit(store, prefix, run_id, sha256_hex(body), commit_clock)
    _clear_spool(d)
    return dict(draft, committed_at=committed_at)


def settle_availability(store, scan: HistoryScan, commit_clock) -> list[str]:
    """Record availability for committed runs lacking commit.json, stamped now (never earlier)."""
    settled = []
    for run in scan.runs:
        if run.committed_at is None:
            m = run.manifest
            m["committed_at"] = _record_commit(store, run_prefix(m["observation_date"], m["run_id"]),
                                               m["run_id"], run.manifest_sha256, commit_clock)
            settled.append(m["run_id"])
    return settled


def pending_spools(spool_root) -> list[str]:
    root = Path(spool_root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "manifest.draft.json").is_file())


def recover_spools(store, spool_root, commit_clock) -> list[str]:
    """Commit any run left spooled by an interrupted process, reusing its run_id."""
    recovered = []
    for run_id in pending_spools(spool_root):
        commit_spooled(store, spool_root, run_id, commit_clock)
        recovered.append(run_id)
    return recovered


def _object_meta(prefix: str, name: str, data: bytes, records: int) -> dict:
    return {"key": f"{prefix}{name}.jsonl.gz", "bytes": len(data), "sha256": sha256_hex(data), "records": records}


def _draft(run_id, kind, started, finished, sources, counts, enrichment, observations, classifications, status,
           source_scope=None):
    date = started.strftime("%Y-%m-%d")
    prefix = run_prefix(date, run_id)
    blobs = {"observations": encode_jsonl_gz(observations), "classifications": encode_jsonl_gz(classifications)}
    draft = {
        "schema_version": config.MANIFEST_SCHEMA,
        "record_schema": config.SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": kind,
        "namespace": config.NAMESPACE,
        "code_version": config.CODE_VERSION,
        "git_revision": os.environ.get("NEWS_GIT_REVISION") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or None,
        "adapter_version": config.ADAPTER_VERSION,
        "rules_version": config.RULES_VERSION,
        "config_digest": config.config_digest(),
        "rules_digest": config.rules_digest(),
        "source_baselines": config.SOURCE_BASELINES,
        "started_at": iso(started),
        "finished_at": iso(finished),
        "observation_date": date,
        "status": status,
        "sources": sources,
        "source_scope": source_scope,
        "counts": counts,
        "enrichment": enrichment,
        "objects": {name: _object_meta(prefix, name, blobs[name],
                                       len(observations if name == "observations" else classifications))
                    for name in OBJECT_NAMES},
    }
    return draft, blobs


@dataclass
class RunReport:
    run_id: str
    manifest: dict
    recovered: list[str]
    index_error: str = ""
    settled: list[str] = field(default_factory=list)
    checkpoint: dict = field(default_factory=dict)   # how history was read, and whether the checkpoint was saved


def _enrichment_summary(classifications, failed, error) -> dict:
    status = "complete" if not failed else ("failed" if not classifications else "partial")
    return {"method": "rules", "version": config.RULES_VERSION, "status": status,
            "classified": len(classifications), "failed": len(failed),
            "pending_revision_ids": failed, "error": error}


def collect_once(store, spool_root, fetch, clock, *, streams=None, extra_sources=(), only_sources=None,
                 sleep=time.sleep,
                 classifier=classify_rules, commit_clock=None, run_id=None, lease_holder=None,
                 break_stale_lease=False, budget_s=config.CYCLE_BUDGET_S, monotonic=time.monotonic,
                 rebuild_index=True, fetch_pool=None, use_checkpoint=True) -> RunReport:
    """One bounded collection cycle: recover, replay history, fetch, normalize, classify, spool, commit."""
    commit_clock = commit_clock or clock
    with WriterLease(store, clock, lease_holder, break_stale=break_stale_lease):
        recovered = recover_spools(store, spool_root, commit_clock)
        scan = _writer_history(store, commit_clock, use_checkpoint)
        if scan.problems:
            raise HistoryCorrupt(scan.problems)
        settled = settle_availability(store, scan, commit_clock)
        state = identity_state(scan.runs)

        started = clock()
        run_id = run_id or new_run_id(started)
        sources = config.enabled_sources(streams=streams, extra=tuple(extra_sources))
        if only_sources is not None:
            sources = [s for s in sources if s.id in set(only_sources)]
        # The full roster is recorded so that readers never infer coverage from
        # the source rows alone (rows for other streams are omitted below).
        source_scope = {
            "streams_requested": sorted(streams) if streams is not None else None,
            "only_sources": sorted(only_sources) if only_sources is not None else None,
            "extra_sources": sorted(extra_sources),
            "selected": [s.id for s in sources],
            "roster": [{"source_id": s.id, "streams": list(s.streams), "enabled": s.enabled or s.id in extra_sources}
                       for s in config.SOURCES.values()],
        }
        pool = fetch_pool or default_pool()
        busy_at_start = pool.busy()
        results = fetch_all(sources, fetch, clock, sleep, budget_s=budget_s, monotonic=monotonic, pool=pool)
        digest = config.config_digest()
        observations, source_rows = [], []
        totals = {"entries": 0, "new_items": 0, "new_revisions": 0, "unchanged": 0, "failed_entries": 0}
        for src in sources:
            res = results[src.id]
            row = {"source_id": src.id, "name": src.name, "streams": list(src.streams), "status": res.status,
                   "http_status": res.http_status, "attempts": res.attempts, "error": res.error,
                   "observed_at": res.observed_at, "bytes": res.bytes,
                   "entries": len(res.entries), "new_items": 0, "new_revisions": 0, "unchanged": 0,
                   "failed_entries": 0, "entry_failures": []}
            for index, entry in enumerate(res.entries):
                try:
                    record, outcome = normalize_entry(entry, src, run_id, res.observed_at, digest, state)
                except Exception as exc:  # one bad item never costs the other items or sources
                    row["failed_entries"] += 1
                    if len(row["entry_failures"]) < MAX_ENTRY_FAILURES_LISTED:
                        row["entry_failures"].append({
                            "index": index, "guid": str(entry.get("guid") or "")[:300],
                            "link": str(entry.get("link") or "")[:500],
                            "title": str(entry.get("title") or "")[:300],
                            "error": f"{type(exc).__name__}: {exc}"[:300]})
                    continue
                key = {"new_item": "new_items", "new_revision": "new_revisions", "unchanged": "unchanged"}[outcome]
                row[key] += 1
                if record:
                    observations.append(record)
            for k in totals:
                totals[k] += row[k]
            source_rows.append(row)
        for src_id in sorted(set(config.SOURCES) - {s.id for s in sources}):
            src = config.SOURCES[src_id]
            if streams is None or set(streams).intersection(src.streams):
                source_rows.append({"source_id": src_id, "name": src.name, "streams": list(src.streams),
                                    "status": "not_selected" if src.enabled else "disabled"})
        finished = clock()

        classifications, failed, error = classify_all(observations, run_id, clock, classifier)
        ok = sum(1 for r in source_rows if r["status"] == "ok")
        attempted = sum(1 for r in source_rows if r["status"] not in ("disabled", "not_selected"))
        status = "complete" if attempted and ok == attempted else ("failed" if ok == 0 else "partial")
        if status == "complete" and totals["failed_entries"]:
            status = "partial"
        totals["sources_attempted"], totals["sources_ok"] = attempted, ok
        totals["fetch_workers_busy_at_start"] = busy_at_start
        draft, blobs = _draft(run_id, "collect", started, finished, source_rows, totals,
                              _enrichment_summary(classifications, failed, error),
                              observations, classifications, status, source_scope)
        spool_run(spool_root, draft, blobs)
        manifest = commit_spooled(store, spool_root, run_id, commit_clock)
        report = RunReport(run_id, manifest, recovered, settled=settled,
                           checkpoint=_save_checkpoint(store, scan, commit_clock, use_checkpoint))
        if rebuild_index:
            report.index_error = _try_rebuild(store, commit_clock)
        return report


def enrich_pending(store, spool_root, clock, *, classifier=classify_rules, commit_clock=None,
                   run_id=None, lease_holder=None, break_stale_lease=False, rebuild_index=True,
                   use_checkpoint=True) -> RunReport | None:
    """Classify committed revisions that have no classification for the current rules digest.

    Writes a classifications-only run; its classified_at is the time of this
    enrichment, so as-of readers never see it at an earlier cutoff.
    """
    commit_clock = commit_clock or clock
    with WriterLease(store, clock, lease_holder, break_stale=break_stale_lease):
        recovered = recover_spools(store, spool_root, commit_clock)
        scan = _writer_history(store, commit_clock, use_checkpoint)
        if scan.problems:
            raise HistoryCorrupt(scan.problems)
        settle_availability(store, scan, commit_clock)
        digest = config.rules_digest()
        done = {c["input_revision_id"] for r in scan.runs for c in r.classifications
                if c["method"] == "rules" and c["rules_digest"] == digest}
        pending = [o for r in scan.runs for o in r.observations if o["revision_id"] not in done]
        if not pending:
            return None
        started = clock()
        run_id = run_id or new_run_id(started)
        classifications, failed, error = classify_all(pending, run_id, clock, classifier)
        status = "complete" if not failed else ("failed" if not classifications else "partial")
        draft, blobs = _draft(run_id, "enrich", started, clock(), [], {"pending": len(pending)},
                              _enrichment_summary(classifications, failed, error), [], classifications, status)
        spool_run(spool_root, draft, blobs)
        manifest = commit_spooled(store, spool_root, run_id, commit_clock)
        report = RunReport(run_id, manifest, recovered,
                           checkpoint=_save_checkpoint(store, scan, commit_clock, use_checkpoint))
        if rebuild_index:
            report.index_error = _try_rebuild(store, commit_clock)
        return report


def _writer_history(store, clock, use_checkpoint: bool) -> HistoryScan:
    from .checkpoint import read_history

    return read_history(store, clock=clock, use_checkpoint=use_checkpoint)


def _save_checkpoint(store, scan: HistoryScan, clock, use_checkpoint: bool) -> dict:
    """After a commit, under the lease. History is already durable, so a failure is reported, not raised."""
    from .checkpoint import save_checkpoint

    read = dict(scan.checkpoint)
    if not use_checkpoint:
        return dict(read, saved=False, save_error="")
    try:
        saved = save_checkpoint(store, scan, clock)
    except Exception as exc:  # disposable: the next cycle reads more runs, or replays them all
        return dict(read, saved=False, save_error=f"{type(exc).__name__}: {exc}"[:300])
    return dict(read, saved=True, save_error="", checkpoint_key=saved["key"], checkpoint_runs=saved["runs"])


def _try_rebuild(store, clock) -> str:
    from .reader import rebuild_indexes

    try:
        rebuild_indexes(store, clock)
    except Exception as exc:  # history is committed; indexes are rebuildable
        return f"{type(exc).__name__}: {exc}"[:300]
    return ""
