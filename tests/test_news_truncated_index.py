"""A cut index (index_views or index_bytes) publishes: the builder emits pages only for the views it lists.

Before M6b, the omitted views' pages stayed in the generation and the strict publisher refused it as
orphan pages (the documented M5.2 limit). M6b.1: a cut retains the newest dates first (within a date,
the build order: mode, then stream), and a cut that leaves no view at all is refused by the publisher.
Covered: both caps and their boundaries, one and two dates, truthful omitted-view counts and reasons,
no orphan pages, navigation, rendering through the unchanged panel, publication, invalid input and the
zero-view cut leaving the previous good generation untouched, and recovery. Local only: fixture feeds
and temporary stores.
"""
import json
import unittest

from test_news_publish import BUILD_INDEX, INDEX, Base, qualified
from test_news_publish_contract import probe, render_failed
from test_news_publish_repairs import DATE, Small, navigate
from market_data.news import publish as pub
from market_data.news.archive import LocalObjectStore
from market_data.news.reader import load_archive
from market_data.news.views import (LIMITS, MODES, STREAM_KEYS, BuildError, build_views, encode, limits_from,
                                    page_key)

LABEL = "PILOT news: prices unavailable (no price input connected)"
PER_DATE = len(MODES) * len(STREAM_KEYS)


def view_id(v):
    return f"{v['mode']}/{v['stream']}/{v['date']}"


def view_prefix(v):
    return page_key(v["mode"], v["stream"], v["date"], 1).rsplit("/", 1)[0] + "/"


