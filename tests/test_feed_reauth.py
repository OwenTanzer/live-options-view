"""DXLink streamer-token expiry must lead to re-authentication (E1).

Replays the provider frames recorded in the collector's Railway logs on
2026-11-13 (deployment db1eefd8): the price lane's streamer token expired at
00:23:29Z, the server closed the socket, and websocket-client's
``run_forever(reconnect=5)`` redialled every ~5 s *without* calling
``on_close``. Before the repair ``needs_reauth()`` stayed False forever
(11,579 reconnects by 17:35Z) and the lane kept publishing pre-expiry prices.

No credentials, sockets or provider calls: the real DXLinkFeed handlers are
driven directly with a fake websocket.
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_data.live_market as entry
from market_data.live_market import price_lane, price_sessions
from market_data.live_market.feed import DXLinkFeed

UTC = timezone.utc

EXPIRED = json.dumps({"type": "ERROR", "channel": 0, "error": "UNAUTHORIZED",
                      "message": "Your authentication token has expired, reauthentication is required"})
AUTH_TIMEOUT = json.dumps({"type": "ERROR", "channel": 0, "error": "TIMEOUT",
                           "message": "The timeout for AUTH has been reached. The message was last received 30 seconds ago."})
REJECTED = json.dumps({"type": "ERROR", "channel": 0, "error": "UNAUTHORIZED",
                       "message": "Authentication failed"})


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = 0

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed += 1


def make_feed(token="stream-1"):
    feed = DXLinkFeed("wss://dxlink.test", token)
    feed._ws = FakeWS()
    feed._thread = None
    return feed


def authorize(feed):
    """One healthy connection: open -> SETUP -> AUTHORIZED -> channel -> data."""
    feed._on_open(feed._ws)
    feed._on_message(feed._ws, json.dumps({"type": "SETUP", "channel": 0}))
    feed._on_message(feed._ws, json.dumps({"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"}))
    feed._on_message(feed._ws, json.dumps({"type": "CHANNEL_OPENED", "channel": 1}))


def silent_redial(feed):
    """What run_forever(reconnect=5) does: no _on_close, straight to _on_open."""
    feed._on_open(feed._ws)
    feed._on_message(feed._ws, json.dumps({"type": "SETUP", "channel": 0}))
    feed._on_message(feed._ws, REJECTED)


class TokenExpiry(unittest.TestCase):
    def test_recorded_expiry_requests_reauth_immediately(self):
        feed = make_feed()
        authorize(feed)
        self.assertFalse(feed.needs_reauth())
        feed._on_message(feed._ws, EXPIRED)
        self.assertTrue(feed.needs_reauth(), "an explicit expiry needs a new streamer token now")
        health = feed.get_health()
        self.assertFalse(health["authorized"])
        self.assertIn("expired", health["last_error"])

    def test_silent_redial_loop_reaches_reauth_without_on_close(self):
        feed = make_feed()
        authorize(feed)
        for _ in range(2):
            silent_redial(feed)
            self.assertFalse(feed.needs_reauth(), "a transient rejection is not yet a bad token")
        silent_redial(feed)
        self.assertTrue(feed.needs_reauth(),
                        "three rejected AUTHs on redial must trip reauth even though _on_close never ran")

    def test_full_recorded_sequence(self):
        feed = make_feed()
        authorize(feed)
        feed._on_message(feed._ws, EXPIRED)
        feed._on_message(feed._ws, AUTH_TIMEOUT)
        for _ in range(5):
            silent_redial(feed)
        self.assertTrue(feed.needs_reauth())
        self.assertEqual(feed.get_health()["reconnect_count"], 5)
        self.assertFalse(feed.get_health()["authorized"])

    def test_auth_timeout_alone_is_not_an_auth_rejection(self):
        feed = make_feed()
        authorize(feed)
        feed._on_message(feed._ws, AUTH_TIMEOUT)
        self.assertFalse(feed.needs_reauth())
        self.assertTrue(feed.get_health()["authorized"])

    def test_redial_clears_the_previous_login_in_health(self):
        feed = make_feed()
        authorize(feed)
        self.assertTrue(feed.get_health()["authorized"])
        feed._on_open(feed._ws)  # silent redial, nothing authorized yet
        health = feed.get_health()
        self.assertTrue(health["connected"])
        self.assertFalse(health["authorized"])
        self.assertFalse(health["channel_open"])

    def test_update_token_resets_and_next_redial_uses_it(self):
        feed = make_feed("old")
        authorize(feed)
        feed._on_message(feed._ws, EXPIRED)
        feed.update_token("new")
        self.assertFalse(feed.needs_reauth())
        feed._ws.sent.clear()
        feed._on_open(feed._ws)
        feed._on_message(feed._ws, json.dumps({"type": "SETUP", "channel": 0}))
        auth = [m for m in feed._ws.sent if m["type"] == "AUTH"]
        self.assertEqual(auth[-1]["token"], "new")

    def test_timesale_channel_error_is_not_an_auth_failure(self):
        feed = make_feed()
        authorize(feed)
        feed._on_message(feed._ws, json.dumps({"type": "ERROR", "channel": 3,
                                               "error": "UNAUTHORIZED", "message": "x"}))
        self.assertFalse(feed.needs_reauth())
        self.assertTrue(feed.get_health()["authorized"])

    def test_stopped_feed_never_authenticates_a_late_redial(self):
        feed = make_feed()
        authorize(feed)
        feed.stop()
        feed._ws.sent.clear()
        before = feed._ws.closed
        feed._on_open(feed._ws)
        self.assertEqual(feed._ws.sent, [], "no SETUP/AUTH after stop()")
        self.assertGreater(feed._ws.closed, before)


class LaneRecoversFromExpiry(unittest.TestCase):
    """The lane must obtain a new streamer token instead of redialling forever."""

    def setUp(self):
        self.tokens = iter(["stream-1", "stream-2", "stream-3"])
        self.auth_calls = 0
        self.feeds = []
        self.published = 0

        def fake_auth(_manager):
            self.auth_calls += 1
            return {"access_token": "a", "streamer_token": next(self.tokens),
                    "streamer_url": "wss://dxlink.test"}

        def factory(url, token):
            feed = DXLinkFeed(url, token)
            feed._ws = FakeWS()
            feed._thread = None
            feed.start = lambda: None
            feed.wait_ready = lambda timeout=60.0: True
            feed.restart_if_dead = lambda: None
            self.feeds.append(feed)
            return feed

        def fake_publish(*args, **kwargs):
            self.published += 1

        calendar = price_sessions.build_equity_calendar
        self.lane = price_lane.PriceLane(
            object(), None, None, writer_id="test-writer",
            clock=lambda: datetime(2026, 11, 13, 0, 25, tzinfo=UTC),
            sleep=lambda secs: None, auth=fake_auth, feed_factory=factory,
            resolve=lambda token, force=False: {}, publish=fake_publish,
            calendar_builder=lambda now: None)

    def test_expired_token_triggers_one_new_auth_and_a_new_feed(self):
        self.lane.cycle()
        self.assertEqual(self.auth_calls, 1)
        first = self.feeds[0]
        authorize(first)
        first._on_message(first._ws, EXPIRED)
        for _ in range(3):
            silent_redial(first)
        self.lane.cycle()
        self.assertEqual(self.auth_calls, 2, "expiry must fetch a new streamer token")
        self.assertEqual(len(self.feeds), 2)
        self.assertEqual(self.feeds[1]._token, "stream-2")
        self.assertTrue(first._stopped, "the expired connection is stopped, not left redialling")
        self.assertEqual(self.lane.health()["state"], "streaming")


if __name__ == "__main__":
    unittest.main()
