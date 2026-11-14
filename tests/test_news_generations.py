"""G3 generations under the G6 contract: qualification, incarnations, lifecycle clocks, cleanup, the cap.

NOT RUN: written for static review only (Chej's G6 brief: no test execution). Local only: fixture feeds
and temporary directory stores. The stores are scripted fakes: a controllable store clock (a real
bucket's LastModified would be the store clock) and scripted conditional-write outcomes. They model the
contract the publisher relies on; they are not evidence about any provider (mock-contract preparation
is not operational validation).

Replaces the G3 file of the same name. Its ledger/uncertain cases are superseded by the head record and
open commits; the settlement cases are in test_news_settlement.py. See the G6 coverage map.

Covered here: qualification (logical vs final digests, links stay inside one incarnation, the published
key grammar, a fresh nonce per index write, tombstones), the lifecycle (a replaced generation is ceased
from a reading taken after the observation and stays public; expiry needs both clocks, agreement, a
verified bound; a backward clock refuses; protection follows the latest epoch), orphans, blocked runs
(foreign index, unreadable head), unknown public incarnations, and the page cap (pause, never evict).
"""
import json
import unittest

from test_news_publish import BUILD_INDEX, INDEX, NONCE0, Base, RecordingStore, qualified, with_nonce0
from test_news_support import Clock, at
from market_data.news import publish as pub
from market_data.news.archive import CasOutcome, LocalObjectStore
from market_data.news.views import (BuildError, commit_index, commit_nonce_of, files_digest, generation_name,
                                    pages_digest, qualify_generation, tombstone_index)

VERIFIED = pub.GenerationPolicy(clock_bound_verified=True)


class Scripted(RecordingStore):
    """A local store with a fake store clock and scripted conditional-write outcomes.

    script[key] is a list consumed one entry per put_if_match call on that key:
      None       evaluate the condition and answer normally
      "lost"     ambiguous: nothing is written, no answer reaches the caller
      "landed"   ambiguous: the write is applied, the answer is lost
      "hold"     ambiguous: the request is kept in `held`; deliver() evaluates it later (a late write)
      "reject"   rejected (412) without evaluating
    before_cas[key] is called once, just before the next conditional write on that key is evaluated
    (another actor acting first). hold_delete(key) -> True makes one delete fail with no outcome.
    hold_put(key) -> True makes one create-if-absent time out with no answer; the request is kept in
    `held_puts` and land_put() applies it later (a page PUT that lands after its caller gave up).
    """

    def __init__(self, root, log=None, store_clock=None):
        super().__init__(root, log)
        self.store_clock = store_clock
        self.script = {}
        self.held = []
        self.before_cas = {}
        self.hold_delete = None
        self.held_deletes = []
        self.hold_put = None
        self.held_puts = []

    def last_modified(self, key):
        if self.store_clock is None:
            return super().last_modified(key)
        return self.store_clock() if self.get(key) is not None else None

    def put_if_match(self, key, data, etag, content_type="application/octet-stream", cache_control=None,
                     write_id=None):
        hook = self.before_cas.pop(key, None)
        if hook is not None:
            hook()
        modes = self.script.get(key) or []
        mode = modes.pop(0) if modes else None
        if mode == "lost":
            return CasOutcome("ambiguous", detail="scripted: not written, no answer")
        if mode == "hold":
            self.held.append((key, data, etag, content_type, cache_control))
            return CasOutcome("ambiguous", detail="scripted: in flight")
        if mode == "reject":
            return CasOutcome("rejected", detail="scripted 412")
        outcome = super().put_if_match(key, data, etag, content_type, cache_control=cache_control, write_id=write_id)
        if mode == "landed":
            return CasOutcome("ambiguous", detail="scripted: written, answer lost")
        return outcome

    def deliver(self, i=0) -> CasOutcome:
        """A held request reaches the store now: its condition is evaluated against the current object."""
        key, data, etag, content_type, cache_control = self.held.pop(i)
        return LocalObjectStore.put_if_match(self, key, data, etag, content_type, cache_control=cache_control)

    def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
        if self.hold_put is not None and self.hold_put(key):
            self.hold_put = None
            self.held_puts.append((key, data, content_type, cache_control))
            raise TimeoutError(f"scripted: put of {key} timed out; it may still land")
        return super().put_if_absent(key, data, content_type=content_type, cache_control=cache_control)

    def land_put(self, i=0) -> bool:
        """A held page PUT reaches the store now: create-if-absent, evaluated against the key as it is."""
        key, data, content_type, cache_control = self.held_puts.pop(i)
        return LocalObjectStore.put_if_absent(self, key, data, content_type=content_type, cache_control=cache_control)

    def delete(self, key):
        if self.hold_delete is not None and self.hold_delete(key):
            self.hold_delete = None
            self.held_deletes.append(key)
            raise RuntimeError(f"scripted: delete of {key} has no outcome")
        super().delete(key)


