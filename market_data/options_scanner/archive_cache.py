"""Bounded local cache lifecycle for finalized scanner day archives.

Two burdens, kept separate (see docs/130-live-window-wiring.md):

* F2-added derived output -- per-cycle window emit -- lives on ephemeral
  scratch (see windows_live.run_after_finalize). This module does not
  touch it.
* The pre-existing local mirror of finalized day archives under the spool
  (``<spool>/<YYYY-MM-DD>/``) grows with every session and has no bound.
  This module bounds ONLY the mirror.

Rules (all fail closed; nothing is ever deleted on a failed check):

* Retain at least the newest KEEP_FINALIZED_SESSIONS finalized sessions
  (the builder's 10-session lookback; pinned equal to
  windows_live.MAX_SESSIONS by test, not imported, so windows_live can
  import this module without a cycle) plus every unfinalized, current or
  recovery-marked day. ``keep_sessions`` below that floor is refused.
  The current day (today in the America/New_York trading timezone) is
  NEVER prunable, even if its summary claims final=true and even if it
  is otherwise malformed -- the date alone protects it.
* A day is pruned only after FRESH exact remote verification at prune
  time: every local artifact is re-read from the remote backend and its
  SHA-256 compared to the local bytes, and the DayArchive journal's
  recorded digest must agree. A stale journal entry alone proves nothing
  (the journal's own upload check is length-only -- see below). The remote
  journal is checked for contradictions; a missing entry there is noted,
  not blocking, because files added after the journal's last push (notably
  windows_live.json) are legitimately absent from it.
* In-progress snapshots are verified the same way (GET + SHA-256), except
  the local-only upload-attempts ledger, which is corroborated by exact
  set match against the remote budget tokens, and transient build debris
  (.build.lock, *.building), which protects the day outright.
* HARD REMOTE BUDGETS: every verification/prune run is bounded by a
  RemoteBudget -- a maximum number of remote requests, a maximum number
  of remote bytes read, and a maximum wall-clock time. Any excess raises
  RemoteBudgetExceeded and the run fails closed (nothing deleted, failure
  recorded). The bounds are enforced BEFORE / WHILE consuming, not after:
  the request count and deadline are preflighted BEFORE a GET is issued
  (an exhausted budget refuses without any remote call), and a remote
  object is read through a bounded stream -- its ContentLength is
  checked against the REMAINING byte budget before any body byte is
  read, the body is read in chunks capped so the in-memory overshoot is
  at most one byte past the budget when the server lies about the
  length, and the body is always closed (success, refusal, or error).
  That hard streaming bound applies to backends offering get_stream
  (R2Backend, the production path). Backends without a streaming read
  (LocalFilesystemBackend, test stubs) read the whole object and charge
  afterward: that is ACCOUNTING-ONLY, not a hard resource bound, and
  the evidence says so ("bounded_read": "accounting-only" vs
  "streaming"). A local-disk read cannot exhaust memory from a network
  body; the R2 path, where the risk is an unbounded network body, is
  the hard one. A remote GET can exceed local byte estimates and many
  in-progress files can exceed an operation budget, so local-size/day
  caps alone are not the bound.
* UNRECOGNIZED FILES: before any delete, the ACTUAL local directory is
  enumerated (rglob -- every file, including paths DayArchive.artifacts()
  skips such as *.tmp) and compared against the verified evidence set.
  Any local file the evidence does not explicitly account for -- a .tmp
  file, a root-level recovery/operator file, a stray file at any level --
  aborts the day's deletion, fail closed. The artifacts() listing is
  never treated as the universe of what exists.
* PRE-DELETE RECHECK: after verification and immediately before the
  delete of each day, the day is re-listed and re-hashed; if anything
  changed since verification (added, removed, or modified), the delete
  aborts and the run stops. No deletion ever happens on stale evidence.
* Bounded operations: at most max_days_per_run days and max_bytes_per_run
  bytes per run, oldest first. Dry-run evidence first: the report lists
  exactly what would be deleted and the verification evidence for each
  day before any delete. Deletion requires the explicit switch
  OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1 (Chej) AND dry_run=False.
* Every R2 object is preserved: this module never calls backend.delete
  and never overwrites a remote key. A pruned day is restored from R2
  with restore_day(), which restores EXACTLY the artifacts listed in
  the remote journal -- see the restore docstring for what the journal
  omits and what is therefore NOT restorable from the journal alone.

What the DayArchive journal proves (and does not prove):

* The journal (uploads.json) records, per artifact, the SHA-256 of the
  local bytes as last uploaded, plus the R2 key and verified_at. The
  upload itself (upload_file_verified) PUTs the file and checks the
  remote ContentLength -- it does NOT GET the remote bytes back and hash
  them. So the journal proves "these bytes were PUT and the remote object
  had the same length", not "the remote bytes hash to this digest".
  That is exactly why this module re-reads every remote member and
  hashes it at prune time instead of trusting the journal.
* Nothing ever re-verifies remote objects after upload, and no code reads
  the remote journal back. The fresh verification here is the first
  digest-level proof that the remote bytes match the local archive.

Production operation:

* The same verification code serves two backends: LocalFilesystemBackend
  (offline / tests) and R2Backend via windows_publish.r2_backend()
  (production). The CLI takes --r2 for the production path; the finalize
  hook (windows_live.run_after_finalize) runs the audit against its
  effective backend (R2 in production) when OA203_ARCHIVE_CACHE_AUDIT_ENABLED=1
  (Chej, OFF by default), and prunes only when the destructive switch
  OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1 is also set. Every lifecycle run
  persists its report to <spool>/archive-cache-reports/<day>.json; a
  lifecycle failure never unfinalizes the day, unpublishes windows, or
  fails the finalize -- it is logged and reported, and publication stands.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .active import (
    ACTIVE_MAX_SNAPSHOTS_PER_DAY,
    ACTIVE_MAX_SWEEP,
    ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP,
)
from .archive import (
    INTERRUPTIONS,
    RECOVERY_ERRORS,
    UPLOAD_JOURNAL,
    UPLOAD_RECEIPT,
    DayArchive,
    _sha256_file,
    published,
)
from .runtime import ET
from .windows_publish import StorageBackend

ARCHIVE_CACHE_PRUNE_ENV = "OA203_ARCHIVE_CACHE_PRUNE_ENABLED"
ARCHIVE_CACHE_AUDIT_ENV = "OA203_ARCHIVE_CACHE_AUDIT_ENABLED"
ARCHIVE_CACHE_REPORTS_DIR = "archive-cache-reports"
KEEP_FINALIZED_SESSIONS = 10  # the builder's 10-session lookback; == windows_live.MAX_SESSIONS (pinned by test)
DEFAULT_MAX_DAYS_PER_RUN = 2
DEFAULT_MAX_BYTES_PER_RUN = 512 * 1024 * 1024
R2_PREFIX = "oa203/scanner"
RECOVERY_MARKERS = (INTERRUPTIONS, RECOVERY_ERRORS)
_BUDGET_KEY = re.compile(r"(.+)/inprogress/budget/([0-9]{4})-([123])\.json")

# Hard remote budgets. A day's remote reads are about its local bytes
# (~110 MB/day measured); 512 MiB/day is ~4.6x that, 512 requests/day
# covers ~50 sweep files plus in-progress members (16 sweeps x 5 files
# + pointers) plus the journal plus the token listing, with headroom.
# A run verifies at most max_days_per_run days, so the run defaults are
# 2x the per-day defaults. These are hard fails, not advisories.
DEFAULT_MAX_REMOTE_REQUESTS_PER_DAY = 512
DEFAULT_MAX_REMOTE_BYTES_PER_DAY = 512 * 1024 * 1024
DEFAULT_MAX_VERIFY_SECONDS_PER_DAY = 300.0
DEFAULT_MAX_REMOTE_REQUESTS_PER_RUN = 1024
DEFAULT_MAX_REMOTE_BYTES_PER_RUN = 1024 * 1024 * 1024
DEFAULT_MAX_VERIFY_SECONDS_PER_RUN = 600.0


def archive_cache_prune_enabled() -> bool:
    """Explicit destructive switch, owned by Chej: only the exact value "1" enables deletion."""
    return os.getenv(ARCHIVE_CACHE_PRUNE_ENV) == "1"


def archive_cache_audit_enabled() -> bool:
    """Explicit audit switch, owned by Chej: only the exact value "1" enables the finalize-hook audit."""
    return os.getenv(ARCHIVE_CACHE_AUDIT_ENV) == "1"


def archive_cache_destructive_enabled() -> bool:
    """Destructive pruning needs BOTH the audit flag and the prune flag."""
    return archive_cache_audit_enabled() and archive_cache_prune_enabled()


class RemoteBudgetExceeded(Exception):
    """A hard remote-operation budget was exceeded; the run fails closed."""


class RemoteBudget:
    """Hard budgets for remote operations in one verification or prune run.

    Counts remote requests (every backend.get / list_keys), total remote
    bytes read, and wall-clock time (monotonic deadline). Any excess
    raises RemoteBudgetExceeded; callers fail closed (nothing deleted).
    Bounds are enforced BEFORE / WHILE consuming: preflight() refuses
    before a call is issued when the budget is already exhausted, and
    byte charges land per chunk while a body is being read (see
    _bounded_get), so an oversized object is refused without ever being
    fully read into memory. A single read() call is capped so the
    in-memory overshoot past the budget is at most one byte when the
    server lies about ContentLength.
    """

    def __init__(self, *, max_requests: int, max_bytes: int, max_seconds: float) -> None:
        if max_requests < 1 or max_bytes < 1 or max_seconds <= 0:
            raise ValueError("remote budgets must be positive")
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.deadline = time.monotonic() + max_seconds
        self.requests = 0
        self.bytes_read = 0

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise RemoteBudgetExceeded(
                f"remote time budget exceeded ({self.requests} requests, "
                f"{self.bytes_read} bytes read)")

    def preflight(self) -> None:
        """Refuse BEFORE issuing a remote call when the budget is already
        exhausted (request count or deadline); no remote call is made."""
        self.check_time()
        if self.requests >= self.max_requests:
            raise RemoteBudgetExceeded(
                f"remote request budget already exhausted: "
                f"{self.requests} >= {self.max_requests}")

    def charge_request(self) -> None:
        """Preflight, then charge one remote request at issue time."""
        self.preflight()
        self.requests += 1

    def charge_bytes(self, nbytes: int) -> None:
        """Charge nbytes read; fail when the byte budget is exceeded."""
        self.check_time()
        self.bytes_read += nbytes
        if self.bytes_read > self.max_bytes:
            raise RemoteBudgetExceeded(
                f"remote byte budget exceeded: {self.bytes_read} > {self.max_bytes} bytes")

    def state(self) -> dict[str, Any]:
        return {"max_requests": self.max_requests, "max_bytes": self.max_bytes,
                "requests": self.requests, "bytes_read": self.bytes_read}


def _bounded_get(backend: StorageBackend, key: str, budget: RemoteBudget, *,
                 chunk_size: int = 1024 * 1024) -> tuple[bytes, str]:
    """Read one remote object under the hard budget.

    Preflight first (no remote call when the budget is already
    exhausted). Then:
    - streaming path (backends offering get_stream, i.e. R2Backend):
      the ContentLength is checked against the REMAINING byte budget
      before any body byte is read; the body is read in chunks capped so
      the total read exceeds the budget by at most one byte even when
      the server lies about the length; the body is always closed.
      Returns (data, "streaming").
    - accounting-only path (backends without get_stream): the whole
      object is read and charged afterward. This is NOT a hard resource
      bound -- the bytes are already in memory when the charge lands --
      and the caller must label it so ("accounting-only"). Returns
      (data, "accounting-only").
    KeyError (missing object) propagates; RemoteBudgetExceeded is raised
    on any budget breach, with the body closed.
    """
    budget.charge_request()
    get_stream = getattr(backend, "get_stream", None)
    if not callable(get_stream):
        data = backend.get(key)
        budget.charge_bytes(len(data))
        return data, "accounting-only"
    body, content_length = get_stream(key)
    remaining = budget.max_bytes - budget.bytes_read
    if content_length >= 0 and content_length > remaining:
        body.close()
        raise RemoteBudgetExceeded(
            f"remote object {key} is {content_length} bytes, exceeding "
            f"remaining byte budget {remaining}")
    parts: list[bytes] = []
    total = 0
    try:
        while True:
            budget.check_time()
            chunk = body.read(min(chunk_size, remaining - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > remaining:
                raise RemoteBudgetExceeded(
                    f"remote object {key} exceeded remaining byte budget "
                    f"{remaining} while streaming")
            parts.append(chunk)
            budget.charge_bytes(len(chunk))
    finally:
        body.close()
    return b"".join(parts), "streaming"


def _is_link(path: Path) -> bool:
    """True for symlinks and (on Windows) junctions; False on any stat failure."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return False


