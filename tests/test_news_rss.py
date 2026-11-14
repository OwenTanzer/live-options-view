import threading
import time
import unittest

from test_news_support import FIXTURES, fixed, no_sleep
from market_data.news import config
from market_data.news.adapters.rss import FetchError, FetchResponse, fetch_all, fetch_source, parse_feed

PARSER = FIXTURES / "parser"
SRC = config.SOURCES["techcrunch"]
CLOCK = fixed("2026-11-09T10:05:00Z")


class Parsing(unittest.TestCase):
    def test_atom_entry(self):
        [e] = parse_feed((PARSER / "atom.xml").read_bytes())
        self.assertEqual(e["link"], "https://example.com/Atom/Entry-One?ref=Feed&utm_campaign=x")
        self.assertEqual(e["guid"], "urn:fixture:atom:1")
        self.assertEqual((e["published_raw"], e["updated_raw"]), ("2026-11-09T09:00:00+01:00", "2026-11-09T09:30:00+01:00"))
        self.assertEqual(e["outlet"], "Syndicated Outlet")
        self.assertEqual(e["categories"], ["Energy"])

    def test_rss1_rdf_items_and_dublin_core_date(self):
        [e] = parse_feed((PARSER / "rdf.xml").read_bytes())
        self.assertEqual(e["title"], "Synthetic RDF item")
        self.assertEqual(e["published_raw"], "2026-11-09T08:00:00+09:00")
        self.assertEqual(e["categories"], ["Diplomacy"])

    def test_rss2_raw_times_survive_unmodified(self):
        entries = parse_feed((FIXTURES / "feeds" / "oilprice_com.xml").read_bytes())
        self.assertEqual([e["published_raw"] for e in entries],
                         ["Mon, 09 Nov 2026 09:40:00 GMT", "Mon, 09 Nov 2026 09:30:00", None])

    def test_malformed_and_entity_feeds_rejected(self):
        for name in ("malformed.xml", "entities.xml"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_feed((PARSER / name).read_bytes())


class Fetching(unittest.TestCase):
    def scripted(self, *responses):
        calls = []

        def fetch(url, timeout):
            calls.append(timeout)
            r = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(r, Exception):
                raise r
            return r
        return fetch, calls

    OK = FetchResponse(200, (FIXTURES / "feeds" / "techcrunch.xml").read_bytes())

    def test_retry_then_success_uses_contract_timeout_and_backoff(self):
        fetch, calls = self.scripted(FetchResponse(429, b""), self.OK)
        slept = []
        r = fetch_source(SRC, fetch, CLOCK, slept.append)
        self.assertEqual((r.status, r.attempts, len(r.entries)), ("ok", 2, 3))
        self.assertEqual(calls, [20, 20])
        self.assertEqual(slept, [2.0])
        self.assertEqual(r.observed_at, "2026-11-09T10:05:00Z")

    def test_at_most_two_retries(self):
        fetch, calls = self.scripted(FetchError("timed out"))
        slept = []
        r = fetch_source(SRC, fetch, CLOCK, slept.append)
        self.assertEqual((r.status, r.attempts, slept), ("fetch_error", 3, [2.0, 4.0]))

    def test_client_error_and_parse_error_not_retried(self):
        fetch, _ = self.scripted(FetchResponse(404, b""))
        self.assertEqual(fetch_source(SRC, fetch, CLOCK, no_sleep).attempts, 1)
        fetch, _ = self.scripted(FetchResponse(200, b"<html>not a feed"))
        r = fetch_source(SRC, fetch, CLOCK, no_sleep)
        self.assertEqual((r.status, r.attempts), ("parse_error", 1))

    def test_zero_entries_is_success_not_failure(self):
        fetch, _ = self.scripted(FetchResponse(200, b'<rss version="2.0"><channel><title>x</title></channel></rss>'))
        r = fetch_source(SRC, fetch, CLOCK, no_sleep)
        self.assertEqual((r.status, r.entries), ("ok", []))

    def test_concurrency_bounded_to_four(self):
        active, peak, lock = [0], [0], threading.Lock()

        def fetch(url, timeout):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return self.OK
        sources = list(config.SOURCES.values())[:10]
        results = fetch_all(sources, fetch, CLOCK, no_sleep)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(r.status == "ok" for r in results.values()))
        self.assertLessEqual(peak[0], 4)
        self.assertGreater(peak[0], 1)

    def test_cycle_budget_records_unfinished_sources(self):
        release = threading.Event()
        slow = config.SOURCES["ars_technica"]

        def fetch(url, timeout):
            if url == slow.url:
                release.wait(5)
            return self.OK
        try:
            results = fetch_all([SRC, slow], fetch, CLOCK, no_sleep, budget_s=0.3)
        finally:
            release.set()
        self.assertEqual(results[SRC.id].status, "ok")
        self.assertEqual(results[slow.id].status, "budget_exceeded")
        self.assertEqual(results[slow.id].entries, [])


if __name__ == "__main__":
    unittest.main()