class Gen(Base):
    """One published generation A (incarnation A.1) at 10:05 on both clocks."""

    def setUp(self):
        super().setUp()
        self.store_clock = Clock("2026-11-09T10:05:00Z")
        self.plog = []
        self.private = Scripted(self.tmp / "private-g", self.plog, self.store_clock)
        self.raw_public = Scripted(self.tmp / "public-g", self.log)
        self.public = pub.PublicViewStore(self.raw_public)
        self.cycle("2026-11-09T10:05:00Z")
        self.A = pub.read_state(self.private)["committed"]["gen_id"]
        self.A_index_raw = self.raw_public.get(INDEX)

    def build(self, when, store=None):
        files = self.generation(when)          # sets the producer clock to `when`
        self.store_clock.set(store or when)
        return files

    def at(self, when, store=None):
        self.clock.set(when)
        self.store_clock.set(store or when)

    def publish(self, files, policy=pub.DEFAULT_POLICY):
        return pub.publish(self.private, self.public, files, self.clock, policy=policy)

    def reading(self):
        now = {"producer": self.clock(), "storage": self.store_clock()}
        return dict(now, refused=[], stamp=dict(now))

    def events(self, inc):
        return [e["kind"] for e in self.head()["incarnations"][inc]["events"]]

    def event(self, inc, kind):
        return [e for e in self.head()["incarnations"][inc]["events"] if e["kind"] == kind][-1]

    def live(self):
        return json.loads(self.raw_public.get(INDEX)).get("gen")

    def open(self):
        return pub._open_commits(self.head())

    def klass(self, inc, policy=pub.DEFAULT_POLICY):
        return pub.status(self.private, self.public, self.clock, policy=policy)["generations"][inc]["class"]