def _resolve_inside(path: Path, root: Path) -> Path | None:
    """``path`` resolved, when it stays inside resolved ``root``; else None."""
    try:
        resolved = path.resolve()
        rroot = root.resolve()
    except OSError:
        return None
    if resolved == rroot or rroot in resolved.parents:
        return resolved
    return None


def _dir_size(root: Path) -> int:
    """Total bytes of real files under a dir (links never followed)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_link(here / d)]
        for name in filenames:
            p = here / name
            if _is_link(p):
                continue
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def _day_dir_safe(day_dir: Path, spool: Path) -> tuple[bool, str]:
    """Containment + link checks for one day dir.

    The day dir must resolve inside the spool, must not itself be a link,
    and must hold no symlink/junction anywhere inside it.
    """
    if _is_link(spool):
        return False, "spool is a symlink/junction"
    if _is_link(day_dir):
        return False, "day dir is a symlink/junction"
    resolved = _resolve_inside(day_dir, spool)
    if resolved is None:
        return False, "day dir escapes the spool"
    for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames) + filenames:
            if _is_link(here / name):
                return False, f"symlink/junction inside day dir: {name}"
    return True, "safe"


def _verify_attempt_ledger(day_dir: Path, backend: StorageBackend, *,
                           r2_prefix: str, budget: RemoteBudget) -> tuple[bool, str | None]:
    """Corroborate the local-only upload-attempts ledger against R2 budget tokens.

    The ledger (inprogress/upload-attempts.json) is never uploaded: its
    content is fully determined by the remote budget tokens -- one token
    per (sweep, attempt) -- so exact set match against the token keys is
    the check. A missing ledger means snapshots never ran for the day:
    nothing to corroborate. Returns (corroborated, error).
    """
    ledger_path = day_dir / "inprogress" / "upload-attempts.json"
    if not ledger_path.is_file():
        return True, None
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"upload-attempts ledger unreadable ({exc})"
    if (not isinstance(ledger, dict) or ledger.get("schema_version") != 1
            or ledger.get("trade_date") != day_dir.name
            or not isinstance(ledger.get("attempts"), dict)):
        return False, "upload-attempts ledger malformed"
    attempts = ledger["attempts"]
    if len(attempts) > ACTIVE_MAX_SNAPSHOTS_PER_DAY:
        return False, "upload-attempts ledger exceeds snapshot bound"
    want: set[tuple[int, int]] = set()
    for key, count in attempts.items():
        if (not re.fullmatch(r"[1-9][0-9]{0,3}", key) or int(key) > ACTIVE_MAX_SWEEP
                or type(count) is not int
                or not 1 <= count <= ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP):
            return False, f"upload-attempts ledger entry malformed: {key!r}"
        want.update((int(key), a) for a in range(1, count + 1))
    budget.preflight()
    budget.charge_request()
    try:
        listed = backend.list_keys(f"{r2_prefix}/{day_dir.name}/inprogress/budget/")
    except Exception as exc:  # noqa: BLE001 - recorded, fail closed
        return False, f"remote budget token listing failed ({type(exc).__name__}: {exc})"
    have: set[tuple[int, int]] = set()
    for obj in listed:
        m = _BUDGET_KEY.fullmatch(obj["key"])
        if m is None:
            return False, f"unexpected remote budget key: {obj['key']}"
        have.add((int(m.group(2)), int(m.group(3))))
    if have != want:
        return False, ("remote budget tokens do not match the ledger "
                       f"(missing={sorted(want - have)}, extra={sorted(have - want)})")
    return True, None


def verify_day_remote(day_dir: Path, backend: StorageBackend, *,
                      r2_prefix: str = R2_PREFIX,
                      budget: RemoteBudget | None = None,
                      today: date | None = None) -> dict[str, Any]:
    """Fresh exact remote verification for one local day directory.

    Returns an evidence dict with ``verified`` True only when every check
    passed: the day is not the current day (never prunable), is finalized,
    holds no links, has no pending uploads, every local artifact's SHA-256
    matches both the local journal and a fresh GET of the remote object,
    the remote journal does not contradict the local digests, every
    in-progress snapshot file matches a fresh GET (with the upload-attempts
    ledger corroborated by budget tokens), and the ACTUAL local inventory
    (every file found by enumerating the directory, including paths
    DayArchive.artifacts() skips such as *.tmp) is exactly the verified
    evidence set -- any unrecognized file fails the day. The evidence
    carries ``snapshot`` ({relative path: SHA-256} for every verified
    file) and ``inventory`` (sorted relative paths) for the pre-delete
    recheck. Any failure leaves ``verified`` False with the reason in
    ``errors``. Nothing here deletes anything. Remote work is charged to
    ``budget`` (a per-day default budget when None); exceeding it fails
    the day, fail closed.
    """
    day_dir = Path(day_dir)
    if today is None:
        today = datetime.now(ET).date()
    if budget is None:
        budget = RemoteBudget(max_requests=DEFAULT_MAX_REMOTE_REQUESTS_PER_DAY,
                              max_bytes=DEFAULT_MAX_REMOTE_BYTES_PER_DAY,
                              max_seconds=DEFAULT_MAX_VERIFY_SECONDS_PER_DAY)
    evidence: dict[str, Any] = {
        "day": day_dir.name, "verified": False,
        "artifacts": [], "inprogress": [],
        "attempts_corroborated": None,
        "remote_journal_missing_entries": [],
        "snapshot": {}, "inventory": [],
        "remote_budget": budget.state(),
        "bounded_read": ("streaming" if callable(getattr(backend, "get_stream", None))
                         else "accounting-only"),
        "errors": [],
    }

    def fail(reason: str) -> dict[str, Any]:
        evidence["errors"].append(reason)
        evidence["remote_budget"] = budget.state()
        return evidence

    try:
        day = date.fromisoformat(day_dir.name)
    except ValueError:
        return fail(f"not an ISO date directory: {day_dir.name}")
    # The current day is never prunable, even if its summary claims
    # final=true and even if it is otherwise malformed: the date alone
    # protects it. (Future-dated dirs are protected by the same rule.)
    if day >= today:
        return fail("current day is never prunable")
    try:
        return _verify_day_remote_inner(day_dir, day, backend, evidence, budget,
                                        r2_prefix=r2_prefix)
    except RemoteBudgetExceeded as exc:
        return fail(f"remote budget exceeded: {exc}")


def _verify_day_remote_inner(day_dir: Path, day: date, backend: StorageBackend,
                             evidence: dict[str, Any], budget: RemoteBudget, *,
                             r2_prefix: str) -> dict[str, Any]:
    """Verification body (budget failures surface as RemoteBudgetExceeded)."""

    def fail(reason: str) -> dict[str, Any]:
        evidence["errors"].append(reason)
        evidence["remote_budget"] = budget.state()
        return evidence

    snapshot: dict[str, str] = {}
    if not day_dir.is_dir() or _is_link(day_dir):
        return fail("day dir is not a real directory")
    try:
        summary = json.loads((day_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return fail(f"summary.json unreadable ({exc})")
    if not isinstance(summary, dict) or not summary.get("final"):
        return fail("day is not finalized")
    for dirpath, dirnames, filenames in os.walk(day_dir, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames) + filenames:
            if _is_link(here / name):
                return fail(f"symlink/junction inside day dir: {name}")
    archive = DayArchive(day_dir.parent, day, r2_prefix, upload=True)
    try:
        pending = archive.pending_uploads()
    except (OSError, ValueError) as exc:
        return fail(f"pending-upload check failed ({exc})")
    if pending:
        return fail(f"unuploaded artifacts: {sorted(pending)}")
    try:
        journal = json.loads((day_dir / UPLOAD_JOURNAL).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return fail(f"local journal unreadable ({exc})")
    if not isinstance(journal, dict):
        return fail("local journal is not a JSON object")
    for name in archive.artifacts():
        local = day_dir / name
        try:
            local_bytes = local.read_bytes()
        except OSError as exc:
            return fail(f"{name}: local read failed ({exc})")
        local_hash = hashlib.sha256(local_bytes).hexdigest()
        recorded = journal.get(name)
        if not isinstance(recorded, dict) or recorded.get("sha256") != local_hash:
            return fail(f"{name}: local journal digest does not match local bytes")
        try:
            remote, _mode = _bounded_get(
                backend, f"{r2_prefix}/{day.isoformat()}/{name}", budget)
        except KeyError:
            return fail(f"{name}: missing in remote")
        except RemoteBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, fail closed
            return fail(f"{name}: remote read failed ({type(exc).__name__}: {exc})")
        if hashlib.sha256(remote).hexdigest() != local_hash:
            return fail(f"{name}: remote digest does not match local bytes")
        snapshot[name] = local_hash
        evidence["artifacts"].append({"name": name, "sha256": local_hash,
                                      "bytes": len(local_bytes),
                                      "remote_bytes": len(remote),
                                      "journal_match": True})
    try:
        remote_journal_raw, _mode = _bounded_get(
            backend, f"{r2_prefix}/{day.isoformat()}/{UPLOAD_JOURNAL}", budget)
    except KeyError:
        return fail("remote journal missing")
    except RemoteBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, fail closed
        return fail(f"remote journal read failed ({type(exc).__name__}: {exc})")
    try:
        remote_journal = json.loads(remote_journal_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return fail(f"remote journal unreadable ({exc})")
    if not isinstance(remote_journal, dict):
        return fail("remote journal is not a JSON object")
    by_name = {a["name"]: a["sha256"] for a in evidence["artifacts"]}
    for name, entry in remote_journal.items():
        if not isinstance(entry, dict):
            return fail(f"remote journal entry malformed: {name}")
        if name in by_name and entry.get("sha256") != by_name[name]:
            return fail(f"remote journal contradicts local digest: {name}")
    evidence["remote_journal_missing_entries"] = sorted(
        n for n in by_name if n not in remote_journal)
    inprog = day_dir / "inprogress"
    if inprog.is_dir():
        for dirpath, dirnames, filenames in os.walk(inprog, followlinks=False):
            if any(d.endswith(".building") for d in dirnames):
                return fail("inprogress build debris present (*.building)")
            if ".build.lock" in filenames:
                return fail("inprogress build lock present (.build.lock)")
        for path in sorted(p for p in inprog.rglob("*") if p.is_file()):
            rel = path.relative_to(day_dir).as_posix()
            if rel == "inprogress/upload-attempts.json":
                continue
            try:
                local_bytes = path.read_bytes()
            except OSError as exc:
                return fail(f"{rel}: local read failed ({exc})")
            try:
                remote, _mode = _bounded_get(
                    backend, f"{r2_prefix}/{day.isoformat()}/{rel}", budget)
            except KeyError:
                return fail(f"{rel}: missing in remote")
            except RemoteBudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded, fail closed
                return fail(f"{rel}: remote read failed ({type(exc).__name__}: {exc})")
            local_hash = hashlib.sha256(local_bytes).hexdigest()
            if hashlib.sha256(remote).hexdigest() != local_hash:
                return fail(f"{rel}: remote digest does not match local bytes")
            snapshot[rel] = local_hash
            evidence["inprogress"].append({"name": rel, "sha256": local_hash,
                                          "bytes": len(local_bytes)})
        corroborated, error = _verify_attempt_ledger(day_dir, backend,
                                                     r2_prefix=r2_prefix, budget=budget)
        evidence["attempts_corroborated"] = corroborated
        if error is not None:
            return fail(error)
        ledger = day_dir / "inprogress" / "upload-attempts.json"
        if ledger.is_file():
            snapshot["inprogress/upload-attempts.json"] = _sha256_file(ledger)
    # Full local inventory vs verified evidence. Enumerate the ACTUAL
    # directory: rglob finds every file, including paths
    # DayArchive.artifacts() skips (notably *.tmp, which a crash or an
    # operator can leave behind). Any local file the evidence does not
    # explicitly account for aborts the day -- the artifacts() listing
    # is never treated as the universe of what exists.
    try:
        journal_bytes = (day_dir / UPLOAD_JOURNAL).read_bytes()
        receipt_bytes = (day_dir / UPLOAD_RECEIPT).read_bytes()
    except OSError as exc:
        return fail(f"journal/receipt unreadable ({exc})")
    snapshot[UPLOAD_JOURNAL] = hashlib.sha256(journal_bytes).hexdigest()
    snapshot[UPLOAD_RECEIPT] = hashlib.sha256(receipt_bytes).hexdigest()
    inventory = sorted(p.relative_to(day_dir).as_posix()
                       for p in day_dir.rglob("*") if p.is_file())
    allowed = set(snapshot)
    unrecognized = [p for p in inventory if p not in allowed]
    if unrecognized:
        return fail(f"unrecognized local files not in verified evidence: {unrecognized}")
    evidence["snapshot"] = snapshot
    evidence["inventory"] = inventory
    evidence["remote_budget"] = budget.state()
    evidence["verified"] = True
    return evidence


def _recheck_day_unchanged(day_dir: Path, evidence: dict[str, Any]) -> list[str]:
    """Re-list + re-hash a verified day immediately before deletion.

    Returns a list of changes (added/removed/modified files, or a link
    appearing) since verification; empty means unchanged. Any change
    aborts the delete: no deletion ever happens on stale evidence.
    """
    snapshot = evidence.get("snapshot") or {}
    inventory = evidence.get("inventory") or []
    changes: list[str] = []
    current: dict[str, str] = {}
    for path in sorted(p for p in Path(day_dir).rglob("*") if p.is_file()):
        if _is_link(path):
            changes.append(f"link appeared: {path.relative_to(day_dir).as_posix()}")
            continue
        rel = path.relative_to(day_dir).as_posix()
        try:
            current[rel] = _sha256_file(path)
        except OSError as exc:
            changes.append(f"unreadable since verification: {rel} ({exc})")
    for rel in sorted(set(inventory) - set(current)):
        changes.append(f"removed since verification: {rel}")
    for rel in sorted(set(current) - set(inventory)):
        changes.append(f"added since verification: {rel}")
    for rel in sorted(set(current) & set(inventory)):
        if current[rel] != snapshot.get(rel):
            changes.append(f"modified since verification: {rel}")
    return changes


def _delete_day_dir(day_dir: Path, spool: Path) -> None:
    """Delete one verified day dir after its safety re-check; re-list to confirm."""
    ok, reason = _day_dir_safe(day_dir, spool)
    if not ok:
        raise RuntimeError(f"refusing unsafe day dir {day_dir}: {reason}")
    shutil.rmtree(day_dir)
    if day_dir.exists():
        raise RuntimeError(f"day dir still exists after delete: {day_dir}")


def prune_archive_cache(spool: Path, backend: StorageBackend, *,
                        r2_prefix: str = R2_PREFIX,
                        keep_sessions: int = KEEP_FINALIZED_SESSIONS,
                        max_days_per_run: int = DEFAULT_MAX_DAYS_PER_RUN,
                        max_bytes_per_run: int = DEFAULT_MAX_BYTES_PER_RUN,
                        max_remote_requests: int = DEFAULT_MAX_REMOTE_REQUESTS_PER_RUN,
                        max_remote_bytes: int = DEFAULT_MAX_REMOTE_BYTES_PER_RUN,
                        max_verify_seconds: float = DEFAULT_MAX_VERIFY_SECONDS_PER_RUN,
                        dry_run: bool = True,
                        today: date | None = None) -> dict[str, Any]:
    """Prune older redundant local day archives after fresh exact verification.

    Only finalized days older than the newest ``keep_sessions`` finalized
    sessions are candidates, oldest first, bounded by ``max_days_per_run``
    and ``max_bytes_per_run``. Every selected day is verified first
    (verify_day_remote, sharing ONE run-level RemoteBudget across all
    selected days); if ANY verification fails, NOTHING is deleted in
    this run and the failure is recorded. Immediately before each delete
    the day is re-listed and re-hashed (_recheck_day_unchanged); any
    change since verification stops the run. Deletion requires
    OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1 AND dry_run=False; otherwise the
    report is dry-run evidence only. The current day (``today``, default
    today in America/New_York) is never prunable. Unfinalized days,
    recovery-marked days, and days within the keep set are protected,
    never candidates. No R2 object is ever deleted or overwritten by
    this function.
    """
    spool = Path(spool)
    if today is None:
        today = datetime.now(ET).date()
    budget = RemoteBudget(max_requests=max_remote_requests,
                          max_bytes=max_remote_bytes,
                          max_seconds=max_verify_seconds)
    report: dict[str, Any] = {
        "spool": str(spool), "r2_prefix": r2_prefix,
        "keep_sessions": keep_sessions,
        "max_days_per_run": max_days_per_run,
        "max_bytes_per_run": max_bytes_per_run,
        "dry_run": dry_run, "enabled": archive_cache_prune_enabled(),
        "today": today.isoformat(),
        "decision": "dry-run", "protected": [], "candidates": [],
        "evidence": [], "pruned": [], "bytes_pruned": 0, "errors": [],
        "remote_budget": budget.state(),
    }

    def sync_budget() -> None:
        report["remote_budget"] = budget.state()

    if not spool.is_dir():
        raise ValueError(f"spool does not exist: {spool}")
    if keep_sessions < KEEP_FINALIZED_SESSIONS:
        raise ValueError(
            f"keep_sessions must retain at least the newest "
            f"{KEEP_FINALIZED_SESSIONS} finalized sessions, got {keep_sessions}")
    if max_days_per_run < 1 or max_bytes_per_run < 1:
        raise ValueError("max_days_per_run and max_bytes_per_run must be >= 1")
    if not dry_run and not archive_cache_prune_enabled():
        raise ValueError(
            "archive cache prune is not enabled: set "
            "OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1 (Chej) and pass --confirm; "
            "dry-run lists evidence only")
    days: list[tuple[date, Path]] = []
    for child in sorted(spool.iterdir()):
        if _is_link(child) or not child.is_dir():
            continue
        try:
            days.append((date.fromisoformat(child.name), child))
        except ValueError:
            continue
    finalized: list[tuple[date, Path]] = []
    for day, child in days:
        if day >= today:
            report["protected"].append({"day": day.isoformat(),
                                       "reason": "current day (never prunable)"})
            continue
        if any((child / marker).exists() for marker in RECOVERY_MARKERS):
            report["protected"].append({"day": day.isoformat(),
                                       "reason": "recovery files present"})
            continue
        try:
            summary = json.loads((child / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report["protected"].append({"day": day.isoformat(),
                                       "reason": "not finalized (no readable final summary)"})
            continue
        if not isinstance(summary, dict) or not summary.get("final"):
            report["protected"].append({"day": day.isoformat(),
                                       "reason": "not finalized"})
            continue
        finalized.append((day, child))
    finalized.sort()
    keep = {child for _, child in finalized[-keep_sessions:]}
    candidates: list[tuple[date, Path]] = []
    for day, child in finalized:
        if child in keep:
            report["protected"].append(
                {"day": day.isoformat(),
                 "reason": f"within newest {keep_sessions} finalized sessions"})
            continue
        candidates.append((day, child))
    selected: list[tuple[date, Path, int]] = []
    total_bytes = 0
    for day, child in candidates:
        if len(selected) >= max_days_per_run:
            break
        size = _dir_size(child)
        if total_bytes + size > max_bytes_per_run:
            break
        selected.append((day, child, size))
        total_bytes += size
    for day, child, size in selected:
        report["candidates"].append({"day": day.isoformat(), "bytes": size})
    verified: list[tuple[date, Path, int, dict[str, Any]]] = []
    for day, child, size in selected:
        ev = verify_day_remote(child, backend, r2_prefix=r2_prefix,
                               budget=budget, today=today)
        report["evidence"].append(ev)
        if not ev["verified"]:
            report["errors"].append({"day": day.isoformat(), "errors": ev["errors"]})
            report["decision"] = "refused: verification failed, nothing deleted"
            sync_budget()
            return report
        verified.append((day, child, size, ev))
    if dry_run:
        report["decision"] = "dry-run"
        for day, child, size, _ev in verified:
            report["pruned"].append({"day": day.isoformat(), "bytes": size,
                                     "projected": True})
        sync_budget()
        return report
    report["decision"] = "pruned"
    for day, child, size, ev in verified:
        changes = _recheck_day_unchanged(child, ev)
        if changes:
            report["errors"].append({"day": day.isoformat(),
                                    "errors": [f"changed since verification: {c}"
                                               for c in changes]})
            report["decision"] = "refused: changed since verification, run stopped"
            sync_budget()
            return report
        try:
            _delete_day_dir(child, spool)
        except Exception as exc:  # noqa: BLE001 - recorded, run stops here
            report["errors"].append({"day": day.isoformat(),
                                    "errors": [f"delete failed ({type(exc).__name__}: {exc})"]})
            report["decision"] = "partial: delete failed, run stopped"
            sync_budget()
            return report
        report["pruned"].append({"day": day.isoformat(), "bytes": size,
                                 "projected": False})
        report["bytes_pruned"] += size
    sync_budget()
    return report


def restore_day(spool: Path, day: date | str, backend: StorageBackend, *,
                r2_prefix: str = R2_PREFIX) -> dict[str, Any]:
    """Restore one pruned day from R2 into the spool, digest-verified.

    Restores EXACTLY the artifacts listed in the remote journal
    (uploads.json): downloads each one, verifying SHA-256 + size against
    the journal before writing it atomically. The local journal is
    restored from the remote journal bytes verbatim plus a fresh receipt,
    so pending_uploads() is empty for the restored day. The journal does
    NOT list every remote object for the day: windows_live.json (pushed
    after the journal's last push) and all inprogress/ members (never
    journaled) and budget tokens are absent from it, so they are NOT
    restored by this function -- they remain preserved on R2 under their
    known keys and are reported as ``not_restored``. A restore with a
    non-empty ``not_restored`` is therefore a PARTIAL restore
    (``complete`` is False): the journaled artifacts are byte-identical,
    but it is NOT a full restoration of the original local day from the
    journal alone. Remote keys NOT listed in the journal are reported,
    not silently skipped. The target must not already exist; any failure
    removes the partial restore and raises. Nothing remote is modified.
    """
    day = date.fromisoformat(day) if isinstance(day, str) else day
    spool = Path(spool)
    target = spool / day.isoformat()
    if target.exists():
        raise ValueError(f"restore target already exists: {target}")
    # A restore reads a whole day (~110 MB); the same hard per-day
    # budget bounds it, enforced by _bounded_get while streaming.
    budget = RemoteBudget(max_requests=DEFAULT_MAX_REMOTE_REQUESTS_PER_DAY,
                          max_bytes=DEFAULT_MAX_REMOTE_BYTES_PER_DAY,
                          max_seconds=DEFAULT_MAX_VERIFY_SECONDS_PER_DAY)
    try:
        remote_journal_raw, _mode = _bounded_get(
            backend, f"{r2_prefix}/{day.isoformat()}/{UPLOAD_JOURNAL}", budget)
    except KeyError:
        raise ValueError(f"remote journal missing for {day.isoformat()}") from None
    try:
        journal = json.loads(remote_journal_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"remote journal unreadable for {day.isoformat()} ({exc})") from None
    if not isinstance(journal, dict) or not journal:
        raise ValueError(f"remote journal empty or malformed for {day.isoformat()}")
    target.mkdir(parents=True)
    restored: list[dict[str, Any]] = []
    try:
        for name in sorted(journal):
            entry = journal[name]
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts:
                raise ValueError(f"unsafe journal entry: {name!r}")
            if not isinstance(entry, dict) or not re.fullmatch(r"[0-9a-f]{64}",
                                                               entry.get("sha256") or ""):
                raise ValueError(f"journal entry malformed: {name}")
            try:
                data, _mode = _bounded_get(
                    backend, f"{r2_prefix}/{day.isoformat()}/{name}", budget)
            except KeyError:
                raise ValueError(f"remote artifact missing for restore: {name}") from None
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"restore digest mismatch: {name}")
            if len(data) != entry.get("bytes"):
                raise ValueError(f"restore size mismatch: {name}")
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with published(dest) as tmp:
                tmp.write_bytes(data)
            restored.append({"name": name, "sha256": entry["sha256"],
                             "bytes": len(data)})
        with published(target / UPLOAD_JOURNAL) as tmp:
            tmp.write_bytes(remote_journal_raw)
        receipt = {"sha256": hashlib.sha256(remote_journal_raw).hexdigest(),
                   "bytes": len(remote_journal_raw),
                   "key": f"{r2_prefix}/{day.isoformat()}/{UPLOAD_JOURNAL}",
                   "verified_at": datetime.now(timezone.utc).isoformat()}
        with published(target / UPLOAD_RECEIPT) as tmp:
            tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
        summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary, dict) or not summary.get("final"):
            raise ValueError("restored summary is not final")
        archive = DayArchive(spool, day, r2_prefix, upload=True)
        pending = archive.pending_uploads()
        if pending:
            raise ValueError(f"restored day still has pending uploads: {sorted(pending)}")
        listed = {f"{r2_prefix}/{day.isoformat()}/{n}" for n in journal}
        listed.add(f"{r2_prefix}/{day.isoformat()}/{UPLOAD_JOURNAL}")
        budget.preflight()
        budget.charge_request()
        keys = backend.list_keys(f"{r2_prefix}/{day.isoformat()}/")
        not_restored = sorted(k["key"] for k in keys if k["key"] not in listed)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"day": day.isoformat(), "restored": restored,
            "restored_from_journal": sorted(journal),
            "not_restored": not_restored,
            "complete": len(not_restored) == 0,
            "bytes": sum(r["bytes"] for r in restored),
            "journal_match": True,
            "remote_budget": budget.state(),
            "bounded_read": ("streaming" if callable(getattr(backend, "get_stream", None))
                             else "accounting-only")}


def run_archive_cache_audit(spool: Path, backend: StorageBackend, day: date | str, *,
                            r2_prefix: str = R2_PREFIX,
                            dry_run: bool = True,
                            today: date | None = None) -> dict[str, Any]:
    """Run the lifecycle audit for one finalized day and persist the report.

    Calls prune_archive_cache (dry-run unless dry_run=False) and writes
    the report to <spool>/archive-cache-reports/<day>.json. The report
    dict gains "report_path" (the exact path) or "report_error" when the
    write itself fails; a report-write failure is recorded, never raised.
    The audit never raises for a verification/budget failure -- those are
    recorded in the report with decision "refused..." and no deletion.
    """
    day = date.fromisoformat(day) if isinstance(day, str) else day
    spool = Path(spool)
    report = prune_archive_cache(spool, backend, r2_prefix=r2_prefix,
                                 dry_run=dry_run, today=today)
    reports_dir = spool / ARCHIVE_CACHE_REPORTS_DIR
    report_path = reports_dir / f"{day.isoformat()}.json"
    report["report_path"] = str(report_path)
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        with published(report_path) as tmp:
            tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                           encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - recorded, never fails the caller
        report["report_error"] = f"{type(exc).__name__}: {exc}"
    return report
