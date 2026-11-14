"""G6: settlement of index writes (the accepted G5 design and its lifecycle addendum).

NOT RUN: authored for static review only (Chej's G6 brief: no test execution, no fixture generation).
Everything here uses the scripted stores in test_news_generations.py. They model the conditional-write
contract the publisher relies on (atomic If-Match / If-None-Match, 412 on a failed condition, an
unanswered request that may or may not have been applied, a request that lands late). They are a
mock contract, not evidence about R2, boto3 or any private store.

Failure transitions of the addendum:
  F1  a child commit is prepared, the process stops before it is sent      FailureTransitions.test_f1_*
  F2  the child lands during a gap; the next run observes it               FailureTransitions.test_f2_*
  F3  a commit landed and was replaced without ever being recorded         FailureTransitions.test_f3_*
  F4  a page delete has no outcome; the generation is restored             FailureTransitions.test_f4_*
  F5  cleanup and restore race for one incarnation                         FailureTransitions.test_f5_*
  F6  a stale publisher finishes after its incarnation was retired         FailureTransitions.test_f6_*
  F7  withdrawal; tombstone unanswered; an older commit lands              FailureTransitions.test_f7_*
  F8  legacy keys and a late legacy request                                FailureTransitions.test_f8_*
The four criteria:
  C1  no commit preparation while withdrawing                              Criteria.test_c1_*
  C2  the retirement CAS revalidates the current commit protections        Criteria.test_c2_*
  C3  after an ambiguous head write, exact intent is verified first        Criteria.test_c3_*
  C4  page-cap capacity is reserved atomically (open and in-flight too)    Criteria.test_c4_*
G6 r1 (Chej 11-13): a page PUT with no answer keeps durable protection and the full reservation:
  timeout -> next-run cleanup/admission -> late landing                    Uploads.test_u1_*, test_u2_*
  the withdrawal variant                                                   Uploads.test_u4_*
  intent before the first PUT; a retiring mark that wins first             Uploads.test_u5_*, test_u6_*
CAS ambiguity: Ambiguity.*, Genesis.*, ProviderMapping.* (the store-level outcome mapping).
"""
import json
import unittest
from unittest import mock

from test_news_generations import VERIFIED, Gen, Scripted
from test_news_publish import INDEX, Base, qualified, with_nonce0
from test_news_support import at
from market_data.news import publish as pub
from market_data.news.archive import ArchiveError, CasOutcome, LocalObjectStore, R2ObjectStore, content_etag
from market_data.news.views import commit_index, generation_name, qualify_generation, tombstone_index

HEAD = pub.HEAD_KEY
LEGACY_INDEX = "news/v1/views/index.json"
LEGACY_PAGE = "news/v1/views/g/20261109T100500Z-0123456789abcdef/collector/oil/2026-11-09/page-0001.json"


class Settle(Gen):
    def index_puts(self):
        return [k for op, k in self.log if op == "put" and k == INDEX]

    def held_publish(self, when):
        """A publication whose index write is in flight: prepared, sent, unanswered, not yet applied."""
        files = self.build(when)
        self.raw_public.script[INDEX] = ["hold"]
        with self.assertRaises(pub.PublishIntegrityError):
            self.publish(files)
        return files, generation_name(files)

    def etag(self):
        return content_etag(self.raw_public.get(INDEX))


