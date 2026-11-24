"""Private One Clock replay of confirmed crypto panel publications."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3


def utc_key(stamp):
    if stamp.utcoffset() is None:
        raise ValueError("A timezone is required")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def retain(database, body, confirmed_at):
    database = Path(database)
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS receipts (
            digest TEXT PRIMARY KEY, confirmed_at TEXT NOT NULL, payload BLOB NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS by_time ON receipts(confirmed_at, digest)")
        db.execute("""INSERT INTO receipts VALUES (?, ?, ?)
            ON CONFLICT(digest) DO NOTHING""",
                   (hashlib.sha256(body).hexdigest(), utc_key(confirmed_at), gzip.compress(body)))


def read_as_of(database, cutoff):
    at = utc_key(cutoff)
    path = Path(database)
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("""SELECT confirmed_at, digest, payload FROM receipts
            WHERE confirmed_at <= ? ORDER BY confirmed_at DESC, digest DESC LIMIT 1""", (at,)).fetchone()
    if row:
        return {"confirmed_at": row[0], "sha256": row[1], "snapshot": json.loads(gzip.decompress(row[2]))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(read_as_of(args.database, args.as_of)))
