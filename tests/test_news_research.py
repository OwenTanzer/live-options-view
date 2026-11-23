"""P1: research captures and annotations (market_data/news/research.py).

Mode-specific time gates are explicit tables here (Chej, 2026-11-12: "make the mode-specific time gates
explicit in tests, including publication's deliberate hindsight exception, and show capture-commit
times in the cutoff fixtures"). Stores are temporary directories; inputs are the prepared bundle and
the SYNTHETIC timeline in tests/news_research_scenarios.py. Nothing is fetched.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import news_research_scenarios as S  # noqa: E402
from market_data.news import research as R  # noqa: E402
from market_data.news.archive import LocalObjectStore  # noqa: E402

# Capture-commit and annotation times of the SYNTHETIC timeline (the cutoff fixtures below use these).
SYN_TIMES = {
    "S-site rev1": {"published": "2026-11-01T12:00Z", "accessed": "2026-11-02T09:00:00Z",
                    "run": "2026-11-02T09:30:00Z", "capture_commit": "2026-11-02T11:00:00Z (delayed)"},
    "S-a v1": {"annotated": "2026-11-02T12:00:00Z", "commit": "2026-11-02T12:00:05Z"},
    "S-obs": {"observed": "2026-11-02T08:00:00Z", "archived": "2026-11-02T08:00:05Z",
              "annotated": "2026-11-02T13:00:00Z", "commit": "2026-11-02T15:00:00Z (delayed)"},
    "S-a v2": {"annotated": "2026-11-03T10:00:00Z", "commit": "2026-11-03T10:00:05Z"},
    "S-site rev2": {"updated": "2026-11-04T07:30Z", "accessed": "2026-11-04T07:45:00Z",
                    "capture_commit": "2026-11-04T08:00:05Z"},
    "S-b": {"annotated": "2026-11-04T09:00:00Z", "commit": "2026-11-04T09:00:05Z"},
    "S-13f": {"edgar_acceptance": "2026-11-16T21:05:12Z", "accessed": "2026-11-17T14:00:00Z",
              "capture_commit": "2026-11-17T14:30:05Z", "due": "2026-11-16"},
}


def summary(view):
    """{source title prefix: [(lineage, version, made_after_cutoff)]} plus revision numbers."""
    out = {}
    for it in view["items"]:
        s = it["source"]
        key = (s.get("capture_key") or s["item_id"], it["revision_count"])
        out[key] = sorted((e["annotation"]["lineage"], e["annotation"]["version"], e["made_after_cutoff"])
                          for e in it["annotations"])
    return out


class Synthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store, cls.obs, cls.log = S.synthetic_store(Path(cls.tmp.name) / "s")
        cls.ledger = R.load_research(cls.store)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def view(self, cutoff, mode):
        return R.research_view(self.ledger, cutoff, mode, self.obs)

    def test_commit_times_are_recorded_as_stated_in_the_fixture(self):
        runs = {r.manifest["started_at"]: r.committed_at for r in self.ledger.runs}
        self.assertEqual(runs["2026-11-02T09:30:00Z"], "2026-11-02T11:00:00Z", "R1 commit delayed to the settle")
        self.assertEqual(runs["2026-11-02T13:00:00Z"], "2026-11-02T15:00:00Z", "R3 commit delayed to the settle")
        self.assertEqual(runs["2026-11-17T14:30:00Z"], "2026-11-17T14:30:05Z")
        self.assertIsNone(self.log["R1"]["committed_at"])
        self.assertEqual(self.log["R1_settled"], [self.log["R1"]["run_id"]])

    def test_research_mode_gate_table(self):
        site1, site2 = ("S-site", 1), ("S-site", 2)
        obs = ("obs-item-1", 1)
        table = [
            # cutoff                expected (source -> visible annotations)            why
            ("2026-11-02T08:59:00Z", {}, "capture not yet accessed"),
            ("2026-11-02T09:15:00Z", {}, "accessed 09:00 but its run starts 09:30 and is uncommitted"),
            ("2026-11-02T10:00:00Z", {}, "capture commit delayed to 11:00"),
            ("2026-11-02T11:30:00Z", {site1: []}, "capture committed 11:00; no annotation yet"),
            ("2026-11-02T12:00:02Z", {site1: []}, "S-a v1 made 12:00:00, committed 12:00:05"),
            ("2026-11-02T12:30:00Z", {site1: [("S-a", "syn-1", False)]}, "S-a v1 made and committed"),
            ("2026-11-02T14:00:00Z", {site1: [("S-a", "syn-1", False)]}, "S-obs made 13:00, commit delayed to 15:00"),
            ("2026-11-02T15:30:00Z", {site1: [("S-a", "syn-1", False)], obs: [("S-obs", "syn-1", False)]},
             "S-obs committed; the collected revision was archived 08:00:05"),
            ("2026-11-03T12:00:00Z", {site1: [("S-a", "syn-2", False)], obs: [("S-obs", "syn-1", False)]},
             "S-a revised (v2) after the earlier cutoffs"),
            ("2026-11-04T07:50:00Z", {site1: [("S-a", "syn-2", False)], obs: [("S-obs", "syn-1", False)]},
             "rev 2 accessed 07:45 but its run commits 08:00:05: rev 1 still shown"),
            ("2026-11-04T08:30:00Z", {site2: [], obs: [("S-obs", "syn-1", False)]},
             "rev 2 shown; S-a was made for rev 1 and is not carried over"),
            ("2026-11-04T09:30:00Z", {site2: [("S-b", "syn-1", False)], obs: [("S-obs", "syn-1", False)]},
             "S-b on rev 2"),
        ]
        for cutoff, expected, why in table:
            with self.subTest(cutoff=cutoff, why=why):
                got = {k: v for k, v in summary(self.view(cutoff, "research")).items() if k[0] != "S-13f"}
                self.assertEqual(got, expected)

    def test_research_mode_lists_lineages_left_on_an_earlier_revision(self):
        it = [i for i in self.view("2026-11-04T09:30:00Z", "research")["items"] if i["source"].get("capture_key") == "S-site"][0]
        self.assertEqual(it["annotations_on_other_revisions"], ["S-a"])

    def test_collector_and_archive_never_show_captures_and_gate_annotations_differently(self):
        obs = ("obs-item-1", 1)
        table = [
            # cutoff                 collector                         archive                          why
            ("2026-11-02T12:30:00Z", {},                               {},                              "S-obs not made yet (13:00)"),
            ("2026-11-02T13:30:00Z", {obs: [("S-obs", "syn-1", False)]}, {},                            "collector: made 13:00; archive: commit 15:00"),
            ("2026-11-02T15:30:00Z", {obs: [("S-obs", "syn-1", False)]}, {obs: [("S-obs", "syn-1", False)]}, "both"),
            ("2026-11-20T00:00:00Z", {obs: [("S-obs", "syn-1", False)]}, {obs: [("S-obs", "syn-1", False)]},
             "captures (S-site, S-13f) are never visible in either mode"),
        ]
        for cutoff, collector, archive, why in table:
            with self.subTest(cutoff=cutoff, why=why):
                self.assertEqual(summary(self.view(cutoff, "collector")), collector)
                self.assertEqual(summary(self.view(cutoff, "archive")), archive)

    def test_archive_mode_requires_the_observation_commit_too(self):
        late = dict(self.obs[0], archived_at="2026-11-02T16:00:00Z")
        v = R.research_view(self.ledger, "2026-11-02T15:30:00Z", "archive", [late])
        self.assertEqual(v["items"], [], "observation archived 16:00 > cutoff")
        v = R.research_view(self.ledger, "2026-11-02T15:30:00Z", "collector", [late])
        self.assertEqual(len(v["items"]), 1, "collector mode ignores the archive commit")

    def test_publication_mode_is_hindsight_by_design(self):
        table = [
            # cutoff                 expected                                                     why
            ("2026-11-01T11:59:00Z", {}, "published 12:00 (minute precision: latest 12:00:59)"),
            ("2026-11-01T12:01:00Z", {("S-site", 2): [("S-b", "syn-1", True)]},
             "before ANY access or annotation: everything known now, latest revision, every lineage flagged"),
            ("2026-11-02T07:55:00Z", {("S-site", 2): [("S-b", "syn-1", True)],
                                      ("obs-item-1", 1): [("S-obs", "syn-1", True)]},
             "the collected revision by its publication time (07:50)"),
            ("2026-11-20T00:00:00Z", {("S-site", 2): [("S-b", "syn-1", False)],
                                      ("S-13f", 1): [("S-13f", "syn-1", False)],
                                      ("obs-item-1", 1): [("S-obs", "syn-1", False)]},
             "after every annotation and commit: nothing flagged"),
        ]
        for cutoff, expected, why in table:
            with self.subTest(cutoff=cutoff, why=why):
                v = self.view(cutoff, "publication")
                self.assertTrue(v["hindsight"])
                self.assertIn("RETROSPECTIVE", v["label"])
                self.assertEqual(summary(v), expected)

    def test_publication_mode_shows_only_annotations_for_the_shown_revision(self):
        # The latest revision (rev 2) is shown; S-a was made for rev 1, so it is not in publication either.
        v = self.view("2026-11-20T00:00:00Z", "publication")
        it = [i for i in v["items"] if i["source"].get("capture_key") == "S-site"][0]
        self.assertEqual([e["annotation"]["lineage"] for e in it["annotations"]], ["S-b"])
        self.assertEqual(it["annotations_on_other_revisions"], ["S-a"])

    def test_edgar_acceptance_access_and_capture_commit_are_separate_gates(self):
        f = S.FILING
        self.assertEqual(f["published"]["kind"], "edgar_acceptance")
        table = [
            # cutoff                 publication  research   why
            ("2026-11-16T12:00:00Z", False, False, "the due date (11-16) is never availability"),
            ("2026-11-16T21:05:11Z", False, False, "one second before EDGAR acceptance"),
            ("2026-11-16T21:05:12Z", True,  False, "accepted: hindsight view only; we had not accessed it"),
            ("2026-11-17T14:15:00Z", True,  False, "accessed 14:00 but the capture commit is 14:30:05"),
            ("2026-11-17T14:30:05Z", True,  True,  "capture and annotation committed"),
        ]
        for cutoff, pub, res, _why in table:
            with self.subTest(cutoff=cutoff):
                for mode, want in (("publication", pub), ("research", res)):
                    got = any(i["source"].get("capture_key") == "S-13f" for i in self.view(cutoff, mode)["items"])
                    self.assertEqual(got, want, mode)
        proj = R.project(self.view("2026-11-17T15:00:00Z", "research"), "x")
        src = [i["source"] for i in proj["items"] if i["source"].get("title", "").startswith("SYNTHETIC 13F")][0]
        self.assertEqual((src["published"]["value"], src["published"]["kind"]), ("2026-11-16T21:05:12Z", "edgar_acceptance"))
        self.assertEqual(src["access"]["at"]["value"], "2026-11-17T14:00:00Z")
        self.assertEqual(src["committed_at"], "2026-11-17T14:30:05Z")
        q = [i for i in proj["items"] if i["source"]["title"].startswith("SYNTHETIC 13F")][0]["annotations"][0]["claims"][0]["quantities"][0]
        self.assertEqual(q["measured_at"], "2026-09-30", "holdings stay as of quarter end, never 'current'")

    def test_hypothesis_timing_and_withholding(self):
        v = self.view("2026-11-03T12:00:00Z", "research")
        timings = {e["annotation"]["lineage"]: e["hypothesis_timing"] for i in v["items"] for e in i["annotations"]}
        self.assertEqual(timings, {"S-a": "contemporaneous", "S-obs": "retrospective"})
        # S-obs: made 11-02 13:00 for a horizon starting 11-01: retrospective. Withheld outside publication/research.
        for mode in ("collector", "archive"):
            e = [e for i in self.view("2026-11-03T12:00:00Z", mode)["items"] for e in i["annotations"]][0]
            self.assertIn("hypothesis_withheld", e)
            p = R.project(self.view("2026-11-03T12:00:00Z", mode), "x")
            self.assertIsNone(p["items"][0]["annotations"][0]["hypothesis"])
        e = [e for i in self.view("2026-11-03T12:00:00Z", "publication")["items"] for e in i["annotations"]
             if e["annotation"]["lineage"] == "S-obs"][0]
        self.assertNotIn("hypothesis_withheld", e)

    def test_annotation_revision_chain(self):
        v = self.view("2026-11-03T12:00:00Z", "research")
        e = [e for i in v["items"] for e in i["annotations"] if e["annotation"]["lineage"] == "S-a"][0]
        self.assertEqual(e["revision_count"], 2)
        self.assertEqual(e["annotation"]["previous_annotation_id"], e["previous"][0])
        v1 = [e for i in self.view("2026-11-02T12:30:00Z", "research")["items"] for e in i["annotations"]][0]
        self.assertEqual(v1["annotation"]["annotation_id"], e["previous"][0], "the earlier cutoff sees v1 exactly")


class Prepared(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = S.prepared_store(Path(cls.tmp.name) / "p")
        cls.ledger = R.load_research(cls.store)
        cls.bundle = S.load_prepared()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_six_records_nine_captures_and_their_claims(self):
        anns = self.ledger.annotations
        self.assertEqual(sorted(a["lineage"] for a in anns), list("ABCDEF"))
        claims = {a["lineage"]: [c["id"] for c in a["claims"]] for a in anns}
        self.assertEqual(claims, {"A": ["A1", "A2", "A3"], "B": ["B1", "B2"], "C": ["C1", "C2", "C3", "C4"],
                                  "D": ["D1"], "E": ["E1", "E2", "E3"], "F": ["F1"]})
        self.assertEqual(len(self.ledger.captures), 9, "six read sources plus three unread hops kept as captures")

    def test_v2_corrections_are_kept(self):
        a = {x["lineage"]: x for x in self.ledger.annotations}
        a2 = [c for c in a["A"]["claims"] if c["id"] == "A2"][0]
        self.assertTrue(all(q["of"].startswith("net exposure / equity") for q in a2["quantities"]))
        d1 = a["D"]["claims"][0]
        self.assertEqual(d1["dates"], [{"kind": "bid_deadline", "at": "2026-11-19"}])
        self.assertTrue(all(q["measured_at"] == "unknown" for q in d1["quantities"]), "Nov 19 is not a measurement time")
        self.assertEqual(d1["action"]["status"], "offered")
        for lineage in "BC":
            self.assertEqual(len({c["action"]["kind"] for c in a[lineage]["claims"]}), len(a[lineage]["claims"]),
                             f"{lineage}: one action per claim")
        self.assertFalse([q for c in a["C"]["claims"] + [{"quantities": a["C"]["context"]}] for q in c["quantities"]
                          if "800" in str(q["value"])], "the search-summary-only figure is dropped")
        self.assertEqual([c["action"]["status"] for c in a["C"]["claims"] if c["id"] == "C4"], ["unverified"])
        self.assertTrue(all(c["evidence_kind"] == "thesis_assertion" and c["action"]["status"] == "unverified"
                            for c in a["E"]["claims"]))
        self.assertEqual(a["F"]["claims"][0]["exposure"]["direction"], "long")

    def test_unread_hops_have_no_revision_and_read_ones_point_at_their_capture(self):
        caps = {c["capture_key"]: c for c in self.ledger.captures}
        a = {x["lineage"]: x for x in self.ledger.annotations}
        b1 = a["B"]["claims"][0]["chain"]
        self.assertEqual(b1[0]["revision"]["revision_id"], caps["B-hedgeweek-walleye"]["revision_id"])
        self.assertEqual(b1[1]["revision"]["revision_id"], caps["B-business-insider"]["revision_id"])
        self.assertEqual([x["result"] for x in b1[1]["access"]["attempts"]], ["bot_blocked", "http_403"])
        self.assertIsNone(b1[2]["revision"], "the Walleye letter was never captured")
        self.assertEqual(caps["B-business-insider"]["access"]["result"], "http_403")

    def test_research_cutoffs_around_the_prepared_import(self):
        # Test clock: imported 2026-11-13T18:00:00Z, committed 18:00:05Z (a disposable-store fixture value).
        table = [("2026-11-12T16:00:00Z", 0, "before any access"),
                 ("2026-11-12T17:30:00Z", 0, "accessed (16:59:58-17:17:03) but not captured until 11-13"),
                 ("2026-11-13T18:00:03Z", 0, "imported 18:00:00, committed 18:00:05"),
                 ("2026-11-13T18:00:05Z", 9, "committed")]
        for cutoff, n, why in table:
            with self.subTest(cutoff=cutoff, why=why):
                self.assertEqual(len(R.research_view(self.ledger, cutoff, "research")["items"]), n)

    def test_prepared_hypotheses_are_timed_from_the_real_commit_not_the_design_note(self):
        v = R.research_view(self.ledger, "2026-11-13T18:01:00Z", "research")
        t = {e["annotation"]["lineage"]: e["hypothesis_timing"] for i in v["items"] for e in i["annotations"]}
        self.assertEqual(t["A"], "retrospective", "horizon started in the week of 11-09")
        self.assertEqual(t["D"], "contemporaneous", "committed 11-13, before the 11-19 bid deadline window")
        self.assertEqual(t["E"], "retrospective",
                         "the design note assumed an 11-12 commit; this commit (11-13 18:00Z) is after the 11-13 horizon start")

    def test_publication_mode_dates_and_unknown_publication_times(self):
        table = [("2026-11-06T12:00:00Z", {"B-hedgeweek-walleye", "C-hedgeweek-arini"},
                  "D is date-only (2026-11-06): its latest instant is 11-07 11:59:59Z"),
                 ("2026-11-07T12:00:00Z", {"B-hedgeweek-walleye", "C-hedgeweek-arini", "D-dcd"}, "D now"),
                 ("2026-11-09T15:31:00Z", {"A-briefs", "B-hedgeweek-walleye", "C-hedgeweek-arini", "D-dcd"},
                  "A at 10:30-05:00 = 15:30Z (minute precision, latest 15:30:59)"),
                 ("2026-11-12T00:00:00Z", {"A-briefs", "B-hedgeweek-walleye", "C-hedgeweek-arini", "D-dcd",
                                           "E-thesis-relay"}, "E by its sent time 11-10 16:09:31Z")]
        for cutoff, keys, why in table:
            with self.subTest(cutoff=cutoff, why=why):
                v = R.research_view(self.ledger, cutoff, "publication")
                self.assertEqual({i["source"]["capture_key"] for i in v["items"]}, keys)
        v = R.research_view(self.ledger, "2026-11-30T00:00:00Z", "publication")
        self.assertEqual(v["excluded"]["no_publication_time"], 4, "unknown publication time is never guessed")
        self.assertTrue(all(e["made_after_cutoff"] is False for i in v["items"] for e in i["annotations"]))
        v = R.research_view(self.ledger, "2026-11-08T00:00:00Z", "publication")
        self.assertTrue(all(e["made_after_cutoff"] for i in v["items"] for e in i["annotations"]))

    def test_projection_keeps_unknowns_precision_and_context_separate(self):
        p = R.project(R.research_view(self.ledger, "2026-11-13T18:01:00Z", "research"), self.bundle["label"])
        self.assertEqual(p["schema_version"], R.VIEW_SCHEMA)
        self.assertIn("not evidence", p["not_proof"])
        d = [i for i in p["items"] if i["source"]["title"].startswith("DCD")][0]
        self.assertEqual(d["source"]["published"]["precision"], "date")
        a = [i for i in p["items"] if i["source"]["title"].startswith("Briefs")][0]
        self.assertEqual(a["source"]["published"]["precision"], "minute")
        self.assertEqual(a["source"]["access"]["at"]["value"], "2026-11-12T16:59:58Z")
        c = [i for i in p["items"] if i["source"]["title"].startswith("Hedgeweek: Arini")][0]["annotations"][0]
        self.assertEqual([q["of"] for q in c["context"]], ["flagship fund AUM (context; never multiplied or added)",
                                                          "firm AUM (context; never multiplied or added)"])
        self.assertTrue(all("AUM" not in q["of"] for cl in c["claims"] for q in cl["quantities"]))
        e = [i for i in p["items"] if i["source"]["party"].startswith("Owen")][0]
        self.assertIn("upper bound", e["source"]["access"]["at"]["basis"])
        self.assertIsNone(e["source"]["url"])

    def test_projection_contains_no_third_party_text_fields(self):
        p = R.project(R.research_view(self.ledger, "2026-11-13T18:01:00Z", "research"), self.bundle["label"])
        found = []

        def walk(n, path):
            if isinstance(n, dict):
                for k, v in n.items():
                    if k in R.TEXT_KEYS:
                        found.append(path + "." + k)
                    walk(v, path + "." + k)
            elif isinstance(n, list):
                for i, v in enumerate(n):
                    walk(v, f"{path}[{i}]")
        walk(p, "view")
        self.assertEqual(found, [])


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalObjectStore(Path(self.tmp.name) / "v")
        self.good = S.bundle([S.SITE], [{"lineage": "x", "capture": "S-site", "method": "manual", "version": "1",
                                         "claims": [S.claim("c1")]}])

    def tearDown(self):
        self.tmp.cleanup()

    def refused(self, mutate, pattern):
        b = copy.deepcopy(self.good)
        mutate(b)
        with self.assertRaisesRegex(R.ResearchError, pattern):
            R.validate_bundle(b)

    def test_the_good_bundle_passes(self):
        R.validate_bundle(self.good)

    def test_refusals(self):
        cl = lambda b: b["annotations"][0]["claims"][0]  # noqa: E731
        cases = [
            ("excerpt", lambda b: b["captures"][0].__setitem__("excerpt", "x"), "no excerpts"),
            ("nested quote", lambda b: cl(b).__setitem__("note", {"quote": "x"}) or None, "no excerpts"),
            ("two actions", lambda b: cl(b).__setitem__("action", [{"kind": "a", "status": "reported"},
                                                                  {"kind": "b", "status": "reported"}]), "one action per claim"),
            ("party and population", lambda b: cl(b).__setitem__("subject", {"party": "X", "population": "Y"}), "one subject"),
            ("no subject", lambda b: cl(b).__setitem__("subject", {}), "one subject"),
            ("unknown evidence kind", lambda b: cl(b).__setitem__("evidence_kind", "rumour"), "evidence_kind"),
            ("quantity without of", lambda b: cl(b)["quantities"][0].__setitem__("of", ""), "say what the figure is of"),
            ("bad status", lambda b: cl(b)["action"].__setitem__("status", "done"), "status"),
            ("javascript url", lambda b: b["captures"][0].__setitem__("url", "javascript:alert(1)"), "http"),
            ("long text", lambda b: cl(b).__setitem__("note", "x" * 401), "max 400"),
            ("supplied annotated_at", lambda b: b["annotations"][0].__setitem__("annotated_at", "2026-01-01"), "never supplied"),
            ("capture without access time", lambda b: b["captures"][0]["access"].pop("accessed_at"), "needs our access time"),
            ("bad time", lambda b: b["captures"][0]["published"].__setitem__("at", "Nov 1"), "YYYY-MM-DD"),
            ("unknown capture key", lambda b: b["annotations"][0].__setitem__("capture", "nope"), "not a capture"),
            ("hypothesis without horizon", lambda b: b["annotations"][0].__setitem__(
                "hypothesis", {"statement": "s", "disconfirmation": "d"}), "horizon"),
        ]
        for name, mutate, pattern in cases:
            with self.subTest(name):
                self.refused(mutate, pattern)

    def test_never_backdated_or_forward_dated(self):
        # Access after the import clock is refused; annotated_at is the import clock; the commit is the commit clock.
        with self.assertRaisesRegex(R.ResearchError, "never backdated or forward-dated"):
            R.import_bundle(self.store, self.good, S.at("2026-11-02T08:00:00Z"))
        rep = R.import_bundle(self.store, self.good, S.at("2026-11-02T09:30:00Z"), commit_clock=S.at("2026-11-02T09:40:00Z"))
        led = R.load_research(self.store)
        self.assertEqual(led.annotations[0]["annotated_at"], "2026-11-02T09:30:00Z")
        self.assertEqual(rep["committed_at"], "2026-11-02T09:40:00Z")

    def test_reimport_is_idempotent_and_a_changed_capture_is_a_new_revision(self):
        R.import_bundle(self.store, self.good, S.at("2026-11-02T09:30:00Z"))
        again = R.import_bundle(self.store, self.good, S.at("2026-11-02T10:00:00Z"))
        self.assertEqual(again["counts"], {"captures": 0, "annotations": 0})
        changed = copy.deepcopy(self.good)
        changed["captures"][0]["title"] = "SYNTHETIC: changed"
        rep = R.import_bundle(self.store, changed, S.at("2026-11-02T10:30:00Z"))
        self.assertEqual(rep["counts"], {"captures": 1, "annotations": 1},
                         "a new capture revision; the annotation now targets it, so it is a new annotation")
        caps = R.load_research(self.store).captures
        self.assertEqual(caps[1]["previous_revision_id"], caps[0]["revision_id"])
        self.assertEqual(caps[0]["item_id"], caps[1]["item_id"])

    def test_a_tampered_object_makes_the_run_corrupt_and_blocks_imports(self):
        R.import_bundle(self.store, self.good, S.at("2026-11-02T09:30:00Z"))
        key = [k for k in self.store.list(R.RESEARCH_RUNS_PREFIX) if k.endswith("annotations.jsonl.gz")][0]
        self.store.put(key, b"tampered")
        led = R.load_research(self.store)
        self.assertEqual(len(led.problems), 1)
        with self.assertRaises(Exception):
            R.import_bundle(self.store, self.good, S.at("2026-11-02T10:00:00Z"))

    def test_the_research_ledger_is_outside_the_collector_runs_prefix(self):
        from market_data.news.archive import RUNS_PREFIX, scan_history
        R.import_bundle(self.store, self.good, S.at("2026-11-02T09:30:00Z"))
        self.assertFalse(R.RESEARCH_RUNS_PREFIX.startswith(RUNS_PREFIX))
        scan = scan_history(self.store)
        self.assertEqual((scan.runs, scan.problems, scan.uncommitted), ([], [], []), "the archive never sees it")

    def test_time_bounds(self):
        b = R.time_bounds("2026-11-06")
        self.assertEqual((R.iso(b["earliest"]), R.iso(b["latest"])), ("2026-11-05T10:00:00Z", "2026-11-07T11:59:59Z"))
        b = R.time_bounds("2026-11-09T10:30-05:00")
        self.assertEqual((R.iso(b["earliest"]), R.iso(b["latest"])), ("2026-11-09T15:30:00Z", "2026-11-09T15:30:59Z"))
        self.assertIsNone(R.time_bounds("unknown"))


class Cli(unittest.TestCase):
    def test_import_settle_view(self):
        import contextlib
        import io
        from market_data.news.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "s")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["research-import", "--store", store, "--bundle", str(S.PREPARED),
                             "--clock", "2026-11-13T18:00:00Z", "--defer-commit"])
            self.assertEqual(code, 0)
            self.assertIsNone(json.loads(out.getvalue())["committed_at"])
            proj = Path(tmp) / "v.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["research-view", "--store", store, "--cutoff", "2026-11-13T19:00:00Z",
                                       "--mode", "research", "--out", str(proj)]), 0)
            self.assertEqual(json.loads(proj.read_text(encoding="utf-8"))["items"], [], "uncommitted: nothing visible")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["research-settle", "--store", store, "--clock", "2026-11-13T18:30:00Z"]), 0)
                self.assertEqual(main(["research-view", "--store", store, "--cutoff", "2026-11-13T19:00:00Z",
                                       "--mode", "research", "--out", str(proj)]), 0)
            p = json.loads(proj.read_text(encoding="utf-8"))
            self.assertEqual(len(p["items"]), 9)
            self.assertTrue(all(a["committed_at"] == "2026-11-13T18:30:00Z" for i in p["items"] for a in i["annotations"]))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["research-view", "--store", store, "--cutoff", "2026-11-13T18:20:00Z",
                                       "--mode", "research", "--out", str(proj)]), 0)
            self.assertEqual(json.loads(proj.read_text(encoding="utf-8"))["items"], [],
                             "the commit is stamped at the settle (18:30), never earlier")


if __name__ == "__main__":
    unittest.main()
