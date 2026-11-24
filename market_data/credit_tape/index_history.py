"""Lossless, daily DTCC index history. One SQLite transaction per HTTP 200."""
import hashlib
import json
import os
import sqlite3
import zlib
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


_partition_lock = threading.Lock()


def encoded(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Writer:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.with_suffix(".remote").exists():
            raise ValueError("refusing to reopen sealed history")
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY, value BLOB UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS listings (id INTEGER PRIMARY KEY, members BLOB UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS polls (name TEXT PRIMARY KEY, header BLOB NOT NULL, listing INTEGER NOT NULL);
        """)
        self.row_ids = {}

    def close(self):
        self.db.close()

    def row_id(self, row):
        key = encoded(row)
        cached = self.row_ids.get(key)
        if cached is None:
            self.db.execute("INSERT OR IGNORE INTO versions(value) VALUES (?)", (key,))
            cached, = self.db.execute("SELECT id FROM versions WHERE value=?", (key,)).fetchone()
            if len(self.row_ids) >= 8192:
                self.row_ids.clear()
            self.row_ids[key] = cached
        return cached

    def save(self, name, snapshot):
        try:
            with self.db:
                members = {}
                for channel, rows in snapshot["entries"].items():
                    # Tag non-list values so that missing or malformed channels round trip.
                    members[channel] = ([self.row_id(row) for row in rows] if isinstance(rows, list)
                                        else {"literal": rows})
                refs = zlib.compress(encoded(members))
                self.db.execute("INSERT OR IGNORE INTO listings(members) VALUES (?)", (refs,))
                listing, = self.db.execute("SELECT id FROM listings WHERE members=?", (refs,)).fetchone()
                header = encoded({k: v for k, v in snapshot.items() if k != "entries"})
                existing = self.db.execute("SELECT header, listing FROM polls WHERE name=?", (name,)).fetchone()
                if existing is None:
                    self.db.execute("INSERT INTO polls VALUES (?,?,?)", (name, header, listing))
                elif existing != (header, listing):
                    raise ValueError("conflicting index receipt: " + name)
        except BaseException:
            self.row_ids.clear()  # inserts from a rolled-back transaction have no usable IDs
            raise


def snapshots(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        # Both tables must describe the same commit. Release the read lock before
        # handing rows to a potentially slow replay consumer.
        db.execute("BEGIN")
        versions = {key: json.loads(value) for key, value in db.execute("SELECT id,value FROM versions")}
        query = "SELECT name,header,members FROM polls JOIN listings ON listing=listings.id ORDER BY name"
        receipts = db.execute(query).fetchall()
    for name, header, packed in receipts:
        members = json.loads(zlib.decompress(packed))
        entries = {k: [versions[i] for i in v] if isinstance(v, list) else v["literal"]
                   for k, v in members.items()}
        yield name, {**json.loads(header), "entries": entries}


def record(root, name, snapshot):
    day = snapshot["fetched_at"][:10]
    with _partition_lock, closing(Writer(Path(root) / "index-history" / (day + ".sqlite"))) as writer:
        writer.save(name, snapshot)


def s3():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ.get("CREDIT_TAPE_S3_ENDPOINT") or None)


@contextmanager
def downloaded(client, location):
    # Use container scratch, never the monitored archive mount.
    with TemporaryDirectory() as tmp:
        path = Path(tmp, "history.sqlite")
        client.download_file(location["bucket"], location["key"], str(path))
        if digest_file(path) != location["sha256"]:
            raise ValueError("archived index partition failed SHA-256 verification")
        yield path


def seal(path, client, bucket, archive_id):
    """Caller owns the sealed day exclusively. Keep local data on any failure."""
    digest = digest_file(path)
    location = {"bucket": bucket, "key": f"credit-tape/{archive_id}/index/{path.stem}/{digest}.sqlite",
                "sha256": digest}
    client.upload_file(str(path), bucket, location["key"])
    with downloaded(client, location):
        pass
    with _partition_lock:
        if digest_file(path) != digest:
            raise ValueError("partition changed during upload")
        marker = path.with_suffix(".remote")
        tmp = marker.with_suffix(".remote.tmp")
        with tmp.open("wb") as stream:
            stream.write(encoded(location))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, marker)
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        path.unlink()


def partitions(root):
    folder = Path(root) / "index-history"
    for day in sorted({p.stem for suffix in ("*.sqlite", "*.remote") for p in folder.glob(suffix)}):
        local = folder / (day + ".sqlite")
        try:
            if local.exists():
                yield from snapshots(local)
                continue
        except sqlite3.OperationalError:
            if local.exists():
                raise
        marker = folder / (day + ".remote")
        with downloaded(s3(), json.loads(marker.read_bytes())) as path:
            yield from snapshots(path)


def rotate(folder, today, bucket, archive_id):
    # The single live poll writer invokes this before the first poll of a UTC day.
    for path in sorted(Path(folder).glob("*.sqlite")):
        if path.stem < today:
            seal(path, s3(), bucket, archive_id)
