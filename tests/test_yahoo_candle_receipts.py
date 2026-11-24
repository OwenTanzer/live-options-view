import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from market_data.nasdaq_collector.yahoo_receipts import retain


class YahooReceipts(unittest.TestCase):
    def test_receipts_preserve_repeated_reads_and_corrected_bytes(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / "AAPL" / "1m"
            for minute, body in [(1,b'{"close":1}'),(2,b'{"close":1}'),(3,b'{"close":2}')]:
                retain(root, "AAPL", "1m", body, f"2026-11-24T15:0{minute}:00+00:00")
            self.assertEqual(len(list(directory.glob("*.gz"))), 2)
            receipts = sorted(directory.glob("*.receipt.json"))
            self.assertEqual(len(receipts), 3)
            for receipt in receipts:
                record = json.loads(receipt.read_bytes())
                raw = gzip.decompress((directory / (record["sha256"]+".json.gz")).read_bytes())
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])
                self.assertEqual(record["source"], "yahoo_finance_chart")
