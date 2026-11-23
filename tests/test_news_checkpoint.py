"""History checkpoint: disposable, verifiable, and equal to full replay (synthetic fixtures, local stores).

Full replay (scan_history / load_archive(use_checkpoint=False)) is the reference throughout: every
comparison is checkpoint-plus-tail against it, across revisions, coverage and known-at behavior.
"""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_news_support import Clock, FailingStore, feeds, no_sleep
from market_data.news import checkpoint as ck
from market_data.news import config
from market_data.news.archive import HistoryCorrupt, LocalObjectStore, collect_once, enrich_pending, scan_history
from market_data.news.join import join
from market_data.news.prices import load_price_file
from market_data.news.reader import MODES, as_of, load_archive
from market_data.news.views import build_views
from market_data.news import publish as pub

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "tests" / "fixtures" / "news" / "prices" / "qqq_2026-11-09.synthetic.jsonl"
CUTOFFS = [f"2026-11-09T{h:02d}:{m:02d}:{s:02d}Z" for h in (14, 15, 16, 17) for m in range(0, 60, 5) for s in (0, 30)]
STREAMS = (None, "oil", "ai_buildout")


class CountingStore(LocalObjectStore):
    """Counts the operations a bucket would bill: object reads (and their bytes) and listings."""

    def __init__(self, root):
        super().__init__(root)
        self.reset()
        self._inner = False

    def reset(self):
        self.gets, self.get_bytes, self.listings, self.puts = 0, 0, 0, 0

    def get(self, key):
        data = super().get(key)
        self.gets += 1
        self.get_bytes += len(data or b"")
        return data

    def put(self, key, data, content_type="application/octet-stream"):
        self.puts += 1
        super().put(key, data, content_type)

    def list(self, prefix):
        if not self._inner:
            self.listings += 1
        return super().list(prefix)

    def list_meta(self, prefix, start_after=None):
        self.listings += 1
        self._inner = True
        try:
            return super().list_meta(prefix, start_after)
        finally:
            self._inner = False


def shape(scan):
    return ([(r.manifest_key, r.manifest_sha256, r.manifest, r.observations, r.classifications) for r in scan.runs],
            scan.problems, scan.uncommitted)


def archive_shape(a):
    return a.observations, a.classifications, a.runs, a.problems, a.uncommitted


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CountingStore(self.root / "store")
        self.spool = self.root / "spool"
        self.clock = Clock("2026-11-09T15:00:00Z")

    def tearDown(self):
        self._tmp.cleanup()

    def collect(self, at, store=None, fetch=None, **kw):
        self.clock.set(at)
        kw.setdefault("sleep", no_sleep)
        return collect_once(store or self.store, self.spool, fetch or feeds(), self.clock, **kw)

    def assert_equal_to_full_replay(self, *, derived=True):
        """Checkpoint-plus-tail and full replay agree on history and everything read from it."""
        full, fast = scan_history(self.store), ck.read_history(self.store)
        self.assertEqual(shape(fast), shape(full))
        ref, got = load_archive(self.store, use_checkpoint=False), load_archive(self.store)
        self.assertEqual(archive_shape(got), archive_shape(ref))
        if derived:
            prices = load_price_file(PRICES)
            for cutoff in CUTOFFS:
                for mode in MODES:
                    for stream in STREAMS:
                        self.assertEqual(as_of(got, cutoff, mode, stream), as_of(ref, cutoff, mode, stream),
                                         (cutoff, mode, stream))
                    self.assertEqual(join(got, prices, cutoff, mode, tolerance_s=120),
                                     join(ref, prices, cutoff, mode, tolerance_s=120), (cutoff, mode))
            kw = dict(as_of="2026-11-09T17:00:00Z", tolerance_s=120, data_label="SYNTHETIC", dates=["2026-11-09"])
            self.assertEqual(build_views(got, pub.unavailable_prices(), **kw),
                             build_views(ref, pub.unavailable_prices(), **kw))
        return fast.checkpoint


