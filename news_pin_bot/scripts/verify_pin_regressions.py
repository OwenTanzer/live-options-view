"""Hermetic orchestration, persisted recovery and signed-alert regressions."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
from bot.discord_bot import build_pin_embed
from correlate.pin_engine import PinEngine
from correlate.price_tracker import PriceTracker
from db.storage import SCHEMA, Storage
from ingest.dedup import RecentTextWindow
from ingest.types import Headline


class PinRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pins.sqlite3"
        self.storage = Storage(self.path)
        self.tracker = PriceTracker()
        self.engine = PinEngine(self.storage, self.tracker)

    def row(self, pin_id):
        with self.storage._connect() as conn:
            return dict(conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone())

    async def test_scoring_delay_keeps_original_ingest_anchor(self):
        clock = [1000.0]
        self.tracker.on_trade("QQQ", 100, 10, 999)

        async def source():
            yield Headline("fixture", "1", ["QQQ"], "QQQ news", "", "", 1000, 1000)

        async def delayed_score(*args):
            clock[0] = 1012.0
            self.tracker.on_trade("QQQ", 110, 100, 1005)
            return 9.0, "fixture", "fixture"

        with patch("time.time", side_effect=lambda: clock[0]), \
             patch.object(app, "score_headline", delayed_score), \
             patch.object(self.engine, "_resolve_pin", new_callable=AsyncMock):
            await app.process_headlines(source(), self.storage, self.engine,
                                        RecentTextWindow(), asyncio.Queue())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        pin = dict(self.storage.open_pins()[0])
        self.assertEqual((pin["window_start"], pin["price_before"]), (1000, 100))
        with self.storage._connect() as conn:
            self.assertEqual(conn.execute("SELECT ingested_at FROM headlines").fetchone()[0], 1000)

    async def test_fixed_endpoint_and_signed_alerts(self):
        for after in (90.0, 110.0):
            with self.subTest(after=after):
                self.tracker = PriceTracker()
                self.engine = PinEngine(self.storage, self.tracker)
                self.tracker.on_trade("QQQ", 100, 10, 900)
                self.tracker.on_trade("QQQ", 100, 10, 999)
                self.tracker.on_trade("QQQ", after, 1000, 1299)
                # Late scheduling/scoring must not pull this unrelated tick into the window.
                self.tracker.on_trade("QQQ", 200, 100000, 1400)
                pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ",
                                                  window_start=1000, price_before=100)
                with patch("time.time", return_value=1500):
                    await self.engine._resolve_pin(pin_id, "QQQ", 100, 1000)
                row = self.row(pin_id)
                expected = after - 100
                self.assertEqual((row["window_end"], row["price_after"], row["pct_move"]),
                                 (1300, after, expected))
                self.assertEqual(row["confirmed"], 1)
                self.assertAlmostEqual(row["volume_ratio"], (1000 / 300) / (20 / 100))
                embed = build_pin_embed(symbol="QQQ", headline="fixture", url="",
                                        impact_score=9, reasoning="", pct_move=row["pct_move"],
                                        volume_ratio=row["volume_ratio"], confirmed=True)
                self.assertIn(f"{expected:+.2f}%", embed.title)

    async def test_restart_closes_expired_and_pending_rows_without_fake_results(self):
        self.storage.insert_headline(source="fixture", external_id="restart", symbols=["QQQ"],
                                     headline="restart fixture", published_at=1)
        valid = self.storage.create_pin(headline_id=1, symbol="QQQ", window_start=1, price_before=100)
        self.storage.resolve_pin(valid, price_after=110, pct_move=10, volume_ratio=3, confirmed=True)
        ids = [self.storage.create_pin(headline_id=1, symbol="QQQ", window_start=start,
                                       price_before=100) for start in (1000, 1490)]
        # Reopen the actual on-disk database with a fresh, empty price tracker.
        self.storage = Storage(self.path)
        engine = PinEngine(self.storage, PriceTracker())
        with patch("time.time", return_value=1500):
            self.assertEqual(engine.recover_open_pins(), 2)
            self.assertEqual(engine.recover_open_pins(), 0)
        for pin_id in ids:
            row = self.row(pin_id)
            self.assertEqual(row["incomplete_reason"], "restart_lost_price_history")
            self.assertIsNotNone(row["window_end"])
            self.assertIsNone(row["price_after"])
            self.assertIsNone(row["pct_move"])
            self.assertEqual(row["confirmed"], 0)
        self.assertEqual(self.storage.accuracy_stats(),
                         {"total_pins": 1, "confirmed_pins": 1, "hit_rate": 1.0})
        self.assertEqual([r["id"] for r in self.storage.unposted_confirmed_pins()], [valid])
        self.assertEqual(self.storage.open_pins(), [])
        # A stale resolver must not overwrite the explicit incomplete outcome.
        self.storage.resolve_pin(ids[0], price_after=200, pct_move=100, volume_ratio=3, confirmed=True)
        self.assertIsNone(self.row(ids[0])["price_after"])

    async def test_missing_window_history_is_incomplete(self):
        for history in ([], [(1400, 120)], [(900, 100), (999, 100)]):
            with self.subTest(history=history):
                tracker = PriceTracker()
                for ts, price in history:
                    tracker.on_trade("QQQ", price, 10, ts)
                pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ",
                                                  window_start=1000, price_before=100)
                with patch("time.time", return_value=1500):
                    await PinEngine(self.storage, tracker)._resolve_pin(pin_id, "QQQ", 100, 1000)
                self.assertEqual(self.row(pin_id)["incomplete_reason"], "missing_window_price_history")
        self.assertEqual(self.storage.accuracy_stats()["total_pins"], 0)

    async def test_missing_baseline_volume_is_incomplete(self):
        self.tracker.on_trade("QQQ", 100, 10, 1000)
        self.tracker.on_trade("QQQ", 110, 1000, 1299)
        pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ",
                                          window_start=1000, price_before=100)
        with patch("time.time", return_value=1500):
            await self.engine._resolve_pin(pin_id, "QQQ", 100, 1000)
        self.assertEqual(self.row(pin_id)["incomplete_reason"], "missing_baseline_volume")
        self.assertIsNone(self.row(pin_id)["pct_move"])
        self.assertEqual(self.storage.accuracy_stats()["total_pins"], 0)

    async def test_old_database_migration_preserves_completed_records(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA.replace("    incomplete_reason TEXT,\n", ""))
            conn.execute("""INSERT INTO pins
                (headline_id, symbol, window_start, window_end, price_before, price_after,
                 pct_move, volume_ratio, confirmed, created_at)
                VALUES (1, 'QQQ', 1000, 1300, 100, 110, 10, 3, 1, 1000)""")
        storage = Storage(path)
        Storage(path)  # Migration is repeatable.
        self.assertEqual(storage.accuracy_stats()["hit_rate"], 1.0)
        with storage._connect() as conn:
            row = dict(conn.execute("SELECT * FROM pins").fetchone())
        self.assertEqual(row["price_after"], 110)
        self.assertIsNone(row["incomplete_reason"])


if __name__ == "__main__":
    unittest.main()