class Qualification(Gen):
    def test_logical_identity_is_fixed_before_keys_and_final_integrity_is_separate(self):
        files = self.build("2026-11-09T10:20:00Z")
        inc, out = qualify_generation(files)
        self.assertEqual(qualify_generation(files), (inc, out), "deterministic")
        gen = generation_name(files)
        self.assertEqual(gen, "20261109T102000Z-" + files_digest(files)[:16], "hash of the UNQUALIFIED build")
        self.assertEqual(inc, gen + ".1")
        index = json.loads(out[INDEX])
        self.assertEqual((index["schema_version"], index["state"]), ("news-view-index/v2", "published"))
        self.assertEqual((index["gen"], index["logical_gen"]), (inc, gen))
        self.assertEqual(index["pages_sha256"], pages_digest(out), "final integrity of the qualified pages")
        self.assertNotIn("commit", index, "the nonce is added per write, not by qualification")
        self.assertNotIn(BUILD_INDEX, out)
        for key, body in out.items():
            if key == INDEX:
                continue
            self.assertEqual(pub.incarnation_of(key), inc)
            page = json.loads(body)
            self.assertEqual(page["gen"], inc)
            for link in (page.get("prev_page"), page.get("next_page")):
                self.assertIn(link if link is None else pub.incarnation_of(link), (None, inc))
        for v in index["views"]:
            self.assertEqual(pub.incarnation_of(v["first_page"]), inc)
        self.assertEqual(pub.validate_qualified(inc, out)["gen"], inc)

    def test_another_incarnation_of_the_same_build_shares_no_key_and_keeps_the_logical_identity(self):
        files = self.build("2026-11-09T10:20:00Z")
        one, out1 = qualify_generation(files, 1)
        two, out2 = qualify_generation(files, 2)
        self.assertEqual((one[:-2], two[:-2]), (generation_name(files),) * 2)
        self.assertEqual(set(out1) & set(out2), {INDEX}, "no page key is shared between incarnations")
        self.assertEqual(json.loads(out2[INDEX])["logical_gen"], generation_name(files))
        for bad in (0, 10000, True, "1"):
            with self.assertRaises(BuildError):
                qualify_generation(files, bad)

    def test_every_index_write_has_different_bytes(self):
        files = self.build("2026-11-09T10:20:00Z")
        _inc, out = qualify_generation(files)
        a, b = commit_index(out[INDEX], "a" * 32), commit_index(out[INDEX], "b" * 32)
        self.assertNotEqual(a, b, "the nonce is in the body, so identical content never repeats an ETag")
        self.assertEqual((commit_nonce_of(a), commit_nonce_of(b)), ("a" * 32, "b" * 32))
        self.assertEqual(with_nonce0(a), with_nonce0(b))
        self.assertIsNone(commit_nonce_of(out[INDEX]))
        self.assertIsNone(commit_nonce_of(b'{"commit": {"nonce": "short"}}'))
        for bad in ("A" * 32, "a" * 31, None):
            with self.assertRaises(BuildError):
                commit_index(out[INDEX], bad)
        tomb = json.loads(tombstone_index(NONCE0))
        self.assertEqual(tomb, {"schema_version": "news-view-index/v2", "state": "withdrawn", "commit": {"nonce": NONCE0}})
        with self.assertRaises(BuildError):
            tombstone_index(NONCE0, "published")

    def test_a_link_outside_the_build_cannot_be_qualified(self):
        files = dict(self.build("2026-11-09T10:20:00Z"))
        key = sorted(k for k in files if k != BUILD_INDEX)[0]
        page = json.loads(files[key])
        page["next_page"] = "news/v1/views/collector/oil/2026-11-09/page-0019.json"
        files[key] = json.dumps(page).encode()
        with self.assertRaises(BuildError):
            qualify_generation(files)

    def test_validate_qualified_refuses_cross_incarnation_and_digest_mismatches(self):
        files = self.build("2026-11-09T10:20:00Z")
        inc, out = qualify_generation(files)
        other, out2 = qualify_generation(files, 2)
        key = sorted(k for k in out if k != INDEX)[0]
        cases = {
            "wrong incarnation": (other, out),
            "a page of another incarnation": (inc, dict(out, **{sorted(k for k in out2 if k != INDEX)[0]: out[key]})),
            "altered page": (inc, dict(out, **{key: out[key].replace(b"{", b"{ ", 1)})),
            "builder index": (inc, dict({k: v for k, v in out.items() if k != INDEX}, **{BUILD_INDEX: files[BUILD_INDEX]})),
            "not an incarnation": (generation_name(files), out),
        }
        for name, (ident, bad) in cases.items():
            with self.subTest(name):
                with self.assertRaises(pub.PublishError):
                    pub.validate_qualified(ident, bad)
        index = json.loads(out[INDEX])
        for name, change in {"state": {"state": "withdrawn"}, "logical_gen": {"logical_gen": "x"},
                             "pages_sha256": {"pages_sha256": "0" * 64}, "schema": {"schema_version": "news-view-index/v1"}}.items():
            with self.subTest(name):
                with self.assertRaises(pub.PublishError):
                    pub.validate_qualified(inc, dict(out, **{INDEX: json.dumps(dict(index, **change)).encode()}))


