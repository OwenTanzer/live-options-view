import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from market_data.crypto_candle_cache.archive import install, read_rows
from market_data.crypto_candle_cache.one_clock import instant_ms


class ArchiveTest(unittest.TestCase):
    def bundle(self, root, count=2):
        first = "2020-01-01T00:00:00Z"
        last = "2020-01-01T01:00:00Z"
        updated = "2020-01-01T01:10:00Z"
        stamp = instant_ms(first) // 1000
        path = root / "archive.zip"
        data = {"BTC": ("coinbase", [[stamp, 1, 4, 2, 3, 5], [stamp+3600, 2, 5, 3, 4, 1]]),
                "X": ("coingecko", [{"timestamp_ms": instant_ms(t), "date": t, "price": 2}
                                     for t in (first, last)])}
        coverage = {}
        with zipfile.ZipFile(path, "w") as archive:
            for symbol, (provider, rows) in data.items():
                coverage[symbol] = {"1hour": dict(source=provider, bars=count, done=False, first=first,
                                                  last=last, updated=updated)}
                archive.writestr(f"data/{provider}/{symbol}_1hour.json", json.dumps(rows))
            coverage["BTC"]["daily"] = dict(source="tiingo", bars=1, done=True, first=first,
                                             last=first, updated=updated)
            archive.writestr("data/tiingo/BTC_daily.json", json.dumps([
                dict(date=first, open=2, high=4, low=1, close=3, volume=5)]))
            archive.writestr("data/coingecko/BTC_daily.json", json.dumps([
                dict(timestamp_ms=instant_ms(first), date=first, price=7)]))
            archive.writestr("manifest.json", json.dumps(dict(started=last, updated=updated, coins=list(data),
                                                              coverage=coverage)))
        return path

    def test_original_receipt_final_bar_and_point_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.bundle(root)
            target = install(source, root/"snapshots")
            self.assertEqual(install(source, root/"snapshots"), target)
            with closing(sqlite3.connect(target)) as db:
                receipt = json.loads(db.execute("SELECT json FROM receipt").fetchone()[0])
                self.assertFalse(receipt["coverage"][0]["source_batch_done"])
                at = instant_ms(receipt["available_at"])
                self.assertEqual(read_rows(db, receipt, "BTC", "1h", at-1, 5), [])
                alternate, = read_rows(db, receipt, "BTC", "1d", at, 1, "coingecko")
                self.assertEqual(alternate["value"], {"price": 7})
                self.assertEqual(read_rows(db, receipt, "BTC", "1d", at, 1), [])  # daily candle still open
                candle, = read_rows(db, receipt, "BTC", "1h", at, 5)
                self.assertEqual(candle["value"]["close"], 3)
                self.assertEqual(candle["eventAt"] - candle["valueAt"], 3600000)
                sample, = read_rows(db, receipt, "X", "1h", at, 1)
                self.assertEqual(sample["value"], {"price": 2})
                self.assertEqual(sample["valueAt"], sample["eventAt"])

    def test_manifest_mismatch_cannot_install_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "count"):
                install(self.bundle(root, count=3), root/"snapshots")
            self.assertFalse(list((root/"snapshots").glob("*.sqlite3")))