class EqualToFullReplay(Case):
    def test_every_step_of_a_mixed_history(self):
        """Revisions, a missing commit record, late classification, a source failure, an uncommitted run."""
        modes = []

        def step():
            modes.append(self.assert_equal_to_full_replay()["mode"])

        self.collect("2026-11-09T15:00:00Z", commit_clock=Clock("2026-11-09T15:00:40Z"))
        step()
        self.collect("2026-11-09T15:15:00Z", fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        step()
        self.collect("2026-11-09T15:30:00Z", fetch=feeds("scenarios/a_v2"), only_sources=["oilprice_com"])
        step()
        # A crash between the manifest and commit.json: committed, availability pending (archive mode excludes it).
        with self.assertRaises(RuntimeError):
            self.collect("2026-11-09T15:45:00Z", store=FailingStore(self.store.root, fail_on="commit.json"),
                         fetch=feeds("scenarios/c"), only_sources=["oilprice_com"])
        step()

        def unavailable(*_a, **_k):
            raise RuntimeError("enrichment unavailable")
        # One source with a fixture, one without (a failure in coverage); classification unavailable.
        report = self.collect("2026-11-09T16:00:00Z", fetch=feeds("scenarios/b"), classifier=unavailable,
                              only_sources=["eia_today_in_energy", "techcrunch"])
        self.assertEqual(report.manifest["status"], "partial")
        self.assertEqual(len(report.recovered), 1, "the 15:45 run's availability is recorded at 16:00")
        step()
        enrich_pending(self.store, self.spool, Clock("2026-11-09T16:12:00Z"))   # classified later: known-at
        step()
        # A crash before the manifest: objects without a manifest (uncommitted) and a pending spool.
        with self.assertRaises(RuntimeError):
            self.collect("2026-11-09T16:30:00Z", store=FailingStore(self.store.root, fail_on="manifest.json"),
                         fetch=feeds("scenarios/d"), only_sources=["oilprice_com"])
        self.assertTrue(scan_history(self.store).uncommitted)
        step()
        report = self.collect("2026-11-09T16:45:00Z")   # recovers the uncommitted run from its spool
        self.assertTrue(report.recovered)
        read = self.assert_equal_to_full_replay()
        self.assertEqual(read["mode"], "checkpoint")
        self.assertGreater(read["runs_from_checkpoint"], 5)
        self.assertEqual(read["runs_read"], 0, "every settled run is in the checkpoint")
        self.assertEqual(modes[0], "checkpoint")
        self.assertTrue(all(m == "checkpoint" for m in modes), modes)

    def test_the_checkpoint_holds_settled_runs_only(self):
        self.collect("2026-11-09T15:00:00Z")
        with self.assertRaises(RuntimeError):
            self.collect("2026-11-09T15:15:00Z", store=FailingStore(self.store.root, fail_on="commit.json"),
                         fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        head, body, why = ck.load_checkpoint(self.store)
        self.assertEqual(why, "")
        self.assertEqual(len(body["runs"]), 1)
        read = ck.read_history(self.store).checkpoint
        self.assertEqual((read["runs_from_checkpoint"], read["runs_read"]), (1, 1), "the pending run is the tail")
        self.collect("2026-11-09T15:30:00Z")
        self.assertEqual(ck.load_checkpoint(self.store)[0]["runs"], 3)
        self.assert_equal_to_full_replay(derived=False)


class Fallbacks(Case):
    """Missing, corrupt, incompatible or stale: full replay, then the next writer rebuilds it."""

    def setUp(self):
        super().setUp()
        self.collect("2026-11-09T15:00:00Z")
        self.collect("2026-11-09T15:15:00Z", fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        self.head = json.loads(self.store.get(ck.HEAD_KEY))

    def assert_falls_back_then_rebuilds(self, reason_prefix):
        read = self.assert_equal_to_full_replay(derived=False)
        self.assertEqual(read["mode"], "full_replay")
        self.assertTrue(read["reason"].startswith(reason_prefix), read["reason"])
        report = self.collect("2026-11-09T15:30:00Z", fetch=feeds("scenarios/a_v2"), only_sources=["oilprice_com"])
        self.assertEqual(report.checkpoint["mode"], "full_replay")
        self.assertTrue(report.checkpoint["saved"], report.checkpoint)
        after = self.assert_equal_to_full_replay()
        self.assertEqual((after["mode"], after["runs_read"]), ("checkpoint", 0))

    def test_missing(self):
        self.store.delete(ck.HEAD_KEY)
        self.assert_falls_back_then_rebuilds("missing")

    def test_head_unreadable(self):
        self.store.put(ck.HEAD_KEY, b"{not json")
        self.assert_falls_back_then_rebuilds("corrupt: head unreadable")

    def test_body_missing(self):
        self.store.delete(self.head["key"])
        self.assert_falls_back_then_rebuilds("corrupt: body missing")

    def test_body_bytes_damaged(self):
        data = bytearray(self.store.get(self.head["key"]))
        data[len(data) // 2] ^= 0xFF
        self.store.put(self.head["key"], bytes(data))
        self.assert_falls_back_then_rebuilds("corrupt: body size/digest mismatch")

    def test_body_with_a_consistent_digest_but_the_wrong_shape(self):
        """A buggy writer: head and body agree byte-wise, but the body drops a run's records."""
        body = json.loads(gzip.decompress(self.store.get(self.head["key"])))
        body["runs"][0]["observations"] = body["runs"][0]["observations"][:-1]
        data = ck.encode_body(body)
        key = f"{ck.BODY_PREFIX}{ck.sha256_hex(data)}.json.gz"
        self.store.put(key, data)
        self.store.put(ck.HEAD_KEY, json.dumps(dict(self.head, key=key, bytes=len(data),
                                                    sha256=ck.sha256_hex(data))).encode())
        self.assert_falls_back_then_rebuilds("corrupt: body entry")

    def test_incompatible_schema(self):
        self.store.put(ck.HEAD_KEY, json.dumps(dict(self.head, schema_version="news-history-checkpoint/v0")).encode())
        self.assert_falls_back_then_rebuilds("incompatible")

    def test_incompatible_after_a_manifest_schema_change(self):
        with mock.patch.object(config, "READABLE_MANIFEST_SCHEMAS", config.READABLE_MANIFEST_SCHEMAS + ("x/v9",)):
            self.assert_falls_back_then_rebuilds("incompatible")

    def test_writers_list_everything_after_the_full_verification_interval(self):
        later = "2026-11-10T15:15:01Z"    # 24 h after the 15:15 full verification
        early = ck.read_history(self.store, Clock("2026-11-10T15:00:00Z")).checkpoint
        self.assertEqual((early["mode"], early["listing"]), ("checkpoint", "after_frontier"))
        read = ck.read_history(self.store, Clock(later)).checkpoint
        # A full LISTING compared run by run with the checkpoint, not a content replay of every object.
        self.assertEqual((read["mode"], read["listing"], read["runs_read"]), ("checkpoint", "full", self.head["runs"]), read)
        self.assertTrue(read["reason"].startswith("full listing verification"), read)
        self.assertEqual(ck.read_history(self.store).checkpoint["listing"], "after_frontier", "readers do not age it")
        report = self.collect(later)
        self.assertEqual((report.checkpoint["mode"], report.checkpoint["listing"]), ("checkpoint", "full"))
        self.assertEqual(json.loads(self.store.get(ck.HEAD_KEY))["full_verified_at"], later)


class ChangedHistoryIsReverifiedOrFailsVisibly(Case):
    """A run at or before the frontier is not listed on every read (bounded listing). A change to it is
    found by the next full listing: a writer's, at most FULL_VERIFY_S after the last one, or an
    operator's (bounded=False, `checkpoint compare`). It is then re-read and fails visibly."""

    LATER = "2026-11-10T15:30:00Z"   # past FULL_VERIFY_S since the 15:00 full verification

    def setUp(self):
        super().setUp()
        self.first = self.collect("2026-11-09T15:00:00Z").manifest
        self.collect("2026-11-09T15:15:00Z", fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        self.prefix = self.first["objects"]["observations"]["key"].rsplit("/", 1)[0] + "/"
        head = json.loads(self.store.get(ck.HEAD_KEY))
        self.assertGreaterEqual(head["frontier"], self.prefix, "the first run is behind the frontier")

    def assert_bounded_read_does_not_list_it(self):
        read = ck.read_history(self.store)
        self.assertEqual((read.checkpoint["listing"], read.checkpoint["reverified"], read.problems),
                         ("after_frontier", [], []))

    def test_tampered_commit_record_is_reverified_and_corrupt(self):
        rec = json.loads(self.store.get(self.prefix + "commit.json"))
        self.store.put(self.prefix + "commit.json",
                       json.dumps(dict(rec, committed_at="2026-11-09T09:00:00Z", manifest_sha256="0" * 64)).encode())
        self.assert_bounded_read_does_not_list_it()
        read = ck.read_history(self.store, bounded=False)
        self.assertEqual(read.checkpoint["reverified"], [self.first["run_id"]])
        self.assertEqual(shape(read), shape(scan_history(self.store)))
        self.assertEqual([p["run_id"] for p in read.problems], [self.first["run_id"]])
        with self.assertRaises(HistoryCorrupt):
            self.collect(self.LATER)

    def test_rewritten_object_is_reverified_and_corrupt(self):
        key = self.prefix + "observations.jsonl.gz"
        self.store.put(key, gzip.compress(b'{"forged": true}\n', mtime=0))
        self.assert_bounded_read_does_not_list_it()
        read = ck.read_history(self.store, bounded=False)
        self.assertEqual(read.checkpoint["reverified"], [self.first["run_id"]])
        self.assertEqual(read.problems[0]["detail"], "observations: size/digest mismatch")
        with self.assertRaises(HistoryCorrupt):
            self.collect(self.LATER)

    def test_identical_rewrite_is_reverified_and_still_clean(self):
        key = self.prefix + "classifications.jsonl.gz"
        self.store.put(key, self.store.get(key))
        read = ck.read_history(self.store, bounded=False)
        self.assertEqual((read.checkpoint["reverified"], read.problems), ([self.first["run_id"]], []))
        report = self.collect(self.LATER)
        self.assertEqual(report.checkpoint["listing"], "full", "the writer's scheduled full listing")
        self.assertEqual(ck.read_history(self.store, bounded=False).checkpoint["reverified"], [], "re-checkpointed")

    def test_checkpointed_run_that_disappears_fails_visibly(self):
        """Full replay alone would silently lose the run; a full listing against the checkpoint shows it."""
        for name in ck.RUN_NAMES:
            self.store.delete(self.prefix + name)
        full = scan_history(self.store)
        self.assertEqual(full.problems, [], "full replay cannot tell a run is gone")
        self.assert_bounded_read_does_not_list_it()
        read = ck.read_history(self.store, bounded=False)
        self.assertEqual(read.problems, [{"run_id": self.first["run_id"], "prefix": self.prefix,
                                          "detail": "checkpointed run is missing from the store"}])
        with self.assertRaises(HistoryCorrupt):
            self.collect(self.LATER)
        self.assertEqual(ck.compare(self.store)["equal"], False, "the operator check shows it too")


class Interruption(Case):
    def setUp(self):
        super().setUp()
        self.collect("2026-11-09T15:00:00Z")
        self.head_before = self.store.get(ck.HEAD_KEY)

    def test_crash_writing_the_body_keeps_the_cycle_and_the_old_checkpoint(self):
        report = self.collect("2026-11-09T15:15:00Z", store=FailingStore(self.store.root, fail_on="checkpoint/history/"),
                              fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        self.assertEqual(report.manifest["status"], "complete", "the run is committed regardless")
        self.assertFalse(report.checkpoint["saved"])
        self.assertIn("simulated crash", report.checkpoint["save_error"])
        self.assertEqual(self.store.get(ck.HEAD_KEY), self.head_before)
        read = self.assert_equal_to_full_replay(derived=False)
        self.assertEqual((read["mode"], read["runs_from_checkpoint"], read["runs_read"]), ("checkpoint", 1, 1))
        self.collect("2026-11-09T15:30:00Z")
        self.assertEqual(ck.read_history(self.store).checkpoint["runs_read"], 0)

    def test_crash_between_body_and_head_leaves_an_orphan_the_next_save_removes(self):
        report = self.collect("2026-11-09T15:15:00Z", store=FailingStore(self.store.root, fail_on="checkpoint/head.json"),
                              fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        self.assertFalse(report.checkpoint["saved"])
        self.assertEqual(len(self.store.list(ck.BODY_PREFIX)), 2, "old body plus the orphan")
        self.assertEqual(self.store.get(ck.HEAD_KEY), self.head_before)
        self.assert_equal_to_full_replay(derived=False)
        self.collect("2026-11-09T15:30:00Z")
        self.collect("2026-11-09T15:45:00Z")
        head = json.loads(self.store.get(ck.HEAD_KEY))
        self.assertEqual(sorted(self.store.list(ck.BODY_PREFIX)), sorted([head["key"], head["previous_key"]]))

    def test_a_body_that_does_not_read_back_is_never_pointed_at(self):
        class Damaging(LocalObjectStore):
            def put(self, key, data, content_type="application/octet-stream"):
                super().put(key, data[:-7] if key.startswith(ck.BODY_PREFIX) else data, content_type)
        report = self.collect("2026-11-09T15:15:00Z", store=Damaging(self.store.root))
        self.assertFalse(report.checkpoint["saved"])
        self.assertIn("did not read back", report.checkpoint["save_error"])
        self.assertEqual(self.store.get(ck.HEAD_KEY), self.head_before)
        self.assertEqual(ck.read_history(self.store).checkpoint["mode"], "checkpoint")

    def test_a_stray_object_in_a_run_keeps_that_run_in_the_tail(self):
        """Readers ignore unknown objects; the checkpoint stays usable and reads that run in full."""
        prefix = scan_history(self.store).runs[0].manifest_key[:-len("manifest.json")]
        self.store.put(prefix + "notes.txt", b"operator note")
        self.collect("2026-11-09T15:15:00Z")
        read = self.assert_equal_to_full_replay(derived=False)
        # Behind the frontier it is not listed, so the bounded read does not see the stray object.
        self.assertEqual((read["mode"], read["listing"], read["runs_read"]), ("checkpoint", "after_frontier", 0))
        # The next full listing does: that run is read in full and kept out of the checkpoint, and the
        # frontier stops before it, visibly.
        self.collect("2026-11-10T15:30:00Z")
        head = json.loads(self.store.get(ck.HEAD_KEY))
        self.assertEqual(head["frontier_stopped_at"], prefix)
        self.assertIsNone(head["frontier"])
        read = self.assert_equal_to_full_replay(derived=False)
        self.assertEqual((read["listing"], read["runs_read"]), ("full", 1))

    def test_a_reader_holding_the_previous_head_still_finds_its_body(self):
        self.collect("2026-11-09T15:15:00Z", fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        old = json.loads(self.head_before)
        self.assertIsNotNone(self.store.get(old["key"]))
        self.store.put(ck.HEAD_KEY, self.head_before)   # what a reader that read the old head would load
        read = self.assert_equal_to_full_replay(derived=False)
        self.assertEqual((read["mode"], read["runs_read"]), ("checkpoint", 1))

    def test_readers_never_write(self):
        self.store.delete(ck.HEAD_KEY)
        before = {k: self.store.get(k) for k in self.store.list("")}
        self.store.reset()
        load_archive(self.store)
        ck.read_history(self.store)
        self.assertEqual(self.store.puts, 0)
        self.assertEqual({k: self.store.get(k) for k in self.store.list("")}, before)

    def test_disabled_checkpoint_is_full_replay_and_writes_nothing(self):
        self.store.delete(ck.HEAD_KEY)
        report = self.collect("2026-11-09T15:15:00Z", use_checkpoint=False)
        self.assertEqual((report.checkpoint["mode"], report.checkpoint["saved"]), ("full_replay", False))
        self.assertIsNone(self.store.get(ck.HEAD_KEY))

    def test_the_archive_is_never_pruned_or_rewritten(self):
        runs_before = {k: self.store.get(k) for k in self.store.list("news/v1/runs/")}
        for minute in ("15", "30", "45"):
            self.collect(f"2026-11-09T15:{minute}:00Z")
        self.store.delete(ck.HEAD_KEY)
        self.collect("2026-11-09T16:00:00Z")
        runs_after = {k: self.store.get(k) for k in self.store.list("news/v1/runs/")}
        self.assertEqual({k: runs_after[k] for k in runs_before}, runs_before)
        self.assertEqual(len(runs_after), len(runs_before) + 4 * 4)


class ReadCounts(Case):
    """Object reads per history read, and per cycle, as history grows: full replay vs checkpoint."""

    SIZES = (6, 12, 24, 48, 96)

    def cycle(self, at, use_checkpoint):
        """A pilot cycle's private reads: collect (with the index rebuild) and loading history for the views."""
        self.store.reset()
        report = self.collect(at, use_checkpoint=use_checkpoint)
        load_archive(self.store, use_checkpoint=use_checkpoint)
        return self.store.gets, report.checkpoint

    def test_reads_stay_flat_with_the_checkpoint_and_grow_with_full_replay(self):
        rows, n, cycle_modes = [], 0, []
        start = Clock("2026-11-09T00:00:00Z")()
        from datetime import timedelta
        from market_data.news.normalize import iso
        for size in self.SIZES:
            while n < size:
                at = iso(start + timedelta(minutes=15 * n))
                fetch = feeds("scenarios/a_v1" if n % 3 == 1 else "scenarios/a_v2" if n % 3 == 2 else "feeds")
                report = self.collect(at, fetch=fetch)
                cycle_modes.append(report.checkpoint["mode"])
                n += 1
            self.store.reset()
            scan_history(self.store)
            full = (self.store.gets, self.store.get_bytes)
            self.store.reset()
            read = ck.read_history(self.store)
            fast = (self.store.gets, self.store.get_bytes)
            self.assertEqual(read.checkpoint["mode"], "checkpoint")
            self.assertEqual(shape(read), shape(scan_history(self.store)))
            runs = len(read.runs)
            # The same cycle both ways. For the M5.2 baseline the head is set aside, so the index
            # rebuild and the views also replay everything, exactly as before the checkpoint.
            head = self.store.get(ck.HEAD_KEY)
            self.store.delete(ck.HEAD_KEY)
            cycle_full, _ = self.cycle(iso(start + timedelta(minutes=15 * n)), False)
            self.store.put(ck.HEAD_KEY, head)
            cycle_fast, how = self.cycle(iso(start + timedelta(minutes=15 * n + 5)), True)
            n += 2
            body = len(self.store.get(json.loads(self.store.get(ck.HEAD_KEY))["key"]))
            rows.append((runs, full, fast, cycle_full, cycle_fast, body, how["mode"], how["reason"]))
        print("\nruns | full replay GETs (bytes) | checkpoint GETs (bytes) | cycle GETs M5.2 / checkpoint | body bytes")
        for runs, full, fast, cf, cc, body, mode, why in rows:
            note = "" if mode == "checkpoint" else f"  <- {why.split(':')[0]}: scheduled full re-verification"
            print(f"{runs:4d} | {full[0]:5d} ({full[1]:8d}) | {fast[0]:2d} ({fast[1]:7d}) | {cf:5d} / {cc:4d} | "
                  f"{body:7d}{note}", flush=True)
        self.assertEqual([r[2][0] for r in rows], [2] * len(rows), "a history read is the head and body only")
        self.assertEqual([r[1][0] for r in rows], [4 * r[0] for r in rows], "full replay reads 4 objects per run")
        steady = [r[4] for r in rows if r[6] == "checkpoint" and not r[7]]
        self.assertEqual(len(set(steady)), 1, f"a cycle's reads do not grow with history: {steady}")
        self.assertTrue(all(r[3] > 3 * 4 * r[0] for r in rows), "the M5.2 cycle replays history three times")
        # Over 24 h (98 runs) the writer lists everything once more, as scheduled: a listing, not a replay.
        self.assertEqual([r[7].split(":")[0] for r in rows if r[7]], ["full listing verification"])
        self.assertEqual(cycle_modes.count("full_replay"), 1, "only the very first cycle (no checkpoint yet)")


class Cli(Case):
    def run_cli(self, *argv):
        import contextlib
        import io
        from market_data.news.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_status_compare_rebuild(self):
        store = ["--store", str(self.store.root)]
        writer = store + ["--spool", str(self.spool)]
        code, out, _ = self.run_cli("collect-once", *writer, "--clock", "2026-11-09T15:00:00Z")
        self.assertEqual((code, json.loads(out)["history"]["saved"]), (0, True))
        code, out, _ = self.run_cli("checkpoint", "compare", *store)
        self.assertEqual((code, json.loads(out)["equal"], json.loads(out)["read"]["mode"]), (0, True, "checkpoint"))
        self.store.put(ck.HEAD_KEY, b"garbage")
        code, out, _ = self.run_cli("checkpoint", "status", *store)
        self.assertEqual(json.loads(out), {"usable": False, "reason": "corrupt: head unreadable"})
        code, out, _ = self.run_cli("checkpoint", "rebuild", *writer, "--clock", "2026-11-09T15:05:00Z")
        self.assertEqual((code, json.loads(out)["previous"], json.loads(out)["runs"]), (0, "corrupt: head unreadable", 1))
        self.assertTrue(json.loads(self.run_cli("checkpoint", "status", *store)[1])["usable"])
        code, out, _ = self.run_cli("collect-once", *writer, "--clock", "2026-11-09T15:15:00Z", "--full-replay")
        self.assertEqual(json.loads(out)["history"]["mode"], "full_replay")

    def test_rebuild_refuses_corrupt_history_and_writes_nothing(self):
        m = self.collect("2026-11-09T15:00:00Z").manifest
        self.store.put(m["objects"]["observations"]["key"].replace("observations.jsonl.gz", "commit.json"), b"{}")
        before = {k: self.store.get(k) for k in self.store.list(ck.CHECKPOINT_PREFIX)}
        code, _, err = self.run_cli("checkpoint", "rebuild", "--store", str(self.store.root), "--spool",
                                    str(self.spool), "--clock", "2026-11-09T15:05:00Z")
        self.assertEqual(code, 2)
        self.assertIn("failed verification", err)
        self.assertEqual({k: self.store.get(k) for k in self.store.list(ck.CHECKPOINT_PREFIX)}, before)


if __name__ == "__main__":
    unittest.main()


# --- Bounded listing (the frontier): long history, recovery and restart -----------------------------

import math  # noqa: E402
from datetime import timedelta  # noqa: E402

from market_data.news import archive as arc  # noqa: E402
from market_data.news.archive import CasOutcome, content_etag  # noqa: E402
from market_data.news.normalize import iso, parse_iso  # noqa: E402


class MemoryStore(LocalObjectStore):
    """The LocalObjectStore contract in memory (fast enough for thousands of runs), recording every
    listing as the ListObjectsV2 requests it would cost: one per 1,000 keys returned."""

    def __init__(self):
        super().__init__("unused-memory-store")
        self.objects, self.tags, self.n = {}, {}, 0
        self.listings = []   # (prefix, start_after, keys returned)
        self.gets = 0

    def _tag(self, key):
        self.n += 1
        self.tags[key] = f"{len(self.objects[key])}:{self.n}"

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        self.objects[arc.check_key(key)] = bytes(data)
        self._tag(key)

    def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
        if key in self.objects:
            return False
        self.put(key, data)
        return True

    def get(self, key):
        self.gets += 1
        return self.objects.get(key)

    def get_versioned(self, key):
        body = self.objects.get(key)
        return None if body is None else (body, content_etag(body))

    def put_if_match(self, key, data, etag, content_type="application/octet-stream", cache_control=None, write_id=None):
        current = self.objects.get(key)
        if (etag is None and current is not None) or (etag is not None and (current is None or content_etag(current) != etag)):
            return CasOutcome("rejected", detail="condition")
        self.put(key, data)
        return CasOutcome("applied", content_etag(data))

    def delete(self, key):
        self.objects.pop(key, None)
        self.tags.pop(key, None)

    def last_modified(self, key):
        return None

    def list(self, prefix):
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        self.listings.append((prefix, None, len(keys)))
        return keys

    def list_meta(self, prefix, start_after=None):
        keys = sorted(k for k in self.objects if k.startswith(prefix)
                      and (start_after is None or k.encode() > start_after.encode()))
        self.listings.append((prefix, start_after, len(keys)))
        return {k: self.tags[k] for k in keys}

    def run_list_requests(self):
        """ListObjectsV2 requests spent on news/v1/runs/ since the last reset."""
        return sum(max(1, math.ceil(n / 1000)) for p, _, n in self.listings if p == arc.RUNS_PREFIX)

    def run_keys_listed(self):
        return sum(n for p, _, n in self.listings if p == arc.RUNS_PREFIX)

    def reset(self):
        self.listings, self.gets = [], 0


def synthetic_run(store, spool, at, *, commit=True):
    """One committed (empty) run started at `at`, written through the real spool/commit path."""
    started = parse_iso(at)
    run_id = arc.new_run_id(started)
    draft, blobs = arc._draft(run_id, "collect", started, started, [], {}, {"status": "complete"}, [], [], "complete")
    arc.spool_run(spool, draft, blobs)
    if commit:
        arc.commit_spooled(store, spool, run_id, lambda: started + timedelta(seconds=1))
    return arc.run_prefix(draft["observation_date"], run_id)


class BoundedListing(unittest.TestCase):
    """The run listing stays bounded as raw history grows, and nothing unprocessed is ever skipped."""

    HISTORY = 1500    # runs (6,000 objects): about 16 days at */15

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.t0 = parse_iso("2026-10-24T00:00:00Z")
        built = MemoryStore()
        spool = Path(tempfile.mkdtemp(dir=cls.base))
        cls.prefixes = [synthetic_run(built, spool, iso(cls.t0 + timedelta(minutes=15 * i)))
                        for i in range(cls.HISTORY)]
        ck.rebuild(built, Clock(iso(cls.t0 + timedelta(minutes=15 * cls.HISTORY))))   # full replay once
        cls.built = built

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.store = MemoryStore()
        self.store.objects, self.store.tags, self.store.n = dict(self.built.objects), dict(self.built.tags), self.built.n
        self.spool = Path(tempfile.mkdtemp(dir=self.base))
        self.clock = Clock(iso(self.t0 + timedelta(minutes=15 * self.HISTORY)))
        self.head = json.loads(self.store.get(ck.HEAD_KEY))
        self.store.reset()

    def run_prefixes(self):
        return set(arc.group_run_keys(k for k in self.store.objects if k.startswith(arc.RUNS_PREFIX)))

    def collect(self, minutes_after_history=0, **kw):
        self.clock.set(iso(self.t0 + timedelta(minutes=15 * self.HISTORY + minutes_after_history)))
        kw.setdefault("sleep", no_sleep)
        kw.setdefault("fetch", feeds())
        return collect_once(self.store, self.spool, kw.pop("fetch"), self.clock, **kw)

    def assert_complete(self):
        """Bounded, operator full-listing and full-replay reads all agree, and nothing was skipped."""
        full = scan_history(self.store)
        self.assertEqual(shape(ck.read_history(self.store)), shape(full))
        self.assertEqual(shape(ck.read_history(self.store, bounded=False)), shape(full))
        self.assertTrue(ck.compare(self.store)["equal"])
        listed = self.run_prefixes()
        self.assertEqual({r.manifest_key[:-len("manifest.json")] for r in full.runs} | {u["prefix"] for u in full.uncommitted},
                         listed, "every run in the store is in history")

    def test_long_history_cycle_lists_only_after_the_frontier(self):
        self.assertEqual(self.head["frontier"], self.prefixes[-1])
        self.store.reset()
        report = self.collect(15)
        cycle_requests, cycle_keys = self.store.run_list_requests(), self.store.run_keys_listed()
        self.assertEqual(report.checkpoint["listing"], "after_frontier")
        self.assertTrue(all(a is not None for p, a, _ in self.store.listings if p == arc.RUNS_PREFIX),
                        "no listing of the whole run prefix")
        self.assertLessEqual(cycle_keys, 4 * 4, "a cycle lists only the runs after the frontier")
        # The same cycle on the same history without a frontier lists all 6,000+ run objects each time.
        self.assertEqual(json.loads(self.store.get(ck.HEAD_KEY))["frontier"],
                         sorted(self.run_prefixes())[-1], "the frontier moved to the new run")
        head = json.loads(self.store.get(ck.HEAD_KEY))
        self.store.put(ck.HEAD_KEY, arc.encode_json(dict(head, frontier=None)))
        self.store.reset()
        self.collect(30)
        unbounded = self.store.run_list_requests()
        self.assertGreaterEqual(unbounded, 2 * math.ceil(4 * self.HISTORY / 1000), unbounded)
        self.assertLess(cycle_requests * 3, unbounded,
                        f"bounded {cycle_requests} vs unbounded {unbounded} ListObjectsV2 requests per cycle")
        # Ten more cycles: the bounded request count does not grow with history.
        counts = []
        for i in range(10):
            self.store.reset()
            self.collect(45 + 15 * i)
            counts.append(self.store.run_list_requests())
        self.assertEqual(len(set(counts)), 1, counts)
        self.assertEqual(counts[0], cycle_requests)
        self.assert_complete()

    def test_the_scheduled_full_verification_reads_raw_bytes(self):
        self.collect(15)
        self.store.reset()
        steady = self.collect(30)
        self.store.reset()
        due = self.collect(24 * 60 + 60)     # past FULL_VERIFY_S since the rebuild
        self.assertEqual((due.checkpoint["listing"], due.checkpoint["runs_read"]), ("full", self.HISTORY + 2), due.checkpoint)
        self.assertEqual(self.store.run_keys_listed() >= 4 * self.HISTORY, True)
        self.assertGreaterEqual(self.store.gets, 4 * due.checkpoint["runs_read"])
        self.assertEqual(steady.checkpoint["listing"], "after_frontier")
        self.assert_complete()

    def test_a_recovered_spool_behind_the_frontier_is_never_skipped(self):
        # A run spooled long ago (older run_id than the frontier) whose upload never happened.
        old = synthetic_run(self.store, self.spool, iso(self.t0 + timedelta(minutes=15 * 7, seconds=1)), commit=False)
        self.assertLess(old, self.head["frontier"])
        report = self.collect(15)
        self.assertEqual(len(report.recovered), 1)
        self.assertEqual(report.checkpoint["frontier_cleared"][0]["for"], [old])
        self.assertEqual(report.checkpoint["listing"], "full", "the read after a cleared frontier lists all")
        self.assertIn(old, {r.manifest_key[:-len("manifest.json")] for r in ck.read_history(self.store).runs})
        self.assertGreater(json.loads(self.store.get(ck.HEAD_KEY))["frontier"], old, "a new frontier past it")
        self.assert_complete()

    def test_a_crash_after_the_recovered_upload_and_before_the_save_skips_nothing(self):
        old = synthetic_run(self.store, self.spool, iso(self.t0 + timedelta(minutes=15 * 3, seconds=1)), commit=False)
        # The writer clears the frontier, uploads the recovered run, then dies before reading or saving.
        cleared = arc._guard_frontier(self.store, arc.spooled_prefixes(self.spool))
        self.assertEqual(cleared["for"], [old])
        arc.recover_spools(self.store, self.spool, self.clock)
        read = ck.read_history(self.store)                 # a reader, before any writer runs again
        self.assertEqual(read.checkpoint["listing"], "full")
        self.assertIn(old, {r.manifest_key[:-len("manifest.json")] for r in read.runs})
        self.assert_complete()
        report = self.collect(15)                          # the next writer restores a frontier
        self.assertIsNotNone(json.loads(self.store.get(ck.HEAD_KEY))["frontier"])
        self.assertEqual(report.checkpoint["listing"], "full")
        self.assert_complete()

    def test_a_crash_between_clearing_and_uploading_is_harmless(self):
        synthetic_run(self.store, self.spool, iso(self.t0 + timedelta(minutes=15 * 3, seconds=1)), commit=False)
        arc._guard_frontier(self.store, arc.spooled_prefixes(self.spool))
        self.assertEqual(ck.read_history(self.store).checkpoint["listing"], "full")
        self.assert_complete()
        self.collect(15)                                    # recovery then uploads it
        self.assert_complete()

    def test_a_clock_that_went_backwards_cannot_hide_the_new_run(self):
        report = self.collect(-15 * 100)                    # this run's id sorts before the frontier
        prefix = report.manifest["objects"]["observations"]["key"].rsplit("/", 1)[0] + "/"
        self.assertLess(prefix, self.head["frontier"])
        self.assertEqual(report.checkpoint["frontier_cleared"][0]["for"], [prefix])
        self.assertIn(prefix, {r.manifest_key[:-len("manifest.json")] for r in ck.read_history(self.store).runs})
        self.assert_complete()

    def test_an_unsettled_run_holds_the_frontier_and_is_listed_every_cycle(self):
        # Objects of a run whose manifest never landed (an interrupted upload), between old runs.
        orphan = arc.run_prefix("2026-10-25", "20261025T000001Z-0badc0de")
        self.store.put(orphan + "observations.jsonl.gz", b"partial")
        self.collect(24 * 60 + 60)                          # the scheduled full listing finds it
        head = json.loads(self.store.get(ck.HEAD_KEY))
        self.assertEqual(head["frontier_stopped_at"], orphan)
        self.assertLess(head["frontier"], orphan)
        for minutes in (24 * 60 + 75, 24 * 60 + 90):
            read = ck.read_history(self.store)
            self.assertIn(orphan, [u["prefix"] for u in read.uncommitted], "still listed, still reported")
            self.collect(minutes)
        self.assert_complete()

    def test_a_restart_without_a_checkpoint_lists_everything_then_bounds_again(self):
        self.store.delete(ck.HEAD_KEY)
        first = self.collect(15)
        self.assertEqual(first.checkpoint["mode"], "full_replay")
        self.store.reset()
        second = self.collect(30)
        self.assertEqual(second.checkpoint["listing"], "after_frontier")
        self.assertLessEqual(self.store.run_keys_listed(), 4 * 4)
        self.assert_complete()
