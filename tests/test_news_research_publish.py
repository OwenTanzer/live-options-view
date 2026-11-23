"""MediaFlow R1 + R2: the research projection as a generation page, published by the inactive pilot.

R1: the sanitized research view is one page of the generation (news/v2/views/p/<gen>.<n>/research/view.json),
named only by the index's "research" entry. It is qualified, validated, reserved against the page cap,
withdrawn and retired with the news view pages, through the unchanged G6 head and CAS paths.
R2: pilot_cycle builds it from the private research ledger, pinned to research mode at the build cutoff.

Local stores only (LocalObjectStore in temporary directories); prepared records from
tests/fixtures/news/research/positioning_p1_prepared.json. No provider, bucket or live source.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import news_research_scenarios as S  # noqa: E402
from market_data.news import publish as pub  # noqa: E402
from market_data.news import views as V  # noqa: E402
from market_data.news.__main__ import DEFAULT_FIXTURES, fixture_fetcher  # noqa: E402
from market_data.news.archive import LocalObjectStore  # noqa: E402
from market_data.news.research import RESEARCH_ROOT  # noqa: E402

CONFIG = ROOT / "market_data/news/deploy/news-pilot.json"
COMMIT = datetime(2026, 11, 13, 18, 0, 5, tzinfo=timezone.utc)     # the prepared import's commit clock


def clock(t: datetime):
    return lambda: t


class ResearchPublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = pub.load_pilot_config(CONFIG)

    def tearDown(self):
        self.tmp.cleanup()

    def stores(self, prepared=True):
        private = S.prepared_store(self.root / "priv") if prepared else LocalObjectStore(self.root / "priv")
        return private, LocalObjectStore(self.root / "pub")

    def cycle(self, private, public, t: datetime):
        return pub.pilot_cycle(private, public, str(self.root / "spool"), fixture_fetcher(Path(DEFAULT_FIXTURES)),
                               clock(t), self.cfg)

    def index(self, public):
        return json.loads(public.get(pub.PUBLIC_INDEX))

    # ── R1 ────────────────────────────────────────────────────────────────

    def test_research_page_is_a_page_of_the_published_incarnation(self):
        private, public = self.stores()
        t = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
        out = self.cycle(private, public, t)
        self.assertEqual(out["status"], "published")
        index = self.index(public)
        inc = index["gen"]
        entry = index["research"]
        self.assertEqual(entry["state"], "published")
        self.assertEqual(entry["page"], V.generation_research_key(inc))
        self.assertEqual((entry["mode"], entry["cutoff"], entry["fixed"]), ("research", index["build_as_of"], True))
        page = json.loads(public.get(entry["page"]))
        self.assertEqual(page["gen"], inc)
        self.assertEqual((page["mode"], page["cutoff"], page["build_as_of"]), ("research", index["build_as_of"], index["build_as_of"]))
        self.assertFalse(page["hindsight"])
        self.assertEqual(len(page["items"]), entry["items"])
        self.assertGreater(entry["items"], 0)
        # Under the incarnation and counted with its pages: head reservation == pages listed.
        listed = pub.PublicViewStore(public).pages_by_incarnation()
        self.assertIn(entry["page"], listed[inc])
        head, _ = pub.read_head(private)
        self.assertEqual(head["incarnations"][inc]["pages"], len(listed[inc]))
        # The mutable single key and the private ledger never reach the public store.
        keys = public.list("news/")
        self.assertNotIn("news/v2/research/view.json", keys)
        self.assertEqual([k for k in keys if k.startswith("news/v1/")], [])
        self.assertTrue(private.list(RESEARCH_ROOT))
        # The integrity digest covers the research page.
        files = {k: public.get(k) for k in listed[inc]}
        self.assertEqual(index["pages_sha256"], V.pages_digest(files))

    def test_publisher_refuses_inconsistent_research(self):
        private, _ = self.stores()
        t = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
        from market_data.news.reader import load_archive
        archive = load_archive(private)
        research = pub.build_research(private, archive, t, "PREPARED test", V.qualified_limits())
        files = V.build_views(archive, pub.unavailable_prices(), as_of=t, tolerance_s=120, data_label="TEST",
                              dates=pub.view_dates_for(t, 1), limits=V.qualified_limits(), research=research)
        pub.validate_generation(files)                       # the consistent build passes
        inc, qualified = V.qualify_generation(files, 1)
        pub.validate_qualified(inc, qualified)

        def broken(mutate):
            f = dict(files)
            mutate(f)
            with self.assertRaises(pub.PublishError):
                pub.validate_generation(f)

        def edit_index(fn):
            def m(f):
                idx = json.loads(f[V.INDEX_KEY])
                fn(idx)
                f[V.INDEX_KEY] = V.encode(idx)
            return m

        def edit_page(fn):
            def m(f):
                pg = json.loads(f[V.RESEARCH_KEY])
                fn(pg)
                f[V.RESEARCH_KEY] = V.encode(pg)
            return m
        broken(edit_index(lambda i: i.pop("research")))                                   # page with no entry
        broken(lambda f: f.pop(V.RESEARCH_KEY))                                           # entry with no page
        broken(edit_index(lambda i: i["research"].update(cutoff="2026-11-13T18:00:00Z")))  # not the build cutoff
        broken(edit_index(lambda i: i["research"].update(mode="publication")))            # not the fixed mode
        broken(edit_index(lambda i: i["research"].update(state="none", page=None)))       # none, page still present
        broken(edit_index(lambda i: i["research"].update(items=99)))                      # count disagrees
        broken(edit_page(lambda p: p.update(cutoff="2026-11-14T00:00:00Z")))
        broken(edit_page(lambda p: p.update(hindsight=True)))
        # A qualified research page from another incarnation is outside this one.
        q = dict(qualified)
        other = V.generation_research_key(inc[:-1] + "2")
        q[other] = q.pop(V.generation_research_key(inc))
        with self.assertRaises(pub.PublishError):
            pub.validate_qualified(inc, q)

    def test_research_page_requires_the_fixed_mode_and_cutoff(self):
        private, _ = self.stores()
        t = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
        from market_data.news.research import load_research, project, research_view
        ledger = load_research(private)
        for mode, cutoff in (("publication", t), ("archive", t), ("research", t - timedelta(hours=1))):
            with self.assertRaises(V.BuildError):
                V.research_page(project(research_view(ledger, cutoff, mode), "X"), as_of=t)

    def test_oversize_research_is_omitted_and_news_still_publishes(self):
        private, public = self.stores()
        t = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
        real = pub.build_research
        try:
            pub.build_research = lambda p, a, now, label, limits: real(p, a, now, label,
                                                                          V.limits_from(page_bytes=1024))
            self.cycle(private, public, t)
        finally:
            pub.build_research = real
        index = self.index(public)
        self.assertEqual(index["research"]["state"], "omitted")
        self.assertEqual(index["research"]["reason"], "research_view_bytes")
        self.assertIsNone(index["research"]["page"])
        self.assertTrue(index["views"])
        self.assertEqual([k for k in public.list("news/") if k.endswith("/research/view.json")], [])

    def test_withdrawal_and_retirement_cover_the_research_page(self):
        private, public = self.stores()
        t1 = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
        self.cycle(private, public, t1)
        first = self.index(public)
        t2 = t1 + timedelta(minutes=15)
        self.cycle(private, public, t2)
        second = self.index(public)
        self.assertNotEqual(first["gen"], second["gen"])
        self.assertNotEqual(first["research"]["page"], second["research"]["page"])
        # The replaced incarnation's research page stays readable for the reader grace (never deleted early).
        self.assertIsNotNone(public.get(first["research"]["page"]))
        pub.stop(private, clock(t2 + timedelta(seconds=30)), "R1 withdrawal test")
        pub.withdraw(private, public, clock(t2 + timedelta(minutes=1)))
        tomb = self.index(public)
        self.assertEqual(tomb["state"], "withdrawn")
        self.assertNotIn("research", tomb)
        self.assertEqual([k for k in public.list("news/v2/views/p/")], [])

    # ── R2 ────────────────────────────────────────────────────────────────

    def test_empty_ledger_publishes_state_none(self):
        private, public = self.stores(prepared=False)
        self.cycle(private, public, datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc))
        entry = self.index(public)["research"]
        self.assertEqual((entry["state"], entry["page"], entry["runs"], entry["items"]), ("none", None, 0, 0))
        self.assertEqual([k for k in public.list("news/") if "research" in k], [])

    def test_research_gates_hold_at_the_build_cutoff(self):
        # One second before the prepared import commits: the research page is published with no records.
        private, public = self.stores()
        self.cycle(private, public, COMMIT - timedelta(seconds=1))
        entry = self.index(public)["research"]
        self.assertEqual((entry["state"], entry["items"]), ("published", 0))
        page = json.loads(public.get(entry["page"]))
        self.assertEqual(page["items"], [])
        self.assertGreater(page["excluded"]["not_yet_visible"], 0)

    def test_unverified_ledger_runs_are_counted_not_shown(self):
        private, public = self.stores()
        manifest = [k for k in private.list(RESEARCH_ROOT) if k.endswith("manifest.json")][0]
        private.put(manifest.replace("manifest.json", "captures.jsonl.gz"), b"corrupt")
        self.cycle(private, public, datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc))
        entry = self.index(public)["research"]
        self.assertEqual((entry["state"], entry["runs"], entry["unverified_runs"]), ("none", 0, 1))

    def test_config_pins_the_research_view(self):
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(raw["research"]["mode"], "research")
        self.assertEqual(raw["research"]["cutoff"], "build_as_of")
        self.assertFalse(raw["active"])
        for change in ({"mode": "publication"}, {"cutoff": "2026-11-13T00:00:00Z"}, {"data_label": ""}):
            bad = copy.deepcopy(raw)
            bad["research"].update(change)
            path = self.root / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(pub.PublishError):
                pub.load_pilot_config(path)

    def test_snapshot_restore_requalifies_the_research_page(self):
        private, public = self.stores()
        t = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
        self.cycle(private, public, t)
        gen = self.index(public)["logical_gen"]
        snap = pub.load_snapshot(private, gen)
        self.assertIn(V.RESEARCH_KEY, snap)
        pub.validate_generation(snap)


if __name__ == "__main__":
    unittest.main()
