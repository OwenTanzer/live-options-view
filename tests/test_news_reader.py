"""The two decisive as-of fixtures from the build contract (synthetic data)."""
import tempfile
import unittest
from pathlib import Path

from test_news_support import fixed, feeds, no_sleep
from market_data.news.archive import LocalObjectStore, collect_once, enrich_pending
from market_data.news.reader import as_of, load_archive


class FixtureA(unittest.TestCase):
    """Published 10:00, collected 10:05, archived 10:06, revised (collected) 10:10."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        store = LocalObjectStore(root / "store")
        for version, observed, committed in (("a_v1", "10:05:00", "10:06:00"), ("a_v2", "10:10:00", "10:10:30")):
            collect_once(store, root / "spool", feeds(f"scenarios/{version}"), fixed(f"2026-11-09T{observed}Z"),
                         commit_clock=fixed(f"2026-11-09T{committed}Z"), only_sources=["oilprice_com"],
                         sleep=no_sleep)
        cls.archive = load_archive(store)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def view(self, cutoff, mode="collector"):
        return as_of(self.archive, f"2026-11-09T{cutoff}Z", mode)["items"]

    def test_collector_as_of_1003_excludes_it(self):
        self.assertEqual(self.view("10:03:00"), [])

    def test_collector_as_of_1007_shows_only_the_original(self):
        [item] = self.view("10:07:00")
        self.assertEqual(item["title"], "Tanker attacked near the Strait of Hormuz")
        self.assertEqual(item["revision_count"], 1)
        self.assertEqual((item["published_at"], item["first_seen_at"], item["archived_at"]),
                         ("2026-11-09T10:00:00Z", "2026-11-09T10:05:00Z", "2026-11-09T10:06:00Z"))

    def test_collector_as_of_1011_shows_the_revision_and_keeps_history(self):
        [item] = self.view("10:11:00")
        self.assertEqual(item["title"], "Tanker attacked near the Strait of Hormuz; crew evacuated")
        self.assertEqual([r["title"] for r in item["revisions"]],
                         ["Tanker attacked near the Strait of Hormuz",
                          "Tanker attacked near the Strait of Hormuz; crew evacuated"])
        self.assertEqual(item["first_seen_at"], "2026-11-09T10:05:00Z")

    def test_archive_as_of_10_05_30_still_excludes_it(self):
        self.assertEqual(self.view("10:05:30", "archive"), [])
        self.assertEqual(len(self.view("10:05:30", "collector")), 1)  # observed, not yet durable
        [item] = self.view("10:06:00", "archive")
        self.assertEqual(item["revision_count"], 1)

    def test_publication_view_is_labeled_hindsight(self):
        view = as_of(self.archive, "2026-11-09T10:03:00Z", "publication")
        self.assertTrue(view["hindsight"])
        self.assertIn("RETROSPECTIVE", view["label"])
        [item] = view["items"]
        self.assertIn("crew evacuated", item["title"])  # uses knowledge from after the cutoff, by design


class FixtureB(unittest.TestCase):
    """One EIA item in both streams; its classification is added at 10:12."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        store = LocalObjectStore(root / "store")

        def unavailable(*_a, **_k):
            raise RuntimeError("enrichment unavailable")
        collect_once(store, root / "spool", feeds("scenarios/b"), fixed("2026-11-09T10:05:00Z"),
                     commit_clock=fixed("2026-11-09T10:06:00Z"), only_sources=["eia_today_in_energy"],
                     classifier=unavailable, sleep=no_sleep)
        enrich_pending(store, root / "spool", fixed("2026-11-09T10:12:00Z"))
        cls.archive = load_archive(store)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_single_identity_in_both_streams(self):
        self.assertEqual(len(self.archive.observations), 1)
        [cls] = self.archive.classifications
        self.assertEqual(cls["stream_memberships"], ["ai_buildout", "oil"])
        self.assertEqual(cls["input_revision_id"], self.archive.observations[0]["revision_id"])
        self.assertEqual(cls["instrument_mapping"], "unknown")
        oil = as_of(self.archive, "2026-11-09T10:13:00Z", stream="oil")["items"]
        ai = as_of(self.archive, "2026-11-09T10:13:00Z", stream="ai_buildout")["items"]
        self.assertEqual([i["item_id"] for i in oil], [i["item_id"] for i in ai])

    def test_classification_made_at_1012_is_absent_at_1011(self):
        [item] = as_of(self.archive, "2026-11-09T10:11:00Z")["items"]
        self.assertIsNone(item["classification"])
        view = as_of(self.archive, "2026-11-09T10:11:00Z", stream="oil")
        self.assertEqual((view["items"], view["excluded"]["unclassified_for_stream"]), ([], 1))
        [later] = as_of(self.archive, "2026-11-09T10:13:00Z")["items"]
        self.assertEqual(later["classification"]["classified_at"], "2026-11-09T10:12:00Z")


if __name__ == "__main__":
    unittest.main()
