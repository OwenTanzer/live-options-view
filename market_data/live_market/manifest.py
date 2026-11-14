"""The bucket-root `manifest.json` index: read it honestly, merge it safely.

WHY this module: `manifest.json` has two owners with different fields. The
external daily pipeline owns `dates` (the trading-day index the Historical tab
selects from); this collector owns `weekly_expirations` (which hourly weekly
archives exist). Both used to write the whole object unconditionally after a
read, so whichever wrote second discarded the other's field, and the daily
writer's reader turned *any* read failure into `{"dates": []}` -- which, written
back, replaces the real index with an empty one.

The rule here is compare-and-swap, not hope. Read the object and its ETag,
merge only your own field while carrying every other key through verbatim, then
write with `IfMatch` (or `IfNoneMatch: "*"` when creating). A lost race is a
conflict to re-read, not a document to overwrite. Retries are bounded and
exhaustion raises: by then the archive CSV is already durable, so a failed index
write loses an index entry the next slot re-derives, never an observation.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .config import R2_BUCKET

MANIFEST_KEY = "manifest.json"

# Five attempts with sub-second backoff: the contention window is a single
# read-modify-write in another process, not a queue.
MANIFEST_ATTEMPTS = 5
MANIFEST_BACKOFF_BASE_SECS = 0.1
MANIFEST_BACKOFF_CAP_SECS = 1.1

_MISSING_CODES = {"NoSuchKey", "NotFound", "404"}
_CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}


class ManifestError(RuntimeError):
    """Base class for manifest failures that must not be written around."""


class ManifestUnreadable(ManifestError):
    """The object could not be read faithfully. Never treated as empty."""


class ManifestDamaged(ManifestError):
    """The object is missing a key its published contract requires."""


class ManifestConflict(ManifestError):
    """Another writer changed the object between our read and our write."""


class ConditionalWriteUnsupported(ManifestError):
    """This client cannot send a precondition, so no safe write is possible."""


def _error_code(exc: BaseException) -> Optional[str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Code") is not None:
            return str(error["Code"])
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status is not None:
            return str(status)
    for name in ("code", "status"):
        value = getattr(exc, name, None)
        if value is not None:
            return str(value)
    return None


def _is_missing(exc: BaseException) -> bool:
    if _error_code(exc) in _MISSING_CODES:
        return True
    # A client whose exception carries no response metadata at all: the only
    # honest reading of "NoSuchKey" in the text is that the key is absent.
    return "NoSuchKey" in f"{type(exc).__name__} {exc}"


def _is_conflict(exc: BaseException) -> bool:
    return _error_code(exc) in _CONFLICT_CODES


def assert_conditional_writes_supported(s3) -> None:
    """Fail before a write if this client cannot send a precondition.

    `PutObject` gained `IfMatch`/`IfNoneMatch` after the `boto3>=1.34` floor
    both producers pin, so the capability is checked rather than assumed. A
    client with no service model (a test double) is trusted to model the
    semantics it implements; the concurrency suite is what proves it does.
    """
    service_model = getattr(getattr(s3, "meta", None), "service_model", None)
    if service_model is None:
        return
    try:
        members = service_model.operation_model("PutObject").input_shape.members
    except Exception as exc:
        raise ConditionalWriteUnsupported(f"cannot inspect PutObject shape: {exc}") from exc
    if "IfMatch" not in members or "IfNoneMatch" not in members:
        raise ConditionalWriteUnsupported(
            "this botocore cannot send PutObject preconditions (IfMatch/IfNoneMatch); "
            "refusing to write manifest.json unconditionally"
        )


def read_manifest(s3, bucket: str = R2_BUCKET) -> tuple[Optional[dict], Optional[str]]:
    """Return `(document, etag)`, or `(None, None)` when the object is absent.

    An absent index is a legitimate first run. An unreadable one is not: a
    timeout or a 5xx raises `ManifestUnreadable` so the caller cannot mistake
    it for an empty index and publish that.
    """
    try:
        response = s3.get_object(Bucket=bucket, Key=MANIFEST_KEY)
    except Exception as exc:
        if _is_missing(exc):
            return None, None
        raise ManifestUnreadable(f"manifest read failed: {exc}") from exc
    body = response["Body"].read()
    etag = response.get("ETag")
    if not etag:
        raise ManifestUnreadable("manifest read returned no ETag; a write could not be conditional")
    try:
        document = json.loads(body.decode() if isinstance(body, (bytes, bytearray)) else body)
    except Exception as exc:
        raise ManifestUnreadable(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestUnreadable("manifest is not a JSON object")
    return document, etag


def write_manifest(s3, document: dict, etag: Optional[str],
                   bucket: str = R2_BUCKET) -> Optional[str]:
    """Write `document` only if the object is still the version we read.

    An `etag` of None means "we read absence", which becomes a create-only
    write so two simultaneous creators cannot both claim the index. Returns the
    new ETag when the client reports one.
    """
    assert_conditional_writes_supported(s3)
    options: dict[str, Any] = {
        "Bucket": bucket,
        "Key": MANIFEST_KEY,
        "Body": json.dumps(document, indent=2, sort_keys=True).encode(),
        "ContentType": "application/json",
    }
    if etag:
        options["IfMatch"] = etag
    else:
        options["IfNoneMatch"] = "*"
    try:
        response = s3.put_object(**options)
    except Exception as exc:
        if _is_conflict(exc):
            raise ManifestConflict(f"manifest changed since it was read: {exc}") from exc
        raise
    return (response or {}).get("ETag") if isinstance(response, dict) else None


def _backoff(attempt: int) -> float:
    return min(MANIFEST_BACKOFF_BASE_SECS * (2 ** attempt), MANIFEST_BACKOFF_CAP_SECS)


def update_manifest(
    s3,
    mutate: Callable[[dict], dict],
    *,
    seed: Optional[dict] = None,
    bucket: str = R2_BUCKET,
    attempts: int = MANIFEST_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    """Read-merge-CAS with bounded retry against a concurrent writer.

    `mutate` receives the freshly read document and returns the document to
    write. It must change only the caller's own field: everything it does not
    touch -- `dates` for this collector, `weekly_expirations` for the daily
    pipeline, `note`, and any field added later -- travels through untouched.

    `seed` is the document to start from when the object is absent, so a
    first run publishes a valid index rather than a half-built one. It is never
    used when the object exists, however damaged it looks: a present document
    is mutated, or refused, never replaced.

    Raises `ManifestConflict` on retry exhaustion. No unconditional write is
    ever attempted, so a caller that gives up leaves the last good index in
    place.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        document, etag = read_manifest(s3, bucket)
        base = dict(document) if document is not None else dict(seed or {})
        next_document = mutate(base)
        if not isinstance(next_document, dict):
            raise ManifestError("mutate() must return the document to write")
        try:
            write_manifest(s3, next_document, etag, bucket)
            return next_document
        except ManifestConflict as exc:
            last_error = exc
            if attempt < attempts - 1:
                sleeper(_backoff(attempt))
    raise ManifestConflict(f"manifest update conflicted after {attempts} attempts: {last_error}")


