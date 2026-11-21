import json
from pathlib import Path
import tempfile
import unittest

from market_data.credit_tape import serve_private as panel
from market_data.credit_tape.site_table import render_table
from market_data.credit_tape.treu_sample import import_sample


class TreuSampleTests(unittest.TestCase):
    def test_source_and_original_survive_import_and_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "UK_TREU_20261120_1730.csv"
            source.write_text(
                '"FileName=UK_TREU_20261120_1730"\n"FileDate=20261120"\n"FileTime=17:30"\n\n'
                'Trading date and time,Publication date and time,Venue of publication,'
                'Transaction identification code,Instrument identification code,Flags\n'
                '2026-11-20T10:00Z,2026-11-20T17:30Z,TREU,42,TESTUPI,"LRGS,TPAC"\n',
                encoding="utf-8")
            dest = root / "sample"
            self.assertEqual(import_sample(source, dest), 1)
            with self.assertRaises(FileExistsError):
                import_sample(source, dest)
            rows, cursor = panel.read_table_window(dest)
            row, = rows
            self.assertEqual(row["summary"]["productName"], "TESTUPI")
            self.assertEqual(panel.read_raw(dest, row["raw_ref"]), source.read_bytes())
            self.assertEqual(row["execution_ts"], "2026-11-20T10:00Z")
            exported = panel.export_events(dest, "2000-01-01T00:00:00Z", "2199-01-01T00:00:00Z")
            self.assertEqual(json.loads(exported)["record"]["Flags"], "LRGS,TPAC")
            page = render_table(rows, cursor, has_sample=True, sample_view=True)
            for value in (b"Tradeweb", b"TREU", b"Unclassified sample", b"Return to live tape"):
                self.assertIn(value, page)
            legacy = dict(row, publisher="DTCC Data Repository (U.S.) LLC", jurisdiction="US-SEC")
            self.assertIn(b'data-publisher="DTCC"', render_table([legacy], cursor))

            source.write_bytes(source.read_bytes().replace(b",TREU,", b",TREA,"))
            with self.assertRaises(ValueError):
                import_sample(source, root / "broken")
            self.assertFalse((root / "broken").exists())
