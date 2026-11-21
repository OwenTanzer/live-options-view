import gzip
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from market_data.credit_tape import treu
from market_data.credit_tape.serve_private import panel_event
from market_data.credit_tape.site_table import render_table


class TreuCollectorTests(unittest.TestCase):
    def test_retry_restart_and_raw_lineage(self):
        name = "UK_TREU_20261120_1730.csv"
        raw = (b'"FileName=UK_TREU_20261120_1730"\n"FileDate=20261120"\n"FileTime=17:30"\n\n'
               b'Trading date and time,Publication date and time,Venue of publication,'
               b'Transaction identification code,Instrument identification code\n'
               b'2026-11-20T10:00Z,2026-11-20T17:30Z,TREU,TX-1,TEST-UPI\n')
        etag = hashlib.md5(raw).hexdigest()
        listing = (f'<ListBucketResult><Prefix>{treu.PREFIX}</Prefix><Contents><Key>{treu.PREFIX}{name}</Key>'
                   f'<ETag>"{etag}"</ETag><LastModified>2026-11-20T17:45:00Z</LastModified>'
                   f'<Size>{len(raw)}</Size></Contents><IsTruncated>false</IsTruncated></ListBucketResult>').encode()
        file_data = b""
        def get(url, limit):
            return listing if "?" in url else file_data
        with TemporaryDirectory() as tmp:
            archive = Path(tmp)
            db = sqlite3.connect(archive / treu.DATABASE)
            treu.initialize(db)
            treu.poll(db, get, Event(), archive)
            self.assertEqual(treu.health(archive)["failed_pending"], 1)
            db.execute("UPDATE files SET retry=0")
            db.commit()
            db.close()
            db = sqlite3.connect(archive / treu.DATABASE)
            try:
                file_data = raw
                treu.poll(db, get, Event(), archive)
                treu.poll(db, get, Event(), archive)
                self.assertEqual(treu.health(archive)["archived_reports"], 1)
                self.assertEqual(gzip.decompress(db.execute("SELECT raw FROM files").fetchone()[0]), raw)
                seq, line = treu.window(archive, 10)[0]
                report = panel_event(line, seq)
                self.assertEqual(report["raw_sha256"], hashlib.sha256(raw).hexdigest())
                html = render_table([report], seq, has_treu=True, treu_view=True)
                self.assertIn(b"Unclassified instrument", html)
                self.assertIn(b"TX-1", html)
                self.assertIn(name.encode(), html)
            finally:
                db.close()
