"""M5.1: Chej's four publisher repairs (validation, navigation, generation identity, abandoned leases).

G3: the old publication is established by a real publish (generation + ledger), and navigation now
cannot mix builds (immutable generation keys). The G3 edits were NOT RUN (static review only).

Local only: fixture feeds, temporary directory stores, subprocesses that exit abruptly on purpose.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import timedelta

from test_news_publish import (BUILD_INDEX, INDEX, PANEL, ROOT, Base, RecordingStore, patched_config_path,
                               qualified)
from market_data.news import publish as pub
from market_data.news.archive import WriterBusy
from market_data.news.reader import load_archive
from market_data.news.views import build_views, encode, generation_name, generation_page_key, limits_from

DATE = "2026-11-09"
P1 = f"news/v1/views/collector/oil/{DATE}/page-0001.json"
P2 = f"news/v1/views/collector/oil/{DATE}/page-0002.json"


def qkey(files, key):
    """The published key of builder page `key` in the generation `files` becomes."""
    _, _, _, mode, stream, date, name = key.split("/")
    return generation_page_key(generation_name(files) + ".1", mode, stream, date, int(name[5:9]))


def navigate(store) -> dict:
    """Follow the live index exactly as the panel does: first_page, then next_page links.

    Returns {view: [(key, build_as_of), ...]}. Fails on a missing, unparsable or unrenderable page, a
    broken back link, a wrong page number, or a cycle.
    """
    index = json.loads(store.get(INDEX))
    out = {}
    for v in index["views"]:
        key, prev, chain, seen = v["first_page"], None, [], set()
        while key:
            assert key not in seen, f"navigation cycle at {key}"
            seen.add(key)
            assert pub.is_public_view_key(key), key
            body = store.get(key)
            assert body is not None, f"reachable page missing: {key} (from {prev or 'index'})"
            page = json.loads(body)
            assert not pub.page_render_problems(page), (key, pub.page_render_problems(page))
            assert page["prev_page"] == prev, f"{key}: prev_page {page['prev_page']} != {prev}"
            assert page["page_number"] == len(chain) + 1, f"{key}: page_number {page['page_number']}"
            chain.append((key, page["build_as_of"]))
            prev, key = key, page["next_page"]
        out[f"{v['mode']}/{v['stream']}/{v['date']}"] = chain
    return out


class Small(Base):
    """Generations restricted to a couple of views, with a chosen rows-per-page to force page counts."""

    def setUp(self):
        super().setUp()
        self.cycle("2026-11-09T10:05:00Z")
        self.cycle("2026-11-09T10:20:00Z")
        self.archive = load_archive(self.private)

    def gen(self, as_of, rows_per_page, streams=("oil", "all")):
        return build_views(self.archive, pub.unavailable_prices(), as_of=as_of, tolerance_s=120,
                           data_label="PILOT news: prices unavailable (no price input connected)",
                           modes=("collector",), streams=streams, dates=[DATE],
                           limits=limits_from(rows_per_page=rows_per_page))

    def reset_public(self, files):
        """Make `files` (builder output) the only, properly published generation: empty public store, fresh
        publisher state and ledger (the raw archive is kept), then a real publish."""
        for key in self.raw_public.list(""):
            self.raw_public.delete(key)
        for key in self.private.list(pub.PUBLISH_PREFIX):
            self.private.delete(key)
        self.raw_public.fail = None
        pub.publish(self.private, self.public, files, self.clock)
        self.log.clear()


class Validation(Small):
    def setUp(self):
        super().setUp()
        self.good = self.gen("2026-11-09T10:20:00Z", 2)
        self.assertGreaterEqual(json.loads(self.good[P1])["page_count"], 2)
        self.old = self.gen("2026-11-09T10:10:00Z", 50)
        self.reset_public(self.old)

    def mutated(self, key, change):
        files = dict(self.good)
        if callable(change):
            page = json.loads(files[key])
            change(page)
            files[key] = encode(page)
        else:
            files[key] = change
        return files

    def test_every_named_page_is_checked_before_any_write(self):
        cases = {
            "invalid JSON": (P2, b"{not json"),
            "null coverage": (P2, lambda p: p.update(coverage=None)),
            "wrong schema": (P2, lambda p: p.update(schema_version="news-view/v0")),
            "rows not objects": (P2, lambda p: p.update(rows=[1, 2])),
            "next link to nowhere": (P2, lambda p: p.update(next_page="news/v1/views/collector/oil/2026-11-09/page-0009.json")),
            "prev link wrong": (P2, lambda p: p.update(prev_page=None)),
            "page number wrong": (P2, lambda p: p.update(page_number=1)),
            "page count disagrees": (P1, lambda p: p.update(page_count=p["page_count"] + 1)),
            "rows_total disagrees": (P1, lambda p: p["view"].update(rows_total=999)),
            "different build": (P2, lambda p: p.update(build_as_of="2026-11-09T10:19:00Z")),
            "wrong stream in page": (P2, lambda p: p.update(stream="all")),
        }
        for name, (key, change) in cases.items():
            with self.subTest(name):
                with self.assertRaises(pub.PublishError):
                    pub.publish(self.private, self.public, self.mutated(key, change), self.clock)
                self.assertEqual([op for op, _ in self.log if op in ("put", "delete")], [], name)
                self.assertEqual(self.public_files(), qualified(self.old), f"{name}: old publication untouched")

    def test_index_and_orphan_problems_are_rejected(self):
        index = json.loads(self.good[BUILD_INDEX])
        bad_first = dict(index, views=[dict(index["views"][0], first_page=P2)] + index["views"][1:])
        dup = dict(index, views=index["views"] + index["views"][:1])
        orphan = dict(self.good, **{"news/v1/views/collector/oil/2026-11-09/page-0020.json": self.good[P1]})
        for name, files in (("first_page not page 1", dict(self.good, **{BUILD_INDEX: encode(bad_first)})),
                            ("view listed twice", dict(self.good, **{BUILD_INDEX: encode(dup)})),
                            ("orphan page", orphan)):
            with self.subTest(name):
                with self.assertRaises(pub.PublishError):
                    pub.publish(self.private, self.public, files, self.clock)
                self.assertEqual(self.public_files(), qualified(self.old))

    def test_render_checks_match_the_panel(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        pages = [json.loads(self.good[P1])]
        for change in (lambda p: p.update(coverage=None), lambda p: p.pop("rows"), lambda p: p.update(page_number="1"),
                       lambda p: p.update(window={"start": 1}), lambda p: p.update(complete="yes"),
                       lambda p: p["coverage"].update(segments=[1]), lambda p: p.update(rows_offset=None),
                       lambda p: p.update(schema_version="x"), lambda p: p.update(view={"rows_total": 1.5})):
            page = json.loads(self.good[P1])
            change(page)
            pages.append(page)
        script = ("const p=require(process.argv[1]);const ps=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "process.stdout.write(JSON.stringify(ps.map(x=>p.validatePage(x).length===0)))")
        out = subprocess.run([node, "-e", script, str(PANEL)], input=json.dumps(pages), capture_output=True,
                             text=True, check=True).stdout
        self.assertEqual(json.loads(out), [not pub.page_render_problems(p) for p in pages])
        self.assertEqual(json.loads(out)[0], True)
        self.assertNotIn(True, json.loads(out)[1:])


class Navigation(Small):
    """Growth, shrinkage and interruption, checked by following links from the live index."""

    def crash_at_every_write(self, old, new):
        page_puts = [k for k in pub.page_write_order(qualified(new))]
        mixed_views = 0
        for crash_at in range(len(page_puts) + 1):     # the last value crashes on the index write
            with self.subTest(crash_at=crash_at):
                self.reset_public(old)
                self.private.delete(pub.PUBLISH_LEASE_KEY)
                self.raw_public.fail = ("put", lambda k: True, crash_at)
                # A crash on a page or on the index write is raised as is (G6: the index commit stays open).
                with self.assertRaises((RuntimeError, pub.PublishError)):
                    pub.publish(self.private, self.public, new, self.clock)
                self.raw_public.fail = None
                chains = navigate(self.raw_public)          # every reachable page exists and renders
                self.assertEqual(self.live_index(), qualified(old)[INDEX], "old index still live")
                mixed_views += sum(1 for c in chains.values() if len({b for _, b in c}) > 1)
                if crash_at == len(page_puts):
                    # The index write has no outcome: its commit stays open. G6: that does not pause; the
                    # next cycle sends a new commit (fresh nonce) on the same expected ETag, and when it
                    # lands the first one is known dead.
                    self.assertTrue(pub.status(self.private, self.public, self.clock)["open_commits"])
                # The next cycle reuses the pinned incarnation. G6 r1: a crash on a page PUT leaves that
                # attempt unanswered, so the incarnation stays quarantined (page_uploads_unsettled) after
                # it goes live; navigation and the public files are unaffected.
                pub.publish(self.private, self.public, new, self.clock)      # the next cycle
                final = navigate(self.raw_public)
                build = json.loads(new[BUILD_INDEX])["build_as_of"]
                self.assertTrue(all(b == build for c in final.values() for _, b in c))
                self.assertEqual(self.public_files(), qualified(new))
        return mixed_views

    def test_growth_one_page_to_two_with_page_two_failing(self):
        old, new = self.gen("2026-11-09T10:10:00Z", 50), self.gen("2026-11-09T10:20:00Z", 3)
        self.assertEqual(json.loads(old[P1])["page_count"], 1)
        self.assertGreaterEqual(json.loads(new[P1])["page_count"], 2)
        self.reset_public(old)
        new_p2 = qkey(new, P2)
        self.raw_public.fail = ("put", lambda k: k == new_p2, 0)        # Chej's case
        with self.assertRaises(RuntimeError):
            pub.publish(self.private, self.public, new, self.clock)
        old_p1 = qkey(old, P1)
        self.assertEqual(self.raw_public.get(old_p1), qualified(old)[old_p1], "the old generation's pages are immutable")
        self.assertEqual(self.live_index(), qualified(old)[INDEX])
        navigate(self.raw_public)

    def test_growth_crash_anywhere_leaves_navigation_working(self):
        self.crash_at_every_write(self.gen("2026-11-09T10:10:00Z", 50), self.gen("2026-11-09T10:20:00Z", 2))

    def test_shrinkage_crash_anywhere_leaves_navigation_working(self):
        mixed = self.crash_at_every_write(self.gen("2026-11-09T10:10:00Z", 2), self.gen("2026-11-09T10:20:00Z", 50))
        self.assertEqual(mixed, 0, "a one-page result replaces each view in one write")

    def test_same_count_replacement_never_mixes_builds(self):
        # G3 removes the documented fixed-key limit: a reader following the live index sees one build only,
        # whatever write the publication crashes on.
        mixed = self.crash_at_every_write(self.gen("2026-11-09T10:10:00Z", 2), self.gen("2026-11-09T10:20:00Z", 2))
        self.assertEqual(mixed, 0, "generation keys: no mixing mid-publication")


class Identity(Small):
    def test_changed_page_with_identical_index_is_a_different_generation(self):
        a = self.gen("2026-11-09T10:20:00Z", 50)
        page = json.loads(a[P1])
        page["rows"][0]["title"] = page["rows"][0]["title"] + " (revised)"
        b = dict(a, **{P1: encode(page)})
        self.assertEqual(a[BUILD_INDEX], b[BUILD_INDEX])
        ia = pub.generation_id(a, json.loads(a[BUILD_INDEX]))
        ib = pub.generation_id(b, json.loads(b[BUILD_INDEX]))
        self.assertNotEqual(ia, ib)
        pub.publish(self.private, self.public, a, self.clock)
        pub.publish(self.private, self.public, b, self.clock)
        self.assertEqual(pub.read_state(self.private)["committed"]["gen_id"], ib)
        pub.stop(self.private, self.clock, "restore drill")
        pub.restore(self.private, self.public, ia, self.clock)
        self.assertEqual(self.public_files(), qualified(a))
        pub.restore(self.private, self.public, ib, self.clock)
        self.assertEqual(self.public_files(), qualified(b))

    def test_conflicting_snapshot_reuse_is_refused_before_public_writes(self):
        a = self.gen("2026-11-09T10:20:00Z", 50)
        gid = pub.generation_id(a, json.loads(a[BUILD_INDEX]))
        other = self.gen("2026-11-09T10:10:00Z", 50)
        pub._snapshot(self.private, gid, other, self.clock)          # a planted, mismatched snapshot
        self.log.clear()
        with self.assertRaises(pub.PublishError):
            pub.publish(self.private, self.public, a, self.clock)
        self.assertEqual([op for op, _ in self.log], [])
        pub.stop(self.private, self.clock, "restore drill")
        with self.assertRaises(pub.PublishError):
            pub.restore(self.private, self.public, gid, self.clock)


CRASH = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
    from test_news_support import Clock, feeds
    from market_data.news import publish as pub
    from market_data.news.archive import LocalObjectStore
    from market_data.news.adapters import rss

    class Dying(LocalObjectStore):
        n = 0
        def _tick(self):
            Dying.n += 1
            if {where!r} == "publish" and Dying.n == 3:
                os._exit(3)                      # abrupt exit: no finally, no lease release
        def put(self, key, data, content_type="application/octet-stream", cache_control=None):
            self._tick()
            super().put(key, data, content_type, cache_control=cache_control)
        def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
            self._tick()
            return super().put_if_absent(key, data, content_type=content_type, cache_control=cache_control)

    def dying_fetch(url, timeout):
        if {where!r} == "collect":
            os._exit(3)
        return feeds()(url, timeout)

    clock = Clock({when!r})
    pub.pilot_cycle(LocalObjectStore({private!r}), pub.PublicViewStore(Dying({public!r})), {spool!r},
                    dying_fetch, clock, pub.load_pilot_config({config!r}))
    sys.exit(0)
""")


