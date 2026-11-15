"""Regressions for the M2 repair revision (review of 6a14e18).

1. A malformed article URL must not abort the cycle.
2. A category-only change is a new, dated revision (categories are classifier input).
3. Budget-expired cycles must not accumulate fetch workers.
4. Archive availability (committed_at) must not precede the durable manifest.
5. An interrupted spool cleanup must not block later restarts.

All feed bodies here are SYNTHETIC test data.
"""
import json
import threading
import time
import unittest
from unittest import mock

from test_news_archive import ArchiveCase
from test_news_support import Clock, FailingStore, feeds, no_sleep
from market_data.news import archive as archive_mod
from market_data.news import config
from market_data.news.adapters.rss import FetchPool, FetchResponse, fetch_all
from market_data.news.archive import (ArchiveIntegrityError, HistoryCorrupt, LocalObjectStore,
                                      pending_spools, scan_history)
from market_data.news.normalize import canonical_url
from market_data.news.reader import as_of, load_archive

OILPRICE = config.SOURCES["oilprice_com"]


def rss(*items: str) -> bytes:
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>SYNTHETIC test feed</title>'
            + "".join(items) + "</channel></rss>").encode("utf-8")


def item(title, link, guid, pub="Mon, 09 Nov 2026 10:00:00 +0000", category=None, desc="SYNTHETIC body."):
    cat = f"<category>{category}</category>" if category else ""
    return (f"<item><title>{title}</title><link>{link}</link><guid>{guid}</guid>"
            f"<pubDate>{pub}</pubDate><description>{desc}</description>{cat}</item>")


def serve(overrides: dict[str, bytes], fallback=None):
    """Serve given bodies by source URL; everything else from the fixture feeds (or 404)."""
    base = fallback or feeds()

    def fetch(url, timeout):
        if url in overrides:
            return FetchResponse(200, overrides[url])
        return base(url, timeout)
    return fetch


class MalformedUrl(ArchiveCase):
    BAD = "https://example.com:bad/story"

    def test_canonical_url_rejects_bad_ports_instead_of_raising(self):
        for raw in (self.BAD, "https://example.com:99999/x", "http://[::1"):
            self.assertEqual(canonical_url(raw), "")

    def test_bad_link_is_archived_without_aborting_the_cycle(self):
        body = rss(item("Crude stocks draw (SYNTHETIC)", self.BAD, "bad-port-1"),
                   item("Brent edges higher (SYNTHETIC)", "https://oilprice.com/a/good-1", "good-1"))
        report = self.collect(fetch=serve({OILPRICE.url: body}))
        m = report.manifest
        self.assertEqual(m["status"], "complete")
        self.assertEqual({s["source_id"]: s["status"] for s in m["sources"] if s["status"] == "ok"},
                         {k: "ok" for k in ("oilprice_com", "eia_today_in_energy", "techcrunch", "ars_technica")})
        obs = {o["source_guid"]: o for r in scan_history(self.store).runs for o in r.observations}
        bad = obs["bad-port-1"]
        self.assertEqual((bad["original_url"], bad["canonical_url"], bad["url_status"], bad["identity_quality"]),
                         (self.BAD, "", "invalid", "guid"))
        self.assertEqual(obs["good-1"]["url_status"], "ok")
        # The other sources' items are committed too.
        self.assertTrue(any(o["source_id"] == "techcrunch" for o in obs.values()))

    def test_any_item_failure_is_isolated_and_recorded(self):
        real = archive_mod.normalize_entry

        def flaky(entry, *args, **kw):
            if entry.get("guid") == "explodes":
                raise ValueError("synthetic normalizer failure")
            return real(entry, *args, **kw)
        body = rss(item("Crude item (SYNTHETIC)", "https://oilprice.com/a/x", "explodes"),
                   item("Brent item (SYNTHETIC)", "https://oilprice.com/a/y", "fine"))
        with mock.patch.object(archive_mod, "normalize_entry", flaky):
            m = self.collect(fetch=serve({OILPRICE.url: body})).manifest
        row = next(s for s in m["sources"] if s["source_id"] == "oilprice_com")
        self.assertEqual((row["status"], row["entries"], row["new_items"], row["failed_entries"]), ("ok", 2, 1, 1))
        self.assertEqual(row["entry_failures"][0]["guid"], "explodes")
        self.assertIn("synthetic normalizer failure", row["entry_failures"][0]["error"])
        self.assertEqual((m["status"], m["counts"]["failed_entries"]), ("partial", 1))
        guids = {o["source_guid"] for r in scan_history(self.store).runs for o in r.observations}
        self.assertIn("fine", guids)
        self.assertNotIn("explodes", guids)
        self.assertTrue(any(s["source_id"] == "techcrunch" and s["new_items"] for s in m["sources"]))


