"""OA-242 persistent-archive guards: the service writes only to a mounted volume.

Production must name its volume mount (CREDIT_TAPE_VOLUME, or Railway's own
RAILWAY_VOLUME_MOUNT_PATH when a volume is attached). The archive must live
inside that mount and the mount must exist and be a real mount point, so an
absent or detached volume stops the service at startup instead of letting it
write into the container's temporary filesystem.

Every archive carries a one-time identity (archive.json: random archive_id,
created_at). check_archive reports it and --compare requires the same id
before and after a restart, which proves the restart check read the same
persistent archive rather than a fresh directory.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets

IDENTITY = "archive.json"
FORMAT = "credit-tape-archive/1"


def volume_root(env=os.environ) -> str | None:
    return env.get("CREDIT_TAPE_VOLUME") or env.get("RAILWAY_VOLUME_MOUNT_PATH") or None


def require_volume(archive: str, env=os.environ, is_mount=os.path.ismount) -> str:
    """Return the volume root; raise ValueError unless `archive` sits inside a mounted volume."""
    root = volume_root(env)
    if not root:
        raise ValueError("no persistent volume named: set CREDIT_TAPE_VOLUME (or attach a Railway volume)")
    if not os.path.isabs(root) or not os.path.isabs(archive):
        raise ValueError("volume and archive paths must be absolute")
    if not os.path.isdir(root):
        raise ValueError(f"persistent volume absent: {root} does not exist")
    if not is_mount(root):
        raise ValueError(f"persistent volume absent: {root} is not a mount point")
    real_root, real_archive = os.path.realpath(root), os.path.realpath(archive)
    if real_archive == real_root or os.path.commonpath([real_root, real_archive]) != real_root:
        raise ValueError("CREDIT_TAPE_ARCHIVE must be a directory inside the persistent volume")
    return root


def read_identity(archive: str) -> dict | None:
    try:
        with open(os.path.join(archive, IDENTITY), encoding="utf-8") as fh:
            ident = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(ident, dict) or ident.get("format") != FORMAT or not ident.get("archive_id"):
        return None
    return ident


def ensure_identity(archive: str) -> dict:
    """Create the archive identity once (exclusive create); return the existing one otherwise."""
    ident = read_identity(archive)
    if ident:
        return ident
    if os.path.exists(os.path.join(archive, IDENTITY)):
        raise ValueError(f"{IDENTITY} exists but is unreadable; refusing to replace an archive identity")
    os.makedirs(archive, exist_ok=True)
    ident = {"format": FORMAT, "archive_id": secrets.token_hex(16),
             "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    try:
        fd = os.open(os.path.join(archive, IDENTITY), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:  # another writer created it first
        return read_identity(archive) or ident
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ident, fh)
        fh.flush()
        os.fsync(fh.fileno())
    return ident
