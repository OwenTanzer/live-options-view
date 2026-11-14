import gzip
import json
import tempfile
import unittest
from pathlib import Path

from test_news_support import Clock, FailingStore, feeds, no_sleep
from market_data.news import config
from market_data.news.adapters.rss import FetchError, FetchResponse
from market_data.news.archive import (LATEST_KEY, LEASE_KEY, ArchiveIntegrityError, HistoryCorrupt,
                                      LocalObjectStore, WriterBusy, WriterLease, collect_once, decode_jsonl_gz,
                                      enrich_pending, identity_state, pending_spools, scan_history)
from market_data.news.normalize import sha256_hex
from market_data.news.reader import as_of, load_archive, rebuild_indexes


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = LocalObjectStore(self.root / "store")
        self.spool = self.root / "spool"
        self.clock = Clock("2026-11-09T15:00:00Z")

    def tearDown(self):
        self._tmp.cleanup()

    def collect(self, store=None, fetch=None, **kw):
        kw.setdefault("sleep", no_sleep)
        return collect_once(store or self.store, self.spool, fetch or feeds(), self.clock, **kw)


class CommitAndReadback(ArchiveCase):
    def test_objects_read_back_with_manifest_digests(self):
        report = self.collect()
        m = report.manifest
        self.assertEqual(m["status"], "complete")
        self.assertEqual(m["counts"]["new_items"], 10)
        for meta in m["objects"].values():
            data = self.store.get(meta["key"])
            self.assertEqual((len(data), sha256_hex(data)), (meta["bytes"], meta["sha256"]))
            self.assertTrue(meta["key"].startswith(f"news/v1/runs/2026-11-09/{report.run_id}/"))
        self.assertEqual(pending_spools(self.spool), [])
        scan = scan_history(self.store)
        self.assertEqual((len(scan.runs), scan.problems, scan.uncommitted), (1, [], []))
        self.assertIsNone(self.store.get("manifest.json"))  # market manifest namespace untouched

    def test_manifest_records_provenance_and_source_status(self):
        m = self.collect().manifest
        for field in ("code_version", "adapter_version", "rules_version", "config_digest", "rules_digest",
                      "source_baselines", "started_at", "finished_at", "committed_at"):
            self.assertTrue(m[field], field)
        statuses = {s["source_id"]: s["status"] for s in m["sources"]}
        self.assertEqual({k for k, v in statuses.items() if v == "ok"},
                         {"oilprice_com", "eia_today_in_energy", "techcrunch", "ars_technica"})
        self.assertEqual(statuses["bbc_world"], "disabled")

    def test_identical_inputs_give_identical_object_bytes(self):
        digests = []
        for name in ("a", "b"):
            store = LocalObjectStore(self.root / name)
            m = collect_once(store, self.root / f"spool-{name}", feeds(), Clock("2026-11-09T15:00:00Z"),
                             run_id="fixed-run", sleep=no_sleep).manifest
            digests.append({k: v["sha256"] for k, v in m["objects"].items()})
        self.assertEqual(digests[0], digests[1])