class AbandonedLease(Base):
    def crash(self, where, when):
        code = CRASH.format(root=str(ROOT), tests=str(ROOT / "tests"), where=where, when=when,
                            private=str(self.tmp / "private"), public=str(self.tmp / "public"),
                            spool=str(self.spool), config=str(patched_config_path(self.tmp)))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, r.stderr[-2000:])

    def cli(self, *argv):
        from test_news_publish import PilotConfig as P
        return P.run_cli(self, *argv)

    def base_args(self):
        return ["--private", str(self.tmp / "private"), "--public", str(self.tmp / "public")]

    def test_process_exit_during_publication_then_explicit_recovery(self):
        self.cycle("2026-11-09T10:05:00Z")
        self.crash("publish", "2026-11-09T10:20:00Z")
        # Within the lease window: refused, and reported as an interrupted publication with the lease held.
        code, out, err = self.cli("pilot-cycle", *self.base_args(), "--spool", str(self.spool),
                                  "--clock", "2026-11-09T10:25:00Z")
        self.assertEqual(code, 2)
        self.assertIn("writer lease held", err)
        st = pub.status(self.private, self.public, self.clock.set("2026-11-09T10:25:00Z"))
        self.assertIn("publication_interrupted", st["problems"])
        self.assertFalse(st["leases"]["publish"]["expired"])
        self.assertFalse(st["healthy"])
        navigate(self.raw_public)                                    # what readers see still works
        # After expiry: still refused without the explicit flag, and reported as a stale lease.
        later = "2026-11-09T10:40:00Z"
        st = pub.status(self.private, self.public, self.clock.set(later))
        self.assertIn("stale_publish_lease", st["problems"])
        code, out, err = self.cli("pilot-cycle", *self.base_args(), "--spool", str(self.spool), "--clock", later)
        self.assertEqual(code, 2)
        self.assertIn("--break-stale-lease", err)
        code, out, err = self.cli("pilot-cycle", *self.base_args(), "--spool", str(self.spool), "--clock", later,
                                  "--break-stale-lease")
        self.assertEqual(code, 0, err)
        out = json.loads(out)
        self.assertEqual(out["publication"]["recovered_from"] is not None, True)
        st = pub.status(self.private, self.public, self.clock.set("2026-11-09T10:41:00Z"))
        # G6 r1: the process exited during a page PUT, so that upload attempt is never recorded answered;
        # the publisher cannot know the PUT will not land. The recovered incarnation stays quarantined.
        self.assertEqual(st["problems"], ["page_uploads_unsettled"])
        self.assertEqual(st["leases"], {})
        navigate(self.raw_public)

    def test_process_exit_during_first_collection_is_never_published_not_healthy(self):
        self.crash("collect", "2026-11-09T10:05:00Z")
        st = pub.status(self.private, self.public, self.clock.set("2026-11-09T10:06:00Z"))
        self.assertIn("never_published", st["problems"])
        self.assertFalse(st["healthy"])
        self.assertFalse(st["leases"]["writer"]["expired"])
        st = pub.status(self.private, self.public, self.clock.set("2026-11-09T10:30:00Z"))
        self.assertEqual(sorted(st["problems"]), ["never_published", "stale_writer_lease"])
        with self.assertRaises(WriterBusy):
            self.cycle("2026-11-09T10:30:00Z")
        self.clock.set("2026-11-09T10:30:00Z")
        out = pub.pilot_cycle(self.private, self.public, self.spool, __import__("test_news_support").feeds(),
                              self.clock, self.cfg, break_stale_lease=True)
        self.assertEqual(out["status"], "published")
        self.assertTrue(pub.status(self.private, self.public, self.clock)["healthy"])

    def test_an_unexpired_lease_is_never_broken(self):
        self.cycle("2026-11-09T10:05:00Z")
        self.crash("publish", "2026-11-09T10:20:00Z")
        self.clock.set("2026-11-09T10:25:00Z")
        with self.assertRaises(WriterBusy):
            pub.pilot_cycle(self.private, self.public, self.spool, __import__("test_news_support").feeds(),
                            self.clock, self.cfg, break_stale_lease=True)


if __name__ == "__main__":
    import unittest
    unittest.main()
