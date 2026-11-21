"""Collect the publisher's delayed UK MTF minute CSVs on the private archive volume."""
from contextlib import closing
from datetime import datetime
import csv
import gzip
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler
from xml.etree import ElementTree
import zlib

from .treu_sample import import_sample

HOST = "http://fe9cbcfaa266.mifid.io.tradeweb.com"
PREFIX = "Tradeweb_UK_MTF_Post-Trade_TREU/"
NAME = re.compile(r"UK_TREU_[0-9]{8}_[0-9]{4}\.csv")
DATABASE = "tradeweb-treu.sqlite3"
LIMIT = 32 * 1024 * 1024


class DirectOnly(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url, limit):
    with build_opener(DirectOnly).open(url, timeout=15) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("TREU response exceeds size limit")
    return data


def listing(get):
    marker = ""
    while True:
        params = {"delimiter": "/", "prefix": PREFIX}
        if marker:
            params["marker"] = marker
        root = ElementTree.fromstring(get(HOST + "/?" + urlencode(params, safe="/"), 2**20))
        if root.tag.split("}")[-1] != "ListBucketResult" or root.findtext("{*}Prefix") != PREFIX:
            raise ValueError("Unexpected TREU listing")
        for entry in root.findall("{*}Contents"):
            key = entry.findtext("{*}Key", "")
            name = key.removeprefix(PREFIX)
            if key.startswith(PREFIX) and NAME.fullmatch(name):
                yield (name, entry.findtext("{*}ETag", "").strip('"'),
                       entry.findtext("{*}LastModified"), int(entry.findtext("{*}Size")))
        if root.findtext("{*}IsTruncated") != "true":
            return
        new_marker = root.findtext("{*}NextMarker")
        if not new_marker or new_marker <= marker:
            raise ValueError("Incomplete TREU pagination")
        marker = new_marker


def initialize(db):
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files (
          name TEXT, etag TEXT, modified TEXT, size INTEGER, raw BLOB, digest TEXT,
          row_count INTEGER, retry REAL DEFAULT 0, PRIMARY KEY(name,etag));
        CREATE INDEX IF NOT EXISTS pending ON files(retry,modified) WHERE raw IS NULL;
        CREATE TABLE IF NOT EXISTS reports (seq INTEGER PRIMARY KEY, event BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS status (id INTEGER PRIMARY KEY, last_index REAL, last_file REAL);
        INSERT OR IGNORE INTO status VALUES (1,NULL,NULL);
    """)


def capture_file(db, name, etag, modified, size, get):
    if not 0 <= size <= LIMIT:
        raise ValueError("Invalid advertised size")
    blob = get(HOST + "/" + PREFIX + name, size)
    if len(blob) != size:
        raise ValueError("Incomplete TREU file")
    if re.fullmatch("[a-fA-F0-9]{32}", etag) and hashlib.md5(blob).hexdigest() != etag.lower():
        raise ValueError("TREU file changed since listing")
    digest = hashlib.sha256(blob).hexdigest()
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / name
        path.write_bytes(blob)
        archive = path.parent / "parsed"
        count = import_sample(path, archive)
        with db, (archive / "events.jsonl").open(encoding="utf-8") as events:
            for line in events:
                event = json.loads(line)
                event["qualification"] = "Unclassified instrument"
                event["publisher_file_modified"] = modified
                event["backfill"] = time.time() - datetime.fromisoformat(modified).timestamp() > 300
                event["raw_ref"] = name + "#" + digest
                db.execute("INSERT INTO reports(event) VALUES (?)",
                           (zlib.compress(json.dumps(event, separators=(",", ":")).encode()),))
            db.execute("UPDATE files SET raw=?, digest=?, row_count=? WHERE name=? AND etag=?",
                       (gzip.compress(blob, mtime=0), digest, count, name, etag))
            db.execute("UPDATE status SET last_file=? WHERE id=1", (time.time(),))
    return count


def poll(db, get, stop, archive):
    entries = list(listing(get))
    with db:
        db.executemany("INSERT OR IGNORE INTO files(name,etag,modified,size) VALUES(?,?,?,?)", entries)
        db.execute("UPDATE status SET last_index=? WHERE id=1", (time.time(),))
    # Take the latest substantial file first on bootstrap. Drain recent and older work every pass.
    pending = "SELECT name,etag,modified,size FROM files WHERE raw IS NULL AND retry<=?"
    newest = db.execute(pending + " ORDER BY modified DESC LIMIT 8", (time.time(),)).fetchall()
    backlog = db.execute(pending + " ORDER BY size DESC, modified LIMIT 8", (time.time(),)).fetchall()
    for entry in dict.fromkeys(backlog[:1] + newest + backlog[1:]):
        if stop.is_set():
            return
        if shutil.disk_usage(archive).free < 128 * 1024**2:
            raise OSError("TREU capture paused for volume capacity")
        try:
            count = capture_file(db, *entry, get)
            if count:
                print(f"TREU archived {entry[0]} ({count} reports)", flush=True)
        except (ValueError, OSError, OverflowError, csv.Error) as exc:
            with db:
                db.execute("UPDATE files SET retry=? WHERE name=? AND etag=?",
                           (time.time() + 300, *entry[:2]))
            logging.warning("TREU %s: %s", entry[0], exc)


def run(archive, stop):
    with closing(sqlite3.connect(archive / DATABASE)) as db:
        initialize(db)
        while not stop.is_set():
            try:
                poll(db, fetch, stop, archive)
            except (ValueError, OSError, ElementTree.ParseError, sqlite3.Error) as exc:
                logging.warning("TREU capture: %s", exc)
            stop.wait(60)


def reader(archive):
    return closing(sqlite3.connect((archive / DATABASE).resolve().as_uri() + "?mode=ro", uri=True))


def window(archive, limit):
    with reader(archive) as db:
        rows = db.execute("SELECT seq,event FROM reports ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    return [(seq, zlib.decompress(event)) for seq, event in reversed(rows)]


def health(archive):
    with reader(archive) as db:
        last_index, last_file = db.execute("SELECT last_index,last_file FROM status").fetchone()
        pending, failures = db.execute("SELECT count(*),count(nullif(retry,0)) FROM files WHERE raw IS NULL").fetchone()
        seq = db.execute("SELECT max(seq) FROM reports").fetchone()[0] or 0
    return {"listing_ok": bool(last_index and time.time()-last_index < 300), "last_index": last_index,
            "last_file": last_file, "pending_files": pending, "failed_pending": failures, "archived_reports": seq}
