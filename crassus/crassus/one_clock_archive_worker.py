"""Independent public projection of confirmed private backup ranges.

The existing archive tick wakes this worker. A separate durable offset advances
only after a published (or wholly ineligible) chunk. No network call runs on
the decision, trading or private-backup thread.
"""
import json
import logging
import os
import threading
from pathlib import Path

from .durability import atomic_json

log = logging.getLogger("crassus.one_clock")


class ArchiveProjection:
    def __init__(self, archive, public_client, public_bucket, checkpoint):
        self.archive, self.public, self.bucket = archive, public_client, public_bucket
        self.checkpoint = Path(checkpoint)
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="one-clock-archive")
        self.previous_file = ""

    def step(self):
        # The archive owner replaces its checkpoint atomically only after
        # immutable bytes, key range and private blob digest are verified.
        owner = json.loads(self.archive.checkpoint.read_text())
        namespace = owner["namespace"]
        state = json.loads(self.checkpoint.read_text()) if self.checkpoint.exists() else {
            "namespace":namespace, "privateBucket":self.archive.bucket,
            "publicBucket":self.bucket, "files":{}}
        if (state["namespace"] != namespace or state["privateBucket"] != self.archive.bucket
                or state["publicBucket"] != self.bucket):
            raise ValueError("Projection checkpoint owner changed")
        if any(name not in owner["files"] or type(value) is not int or
               not 0 <= value <= owner["files"][name]["offset"] for name,value in state["files"].items()):
            raise ValueError("Projection checkpoint exceeds verified private archive")
        pending = sorted(name for name, entry in owner["files"].items()
                         if entry["offset"] > state["files"].get(name,0))
        if not pending:
            return False
        name = state.get("pendingFile") or next((n for n in pending if n > self.previous_file),pending[0])
        if name not in pending:
            raise ValueError("Pending private range disappeared")
        self.previous_file = name
        start = state["files"].get(name,0)
        prefix = f"crassus-ledger/v1/{namespace}/{name}/{start:020d}-"
        listing = self.archive.client.list_objects_v2(Bucket=self.archive.bucket,Prefix=prefix,MaxKeys=2)
        objects = listing.get("Contents",[])
        if listing.get("IsTruncated") or len(objects) != 1:
            raise ValueError("Archive range is missing or ambiguous")
        from .one_clock_archive import KEY, publish_chunk
        key = objects[0]["Key"]
        match = KEY.fullmatch(key)
        if not key.startswith(prefix) or not match or int(match.group(2)) > owner["files"][name]["offset"]:
            raise ValueError("Archive range outside its verified checkpoint")
        # An immutable intent can exist without its successful acknowledgement.
        # Pin this range before touching any stream so another range cannot
        # consume its intended sequence after a crash or ambiguous write.
        if state.get("pendingFile") is None:
            state["pendingFile"] = name
            atomic_json(self.checkpoint,state)
        receipt = publish_chunk(self.archive.client,self.archive.bucket,self.public,self.bucket,key)
        state.pop("pendingFile")
        state["files"][name] = int(match.group(2))
        atomic_json(self.checkpoint,state)
        log.info("One Clock archive projected commits=%d unqualified=%d",len(receipt["commits"]),
                 receipt["unidentifiedOrIneligible"])
        return True

    def _run(self):
        while not self.stop.is_set():
            self.wake.wait()
            self.wake.clear()
            for _ in range(4):
                if self.stop.is_set():
                    return
                try:
                    if not self.step():
                        break
                except Exception as exc:
                    log.warning("One Clock archive projection deferred (%s)",type(exc).__name__)
                    break

    def start(self):
        self.thread.start()
        self.wake.set()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=1)


def from_environment(archive,state_dir):
    if archive is None or os.environ.get("CRASSUS_ONE_CLOCK_PUBLICATION") == "disabled":
        return None
    prefix = "CRASSUS_ONE_CLOCK_PUBLIC_"
    names = ("ACCOUNT_ID","ACCESS_KEY_ID","SECRET_ACCESS_KEY","BUCKET")
    values = {name:os.environ.get(prefix+name,"").strip() for name in names}
    if not any(values.values()):
        # This is the bucket-scoped public writer already used by the meter.
        # An explicitly supplied override must validate on its own.
        from .oi_skew_publisher import config_from_environment
        values, reason = config_from_environment()
        if values is None:
            return None
    if (not all(values.values()) or len(values["ACCOUNT_ID"]) != 32 or
            any(c not in "0123456789abcdef" for c in values["ACCOUNT_ID"]) or
            values["BUCKET"] == archive.bucket or
            values["ACCESS_KEY_ID"] == os.environ.get("CRASSUS_ARCHIVE_ACCESS_KEY_ID")):
        log.warning("One Clock public archive settings incomplete or bucket not separate")
        return None
    try:
        # Do not inherit or reuse private backup credentials.
        from .oi_skew_publisher import _r2_client
        client = _r2_client(values)
        return ArchiveProjection(archive,client,values["BUCKET"],state_dir / "one-clock" / "checkpoint.json")
    except Exception as exc:
        log.warning("One Clock archive projection disabled (%s)",type(exc).__name__)
        return None
