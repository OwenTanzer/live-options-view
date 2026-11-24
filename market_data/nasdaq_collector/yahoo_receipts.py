"""Private receipt archive for Yahoo chart responses from the existing collector."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile


def _immutable(path, data):
    if path.exists():
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        name = staged.name
        try:
            staged.write(data)
            staged.flush()
            os.fsync(staged.fileno())
            os.link(name, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(name)


def retain(root, symbol, interval, response, received_at):
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,11}", symbol) or interval not in ("1m", "1d"):
        raise ValueError("Invalid Yahoo chart identity")
    digest = hashlib.sha256(response).hexdigest()
    directory = Path(root) / symbol / interval
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _immutable(directory / (digest + ".json.gz"), gzip.compress(response, mtime=0))
    record = {"source": "yahoo_finance_chart", "symbol": symbol, "interval": interval,
              "received_at": received_at, "sha256": digest, "bytes": len(response)}
    stamp = received_at.replace(":", "").replace("+", "_")
    _immutable(directory / (stamp + "-" + digest + ".receipt.json"),
               json.dumps(record, separators=(",", ":")).encode())


def capture_response(symbol, interval, response):
    root = os.environ.get("OPTIONSVIEW_YAHOO_RECEIPTS")
    if root:
        try:
            retain(root, symbol, interval, response.content, datetime.now(timezone.utc).isoformat())
        except (OSError, ValueError) as error:
            logging.getLogger(__name__).error("Yahoo receipt failed for %s %s: %s", symbol, interval, error)