class Cases:
    """Shared cases; subclasses set DATES and AS_OF and build self.archive."""

    DATES: list
    AS_OF: str

    def build(self, rows_per_page=2, archive=None, **limits):
        return build_views(archive or self.archive, pub.unavailable_prices(), as_of=self.AS_OF, tolerance_s=120,
                           data_label=LABEL, dates=self.DATES, limits=limits_from(rows_per_page=rows_per_page, **limits))

    def problems(self):
        """publish-status problems, less the one the inactive config always reports here: generations
        replaced more than 49 min earlier (TwoDates publishes from Nov 8) wait on the unverified store-clock
        bound (G3, clock_bound_verified=false). That one is asserted to be the reason when present."""
        st = pub.status(self.private, self.public, self.clock)
        if "expiry_blocked_clock_bound_unverified" in st["problems"]:
            self.assertFalse(pub.DEFAULT_POLICY.clock_bound_verified)
        return [p for p in st["problems"] if p != "expiry_blocked_clock_bound_unverified"]

    def prepare(self):
        self.views = PER_DATE * len(self.DATES)
        self.full = self.build()
        self.full_index = json.loads(self.full[BUILD_INDEX])
        self.order = [view_id(v) for v in self.full_index["views"]]
        self.assertEqual(len(self.order), self.views)
        self.assertEqual(sorted({v["date"] for v in self.full_index["views"]}), sorted(self.DATES))
        self.assertTrue(self.full_index["complete"])
        self.assertTrue(any(v["page_count"] > 1 for v in self.full_index["views"]), "multi-page views exercised")

    def retained(self, n):
        """The n views a cut must keep: newest date first, then build order; listed in build order."""
        priority = sorted(range(self.views), key=lambda i: (-int(self.order[i][-10:].replace("-", "")), i))
        return [self.order[i] for i in sorted(priority[:n])]

    def assert_truthful_generation(self, files, *, kept, reasons):
        """Index counts match what was built; exactly the retained views' pages are present, all of them."""
        index = json.loads(files[BUILD_INDEX])
        listed = index["views"]
        self.assertEqual([view_id(v) for v in listed], self.retained(kept), "newest dates retained first")
        self.assertEqual(index["views_total"], self.views, "views_total counts every view built")
        self.assertEqual(index["views_omitted"], self.views - kept)
        self.assertEqual(index["complete"], not reasons)
        self.assertEqual(index["overflow_reasons"], sorted(reasons))
        by_id = {view_id(v): v for v in self.full_index["views"]}
        for v in listed:
            self.assertEqual(v, by_id[view_id(v)], "listed entries are unchanged by the cut")
        expected = {BUILD_INDEX} | {page_key(v["mode"], v["stream"], v["date"], n)
                              for v in listed for n in range(1, v["page_count"] + 1)}
        self.assertEqual(set(files), expected, "no orphan pages and no missing pages")
        for key in expected - {BUILD_INDEX}:
            page, uncut = json.loads(files[key]), json.loads(self.full[key])
            self.assertEqual(page.pop("limits"), index["limits"], "pages state the build's limits")
            uncut.pop("limits")
            self.assertEqual(page, uncut, f"{key}: apart from the stated limits, the uncut build's page")
        omitted = [v for v in self.full_index["views"] if view_id(v) not in set(self.retained(kept))]
        self.assertEqual(len(omitted), self.views - kept)
        self.assertFalse([k for k in files for v in omitted if k.startswith(view_prefix(v))])
        self.assertLessEqual(len(files[BUILD_INDEX]), LIMITS.index_bytes)
        return index

    def assert_publishes_navigates_and_renders(self, files):
        self.assertEqual(pub.validate_generation(files)["views_total"], self.views)
        pub.publish(self.private, self.public, files, self.clock)
        self.assertEqual(self.public_files(), qualified(files), "the live generation is exactly this build")
        chains = navigate(self.raw_public)
        index = json.loads(files[BUILD_INDEX])
        self.assertEqual(list(chains), [view_id(v) for v in index["views"]])
        for v in index["views"]:
            self.assertEqual(len(chains[view_id(v)]), v["page_count"])
        parsed = {k: json.loads(b) for k, b in qualified(files).items()}
        results = probe([{"kind": "page", "page": p} for k, p in parsed.items() if k != INDEX] +
                        [{"kind": "mount", "index": parsed[INDEX], "pages": parsed}])
        for r in results[:-1]:
            self.assertIsNone(render_failed(r, ""), r["text"][:200])
        mount = results[-1]
        self.assertIsNone(render_failed(mount, "", mount=True), mount["text"][:300])
        if not index["complete"]:
            text = " ".join(mount["text"].split())
            self.assertIn(f"INCOMPLETE index {index['views_omitted']} of {self.views} views omitted", text)
        return mount

    def smallest_cap_listing(self, n):
        """The smallest index_bytes that still lists n views (found by bisection on the real builder)."""
        lo, hi = 1, LIMITS.index_bytes
        while lo < hi:
            mid = (lo + hi) // 2
            try:
                ok = len(json.loads(self.build(index_bytes=mid)[BUILD_INDEX])["views"]) >= n
            except BuildError:
                ok = False
            lo, hi = (lo, mid) if ok else (mid + 1, hi)
        return lo

    # ── view-count cap ─────────────────────────────────────────────────────────

    def test_view_count_cap_at_and_around_its_boundary(self):
        for cap in sorted({self.views + 1, self.views, self.views - 1, PER_DATE + 1, PER_DATE, PER_DATE - 1, 2, 1}):
            kept = min(cap, self.views)
            with self.subTest(index_views=cap):
                files = self.build(index_views=cap)
                self.assert_truthful_generation(files, kept=kept, reasons=set() if kept == self.views else {"index_views"})
                self.assert_publishes_navigates_and_renders(files)

    # ── byte cap ──────────────────────────────────────────────────────────────

    def test_byte_cap_at_and_around_its_boundary(self):
        complete_at = self.smallest_cap_listing(self.views)
        actual = len(self.full[BUILD_INDEX])
        self.assertGreaterEqual(complete_at, actual, "checked with the counters at their widest")
        print(f"\n{len(self.DATES)} date(s): index {actual} bytes when complete; smallest index_bytes keeping all "
              f"{self.views} views: {complete_at} (headroom {complete_at - actual})", flush=True)
        self.assert_truthful_generation(self.build(index_bytes=complete_at), kept=self.views, reasons=set())
        one_short = self.build(index_bytes=complete_at - 1)
        self.assert_truthful_generation(one_short, kept=self.views - 1, reasons={"index_bytes"})
        self.assertLessEqual(len(one_short[BUILD_INDEX]), complete_at - 1)
        self.assert_publishes_navigates_and_renders(one_short)
        for n in sorted({PER_DATE, 1} - {self.views}):
            at = self.smallest_cap_listing(n)
            with self.subTest(keep=n):
                self.assert_truthful_generation(self.build(index_bytes=at), kept=n, reasons={"index_bytes"})
                self.assert_truthful_generation(self.build(index_bytes=at - 1), kept=n - 1, reasons={"index_bytes"})
                self.assert_publishes_navigates_and_renders(self.build(index_bytes=at))

    def test_both_caps_together(self):
        cap = self.smallest_cap_listing(4)
        files = self.build(index_views=6, index_bytes=cap)
        self.assert_truthful_generation(files, kept=4, reasons={"index_views", "index_bytes"})
        self.assert_publishes_navigates_and_renders(files)

    # ── zero-view cut: refused; genuinely empty: published ─────────────────────

    def test_a_cut_to_no_views_is_refused_and_the_last_good_generation_stays(self):
        good = self.build(index_views=3)
        pub.publish(self.private, self.public, good, self.clock)
        before_private = {k: self.private.get(k) for k in self.private.list("")}
        empty_cut = self.build(index_bytes=self.smallest_cap_listing(1) - 1)
        index = self.assert_truthful_generation(empty_cut, kept=0, reasons={"index_bytes"})
        self.assertEqual((index["views"], set(empty_cut)), ([], {BUILD_INDEX}), "the builder still says what happened")
        self.log.clear()
        with self.assertRaisesRegex(pub.PublishError, f"lists no views although {self.views} were built"):
            pub.publish(self.private, self.public, empty_cut, self.clock)
        self.assertEqual([e for e in self.log if e[0] in ("put", "delete")], [], "no public writes")
        self.assertEqual(self.public_files(), qualified(good), "the previous good generation is byte-identical")
        self.assertEqual({k: self.private.get(k) for k in self.private.list("")}, before_private,
                         "no state, snapshot, lease or receipt writes")
        self.assertEqual(self.problems(), [])
        navigate(self.raw_public)
        recovered = self.build(index_views=self.views - 1)
        self.assert_publishes_navigates_and_renders(recovered)
        self.assertEqual(self.problems(), [])
        with self.assertRaisesRegex(BuildError, "mandatory index envelope"):
            self.build(index_bytes=300)

    def test_genuinely_empty_sources_still_publish(self):
        """Empty sources are not a cut. An empty archive builds the as-of date's views with no rows, which
        publish and render; an index with no views built (views_total 0) stays valid too."""
        empty = load_archive(LocalObjectStore(self.tmp / "empty-private"))
        files = build_views(empty, pub.unavailable_prices(), as_of=self.AS_OF, tolerance_s=120, data_label=LABEL)
        index = json.loads(files[BUILD_INDEX])
        self.assertEqual((len(index["views"]), index["views_omitted"], index["complete"]), (PER_DATE, 0, True))
        self.assertEqual({v["rows_total"] for v in index["views"]}, {0})
        self.views = PER_DATE
        self.assert_publishes_navigates_and_renders(files)
        nothing = build_views(empty, pub.unavailable_prices(), as_of=self.AS_OF, tolerance_s=120, data_label=LABEL,
                              streams=())
        index = json.loads(nothing[BUILD_INDEX])
        self.assertEqual((index["views"], index["views_total"], index["complete"], set(nothing)), ([], 0, True, {BUILD_INDEX}))
        self.assertEqual(pub.validate_generation(nothing)["views_total"], 0)

    # ── publication across cuts and invalid input ──────────────────────────────

    def test_cut_then_uncut_generations_replace_each_other_cleanly(self):
        pub.publish(self.private, self.public, self.full, self.clock)
        cut = self.build(index_views=3)
        pub.publish(self.private, self.public, cut, self.clock)
        self.assertEqual(self.public_files(), qualified(cut), "the live generation names only the cut's views")
        navigate(self.raw_public)
        pub.publish(self.private, self.public, self.full, self.clock)
        self.assertEqual(self.public_files(), qualified(self.full))
        navigate(self.raw_public)

    def test_invalid_input_leaves_the_previous_good_generation_untouched(self):
        good = self.build(index_views=3)
        pub.publish(self.private, self.public, good, self.clock)
        before_private = {k: self.private.get(k) for k in self.private.list("")}
        index = json.loads(good[BUILD_INDEX])
        omitted = next(v for v in self.full_index["views"] if view_id(v) not in set(self.retained(3)))
        orphan = page_key(omitted["mode"], omitted["stream"], omitted["date"], 1)
        first = index["views"][0]
        bad = {
            "an omitted view's page left in": dict(good, **{orphan: self.full[orphan]}),
            "a listed view's page missing": {k: b for k, b in good.items()
                                             if k != page_key(first["mode"], first["stream"], first["date"], 1)},
            "omitted count understated": dict(good, **{BUILD_INDEX: encode(dict(index, views_omitted=0))}),
            "cut index claiming complete": dict(good, **{BUILD_INDEX: encode(dict(index, complete=True))}),
            "cut index without its reason": dict(good, **{BUILD_INDEX: encode(dict(index, overflow_reasons=[]))}),
            "cut index with a page-level reason": dict(good, **{BUILD_INDEX: encode(dict(index,
                                                                overflow_reasons=["rows_beyond_max_pages"]))}),
            "index reason with nothing omitted": dict(good, **{BUILD_INDEX: encode(dict(index, views_omitted=0,
                                                               views_total=len(index["views"])))}),
            "every view removed by hand": {BUILD_INDEX: encode(dict(index, views=[], views_omitted=index["views_total"]))},
        }
        for name, files in bad.items():
            with self.subTest(name):
                self.log.clear()
                with self.assertRaises(pub.PublishError):
                    pub.publish(self.private, self.public, files, self.clock)
                self.assertEqual([e for e in self.log if e[0] in ("put", "delete")], [], "no public writes")
                self.assertEqual(self.public_files(), qualified(good), "previous good generation byte-identical")
                self.assertEqual({k: self.private.get(k) for k in self.private.list("")}, before_private)
                self.assertEqual(self.problems(), [])
        navigate(self.raw_public)