class Lifecycle(Gen):
    def test_a_replaced_generation_is_ceased_and_stays_public(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.publish(files)
        self.assertEqual(self.live(), B + ".1")
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published", "ceased"])
        self.assertEqual(self.events(B + ".1"), ["pinned", "published"])
        self.assertTrue(self.pages_of(self.A), "reader-protected, not deleted at the commit")
        self.assertEqual(self.klass(self.A + ".1"), "protected")
        self.assertEqual(self.klass(B + ".1"), "current")
        self.assertEqual(self.public_files(), qualified(files))

    def test_cessation_is_stamped_after_the_observation_never_at_run_start_or_prepare(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        # The upload takes seven minutes: the clocks have moved on when the index write is sent.
        self.raw_public.before_cas[INDEX] = lambda: self.at("2026-11-09T10:27:00Z")
        self.publish(files)
        ceased = self.event(self.A + ".1", "ceased")
        self.assertEqual((at(ceased["producer_at"]), at(ceased["storage_at"])), (at("2026-11-09T10:27:00Z"),) * 2)
        self.assertEqual(at(self.event(B + ".1", "pinned")["producer_at"]), at("2026-11-09T10:20:00Z"))
        self.assertEqual(ceased["observed"], self.live_nonce(), "it records which observed commit ended it")
        commit = pub.read_commit(self.private, self.live_nonce())
        self.assertEqual(at(commit["recorded"]["producer_at"]), at("2026-11-09T10:20:00Z"), "prepared earlier")

    def test_expiry_is_refused_visibly_while_the_clock_bound_is_unverified(self):
        self.publish(self.build("2026-11-09T10:20:00Z"))
        self.publish(self.build("2026-11-09T12:00:00Z"))          # default policy: bound unverified
        self.assertTrue(self.pages_of(self.A), "100 minutes after it ceased, still there")
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("expiry_blocked_clock_bound_unverified", st["problems"])
        self.assertEqual(st["generations"][self.A + ".1"]["class"], "protected")

    def test_expiry_needs_both_clocks_past_grace_epsilon_and_skew(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)          # A ceased 10:20 on both clocks
        self.publish(self.build("2026-11-09T11:08:00Z"), VERIFIED)          # 48 min: one short of 49
        self.assertTrue(self.pages_of(self.A))
        self.publish(self.build("2026-11-09T11:10:00Z"), VERIFIED)          # 50 min on both
        self.assertEqual(self.pages_of(self.A), [])
        self.assertNotIn(self.A + ".1", self.head()["incarnations"], "retired: its reservation is released")
        self.assertEqual(self.head()["high"][self.A], 1, "the number is never issued again")

    def test_a_forward_producer_clock_jump_alone_deletes_nothing(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.publish(self.build("2026-11-09T13:00:00Z", store="2026-11-09T10:21:00Z"), VERIFIED)
        self.assertTrue(self.pages_of(self.A), "the store clock shows one minute")

    def test_a_backward_producer_clock_refuses_deletion(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.publish(self.build("2026-11-09T10:10:00Z", store="2026-11-09T12:00:00Z"), VERIFIED)
        self.assertTrue(self.pages_of(self.A))
        self.assertTrue(pub.read_state(self.private)["clock"]["refused"])

    def test_protection_follows_the_latest_epoch_after_a_restore(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.publish(files, VERIFIED)                                       # A ceased 10:20
        pub.stop(self.private, self.clock, "rollback")
        self.at("2026-11-09T10:30:00Z")
        pub.restore(self.private, self.public, self.A, self.clock, policy=VERIFIED)   # A restored, B ceased 10:30
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published", "ceased", "restored"])
        pub.resume(self.private)
        self.publish(self.build("2026-11-09T11:15:00Z"), VERIFIED)          # A ceased again 11:15; B at 45 min
        self.assertTrue(self.pages_of(self.A), "55 min after its first cease, but its latest epoch is new")
        self.assertTrue(self.pages_of(B))
        self.publish(self.build("2026-11-09T11:25:00Z"), VERIFIED)          # B at 55 min; A at 10 min
        self.assertEqual(self.pages_of(B), [])
        self.assertTrue(self.pages_of(self.A))


class Orphans(Gen):
    def test_a_never_offered_incarnation_is_retired_by_the_next_run(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        # G6 r1: every page PUT was answered and read back; the run stops at its commit record. A crash
        # on a page PUT instead leaves the upload unanswered and quarantines B.1 (Uploads.* in
        # test_news_settlement.py).
        self.private.fail = ("put", lambda k: k.startswith(pub.COMMIT_PREFIX), 0)
        with self.assertRaises(RuntimeError):
            self.publish(files)
        self.assertNotIn("uploads", self.head()["incarnations"][B + ".1"], "the upload attempt was answered")
        self.assertEqual(self.klass(B + ".1"), "orphan")
        self.assertTrue(self.pages_of(B))
        self.publish(self.build("2026-11-09T10:35:00Z"))
        self.assertEqual(self.pages_of(B), [])
        self.assertNotIn(B + ".1", self.head()["incarnations"])

    def test_the_same_build_reuses_its_pinned_incarnation(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.raw_public.fail = ("put", lambda k: k != INDEX, 2)
        with self.assertRaises(RuntimeError):
            self.publish(files)
        report = self.publish(files)                                  # no delete was started in B.1
        self.assertEqual(report.incarnation, B + ".1")
        self.assertEqual(report.unchanged, 2, "the two pages already written are reused")
        self.assertEqual(self.public_files(), qualified(files))
        # G6 r1: the identical re-upload does not settle the first attempt (its third PUT had no answer).
        self.assertEqual(len(pub._unsettled_uploads(self.head()["incarnations"][B + ".1"])), 1)


class Blocked(Gen):
    def test_a_foreign_live_index_blocks_everything(self):
        self.raw_public.put(INDEX, b'{"schema_version": "news-view-index/v2", "state": "published"}')
        self.log.clear()
        with self.assertRaises(pub.PublishBlocked):
            self.publish(self.build("2026-11-09T10:20:00Z"))
        self.assertEqual(self.log, [], "nothing written or deleted")
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("live_index_unmatched", st["problems"])
        self.assertIn("publication_blocked", st["problems"])

    def test_a_known_nonce_with_other_bytes_is_foreign_too(self):
        doc = json.loads(self.A_index_raw)
        doc["data_label"] = "changed"
        self.raw_public.put(INDEX, json.dumps(doc).encode())
        with self.assertRaises(pub.PublishBlocked):
            self.publish(self.build("2026-11-09T10:20:00Z"))

    def test_a_missing_page_of_the_live_incarnation_blocks(self):
        self.raw_public.delete(self.pages_of(self.A)[0])
        with self.assertRaises(pub.PublishBlocked):
            self.publish(self.build("2026-11-09T10:20:00Z"))

    def test_an_unreadable_head_blocks_everything(self):
        self.private.put(pub.HEAD_KEY, b"not json")
        self.log.clear()
        with self.assertRaises(pub.PublishBlocked):
            self.publish(self.build("2026-11-09T10:20:00Z"))
        self.assertEqual(self.log, [])
        self.assertIn("head_unreadable", pub.status(self.private, self.public, self.clock)["problems"])

    def test_unknown_public_incarnations_are_retained_reported_and_counted(self):
        stray = "news/v2/views/p/20250101T000000Z-0123456789abcdef.1/collector/oil/2025-01-01/page-0001.json"
        self.raw_public.put(stray, b"{}")
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.assertIsNotNone(self.raw_public.get(stray), "no record of it: never deleted")
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("unknown_public_generations", st["problems"])
        self.assertEqual(st["reserved_pages"], sum(e["pages"] for e in self.head()["incarnations"].values()) + 1)


class Cap(Gen):
    def setUp(self):
        super().setUp()
        self.per = len(self.pages_of(self.A))
        self.two = pub.GenerationPolicy(page_cap=2 * self.per, clock_bound_verified=True)

    def test_the_cap_pauses_after_cleanup_and_never_evicts(self):
        files_b = self.build("2026-11-09T10:20:00Z")
        self.publish(files_b, self.two)                                # A + B = the cap
        files_c = self.build("2026-11-09T10:35:00Z")
        with self.assertRaises(pub.PublishPaused):
            self.publish(files_c, self.two)
        self.assertEqual(self.public_files(), qualified(files_b), "the live generation is untouched")
        self.assertEqual(len(self.pages_of(self.A)), self.per, "the protected generation is not evicted")
        self.assertIn("publication_paused", pub.status(self.private, self.public, self.clock)["problems"])
        self.assertEqual(self.pages_of(generation_name(files_c)), [], "nothing of the paused build was written")
        self.publish(self.build("2026-11-09T11:10:00Z"), self.two)      # A expired: cleanup frees its reservation
        self.assertEqual(self.pages_of(self.A), [])

    def test_restore_counts_its_fresh_incarnation_against_the_cap(self):
        files_b = self.build("2026-11-09T10:20:00Z")
        self.publish(files_b, self.two)
        self.publish(self.build("2026-11-09T11:10:00Z"), self.two)      # A retired (50 min); B ceased; C live
        pub.stop(self.private, self.clock, "rollback")
        with self.assertRaises(pub.PublishPaused):                      # A.2 would be a third page set
            pub.restore(self.private, self.public, self.A, self.clock, policy=self.two)
        self.assertEqual(self.pages_of(self.A, 2), [])
        pub.restore(self.private, self.public, generation_name(files_b), self.clock, policy=self.two)
        self.assertEqual(self.live(), generation_name(files_b) + ".1", "a still-pinned incarnation needs no new pages")


if __name__ == "__main__":
    unittest.main()
