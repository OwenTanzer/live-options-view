"""Journal the two crypto panel objects on the publishing host and replay their rows."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

SCHEMA = """CREATE TABLE IF NOT EXISTS spot_attempts (
    id INTEGER PRIMARY KEY, stream TEXT NOT NULL, started TEXT NOT NULL,
    digest TEXT NOT NULL, payload BLOB NOT NULL, confirmed TEXT)"""


def utc_key(stamp):
    if stamp.utcoffset() is None:
        raise ValueError("A timezone is required")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def prepare(database, stream, body, now):
    path = Path(database)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(SCHEMA)
        db.execute("CREATE INDEX IF NOT EXISTS spot_time ON spot_attempts(stream, started, id)")
        return db.execute("""INSERT INTO spot_attempts(stream,started,digest,payload)
            VALUES (?,?,?,?)""", (stream, utc_key(now), hashlib.sha256(body).hexdigest(),
                                  gzip.compress(body))).lastrowid


def confirm(database, attempt, now):
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE spot_attempts SET confirmed=? WHERE id=? AND confirmed IS NULL",
                   (utc_key(now), attempt))


def reconcile(database, stream, body, now):
    """Only a matching current object confirms a pending write, at readback time."""
    if not Path(database).exists():
        return
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(SCHEMA)
        db.execute("""UPDATE spot_attempts SET confirmed=? WHERE confirmed IS NULL AND digest=?
            AND id=(SELECT MAX(id) FROM spot_attempts WHERE stream=?)""",
                   (utc_key(now), hashlib.sha256(body).hexdigest(), stream))


def read_as_of(database, cutoff):
    at = utc_key(cutoff)
    path = Path(database)
    unknown = {"state": "unmeasurable", "rows": None}
    if not path.exists():
        return unknown
    inputs = {}
    receipts = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db, db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='spot_attempts'").fetchone():
            return unknown
        for stream in ("hourly", "minute"):
            row = db.execute("""SELECT confirmed,digest,payload FROM spot_attempts
                WHERE stream=? AND started<=? ORDER BY started DESC,id DESC LIMIT 1""",
                             (stream, at)).fetchone()
            if not row or not row[0] or row[0] > at:
                return {**unknown, "unverified_stream": stream}
            receipts[stream] = {"confirmed_at": row[0], "sha256": row[1]}
            inputs[stream] = json.loads(gzip.decompress(row[2]))
    script = Path(__file__).resolve().parents[2] / "ops/panel-minute/replay.cjs"
    inputs["cutoff"] = at
    output = subprocess.run(["node", str(script)], input=json.dumps(inputs), text=True,
                            capture_output=True, check=True)
    return {"state": "recorded", "rows": json.loads(output.stdout), "receipts": receipts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(read_as_of(args.database, args.as_of)))
