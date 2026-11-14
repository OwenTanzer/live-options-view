import unittest

import test_news_support  # noqa: F401  (sys.path)
from market_data.news import config
from market_data.news.normalize import (canonical_url, item_identity, normalize_entry, parse_source_time,
                                        revision_id)

SRC = config.SOURCES["oilprice_com"]


def entry(**kw):
    base = {"guid": "g-1", "link": "https://oilprice.com/A/Story.html", "title": "Brent rises",
            "summary": "Synthetic.", "published_raw": "Mon, 09 Nov 2026 10:00:00 GMT", "updated_raw": None}
    base.update(kw)
    return base


class CanonicalUrl(unittest.TestCase):
    def test_host_and_tracking_normalized_path_and_query_case_kept(self):
        self.assertEqual(
            canonical_url("HTTPS://WWW.Example.COM:443/Path/To/Item?ID=AbC&utm_source=x&fbclid=y#frag"),
            "https://example.com/Path/To/Item?ID=AbC")

    def test_bing_click_through_unwrapped_without_fetching(self):
        wrapped = "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.reuters.com%2FWorld%2FX&tid=1"
        self.assertEqual(canonical_url(wrapped), "https://reuters.com/World/X")

    def test_non_http_rejected(self):
        for raw in ("javascript:alert(1)", "ftp://x/y", "", "not a url"):
            self.assertEqual(canonical_url(raw), "")


class SourceTimes(unittest.TestCase):
    CASES = {
        "Mon, 09 Nov 2026 09:40:00 GMT": ("2026-11-09T09:40:00Z", "explicit"),
        "Mon, 09 Nov 2026 14:00:00 -0500": ("2026-11-09T19:00:00Z", "explicit"),
        "2026-11-09T09:00:00+01:00": ("2026-11-09T08:00:00Z", "explicit"),
        "2026-11-09T12:30:00Z": ("2026-11-09T12:30:00Z", "explicit"),
        "Mon, 09 Nov 2026 09:30:00": (None, "no_timezone"),
        "2026-11-09T11:45:00": (None, "no_timezone"),
        "Mon, 09 Nov 2026 09:30:00 -0000": (None, "unknown_offset"),
        "Mon, 09 Nov 2026 09:30:00 +0000": ("2026-11-09T09:30:00Z", "explicit"),
        "Mon, 09 Nov 2026 09:30:00 EST": ("2026-11-09T14:30:00Z", "explicit"),
        "Mon, 09 Nov 2026 09:30:00 CET": (None, "unknown_zone"),
        "9 Nov 2026 09:30 GMT": ("2026-11-09T09:30:00Z", "explicit"),
        "2026-11-09": (None, "date_only"),
        "": (None, "missing"),
        None: (None, "missing"),
        "yesterday-ish": (None, "unparseable"),
    }

    def test_zone_is_never_guessed(self):
        for raw, expected in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_source_time(raw), expected)

    def test_missing_publication_time_is_null_not_collection_time(self):
        state = {}
        rec, _ = normalize_entry(entry(published_raw=None), SRC, "r1", "2026-11-09T10:05:00Z", "d", state)
        self.assertIsNone(rec["published_at"])
        self.assertEqual(rec["time_quality"]["published"], "missing")
        self.assertEqual(rec["observed_at"], "2026-11-09T10:05:00Z")

    def test_publication_and_update_kept_separately(self):
        rec, _ = normalize_entry(entry(updated_raw="2026-11-09T11:00:00Z"), SRC, "r1", "2026-11-09T11:05:00Z", "d", {})
        self.assertEqual((rec["published_at"], rec["updated_at"]), ("2026-11-09T10:00:00Z", "2026-11-09T11:00:00Z"))


class Identity(unittest.TestCase):
    def test_identity_quality_ladder(self):
        self.assertEqual(item_identity("s", "https://a.com/x", "g", "t")[1], "url")
        self.assertEqual(item_identity("s", "", "g", "t")[1], "guid")
        self.assertEqual(item_identity("s", "", None, "Some Title")[1], "weak")

    def test_scheme_does_not_split_identity_but_source_does(self):
        a = item_identity("ars_technica", "http://arstechnica.com/x", None, "")[0]
        b = item_identity("ars_technica", "https://arstechnica.com/x", None, "")[0]
        c = item_identity("techcrunch", "https://arstechnica.com/x", None, "")[0]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)  # distinct publishers stay distinct items

    def test_revision_chain_unchanged_and_tracking_only_changes(self):
        state = {}
        first, out1 = normalize_entry(entry(), SRC, "r1", "2026-11-09T10:05:00Z", "d", state)
        same, out2 = normalize_entry(entry(link="https://oilprice.com/A/Story.html?utm_source=rss"), SRC, "r2",
                                     "2026-11-09T10:20:00Z", "d", state)
        revised, out3 = normalize_entry(entry(title="Brent rises 4%"), SRC, "r3", "2026-11-09T10:35:00Z", "d", state)
        self.assertEqual((out1, out2, out3), ("new_item", "unchanged", "new_revision"))
        self.assertIsNone(same)
        self.assertEqual(revised["item_id"], first["item_id"])
        self.assertEqual(revised["previous_revision_id"], first["revision_id"])
        self.assertEqual(revised["first_seen_at"], "2026-11-09T10:05:00Z")
        back, out4 = normalize_entry(entry(), SRC, "r4", "2026-11-09T10:50:00Z", "d", state)
        self.assertEqual(out4, "new_revision")
        self.assertNotEqual(back["revision_id"], first["revision_id"])  # A -> B -> A is three events
        self.assertEqual(back["payload_digest"], first["payload_digest"])

    def test_revision_id_is_content_and_chain_addressed(self):
        self.assertNotEqual(revision_id("i", "p", None), revision_id("i", "p", "prev"))
        self.assertEqual(revision_id("i", "p", "prev"), revision_id("i", "p", "prev"))


if __name__ == "__main__":
    unittest.main()
