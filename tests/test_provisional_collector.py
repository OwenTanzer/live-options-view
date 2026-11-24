import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from market_data.live_market.provisional import RootStore, RootQuotes


class Boundaries(unittest.TestCase):
    def test_root_storage_accepts_only_new_owned_trees(self):
        client = Mock()
        store = RootStore(client)
        for key in ("intraday/slv/latest.json", "intraday/dram/20261123/first.csv",
                    "settlement/slv/2026-11-23.json"):
            store.put_object(Bucket="fixture", Key=key, Body=b"fixture")
        self.assertEqual(client.put_object.call_count, 3)
        for key in ("intraday/latest.json", "intraday/prices.json", "intraday/health.json",
                    "intraday/ibit/latest.json", "baselines/slv_rvol_buckets.json",
                    "one-clock/x", "raw/weekly/x", "intraday/slv/../qqq/x"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                store.put_object(Bucket="fixture", Key=key, Body=b"fixture")
        self.assertEqual(client.put_object.call_count, 3)

    def test_reads_cannot_scan_other_owners(self):
        store = RootStore(Mock())
        for operation, field in ((store.get_object, "Key"), (store.list_objects_v2, "Prefix")):
            with self.assertRaises(ValueError):
                operation(**{field: "intraday/"})

    def test_quote_requests_cannot_expand_the_subscription_budget(self):
        registry = RootQuotes()
        for symbol in ("QQQ261123C00600000", "SLV261123C00030000", "DRAM261123C00040000"):
            result = registry.quote_payload([symbol])
            self.assertEqual(result["quotes"], [])
            self.assertIn(symbol, result["unavailable"])
        self.assertEqual(registry._requested_contracts, set())

    def test_explicit_session_date_precedes_provider_access(self):
        from market_data.live_market import provisional
        with patch.dict("os.environ", {"PROVISIONAL_SESSION_DATE": "2000-01-01"}), \
                patch.object(provisional, "tasty_auth") as auth:
            with self.assertRaises(RuntimeError):
                provisional.main()
            auth.assert_not_called()

    def test_one_session_lifecycle_uses_only_two_roots_and_stops(self):
        from market_data.live_market import provisional as p
        today = p.datetime.now(p.ET).date()
        feed, server, store = Mock(), Mock(), Mock()
        feed.wait_ready.return_value = True
        feed.needs_reauth.return_value = False
        feed.get_state.return_value = {r: {} for r in p.ROOTS}
        feed.get_subscription_delivery.return_value = {"pending_event_pairs": []}
        def chain(token, day, root, weekly):
            self.assertFalse(weekly)
            return ([{"strike": 30, "call_occ": root + "261127C00030000",
                      "put_occ": root + "261127P00030000", "call_sym": "." + root + "C30",
                      "put_sym": "." + root + "P30"}], "2026-11-27", [], "")
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"PROVISIONAL_SESSION_DATE": today.isoformat(),
                "LIVE_QUOTE_KEY": "fixture", "COLLECT_SLV": "1", "COLLECT_DRAM": "1"}))
            stack.enter_context(patch.object(p, "past_stop", side_effect=[False, False, True]))
            stack.enter_context(patch("market_data.live_market.tiers._load_calendar", return_value={today}))
            for name, value in {"tasty_auth": {"access_token": "fixture", "streamer_url": "fixture", "streamer_token": "fixture"},
                                "DXLinkFeed": feed, "start_live_quote_server": server, "make_s3": store,
                                "_fresh_spy_spot": (30, "2026-11-23T16:00:00Z")}.items():
                stack.enter_context(patch.object(p, name, return_value=value))
            stack.enter_context(patch.object(p.OAuthTokenManager, "from_env"))
            stack.enter_context(patch.object(p, "load_chain", side_effect=chain))
            stack.enter_context(patch.object(p, "restore_state"))
            stack.enter_context(patch.object(p.time, "sleep"))
            snapshot = stack.enter_context(patch.object(p, "take_snapshot"))
            p.main()
        feed.set_subscriptions.assert_called_once_with([], ["SLV", "DRAM"])
        self.assertEqual(snapshot.call_count, 2)
        self.assertTrue(all(isinstance(c.args[0], p.RootStore) for c in snapshot.call_args_list))
        self.assertEqual(len(feed.add_option_subscriptions.call_args.args[0]), 4)
        feed.stop.assert_called_once()
        server.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