def empty_manifest_seed() -> dict:
    """A first-run document: an explicit empty date index, ours to extend."""
    return {"dates": [], "weekly_expirations": {}}


def add_weekly_key(document: dict, exp_date: str, snapshot_key: str) -> dict:
    """Collector merge: index one weekly archive key under its expiry.

    `dates` is required, not defaulted. A document that exists without it is a
    damaged index, and writing it back would publish an object the Historical
    tab cannot read (`site/js/historical-panel.js` rejects a manifest whose
    `dates` is not an array). Refuse, log, and let the index be rebuilt from
    `raw/qqq_chain_*.csv`; never invent an empty date list over a real one.
    """
    if "dates" not in document:
        raise ManifestDamaged(
            "manifest has no 'dates' key; refusing to rewrite a damaged date index"
        )
    weekly = document.setdefault("weekly_expirations", {})
    if not isinstance(weekly, dict):
        raise ManifestDamaged("manifest 'weekly_expirations' must be an object")
    keys = weekly.setdefault(exp_date, [])
    if not isinstance(keys, list):
        raise ManifestDamaged(f"manifest 'weekly_expirations[{exp_date}]' must be a list")
    if snapshot_key not in keys:
        keys.append(snapshot_key)
        keys.sort()
    # Preserved contract, not freshness: `updated_at` records which writer
    # touched the object last, and the architecture note already says it is not
    # the date index's freshness. Keeping it stamped avoids a semantic drift in
    # a published field for no gain.
    document["updated_at"] = datetime.now(timezone.utc).isoformat()
    return document


__all__ = [
    "MANIFEST_KEY",
    "MANIFEST_ATTEMPTS",
    "ManifestError",
    "ManifestUnreadable",
    "ManifestDamaged",
    "ManifestConflict",
    "ConditionalWriteUnsupported",
    "assert_conditional_writes_supported",
    "read_manifest",
    "write_manifest",
    "update_manifest",
    "empty_manifest_seed",
    "add_weekly_key",
]