class FailureTransitions(Settle):
    def test_f1_child_prepared_then_crash_before_send_leaves_the_parent_live_and_unceased(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        parent = self.live_nonce()
        self.raw_public.fail = ("put", lambda k: k == INDEX, 0)       # stops before the request is sent
        with self.assertRaises(RuntimeError):
            self.publish(files)
        self.assertEqual(self.raw_public.get(INDEX), self.A_index_raw, "the parent is still live")
        (child, commit), = self.open().items()
        self.assertEqual(commit["expect"]["nonce"], parent, "the child names the parent: the parent was OBSERVED")
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published"], "observed is not retired: no cessation")
        self.assertEqual(self.klass(B + ".1"), "open")
        self.assertIsNotNone(pub.read_commit(self.private, child), "durable intent exists although nothing was sent")
        # Hours later nothing has changed: elapsed time records no cessation either.
        self.at("2026-11-09T12:00:00Z")
        st = pub.status(self.private, self.public, self.clock)
        self.assertEqual(st["live_generation"], self.A + ".1")
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published"])

    def test_f2_child_lands_in_a_gap_and_cessation_is_stamped_by_the_observing_run(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.raw_public.script[INDEX] = ["landed"]                    # applied; the answer never arrives
        with self.assertRaises(pub.PublishIntegrityError):
            self.publish(files)
        self.assertEqual(self.live(), B + ".1", "readers already get B")
        self.assertEqual(self.head()["live"]["inc"], self.A + ".1", "not yet observed by any run")
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published"])
        self.assertTrue(self.pages_of(self.A) and self.pages_of(B), "both page sets are present throughout the gap")
        self.at("2026-11-09T10:40:00Z")                               # the restart gap ends
        report = self.publish(files)
        self.assertEqual(report.written, [], "already current: it only observes and records")
        self.assertEqual(self.events(B + ".1"), ["pinned", "published"])
        ceased = self.event(self.A + ".1", "ceased")
        self.assertEqual(at(ceased["producer_at"]), at("2026-11-09T10:40:00Z"),
                         "from the observing run's reading, not 10:20 when the commit was prepared")
        self.assertEqual(self.open(), {})

    def test_f3_a_commit_that_landed_unrecorded_is_offered_and_ceased_at_the_later_observation(self):
        x, y = "20261109T102000Z-aaaaaaaaaaaaaaaa.1", "20261109T103500Z-bbbbbbbbbbbbbbbb.1"
        c1, c2 = "1" * 32, "2" * 32
        pinned = {"kind": "pinned", "producer_at": "2026-11-09T10:20:00Z", "storage_at": "2026-11-09T10:20:00Z"}
        head = {"live": {"nonce": "0" * 32, "etag": '"e0"', "kind": "publish", "inc": None}, "withdrawing": None,
                "withdrawn": None, "high": {},
                "incarnations": {x: {"state": "pinned", "events": [dict(pinned)]},
                                 y: {"state": "pinned", "events": [dict(pinned)]}},
                "commits": {c1: {"kind": "publish", "inc": x, "expect": {"etag": '"e0"', "nonce": "0" * 32}, "landed": None},
                            c2: {"kind": "publish", "inc": y, "expect": {"etag": '"e1"', "nonce": c1}, "landed": None}}}
        stamp = {"producer_at": "2026-11-09T11:00:00Z", "storage_at": "2026-11-09T11:00:00Z"}
        self.assertTrue(pub._apply_observation(head, {"nonce": c2, "etag": '"e2"'}, stamp))
        kinds = [(e["kind"], e["producer_at"]) for e in head["incarnations"][x]["events"]]
        self.assertEqual(kinds[1:], [("published", stamp["producer_at"]), ("ceased", stamp["producer_at"])],
                         "c2 was conditioned on c1, so c1 landed: its grace starts at this observation")
        self.assertEqual([e["kind"] for e in head["incarnations"][y]["events"]], ["pinned", "published"])
        self.assertEqual(sorted(head["commits"]), [c2], "c1 can no longer land and is closed")
        self.assertEqual(head["live"]["nonce"], c2)

    def test_f4_restore_after_an_unanswered_page_delete_uses_a_fresh_incarnation(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)          # A ceased 10:20
        files_c = self.build("2026-11-09T11:15:00Z")
        self.raw_public.hold_delete = lambda k: pub.incarnation_of(k) == self.A + ".1"
        with self.assertRaises(RuntimeError):
            self.publish(files_c, VERIFIED)                                 # cleanup: one delete has no outcome
        (lost,) = self.raw_public.held_deletes
        self.assertEqual(self.head()["incarnations"][self.A + ".1"]["state"], "retiring",
                         "the mark was durable before the first delete was sent")
        self.assertEqual(pub.read_state(self.private)["last_failure"]["stage"], "cleanup")
        self.publish(files_c, VERIFIED)                                     # cleanup finishes; C is live
        self.assertEqual(self.pages_of(self.A, 1), [])
        pub.stop(self.private, self.clock, "rollback")
        report = pub.restore(self.private, self.public, self.A, self.clock, policy=VERIFIED)
        self.assertEqual(report.incarnation, self.A + ".2", "the namespace with a started delete is never reused")
        self.assertEqual(self.pages_of(self.A, 1), [])
        restored = self.public_files()
        self.assertEqual(restored, qualified(pub.load_snapshot(self.private, self.A), 2))
        # The unanswered delete lands now. It targets A.1; the live pages are under A.2.
        self.assertEqual(pub.incarnation_of(lost), self.A + ".1")
        LocalObjectStore.delete(self.raw_public, lost)
        self.assertEqual(self.public_files(), restored)
        self.assert_index_consistent()

    def test_f5_restore_first_then_cleanup_cannot_retire_the_incarnation_it_names(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)          # A ceased 10:20, B live
        pub.stop(self.private, self.clock, "rollback")
        self.raw_public.script[INDEX] = ["hold"]
        with self.assertRaises(pub.PublishIntegrityError):
            pub.restore(self.private, self.public, self.A, self.clock, policy=VERIFIED)
        pub.resume(self.private)
        self.assertEqual(self.klass(self.A + ".1", VERIFIED), "open")
        self.publish(self.build("2026-11-09T11:15:00Z"), VERIFIED)          # 55 min after A ceased
        self.assertTrue(self.pages_of(self.A), "an open commit names it: not retired by this run's cleanup")
        self.assertEqual(self.open(), {}, "C landed: the held restore can no longer land")
        self.assertTrue(self.raw_public.deliver().rejected)
        self.publish(self.build("2026-11-09T11:25:00Z"), VERIFIED)
        self.assertEqual(self.pages_of(self.A), [], "with no commit naming it, it expires normally")

    def test_f5_cleanup_first_then_restore_takes_a_fresh_incarnation(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.publish(self.build("2026-11-09T11:15:00Z"), VERIFIED)          # A.1 retired
        pub.stop(self.private, self.clock, "rollback")
        report = pub.restore(self.private, self.public, self.A, self.clock, policy=VERIFIED)
        self.assertEqual(report.incarnation, self.A + ".2")
        self.assertEqual(self.head()["high"][self.A], 2)

    def test_f5_two_head_changes_from_the_same_head_cannot_both_win(self):
        head, etag = pub.read_head(self.private)
        first = dict(head, withdrawn={"note": "actor one"})
        pub._write_head(self.private, first, etag, "actor one")
        with self.assertRaises(pub.PublishConflict):
            pub._write_head(self.private, dict(head, withdrawn={"note": "actor two"}), etag, "actor two")
        self.assertEqual(self.head()["withdrawn"], {"note": "actor one"})

    def test_f6_a_stale_publisher_is_refused_at_commit_preparation_and_sends_nothing(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.private.fail = ("put", lambda k: k.startswith(pub.COMMIT_PREFIX), 0)   # stops after its pages
        with self.assertRaises(RuntimeError):
            self.publish(files)
        stale_head, stale_etag = pub.read_head(self.private)                # what the stalled publisher holds
        self.assertEqual(stale_head["incarnations"][B + ".1"]["state"], "pinned")
        self.publish(self.build("2026-11-09T10:35:00Z"))                    # a takeover retires B.1 as an orphan
        self.assertEqual(self.pages_of(B), [])
        inc, out = qualify_generation(files)
        body = commit_index(out[INDEX], "f" * 32)
        seen = {"etag": stale_head["live"]["etag"], "nonce": stale_head["live"]["nonce"]}
        self.log.clear()
        for name, (head, etag) in {"its stale head": (stale_head, stale_etag),
                                   "a fresh head": pub.read_head(self.private)}.items():
            with self.subTest(name):
                with self.assertRaises(pub.PublishConflict):
                    pub._prepare(self.private, head, etag, kind="publish", inc=inc, gen=B, body=body, page_set={},
                                 obs=seen, reading=self.reading())
        self.assertEqual(self.index_puts(), [], "the index write is never sent")
        self.assertNotIn("f" * 32, self.head()["commits"])

    def test_f7_withdrawal_stays_unconfirmed_until_a_tombstone_is_read_back_live(self):
        _files, B = self.held_publish("2026-11-09T10:20:00Z")               # c2 for B is in flight
        pub.stop(self.private, self.clock, "take down")
        self.raw_public.script[INDEX] = ["lost"]                            # the tombstone gets no answer
        out = pub.withdraw(self.private, self.public, self.clock)
        self.assertTrue(out["withdrawal_unconfirmed"])
        self.assertEqual(out["tombstone_attempts"], ["ambiguous"])
        self.assertIn("no time bound", out["note"])
        self.assertEqual(self.raw_public.list(pub.PAGES_PREFIX), [], "every page is deleted at once regardless")
        self.assertEqual(self.raw_public.get(INDEX), self.A_index_raw, "dangling: the old index names deleted pages")
        st = pub.status(self.private, self.public, self.clock)
        self.assertTrue(st["withdrawal_unconfirmed"])
        self.assertIn("withdrawal_unconfirmed", st["problems"])
        # The commit prepared before the withdrawal lands late: an index naming B, whose pages are gone.
        self.assertTrue(self.raw_public.deliver().applied)
        self.assertEqual(self.live(), B + ".1")
        self.assertIn("live_index_unmatched", pub.status(self.private, self.public, self.clock)["problems"])
        out = pub.withdraw(self.private, self.public, self.clock)           # conditioned on the landed index
        self.assertFalse(out["withdrawal_unconfirmed"])
        self.assertEqual(json.loads(self.raw_public.get(INDEX))["state"], "withdrawn")
        head = self.head()
        self.assertIsNone(head["withdrawing"])
        self.assertEqual(sorted(head["commits"]), [head["live"]["nonce"]], "every earlier commit is closed")
        self.assertEqual(head["incarnations"], {})

    def test_f8_legacy_keys_are_never_read_written_or_deleted(self):
        self.raw_public.put(LEGACY_INDEX, b'{"legacy": true}')
        self.raw_public.put(LEGACY_PAGE, b'{"legacy": "page"}')
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.publish(self.build("2026-11-09T11:15:00Z"), VERIFIED)          # includes a cleanup with deletes
        pub.stop(self.private, self.clock, "take down")
        pub.withdraw(self.private, self.public, self.clock)
        self.assertEqual(self.raw_public.get(LEGACY_INDEX), b'{"legacy": true}')
        self.assertEqual(self.raw_public.get(LEGACY_PAGE), b'{"legacy": "page"}')
        self.assertEqual([k for _op, k in self.log if k.startswith("news/v1/")], [LEGACY_INDEX, LEGACY_PAGE],
                         "the only legacy operations are this test's own two puts")
        for key in (LEGACY_INDEX, LEGACY_PAGE):
            self.assertFalse(pub.is_public_view_key(key))
        # A late legacy request (an unconditional put or delete on the old keys) cannot reach the new index.
        self.raw_public.put(LEGACY_INDEX, b'{"late": "legacy write"}')
        LocalObjectStore.delete(self.raw_public, LEGACY_PAGE)
        self.assertEqual(json.loads(self.raw_public.get(INDEX))["state"], "withdrawn")


class Criteria(Settle):
    def test_c1_no_commit_is_prepared_while_a_withdrawal_is_in_progress(self):
        files = self.build("2026-11-09T10:20:00Z")
        pub.stop(self.private, self.clock, "take down")
        self.raw_public.script[INDEX] = ["lost"]
        self.assertTrue(pub.withdraw(self.private, self.public, self.clock)["withdrawal_unconfirmed"])
        pub.resume(self.private)
        commits_before = self.private.list(pub.COMMIT_PREFIX)
        self.log.clear()
        with self.assertRaises(pub.PublishPaused):
            self.publish(files)
        head, etag = pub.read_head(self.private)
        inc, out = qualify_generation(files)
        for kind in pub.INDEX_KINDS:
            with self.subTest(kind):
                with self.assertRaises(pub.PublishPaused):
                    pub._prepare(self.private, head, etag, kind=kind, inc=inc, gen=generation_name(files),
                                 body=commit_index(out[INDEX], "c" * 32), page_set={},
                                 obs={"etag": self.etag(), "nonce": self.live_nonce()}, reading=self.reading())
        with self.assertRaises(pub.PublishPaused):
            pub._pin(self.private, head, etag, generation_name(files), files, "publish", self.reading(),
                     pub.DEFAULT_POLICY, {})
        with self.assertRaises(pub.PublishPaused):
            pub.reseal(self.private, self.public, self.clock)
        self.assertEqual(self.log, [], "no public write of any kind")
        self.assertEqual(self.private.list(pub.COMMIT_PREFIX), commits_before, "not even a commit record")
        self.assertEqual(pub.read_head(self.private)[1], etag, "the head is unchanged")

    def test_c2_retirement_is_refused_when_the_head_gained_a_commit_naming_the_incarnation(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.private.fail = ("put", lambda k: k.startswith(pub.COMMIT_PREFIX), 0)   # every page PUT answered
        with self.assertRaises(RuntimeError):
            self.publish(files)                                             # B.1: pinned, never offered
        stale_head, stale_etag = pub.read_head(self.private)                # the cleanup's earlier read
        inc, out = qualify_generation(files)
        live = stale_head["live"]
        head, etag, _nonce = pub._prepare(self.private, stale_head, stale_etag, kind="publish", inc=inc, gen=B,
                                          body=commit_index(out[INDEX], "d" * 32), page_set={},
                                          obs={"etag": live["etag"], "nonce": live["nonce"]}, reading=self.reading())
        self.at("2026-11-10T10:20:00Z")
        before = self.pages_of(B)
        self.log.clear()
        report = pub.PublishReport("x", "publish")
        with self.assertRaises(pub.PublishConflict):                        # the mark cannot commit on a stale head
            pub._retire(self.private, self.public, stale_head, stale_etag, self.reading(), VERIFIED, report, set())
        self.assertEqual([k for op, k in self.log if op == "delete"], [], "no delete was sent")
        _head, _etag, classes = pub._retire(self.private, self.public, head, etag, self.reading(), VERIFIED, report, set())
        self.assertEqual(classes[inc]["class"], "open", "on the current head it is protected")
        self.assertEqual(self.pages_of(B), before)
        self.assertEqual(self.head()["incarnations"][inc]["state"], "pinned")

    def test_c3_an_ambiguous_head_write_that_did_not_land_permits_no_public_action(self):
        files = self.build("2026-11-09T10:20:00Z")
        self.private.script[HEAD] = ["lost"]                                # the pin: outcome unknown, not applied
        self.log.clear()
        with self.assertRaises(pub.PublishConflict):
            self.publish(files)
        self.assertEqual(self.log, [], "no page and no index write")
        self.assertEqual(self.head()["incarnations"].keys(), {self.A + ".1"})
        self.publish(files)
        self.assertEqual(self.public_files(), qualified(files))

    def test_c3_an_ambiguous_head_write_that_landed_is_verified_exactly_then_used(self):
        files = self.build("2026-11-09T10:20:00Z")
        self.private.script[HEAD] = ["landed"]                              # applied; the answer is lost
        self.publish(files)
        self.assertEqual(self.public_files(), qualified(files), "the committed head is exactly this write: proceed")

    def test_c3_an_ambiguous_head_write_overtaken_by_another_actor_is_not_assumed(self):
        files = self.build("2026-11-09T10:20:00Z")
        self.private.script[HEAD] = ["lost"]

        def other_actor():
            head, etag = pub.read_head(self.private)
            LocalObjectStore.put_if_match(self.private, HEAD, json.dumps(dict(head, seq=head["seq"] + 1)).encode(), etag)
        self.private.before_cas[HEAD] = other_actor
        self.log.clear()
        with self.assertRaises(pub.PublishConflict):
            self.publish(files)
        self.assertEqual(self.log, [])

    def test_c4_capacity_counts_in_flight_and_open_incarnations_not_just_listed_pages(self):
        per = len(self.pages_of(self.A))
        two = pub.GenerationPolicy(page_cap=2 * per)
        files_b = self.build("2026-11-09T10:20:00Z")
        self.raw_public.fail = ("put", lambda k: k != INDEX, 0)             # pinned; not one page written
        with self.assertRaises(RuntimeError):
            self.publish(files_b, two)
        head, etag = pub.read_head(self.private)
        listed = self.public.pages_by_incarnation()
        self.assertEqual(sum(len(v) for v in listed.values()), per, "the listing shows only A's pages")
        self.assertEqual(pub.reserved_pages(head, listed), 2 * per, "B's reservation counts although nothing is written")
        self.assertEqual(len(pub._unsettled_uploads(head["incarnations"][generation_name(files_b) + ".1"])), 1,
                         "G6 r1: the crashed PUT had no answer, so B.1 is quarantined, not an orphan")
        files_c = self.build("2026-11-09T10:35:00Z")
        with self.assertRaises(pub.PublishPaused):
            pub._pin(self.private, head, etag, generation_name(files_c), files_c, "publish", self.reading(), two, listed)
        self.assertEqual(pub.read_head(self.private)[1], etag, "a refused admission changes nothing")

    def test_c4_an_open_commit_keeps_its_reservation(self):
        per = len(self.pages_of(self.A))
        two = pub.GenerationPolicy(page_cap=2 * per, clock_bound_verified=True)
        files_b = self.build("2026-11-09T10:20:00Z")
        self.raw_public.script[INDEX] = ["hold"]
        with self.assertRaises(pub.PublishIntegrityError):
            self.publish(files_b, two)                                      # B.1 written; its commit is open
        with self.assertRaises(pub.PublishPaused):
            self.publish(self.build("2026-11-09T10:35:00Z"), two)           # C would be a third page set
        self.assertTrue(self.pages_of(generation_name(files_b)), "an open incarnation is never evicted")
        self.assertEqual(self.live(), self.A + ".1")

    def test_c4_two_admissions_from_the_same_head_cannot_both_reserve(self):
        per = len(self.pages_of(self.A))
        two = pub.GenerationPolicy(page_cap=2 * per)
        head, etag = pub.read_head(self.private)                            # room for exactly one more
        files_b, files_c = self.build("2026-11-09T10:20:00Z"), self.build("2026-11-09T10:35:00Z")
        listed = self.public.pages_by_incarnation()
        pub._pin(self.private, head, etag, generation_name(files_b), files_b, "publish", self.reading(), two, listed)
        with self.assertRaises(pub.PublishConflict):
            pub._pin(self.private, head, etag, generation_name(files_c), files_c, "publish", self.reading(), two, listed)
        now = self.head()
        self.assertEqual(sorted(now["incarnations"]), sorted([self.A + ".1", generation_name(files_b) + ".1"]))
        self.assertEqual(pub.reserved_pages(now, listed), 2 * per, "never over the cap")


class Uploads(Settle):
    """G6 r1. A page PUT that times out may still land. Its upload attempt was recorded in the head
    before the first PUT and is never recorded answered, so the incarnation keeps its head entry and
    full reservation: no empty listing, elapsed time, process exit, identical re-upload or tombstone
    releases it. NOT RUN."""

    def timed_out(self, when, policy=pub.DEFAULT_POLICY):
        """B's first page PUT times out (no answer) and is held; the run fails at the pages stage."""
        files = self.build(when)
        B = generation_name(files)
        self.raw_public.hold_put = lambda k: pub.incarnation_of(k) == B + ".1"
        with self.assertRaises(TimeoutError):
            self.publish(files, policy)
        self.assertEqual(len(self.raw_public.held_puts), 1)
        self.assertEqual(pub.read_state(self.private)["last_failure"]["stage"], "pages")
        return files, B

    def test_u1_timeout_then_next_run_cleanup_and_admission_then_late_landing_stays_within_the_cap(self):
        per = len(self.pages_of(self.A))
        two = pub.GenerationPolicy(page_cap=2 * per)                        # room for A and one more
        _files_b, B = self.timed_out("2026-11-09T10:20:00Z", two)
        head = self.head()
        self.assertEqual(len(pub._unsettled_uploads(head["incarnations"][B + ".1"])), 1, "recorded before the PUT")
        self.assertEqual(self.pages_of(B), [], "the listing shows nothing of B")
        self.assertEqual(self.klass(B + ".1", two), "quarantined")
        self.assertIn("page_uploads_unsettled", pub.status(self.private, self.public, self.clock, policy=two)["problems"])
        # The next run (ordinary lease, not broken). Before r1, cleanup saw an empty pinned orphan, retired
        # it, released its reservation and admitted C: then the late PUT put the store over the cap.
        with self.assertRaises(pub.PublishPaused):
            self.publish(self.build("2026-11-09T10:35:00Z"), two)
        head = self.head()
        self.assertEqual(head["incarnations"][B + ".1"]["state"], "pinned", "not retired by the empty listing")
        listed = self.public.pages_by_incarnation()
        self.assertEqual(pub.reserved_pages(head, listed), 2 * per, "B's full reservation is retained")
        self.assertEqual(self.live(), self.A + ".1")
        # The timed-out PUT lands now.
        self.assertTrue(self.raw_public.land_put())
        listed = self.public.pages_by_incarnation()
        self.assertEqual(len(listed[B + ".1"]), 1, "the late page is present")
        self.assertLessEqual(sum(len(v) for v in listed.values()), two.page_cap)
        self.assertEqual(pub.reserved_pages(self.head(), listed), 2 * per, "it was already reserved")
        self.assertEqual(self.klass(B + ".1", two), "quarantined", "landing is not settlement either")

    def test_u2_elapsed_time_and_later_runs_do_not_settle_and_a_late_page_is_retained(self):
        _files_b, B = self.timed_out("2026-11-09T10:20:00Z")
        self.at("2026-11-10T10:20:00Z")                                     # a day later
        self.publish(self.build("2026-11-10T10:20:00Z"), VERIFIED)          # cleanup runs; C is admitted
        self.at("2026-11-10T13:00:00Z")
        self.publish(self.build("2026-11-10T13:00:00Z"), VERIFIED)          # and again, past every grace
        self.assertEqual(self.klass(B + ".1", VERIFIED), "quarantined")
        self.assertEqual(self.head()["incarnations"][B + ".1"]["state"], "pinned")
        self.assertTrue(self.raw_public.land_put())                         # a day late
        self.publish(self.build("2026-11-10T13:30:00Z"), VERIFIED)
        self.assertEqual(len(self.pages_of(B)), 1, "retained: it is counted in B's reservation, never evicted")
        self.assertEqual(self.klass(B + ".1", VERIFIED), "quarantined", "landing is not recorded settlement")

    def test_u4_withdrawal_after_a_timeout_keeps_the_reservation_and_deletes_what_lands_later(self):
        _files_b, B = self.timed_out("2026-11-09T10:20:00Z")
        pub.stop(self.private, self.clock, "take down")
        out = pub.withdraw(self.private, self.public, self.clock)
        self.assertFalse(out["withdrawal_unconfirmed"], "the tombstone is confirmed: that covers the index only")
        self.assertEqual(out["uploads_unsettled"], [B + ".1"])
        self.assertIn("reappear", out["uploads_note"])
        self.assertEqual(self.raw_public.list(pub.PAGES_PREFIX), [])
        head = self.head()
        self.assertEqual(sorted(head["incarnations"]), [B + ".1"], "A.1 is released; B.1 is not")
        self.assertEqual(head["incarnations"][B + ".1"]["state"], "retiring")
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("page_uploads_unsettled", st["problems"])
        self.assertGreater(st["generations"][B + ".1"]["reserved"], 0, "the full reservation stays")
        self.assertEqual(st["generations"][B + ".1"]["pages"], 0)
        # The PUT sent before the withdrawal lands after it: the page reappears, named by no index.
        self.assertTrue(self.raw_public.land_put())
        self.assertEqual(len(self.raw_public.list(pub.PAGES_PREFIX)), 1)
        self.assertEqual(json.loads(self.raw_public.get(INDEX))["state"], "withdrawn")
        # Every later withdraw deletes what landed; the entry and its reservation stay.
        out = pub.withdraw(self.private, self.public, self.clock)
        self.assertEqual(out["deleted"], 1)
        self.assertEqual(self.raw_public.list(pub.PAGES_PREFIX), [])
        self.assertEqual(out["uploads_unsettled"], [B + ".1"])
        self.assertIn(B + ".1", self.head()["incarnations"])

    def test_u4_after_resume_cleanup_deletes_a_late_page_and_reports_the_quarantine(self):
        _files_b, B = self.timed_out("2026-11-09T10:20:00Z")
        pub.stop(self.private, self.clock, "take down")
        pub.withdraw(self.private, self.public, self.clock)
        self.assertTrue(self.raw_public.land_put())                         # lands after the withdrawal
        pub.resume(self.private)
        report = self.publish(self.build("2026-11-09T10:35:00Z"))
        self.assertEqual(self.pages_of(B), [], "cleanup deleted the late page (a retiring namespace)")
        self.assertEqual(report.quarantined, [B + ".1"])
        self.assertEqual(self.head()["incarnations"][B + ".1"]["state"], "retiring")

    def test_u5_an_upload_intent_write_that_does_not_land_sends_no_page(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        real = pub._write_head

        def lossy(private, head, etag, intent):
            if intent.startswith("uploads started"):
                self.private.script[HEAD] = ["lost"]                        # ambiguous, not applied
            return real(private, head, etag, intent)

        self.log.clear()
        with mock.patch.object(pub, "_write_head", lossy):
            with self.assertRaises(pub.PublishConflict):
                self.publish(files)
        self.assertEqual([k for op, k in self.log if op == "put" and pub.incarnation_of(k) == B + ".1"], [],
                         "no page PUT without a recorded upload attempt")
        self.assertNotIn("uploads", self.head()["incarnations"][B + ".1"])
        self.assertEqual(self.klass(B + ".1"), "orphan", "nothing was sent, so nothing is outstanding")

    def test_u6_a_retiring_mark_that_commits_first_refuses_the_upload(self):
        files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files)
        self.private.fail = ("put", lambda k: k.startswith(pub.COMMIT_PREFIX), 0)
        with self.assertRaises(RuntimeError):
            self.publish(files)                                             # B.1 pinned, uploads answered
        stale_head, stale_etag = pub.read_head(self.private)
        pub.stop(self.private, self.clock, "take down")
        pub.withdraw(self.private, self.public, self.clock)                 # marks B.1 retiring first
        for name, (head, etag) in {"its stale head": (stale_head, stale_etag),
                                   "a fresh head": pub.read_head(self.private)}.items():
            with self.subTest(name):
                with self.assertRaises(pub.PublishConflict):
                    pub._record_upload(self.private, head, etag, B + ".1", "e" * 32, self.reading(), answered=False)
        self.assertNotIn("uploads", self.head()["incarnations"].get(B + ".1") or {})


class Ambiguity(Settle):
    def test_an_unanswered_write_that_never_lands_is_closed_by_the_next_landed_write(self):
        _files, B = self.held_publish("2026-11-09T10:20:00Z")
        (c2,) = self.open()
        self.assertEqual(self.live(), self.A + ".1")
        self.assertEqual(self.klass(B + ".1"), "open")
        files_c = self.build("2026-11-09T10:35:00Z")
        self.publish(files_c)                                               # no pause: C is conditioned on A's ETag
        self.assertEqual(self.public_files(), qualified(files_c))
        self.assertEqual(self.open(), {}, "c2's condition can no longer hold")
        self.assertEqual(self.events(B + ".1"), ["pinned"], "it never landed: never offered")
        self.assertTrue(self.raw_public.deliver().rejected, "the late request gets 412")
        self.assertEqual(self.public_files(), qualified(files_c))
        self.assertIsNotNone(pub.read_commit(self.private, c2), "its record stays for the audit trail")

    def test_no_timer_or_operator_note_closes_an_open_commit(self):
        _files, B = self.held_publish("2026-11-09T10:20:00Z")
        self.at("2026-11-12T10:20:00Z")                                     # three days on both clocks
        st = pub.status(self.private, self.public, self.clock, policy=VERIFIED)
        self.assertEqual(st["generations"][B + ".1"]["class"], "open")
        self.assertIn("open_index_commits", st["problems"])
        self.assertFalse(hasattr(pub, "fence"))
        self.assertTrue(self.raw_public.deliver().applied, "it can still land while A's index is live")
        self.assertEqual(self.live(), B + ".1")
        self.assert_index_consistent()

    def test_a_known_rejection_closes_the_commit_and_records_who_is_live(self):
        _files, B = self.held_publish("2026-11-09T10:20:00Z")
        files_c = self.build("2026-11-09T10:35:00Z")
        C = generation_name(files_c)
        self.raw_public.before_cas[INDEX] = self.raw_public.deliver         # B's write lands just before C's
        with self.assertRaises(pub.PublishConflict):
            self.publish(files_c)
        self.assertEqual(self.live(), B + ".1")
        head = self.head()
        self.assertEqual(sorted(head["commits"]), [head["live"]["nonce"]], "C's rejected commit is closed")
        self.assertEqual(self.events(B + ".1"), ["pinned", "published"])
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published", "ceased"])
        self.assertEqual(self.events(C + ".1"), ["pinned"])
        self.publish(files_c)                                               # now conditioned on B's index
        self.assertEqual(self.public_files(), qualified(files_c))

    def test_repeated_contents_do_not_revive_an_old_request(self):
        _files, _B = self.held_publish("2026-11-09T10:20:00Z")              # c2 expects the ETag of A's first index
        self.publish(self.build("2026-11-09T10:35:00Z"))
        pub.stop(self.private, self.clock, "rollback")
        pub.restore(self.private, self.public, self.A, self.clock)          # A again: same content, new nonce
        raw = self.raw_public.get(INDEX)
        self.assertEqual(with_nonce0(raw), with_nonce0(self.A_index_raw), "the same index content")
        self.assertNotEqual(content_etag(raw), content_etag(self.A_index_raw), "but never the same ETag")
        self.assertTrue(self.raw_public.deliver().rejected)
        self.assertEqual(self.raw_public.get(INDEX), raw)

    def test_reseal_closes_an_unwanted_open_restore(self):
        files_b = self.build("2026-11-09T10:20:00Z")
        B = generation_name(files_b)
        self.publish(files_b)
        pub.stop(self.private, self.clock, "rollback")
        self.raw_public.script[INDEX] = ["hold"]
        with self.assertRaises(pub.PublishIntegrityError):
            pub.restore(self.private, self.public, self.A, self.clock)
        (restore_commit,) = self.open()
        before = self.live_nonce()
        out = pub.reseal(self.private, self.public, self.clock)
        self.assertEqual((out["resealed"], out["closed_commits"], out["open_commits"]), (B + ".1", [restore_commit], []))
        self.assertNotEqual(self.live_nonce(), before)
        self.assertEqual(self.public_files(), qualified(files_b), "readers see the same generation")
        self.assertEqual(self.events(B + ".1"), ["pinned", "published"], "a reseal ceases nothing")
        self.assertEqual(self.events(self.A + ".1"), ["pinned", "published", "ceased"])
        self.assertTrue(self.raw_public.deliver().rejected)

    def test_a_412_closes_the_commit_at_once_and_leaves_nothing_open(self):
        files = self.build("2026-11-09T10:20:00Z")
        self.raw_public.script[INDEX] = ["reject"]                          # a 412 with nothing else changed
        with self.assertRaises(pub.PublishConflict):
            self.publish(files)
        self.assertEqual(self.live(), self.A + ".1")
        self.assertEqual(self.open(), {}, "known rejected: closed at once, not left open")


class Genesis(Base):
    def setUp(self):
        super().setUp()
        self.raw_public = Scripted(self.tmp / "public-fresh", self.log)
        self.public = pub.PublicViewStore(self.raw_public)
        from market_data.news.archive import collect_once
        from test_news_support import feeds
        collect_once(self.private, self.spool, feeds(), self.clock, streams=list(self.cfg["streams"]),
                     only_sources=list(self.cfg["sources"]))
        self.files = self.generation("2026-11-09T10:05:00Z")

    def test_the_key_is_created_once_with_a_tombstone_then_only_swapped(self):
        pub.publish(self.private, self.public, self.files, self.clock)
        puts = [k for op, k in self.log if op == "put"]
        self.assertEqual(puts[0], INDEX, "the create-only tombstone comes before any page")
        self.assertEqual(puts[-1], INDEX)
        self.assertEqual(self.public_files(), qualified(self.files))
        kinds = sorted(c["kind"] for c in (pub.read_commit(self.private, k.rsplit("/", 1)[1][:-5])
                                           for k in self.private.list(pub.COMMIT_PREFIX)))
        self.assertEqual(kinds, ["genesis", "publish"])

    def test_an_unanswered_create_that_did_not_land_is_sent_again_by_the_next_run(self):
        self.raw_public.script[INDEX] = ["lost"]
        with self.assertRaises(pub.PublishIntegrityError):
            pub.publish(self.private, self.public, self.files, self.clock)
        self.assertIsNone(self.raw_public.get(INDEX))
        self.assertEqual(self.raw_public.list(pub.PAGES_PREFIX), [], "no page before the key exists")
        pub.publish(self.private, self.public, self.files, self.clock)
        self.assertEqual(self.public_files(), qualified(self.files))
        self.assertEqual(pub._open_commits(self.head()), {}, "the first create can never succeed now")

    def test_an_unanswered_create_that_landed_is_observed_not_repeated(self):
        self.raw_public.script[INDEX] = ["landed"]
        with self.assertRaises(pub.PublishIntegrityError):
            pub.publish(self.private, self.public, self.files, self.clock)
        self.assertEqual(json.loads(self.raw_public.get(INDEX))["state"], "unpublished")
        self.log.clear()
        pub.publish(self.private, self.public, self.files, self.clock)
        self.assertEqual([k for op, k in self.log if op == "put" and k == INDEX], [INDEX], "one index write: the publication")
        self.assertEqual(self.public_files(), qualified(self.files))


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Client:
    def __init__(self, error=None, etag='"new"'):
        self.error, self.etag, self.calls = error, etag, []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ETag": self.etag}


class ProviderMapping(unittest.TestCase):
    """How the bucket store maps one request's answer to an outcome. A fake client: this pins the
    code's reading of the SDK contract, not the SDK or the provider."""

    def store(self, **kw):
        client = _Client(**kw)
        return R2ObjectStore(client=None, bucket="b", cas_client=client), client

    def test_conditions_and_write_id_are_sent_once(self):
        store, client = self.store()
        out = store.put_if_match("news/v2/views/index.json", b"{}", '"old"', "application/json", "no-store", "n" * 32)
        self.assertEqual(out, CasOutcome("applied", '"new"'))
        (call,) = client.calls
        self.assertEqual((call["IfMatch"], call["Metadata"], call["CacheControl"]), ('"old"', {"write-id": "n" * 32}, "no-store"))
        self.assertNotIn("IfNoneMatch", call)
        store, client = self.store()
        store.put_if_match("news/v2/views/index.json", b"{}", None)
        self.assertEqual((client.calls[0]["IfNoneMatch"], "IfMatch" in client.calls[0]), ("*", False))

    def test_only_412_is_a_known_rejection(self):
        for code in ("PreconditionFailed", "412"):
            store, client = self.store(error=_Err(code))
            self.assertTrue(store.put_if_match("k/x.json", b"{}", '"old"').rejected, code)
            self.assertEqual(len(client.calls), 1)
        for error in (_Err("InternalError"), _Err("AccessDenied"), _Err("ObjectLockedByBucketPolicy"),
                      _Err("ConditionalRequestConflict"), TimeoutError("read timed out"), ConnectionResetError()):
            store, client = self.store(error=error)
            out = store.put_if_match("k/x.json", b"{}", '"old"')
            self.assertTrue(out.ambiguous, repr(error))
            self.assertEqual(len(client.calls), 1, "no retry inside the store")

    def test_without_a_no_retry_client_conditional_writes_are_refused(self):
        with self.assertRaises(ArchiveError):
            R2ObjectStore(client=_Client(), bucket="b").put_if_match("k/x.json", b"{}", None)

    def test_the_local_stand_in_evaluates_the_condition(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(tmp)
            self.assertTrue(store.put_if_match("k/x.json", b"one", None).applied)
            self.assertTrue(store.put_if_match("k/x.json", b"two", None).rejected, "create-only on an existing key")
            self.assertTrue(store.put_if_match("k/x.json", b"two", '"wrong"').rejected)
            body, etag = store.get_versioned("k/x.json")
            self.assertEqual((body, etag), (b"one", content_etag(b"one")))
            self.assertEqual(store.put_if_match("k/x.json", b"two", etag), CasOutcome("applied", content_etag(b"two")))
            self.assertTrue(store.put_if_match("k/y.json", b"z", '"any"').rejected, "If-Match on an absent key fails")
            self.assertIsNone(store.get_versioned("k/none.json"))
        self.assertEqual(json.loads(tombstone_index("0" * 32, "unpublished"))["state"], "unpublished")


if __name__ == "__main__":
    unittest.main()