class CategoryRevision(ArchiveCase):
    def feed(self, category):
        return serve({OILPRICE.url: rss(item("Quarterly results (SYNTHETIC)", "https://oilprice.com/a/q3",
                                             "q3", category=category, desc="Company figures."))},
                     fallback=lambda url, timeout: FetchResponse(404, b""))

    def test_category_only_change_is_a_new_dated_revision(self):
        self.clock.set("2026-11-09T10:05:00Z")
        self.collect(fetch=self.feed("Finance"), only_sources=["oilprice_com"])
        self.clock.set("2026-11-09T10:10:00Z")
        m = self.collect(fetch=self.feed("Crude"), only_sources=["oilprice_com"]).manifest
        self.assertEqual((m["counts"]["new_revisions"], m["counts"]["unchanged"]), (1, 0))

        revs = sorted((o for r in scan_history(self.store).runs for o in r.observations),
                      key=lambda o: o["observed_at"])
        self.assertEqual([o["categories"] for o in revs], [["Finance"], ["Crude"]])
        self.assertEqual(revs[1]["previous_revision_id"], revs[0]["revision_id"])

        archive = load_archive(self.store)
        before = as_of(archive, "2026-11-09T10:07:00Z", "collector", stream="oil")
        after = as_of(archive, "2026-11-09T10:11:00Z", "collector", stream="oil")
        self.assertEqual(before["items"], [])  # the earlier result is retained
        self.assertEqual([i["revision_id"] for i in after["items"]], [revs[1]["revision_id"]])
        self.assertEqual(after["items"][0]["classification"]["stream_memberships"], ["oil"])

    def test_category_order_and_duplicates_are_not_revisions(self):
        def two(cats):
            body = rss(item("Quarterly results (SYNTHETIC)", "https://oilprice.com/a/q3", "q3", desc="x")
                       .replace("</item>", "".join(f"<category>{c}</category>" for c in cats) + "</item>"))
            return serve({OILPRICE.url: body}, fallback=lambda url, timeout: FetchResponse(404, b""))
        self.collect(fetch=two(["Crude", "Energy"]), only_sources=["oilprice_com"])
        self.clock.set("2026-11-09T15:15:00Z")
        m = self.collect(fetch=two(["Energy", "Crude", "Energy"]), only_sources=["oilprice_com"]).manifest
        self.assertEqual((m["counts"]["new_revisions"], m["counts"]["unchanged"]), (0, 1))


class FetchWorkerBound(unittest.TestCase):
    def test_successive_budget_expired_cycles_never_exceed_the_worker_limit(self):
        release = threading.Event()
        active, peak, lock = [0], [0], threading.Lock()

        def hang(url, timeout):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                release.wait(10)
                return FetchResponse(200, rss())
            finally:
                with lock:
                    active[0] -= 1
        pool = FetchPool(4)
        sources = list(config.SOURCES.values())[:6]
        clock = Clock()
        try:
            first = fetch_all(sources, hang, clock, no_sleep, budget_s=0.2, pool=pool)
            second = fetch_all(sources, hang, clock, no_sleep, budget_s=0.2, pool=pool)
            time.sleep(0.1)
            self.assertLessEqual(peak[0], 4)          # the unrepaired code reached 8 here
            self.assertLessEqual(pool.threads(), 4)
            self.assertEqual(pool.busy(), 4)
            self.assertTrue(all(r.status == "budget_exceeded" for r in [*first.values(), *second.values()]))
            self.assertTrue(all("never started" in r.error for r in second.values()))
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while pool.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(pool.busy(), 0)

    def test_carried_over_workers_are_recorded_in_the_manifest(self):
        release = threading.Event()
        pool = FetchPool(4)
        pool.submit(release.wait, 10)  # a straggler from an earlier cycle
        try:
            with ArchiveTmp() as case:
                m = case.collect(fetch_pool=pool).manifest
            self.assertEqual(m["counts"]["fetch_workers_busy_at_start"], 1)
        finally:
            release.set()


class ArchiveTmp(ArchiveCase):
    """ArchiveCase usable as a context manager inside other tests."""

    def __init__(self):
        super().__init__("setUp")

    def __enter__(self):
        self.setUp()
        return self

    def __exit__(self, *exc):
        self.tearDown()


class SlowManifestStore(LocalObjectStore):
    """The manifest write only becomes durable when the commit clock reaches `durable_at`."""

    def __init__(self, root, commit_clock, durable_at):
        super().__init__(root)
        self.commit_clock, self.durable_at = commit_clock, durable_at

    def put(self, key, data, content_type="application/octet-stream"):
        super().put(key, data, content_type)
        if key.endswith("/manifest.json"):
            self.commit_clock.set(self.durable_at)