class RepeatsRevisionsAndLateArrivals(ArchiveCase):
    def test_duplicate_fetch_is_coverage_evidence_not_a_revision(self):
        self.collect()
        self.clock.set("2026-11-09T15:15:00Z")
        m = self.collect().manifest
        self.assertEqual((m["counts"]["unchanged"], m["counts"]["new_items"], m["counts"]["new_revisions"]), (10, 0, 0))
        self.assertEqual(m["objects"]["observations"]["records"], 0)
        self.assertEqual(len(identity_state(scan_history(self.store).runs)), 10)

    def test_revised_story_is_detected_after_previous_collection(self):
        self.collect(fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        self.clock.set("2026-11-09T15:15:00Z")
        m = self.collect(fetch=feeds("scenarios/a_v2"), only_sources=["oilprice_com"]).manifest
        self.assertEqual(m["counts"]["new_revisions"], 1)
        runs = scan_history(self.store).runs
        first, second = runs[0].observations[0], runs[1].observations[0]
        self.assertEqual(second["item_id"], first["item_id"])
        self.assertEqual(second["previous_revision_id"], first["revision_id"])
        self.assertEqual(second["first_seen_at"], first["observed_at"])
        self.assertIn("crew evacuated", second["title"])

    def test_late_arrival_partitioned_by_observation_date(self):
        self.collect(only_sources=["eia_today_in_energy"])
        runs = scan_history(self.store).runs
        late = [o for o in runs[0].observations if o["published_at"].startswith("2026-11-06")]
        self.assertEqual(len(late), 1)
        self.assertIn("/runs/2026-11-09/", runs[0].manifest["objects"]["observations"]["key"])
        archive = load_archive(self.store)
        self.assertEqual(len(as_of(archive, "2026-11-07T00:00:00Z", "collector")["items"]), 0)
        hindsight = as_of(archive, "2026-11-07T00:00:00Z", "publication")
        self.assertTrue(hindsight["hindsight"])
        self.assertEqual([i["item_id"] for i in hindsight["items"]], [late[0]["item_id"]])


class Failures(ArchiveCase):
    def test_partial_source_failure_is_recorded_not_empty_success(self):
        base = feeds()
        failing = {config.SOURCES["techcrunch"].url: FetchResponse(503, b""),
                   config.SOURCES["ars_technica"].url: FetchError("timed out")}

        def fetch(url, timeout):
            r = failing.get(url)
            if isinstance(r, Exception):
                raise r
            return r or base(url, timeout)
        m = self.collect(fetch=fetch).manifest
        rows = {s["source_id"]: s for s in m["sources"]}
        self.assertEqual(m["status"], "partial")
        self.assertEqual((rows["techcrunch"]["status"], rows["techcrunch"]["attempts"]), ("http_error", 3))
        self.assertEqual((rows["ars_technica"]["status"], rows["ars_technica"]["attempts"]), ("fetch_error", 3))
        self.assertEqual(m["counts"]["new_items"], 5)
        coverage = as_of(load_archive(self.store), "2026-11-09T16:00:00Z")["coverage"]
        self.assertEqual({f["source_id"] for f in coverage["source_failures"]}, {"techcrunch", "ars_technica"})

    def test_all_sources_failing_commits_a_failed_run(self):
        m = self.collect(fetch=lambda url, timeout: FetchResponse(500, b"")).manifest
        self.assertEqual((m["status"], m["counts"]["sources_ok"]), ("failed", 0))

    def test_crash_before_manifest_publishes_nothing_and_retry_reuses_run_id(self):
        crashing = FailingStore(self.root / "store", fail_on="manifest.json")
        with self.assertRaises(RuntimeError):
            self.collect(store=crashing)
        scan = scan_history(self.store)
        self.assertEqual(len(scan.runs), 0)
        self.assertEqual(len(scan.uncommitted), 1)
        self.assertEqual(as_of(load_archive(self.store), "2026-11-09T16:00:00Z")["items"], [])
        [spooled] = pending_spools(self.spool)
        self.assertEqual(scan.uncommitted[0]["run_id"], spooled)

        self.clock.set("2026-11-09T15:15:00Z")
        report = self.collect()  # fresh store object: a restarted process
        self.assertEqual(report.recovered, [spooled])
        runs = scan_history(self.store).runs
        self.assertEqual([r.manifest["run_id"] for r in runs], [spooled, report.run_id])
        self.assertEqual(runs[0].manifest["committed_at"], "2026-11-09T15:15:00Z")  # archived when actually committed
        self.assertEqual(report.manifest["counts"]["new_items"], 0)  # no duplicates after recovery

    def test_crash_after_manifest_leaves_history_and_index_is_rebuildable(self):
        crashing = FailingStore(self.root / "store", fail_on="news/v1/index/")
        report = self.collect(store=crashing)
        self.assertIn("simulated crash", report.index_error)
        self.assertIsNone(self.store.get("news/v1/index/2026-11-09.json"))
        self.assertEqual(len(scan_history(self.store).runs), 1)
        rebuild_indexes(self.store, self.clock)
        index = json.loads(self.store.get("news/v1/index/2026-11-09.json"))
        self.assertEqual([r["run_id"] for r in index["runs"]], [report.run_id])

    def test_lost_indexes_rebuild_identically_from_manifests(self):
        self.collect()
        self.clock.set("2026-11-09T15:15:00Z")
        self.collect(fetch=feeds("scenarios/a_v2"), only_sources=["oilprice_com"])
        before = {k: json.loads(self.store.get(k)) for k in ("news/v1/index/2026-11-09.json", LATEST_KEY)}
        for key in before:
            self.store.delete(key)
        rebuild_indexes(self.store, self.clock)
        after = {k: json.loads(self.store.get(k)) for k in before}
        self.assertEqual(before, after)

    def test_corrupt_object_is_visible_and_stops_collection(self):
        m = self.collect().manifest
        key = m["objects"]["observations"]["key"]
        records = decode_jsonl_gz(self.store.get(key))
        records[0]["title"] = "tampered"
        self.store.put(key, gzip.compress("".join(json.dumps(r) + "\n" for r in records).encode(), mtime=0))
        scan = scan_history(self.store)
        self.assertEqual(len(scan.runs), 0)
        self.assertIn("digest", scan.problems[0]["detail"])
        self.assertEqual(as_of(load_archive(self.store), "2026-11-09T16:00:00Z")["coverage"]["corrupt_runs"],
                         [m["run_id"]])
        self.clock.set("2026-11-09T15:15:00Z")
        with self.assertRaises(HistoryCorrupt):
            self.collect()

    def test_conflicting_existing_object_is_not_overwritten(self):
        crashing = FailingStore(self.root / "store", fail_on="manifest.json")
        with self.assertRaises(RuntimeError):
            self.collect(store=crashing)
        [run_id] = pending_spools(self.spool)
        [key] = [k for k in self.store.list("news/v1/runs/") if k.endswith("observations.jsonl.gz")]
        self.store.put(key, b"something else")
        with self.assertRaises(ArchiveIntegrityError):
            self.collect()
        self.assertEqual(pending_spools(self.spool), [run_id])

    def test_enrichment_failure_still_archives_observations(self):
        def broken(*_a, **_k):
            raise RuntimeError("classifier unavailable")
        m = self.collect(classifier=broken).manifest
        self.assertEqual((m["enrichment"]["status"], m["objects"]["observations"]["records"]), ("failed", 10))
        self.assertEqual(len(m["enrichment"]["pending_revision_ids"]), 10)
        self.clock.set("2026-11-09T15:20:00Z")
        report = enrich_pending(self.store, self.spool, self.clock)
        self.assertEqual((report.manifest["run_kind"], report.manifest["enrichment"]["classified"]), ("enrich", 10))
        self.assertIsNone(enrich_pending(self.store, self.spool, self.clock))


class ReplayAndWriter(ArchiveCase):
    def test_fresh_process_reconstructs_identity_state(self):
        self.collect()
        self.clock.set("2026-11-09T15:15:00Z")
        self.collect(fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        first = identity_state(scan_history(self.store).runs)
        second = identity_state(scan_history(LocalObjectStore(self.root / "store")).runs)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 11)

    def test_single_writer_lease(self):
        with WriterLease(self.store, self.clock, holder="other"):
            with self.assertRaises(WriterBusy):
                self.collect()
        self.assertIsNone(self.store.get(LEASE_KEY))

    def test_expired_lease_needs_explicit_break(self):
        # A process that died without releasing its lease.
        self.store.put_if_absent(LEASE_KEY, json.dumps({"holder": "crashed", "expires_at": "2026-11-09T12:01:00Z"}).encode())
        with self.assertRaisesRegex(WriterBusy, "expired"):
            self.collect()
        self.assertEqual(self.collect(break_stale_lease=True).manifest["status"], "complete")

    def test_missed_intervals_listed_in_daily_index(self):
        self.collect()
        self.clock.set("2026-11-09T16:30:00Z")
        self.collect()
        index = json.loads(self.store.get("news/v1/index/2026-11-09.json"))
        self.assertEqual(len(index["missed_intervals"]), 1)
        self.assertEqual(index["missed_intervals"][0]["gap_seconds"], 5400)


if __name__ == "__main__":
    unittest.main()
