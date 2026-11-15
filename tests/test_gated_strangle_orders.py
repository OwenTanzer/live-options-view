"""Regression checks for paper strangle entry bounds."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "crassus"))
from crassus.client import Book, ExecutionClient
from crassus.strategies.gated_strangle import decide, fresh_spot


class GatedStrangleOrders(unittest.TestCase):
    def test_recover_buy_with_original_limit_after_interruption(self):
        now = datetime.now(timezone.utc)
        call = "QQQ261116C00652000"
        book = Book([dict(sym=call, side="buy", qty=1, price=1, ts=now.isoformat())])
        quote = SimpleNamespace(is_executable=True, age_seconds=1, bid=1.44, ask=1.45)
        ctx = SimpleNamespace(now_et=now, book=book, session_phase="open",
                              quotes=lambda symbols: {symbols[0]: quote})
        order = decide(ctx)
        self.assertEqual((order.action, order.limit_price), ("buy", 1.45))
        session = Mock()
        session.account.alias = "test"
        session._mutation_lock = Lock()
        session.me.return_value.trades = []
        response = Mock(status_code=409)
        response.json.return_value = {"error": "current ask exceeds limit"}
        session._request.side_effect = [KeyboardInterrupt, response]
        with TemporaryDirectory() as directory:
            client = ExecutionClient(session, Path(directory))
            with self.assertRaises(KeyboardInterrupt):
                client.submit(symbol=order.symbol, side=order.action, quantity=1,
                              limit_price=order.limit_price)
            result = ExecutionClient(session, Path(directory)).recover_pending()
        self.assertEqual(result.http_status, 409)
        original, replay = (call.kwargs["json"] for call in session._request.call_args_list)
        self.assertEqual(original, replay)
        self.assertEqual((replay["order_type"], replay["limit_price"]), ("limit", 1.45))

    def test_old_or_future_spot_blocks_new_pair(self):
        now = datetime.now(timezone.utc)
        market = SimpleNamespace(freshness="live", spot_ts=now.isoformat())
        snapshot = SimpleNamespace(timestamp=now.isoformat(), underlying_market=market)
        self.assertTrue(fresh_spot(snapshot, now))
        for delta in (-3600, 60):
            market.spot_ts = (now + timedelta(seconds=delta)).isoformat()
            self.assertFalse(fresh_spot(snapshot, now))
