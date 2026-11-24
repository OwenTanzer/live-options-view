"""Disk budget in decimal bytes, independently observed by the external volume alert."""
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import index_history

WARN = 1_500_000_000
PAUSE = 500_000_000
RESUME = 750_000_000


def status(root):
    free = shutil.disk_usage(root).free
    return {"available_bytes": free, "low_space": free < WARN, "pause_writers": free < PAUSE}


def archive_days(archive, archive_id, stop, bucket):
    folder = Path(archive, "dtcc-slices", "index-history")
    while not stop.is_set():
        try:
            index_history.rotate(folder, datetime.now(timezone.utc).date().isoformat(), bucket, archive_id)
        except Exception as exc:
            # Upload or read-back failure keeps the local partition and retries on the next pass.
            logging.error("Credit Tape index archival retry: %s", type(exc).__name__)
        stop.wait(300)