class CommitBoundary(ArchiveCase):
    def test_archive_availability_waits_for_the_durable_manifest(self):
        self.clock.set("2026-11-09T10:05:00Z")
        commit_clock = Clock("2026-11-09T10:06:00Z")
        store = SlowManifestStore(self.root / "store", commit_clock, "2026-11-09T10:07:00Z")
        m = self.collect(store=store, commit_clock=commit_clock, rebuild_index=False).manifest
        self.assertEqual(m["committed_at"], "2026-11-09T10:07:00Z")
        archive = load_archive(self.store)
        for cutoff, expected in (("2026-11-09T10:06:00Z", 0), ("2026-11-09T10:06:30Z", 0),
                                 ("2026-11-09T10:07:00Z", 10)):
            self.assertEqual(len(as_of(archive, cutoff, "archive")["items"]), expected, cutoff)
        self.assertEqual(len(as_of(archive, "2026-11-09T10:06:00Z", "collector")["items"]), 10)
        self.assertNotIn("committed_at", json.loads(self.store.get(m["objects"]["observations"]["key"]
                                                                   .replace("observations.jsonl.gz", "manifest.json"))))

    def test_crash_between_manifest_and_commit_record(self):
        self.clock.set("2026-11-09T10:05:00Z")
        with self.assertRaises(RuntimeError):
            self.collect(store=FailingStore(self.root / "store", fail_on="commit.json"))
        scan = scan_history(self.store)
        self.assertEqual((len(scan.runs), scan.problems, len(scan.availability_pending)), (1, [], 1))
        archive = load_archive(self.store)
        self.assertEqual(len(as_of(archive, "2026-11-09T12:00:00Z", "collector")["items"]), 10)
        self.assertEqual(as_of(archive, "2026-11-09T12:00:00Z", "archive")["items"], [])

        self.clock.set("2026-11-09T10:20:00Z")
        report = self.collect()
        self.assertEqual(report.manifest["counts"]["new_items"], 0)  # dedup survived
        first = scan_history(self.store).runs[0]
        self.assertEqual(first.committed_at, "2026-11-09T10:20:00Z")  # stamped late, never early

    def test_commit_record_without_spool_is_settled_by_the_next_writer(self):
        self.clock.set("2026-11-09T10:05:00Z")
        with self.assertRaises(RuntimeError):
            self.collect(store=FailingStore(self.root / "store", fail_on="commit.json"))
        for run_id in pending_spools(self.spool):  # the spool is lost with the old machine
            for p in (self.spool / run_id).iterdir():
                p.unlink()
            (self.spool / run_id).rmdir()
        self.clock.set("2026-11-09T10:30:00Z")
        report = self.collect()
        self.assertEqual(len(report.settled), 1)
        self.assertEqual(scan_history(self.store).runs[0].committed_at, "2026-11-09T10:30:00Z")

    def test_tampered_commit_record_is_corrupt_history(self):
        report = self.collect()
        key = report.manifest["objects"]["observations"]["key"].replace("observations.jsonl.gz", "commit.json")
        rec = json.loads(self.store.get(key))
        rec["committed_at"] = "2026-11-09T09:00:00Z"
        rec["manifest_sha256"] = "0" * 64
        self.store.put(key, json.dumps(rec).encode())
        # Behind the checkpoint frontier: found by the writer's scheduled full listing (FULL_VERIFY_S).
        self.clock.set("2026-11-10T15:30:00Z")
        with self.assertRaises(HistoryCorrupt):
            self.collect()


class InterruptedCleanup(ArchiveCase):
    def test_restart_recognizes_the_committed_run_before_reading_the_spool(self):
        real_clear = archive_mod._clear_spool

        def crash_mid_cleanup(d):
            (d / "observations.jsonl.gz").unlink()
            raise RuntimeError("simulated crash during spool cleanup")
        with mock.patch.object(archive_mod, "_clear_spool", crash_mid_cleanup):
            with self.assertRaises(RuntimeError):
                self.collect()
        [run_id] = pending_spools(self.spool)
        self.assertEqual([r.manifest["run_id"] for r in scan_history(self.store).runs], [run_id])

        self.assertIs(archive_mod._clear_spool, real_clear)
        for minute in ("15", "30"):  # every later restart works, not just the first
            self.clock.set(f"2026-11-09T15:{minute}:00Z")
            report = self.collect()
            self.assertEqual(report.manifest["counts"]["new_items"], 0)
        self.assertEqual(pending_spools(self.spool), [])
        runs = scan_history(self.store).runs
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[0].committed_at, "2026-11-09T15:00:00Z")  # original availability kept

    def test_uncommitted_run_with_a_missing_spool_object_fails_visibly(self):
        crashing = FailingStore(self.root / "store", fail_on="manifest.json")
        with self.assertRaises(RuntimeError):
            self.collect(store=crashing)
        [run_id] = pending_spools(self.spool)
        (self.spool / run_id / "classifications.jsonl.gz").unlink()
        with self.assertRaises(ArchiveIntegrityError):
            self.collect()


if __name__ == "__main__":
    unittest.main()