class OneDate(Cases, Small):
    DATES = [DATE]
    AS_OF = "2026-11-09T10:20:00Z"

    def setUp(self):
        super().setUp()
        self.prepare()

    def test_cli_build_views_reports_the_cut(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from market_data.news.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["build-views", "--store", str(self.private.root), "--as-of", self.AS_OF,
                             "--tolerance-s", "120", "--label", LABEL, "--out", str(Path(tmp) / "v"), "--date", DATE,
                             "--index-views", "4"])
            report = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual((report["views_total"], report["views_omitted"], report["complete"]),
                             (self.views, self.views - 4, False))
            written = sorted(p.relative_to(Path(tmp) / "v").as_posix() for p in (Path(tmp) / "v").rglob("*.json"))
            self.assertEqual(len(written), report["files"])
            index = json.loads((Path(tmp) / "v" / BUILD_INDEX).read_bytes())
            self.assertEqual(set(written), {BUILD_INDEX} | {page_key(v["mode"], v["stream"], v["date"], n)
                                                      for v in index["views"] for n in range(1, v["page_count"] + 1)})


class TwoDates(Cases, Small):
    """November 8 and 9: a cut keeps November 9 first (Chej's index_views=9 case)."""

    DATES = ["2026-11-08", "2026-11-09"]
    AS_OF = "2026-11-09T10:20:00Z"

    def setUp(self):
        Base.setUp(self)
        for when in ("2026-11-08T10:05:00Z", "2026-11-08T10:20:00Z", "2026-11-09T10:05:00Z",
                     "2026-11-09T10:20:00Z"):
            self.cycle(when)
        self.archive = load_archive(self.private)
        self.prepare()

    def test_index_views_9_keeps_all_of_the_newest_date(self):
        files = self.build(index_views=PER_DATE)
        index = self.assert_truthful_generation(files, kept=PER_DATE, reasons={"index_views"})
        self.assertEqual({v["date"] for v in index["views"]}, {"2026-11-09"})
        self.assertEqual([view_id(v) for v in index["views"]],
                         [i for i in self.order if i.endswith("2026-11-09")], "within-date order unchanged")
        self.assertEqual(index["views_omitted"], PER_DATE)
        self.assert_publishes_navigates_and_renders(files)

    def test_one_more_view_than_a_date_adds_the_older_dates_first_view(self):
        index = json.loads(self.build(index_views=PER_DATE + 1)[BUILD_INDEX])
        self.assertEqual([view_id(v) for v in index["views"]],
                         [self.order[0]] + [i for i in self.order if i.endswith("2026-11-09")])

    def test_the_byte_cap_also_drops_the_oldest_date_first(self):
        index = json.loads(self.build(index_bytes=self.smallest_cap_listing(PER_DATE))[BUILD_INDEX])
        self.assertEqual({v["date"] for v in index["views"]}, {"2026-11-09"})
        self.assertEqual(index["overflow_reasons"], ["index_bytes"])

    def test_the_cut_is_deterministic(self):
        for limits in ({"index_views": 5}, {"index_bytes": self.smallest_cap_listing(5)}):
            with self.subTest(**limits):
                self.assertEqual(self.build(**limits), self.build(**limits))


if __name__ == "__main__":
    unittest.main()
