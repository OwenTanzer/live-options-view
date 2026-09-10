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
from config import settings
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

    async def test_ingest_never_blocks_on_scoring_and_anchor_is_preserved(self):
        """MOO-170 finding 1: ingest_loop parses/persists every headline off
        the source regardless of scorer speed (proved here by fully
        draining a two-item burst through ingest_loop before score_loop
        ever runs), and a slow scorer still resolves the pin against the
        headline's own ingest time, not a later tick that arrived during
        the delay."""
        clock = [1000.0]
        self.tracker.on_trade("QQQ", 100, 10, 999)

        async def source():
            yield Headline("fixture", "1", ["QQQ"], "QQQ news", "", "", 1000, 1000)
            yield Headline("fixture", "2", ["QQQ"], "Totally unrelated MSFT antitrust ruling headline", "", "", 1001, 1001)

        async def delayed_score(*args):
            clock[0] = 1012.0
            self.tracker.on_trade("QQQ", 110, 100, 1005)
            return 9.0, "fixture", "fixture"

        score_queue: asyncio.Queue = asyncio.Queue()
        with patch("time.time", side_effect=lambda: clock[0]), \
             patch.object(app, "score_headline", delayed_score), \
             patch.object(self.engine, "_resolve_pin", new_callable=AsyncMock):
            await asyncio.wait_for(
                app.ingest_loop(source(), self.storage, RecentTextWindow(), score_queue), timeout=1.0,
            )
            # Both headlines are already parsed and durably ingested even
            # though the (slow) scorer hasn't run for either yet.
            self.assertEqual(score_queue.qsize(), 2)
            with self.storage._connect() as conn:
                self.assertEqual(
                    [r[0] for r in conn.execute("SELECT ingested_at FROM headlines ORDER BY id").fetchall()],
                    [1000, 1001],
                )

            score_task = asyncio.create_task(app.score_loop(self.storage, self.engine, score_queue))
            for _ in range(5):
                await asyncio.sleep(0)
            score_task.cancel()
            try:
                await score_task
            except asyncio.CancelledError:
                pass

        with self.storage._connect() as conn:
            pin = dict(conn.execute("SELECT * FROM pins WHERE headline_id = 1").fetchone())
        self.assertEqual((pin["window_start"], pin["price_before"]), (1000, 100))

    async def test_fixed_endpoint_and_signed_alerts(self):
        for after in (90.0, 110.0):
            with self.subTest(after=after):
                self.tracker = PriceTracker()
                self.engine = PinEngine(self.storage, self.tracker)
                # Adequate baseline coverage (>= VOLUME_MIN_COVERAGE_FRACTION
                # of the 1800s window, >= VOLUME_MIN_TRADE_COUNT trades) so
                # baseline_volume_rate doesn't report "insufficient" -- see
                # test_missing_baseline_volume_is_incomplete for that path.
                for i in range(20):
                    self.tracker.on_trade("QQQ", 100, 10, -700.0 + i * 85.0)
                self.tracker.on_trade("QQQ", 100, 10, 999)
                self.tracker.on_trade("QQQ", after, 1000, 1299)
                # Late scheduling/scoring must not pull this unrelated tick into the window.
                self.tracker.on_trade("QQQ", 200, 100000, 1400)
                expected_baseline_rate = self.tracker.state("QQQ").baseline_volume_rate(1800.0, 1000.0)
                pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ",
                                                  window_start=1000, price_before=100)
                with patch("time.time", return_value=1500):
                    await self.engine._resolve_pin(pin_id, "QQQ", 100, 1000)
                row = self.row(pin_id)
                expected = after - 100
                self.assertEqual((row["window_end"], row["price_after"], row["pct_move"]),
                                 (1300, after, expected))
                self.assertEqual(row["confirmed"], 1)
                self.assertIsNotNone(expected_baseline_rate)
                self.assertAlmostEqual(row["volume_ratio"], (1000 / 300) / expected_baseline_rate)
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

    async def test_classification_distinguishes_already_moving_from_subsequent(self):
        """MOO-170 finding 2: a pin whose price was already trending before
        the headline is classified differently from one that only moved
        after it, even when both confirm on the post-window threshold."""
        for baseline_direction, expected in ((1.0, "already_moving_before"), (0.0, "subsequent_move")):
            with self.subTest(expected=expected):
                tracker = PriceTracker()
                engine = PinEngine(self.storage, tracker)
                window_start = 10_000.0
                # Chronological insertion order matters: SymbolState.trades
                # is an append-only deque, and baseline_volume_rate/
                # return_over_interval both assume trades[0] is the oldest.
                # 17 trades older than PIN_BASELINE_LOOKBACK_SECS (300s
                # before window_start, i.e. ts <= 9700) establish coverage;
                # the last 3 sit inside that lookback window and carry the
                # "already moving" price when baseline_direction is set.
                older_price = 95.0 if baseline_direction else 100.0
                for i in range(20):
                    ts = window_start - 1700.0 + i * 85.0
                    price = older_price if ts <= window_start - settings.PIN_BASELINE_LOOKBACK_SECS else 100.0
                    tracker.on_trade("QQQ", price, 10, ts)
                tracker.on_trade("QQQ", 100.0, 10, window_start)
                tracker.on_trade("QQQ", 110.0, 1000, window_start + 300.0)
                pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ",
                                                  window_start=window_start, price_before=100.0)
                with patch("time.time", return_value=window_start + 500.0):
                    await engine._resolve_pin(pin_id, "QQQ", 100.0, window_start)
                self.assertEqual(self.row(pin_id)["classification"], expected)

    async def test_shadow_pin_is_observed_but_never_posted(self):
        """MOO-170 finding 6: a below-threshold headline still gets a
        measured observation (for the evaluation denominator) but is
        excluded from both accuracy_stats() and the Discord posting query."""
        for i in range(20):
            self.tracker.on_trade("QQQ", 100.0, 10, -700.0 + i * 85.0)
        self.tracker.on_trade("QQQ", 100.0, 10, 999.0)
        self.tracker.on_trade("QQQ", 110.0, 1000, 1299.0)
        await self.engine.open_pin(headline_id=1, symbol="QQQ", window_start=1000.0, shadow=True)
        with patch("time.time", return_value=1500.0):
            await asyncio.sleep(0)
        with self.storage._connect() as conn:
            pin = dict(conn.execute("SELECT * FROM pins WHERE headline_id = 1").fetchone())
        self.assertEqual(pin["shadow"], 1)
        self.assertEqual(self.storage.accuracy_stats()["total_pins"], 0)
        self.assertEqual(self.storage.unposted_confirmed_pins(), [])
        report = self.storage.evaluation_report()
        self.assertEqual(report["shadow_total"], 1)

    async def test_replay_price_observations_are_persisted(self):
        """MOO-170 acceptance: interval returns must be replayable from
        stored evidence, not only from the in-memory ring buffer."""
        for i in range(20):
            self.tracker.on_trade("QQQ", 100.0, 10, -700.0 + i * 85.0)
        self.tracker.on_trade("QQQ", 100.0, 10, 999.0)
        self.tracker.on_trade("QQQ", 110.0, 1000, 1299.0)
        pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ", window_start=1000.0, price_before=100.0)
        with patch("time.time", return_value=1500.0):
            await self.engine._resolve_pin(pin_id, "QQQ", 100.0, 1000.0)
        samples = self.storage.price_observations_for("pin", pin_id)
        self.assertTrue(len(samples) >= 1)
        self.assertEqual(samples[-1]["price"], 110.0)

    async def test_control_window_is_skipped_near_a_headline_and_recorded_otherwise(self):
        """MOO-170 finding 6: a same-time-of-day no-news control is recorded
        when the shifted window is clean, and explicitly left NULL (not
        zero-filled) when a headline is too close to serve as a control."""
        window_start = 100_000.0
        control_start = window_start - settings.CONTROL_LOOKBACK_SECS

        # Chronological insertion order matters (trade_at_or_before assumes
        # an ascending-ts deque): the control window sits ~a day before
        # window_start, so it's inserted first.
        self.tracker.on_trade("QQQ", 50.0, 10, control_start - 1.0)
        self.tracker.on_trade("QQQ", 51.0, 10, control_start + 300.0)
        for i in range(20):
            self.tracker.on_trade("QQQ", 100.0, 10, window_start - 1700.0 + i * 85.0)
        self.tracker.on_trade("QQQ", 100.0, 10, window_start - 1.0)
        self.tracker.on_trade("QQQ", 110.0, 1000, window_start + 300.0)

        pin_id = self.storage.create_pin(headline_id=1, symbol="QQQ", window_start=window_start, price_before=100.0)
        with patch("time.time", return_value=window_start + 500.0):
            await self.engine._resolve_pin(pin_id, "QQQ", 100.0, window_start)
        with self.storage._connect() as conn:
            row = dict(conn.execute("SELECT control_pct_move FROM pins WHERE id = ?", (pin_id,)).fetchone())
        self.assertAlmostEqual(row["control_pct_move"], 2.0)

        # Now repeat with a headline sitting right on top of the shifted
        # control window -- it must be skipped (left NULL), not silently
        # treated as a zero move.
        self.storage.insert_headline(source="fixture", external_id="near-control", symbols=["QQQ"],
                                      headline="a headline near the control window",
                                      published_at=control_start, ingested_at=control_start)
        pin_id2 = self.storage.create_pin(headline_id=1, symbol="QQQ", window_start=window_start, price_before=100.0)
        with patch("time.time", return_value=window_start + 500.0):
            await self.engine._resolve_pin(pin_id2, "QQQ", 100.0, window_start)
        with self.storage._connect() as conn:
            row2 = dict(conn.execute("SELECT control_pct_move FROM pins WHERE id = ?", (pin_id2,)).fetchone())
        self.assertIsNone(row2["control_pct_move"])

    async def test_old_database_migration_preserves_completed_records(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        try:
            with conn:
                conn.executescript(SCHEMA.replace("    incomplete_reason TEXT,\n", ""))
                conn.execute("""INSERT INTO pins
                    (headline_id, symbol, window_start, window_end, price_before, price_after,
                     pct_move, volume_ratio, confirmed, created_at)
                    VALUES (1, 'QQQ', 1000, 1300, 100, 110, 10, 3, 1, 1000)""")
        finally:
            conn.close()  # `with conn:` only manages the transaction, not the connection -- an
            # unclosed handle keeps the file locked on Windows through the tempdir cleanup below.
        storage = Storage(path)
        Storage(path)  # Migration is repeatable.
        self.assertEqual(storage.accuracy_stats()["hit_rate"], 1.0)
        with storage._connect() as conn:
            row = dict(conn.execute("SELECT * FROM pins").fetchone())
        self.assertEqual(row["price_after"], 110)
        self.assertIsNone(row["incomplete_reason"])


if __name__ == "__main__":
    unittest.main()
