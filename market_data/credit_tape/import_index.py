"""Build compact partitions off-volume from a quiescent full-volume tar.gz backup."""
import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path

from .index_history import Writer, digest_file, snapshots


def import_backup(backup, target, expected):
    if digest_file(backup) != expected:
        raise ValueError("backup SHA-256 mismatch")
    target.mkdir(exist_ok=False, parents=True)
    writers, proofs = {}, {}
    count = size = 0
    try:
        with tarfile.open(backup, "r|gz") as archive:
            for item in archive:
                name = item.name.removeprefix("./")
                if not name.startswith("credit-tape/dtcc-slices/index/") or not item.isfile():
                    continue
                match = re.fullmatch(r"credit-tape/dtcc-slices/index/(\d{4}-\d{2}-\d{2})/([^/]+\.json)", name)
                if not match:
                    raise ValueError("unexpected index path: " + name)
                day, leaf = match.groups()
                value = json.load(archive.extractfile(item))
                if day != value["fetched_at"][:10] or (day, leaf) in proofs:
                    raise ValueError("invalid or duplicate index receipt: " + name)
                if day not in writers:
                    writers[day] = Writer(target / (day + ".sqlite"))
                    # Only this rebuildable, offline copy batches its transactions.
                    writers[day].db.execute("PRAGMA synchronous=OFF")
                writers[day].save(leaf, value)
                proofs[day, leaf] = fingerprint(value)
                count += 1
                size += item.size
    finally:
        for writer in writers.values():
            writer.close()
    if not count:
        raise ValueError("no index evidence found")
    for path in target.glob("*.sqlite"):
        for leaf, value in snapshots(path):
            if proofs.pop((path.stem, leaf)) != fingerprint(value):
                raise ValueError("restored snapshot mismatch: " + leaf)
    if proofs:
        raise ValueError("incomplete reconstruction")
    print(json.dumps({"verified_snapshots": count, "legacy_bytes": size,
                      "partition_bytes": sum(p.stat().st_size for p in target.glob("*.sqlite")),
                      "sha256": {p.name: digest_file(p) for p in sorted(target.glob("*.sqlite"))}}))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).digest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("new_directory", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    import_backup(args.backup, args.new_directory, args.sha256)
