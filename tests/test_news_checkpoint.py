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

    def list_meta(self, prefix):
        self.listings += 1
        self._inner = True
        try:
            return super().list_meta(prefix)
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

    def test_stale_for_writers_after_the_full_verification_interval(self):
        later = "2026-11-10T15:15:01Z"    # 24 h after the 15:15 full verification
        self.assertEqual(ck.read_history(self.store, Clock("2026-11-10T15:00:00Z")).checkpoint["mode"], "checkpoint")
        read = ck.read_history(self.store, Clock(later)).checkpoint
        self.assertEqual(read["mode"], "full_replay")
        self.assertTrue(read["reason"].startswith("stale"), read)
        self.assertEqual(ck.read_history(self.store).checkpoint["mode"], "checkpoint", "readers do not age it")
        report = self.collect(later)
        self.assertEqual(report.checkpoint["mode"], "full_replay")
        self.assertEqual(json.loads(self.store.get(ck.HEAD_KEY))["full_verified_at"], later)


class ChangedHistoryIsReverifiedOrFailsVisibly(Case):
    def setUp(self):
        super().setUp()
        self.first = self.collect("2026-11-09T15:00:00Z").manifest
        self.collect("2026-11-09T15:15:00Z", fetch=feeds("scenarios/a_v1"), only_sources=["oilprice_com"])
        self.prefix = self.first["objects"]["observations"]["key"].rsplit("/", 1)[0] + "/"

    def test_tampered_commit_record_is_reverified_and_corrupt(self):
        rec = json.loads(self.store.get(self.prefix + "commit.json"))
        self.store.put(self.prefix + "commit.json",
                       json.dumps(dict(rec, committed_at="2026-11-09T09:00:00Z", manifest_sha256="0" * 64)).encode())
        read = ck.read_history(self.store)
        self.assertEqual(read.checkpoint["reverified"], [self.first["run_id"]])
        self.assertEqual(shape(read), shape(scan_history(self.store)))
        self.assertEqual([p["run_id"] for p in read.problems], [self.first["run_id"]])
        with self.assertRaises(HistoryCorrupt):
            self.collect("2026-11-09T15:30:00Z")

    def test_rewritten_object_is_reverified_and_corrupt(self):
        key = self.prefix + "observations.jsonl.gz"
        self.store.put(key, gzip.compress(b'{"forged": true}\n', mtime=0))
        read = ck.read_history(self.store)
        self.assertEqual(read.checkpoint["reverified"], [self.first["run_id"]])
        self.assertEqual(read.problems[0]["detail"], "observations: size/digest mismatch")
        with self.assertRaises(HistoryCorrupt):
            self.collect("2026-11-09T15:30:00Z")

    def test_identical_rewrite_is_reverified_and_still_clean(self):
        key = self.prefix + "classifications.jsonl.gz"
        self.store.put(key, self.store.get(key))
        read = ck.read_history(self.store)
        self.assertEqual((read.checkpoint["reverified"], read.problems), ([self.first["run_id"]], []))
        self.collect("2026-11-09T15:30:00Z")
        self.assertEqual(ck.read_history(self.store).checkpoint["reverified"], [], "re-checkpointed")

    def test_checkpointed_run_that_disappears_fails_visibly(self):
        """Full replay alone would silently lose the run; the checkpoint makes the loss visible."""
        for name in ck.RUN_NAMES:
            self.store.delete(self.prefix + name)
        full = scan_history(self.store)
        self.assertEqual(full.problems, [], "full replay cannot tell a run is gone")
        read = ck.read_history(self.store)
        self.assertEqual(read.problems, [{"run_id": self.first["run_id"], "prefix": self.prefix,
                                          "detail": "checkpointed run is missing from the store"}])
        with self.assertRaises(HistoryCorrupt):
            self.collect("2026-11-09T15:30:00Z")
        self.assertTrue(load_archive(self.store).problems, "readers report it as corrupt coverage")


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
        self.assertEqual((read["mode"], read["runs_read"], read["runs_from_checkpoint"]), ("checkpoint", 1, 1))

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
        steady = [r[4] for r in rows if r[6] == "checkpoint"]
        self.assertEqual(len(set(steady)), 1, f"a cycle's reads do not grow with history: {steady}")
        self.assertTrue(all(r[3] > 3 * 4 * r[0] for r in rows), "the M5.2 cycle replays history three times")
        # Over 24 h (98 runs) the writer replays everything once more, as scheduled.
        self.assertEqual([r[7].split(":")[0] for r in rows if r[6] != "checkpoint"], ["stale"])
        self.assertEqual(cycle_modes.count("full_replay"), 1, "the very first cycle has no checkpoint yet")


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
