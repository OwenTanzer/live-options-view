"""M3: offline as-of join of archived news to (synthetic) QQQ prices.

Scenario on 2026-11-09 (all times UTC, all data SYNTHETIC):
  R1 10:05 collect, committed 10:06:00   fixture A v1 (oilprice) + fixture B (EIA, classifier down)
  R2 10:10 collect, committed 10:10:30   fixture A v2 (revision)
  E  10:12 enrich,  committed 10:12:20   classifies B (oil + ai_buildout)
  R3 10:15 collect, CRASH after manifest  new item C; no commit.json (availability pending)
  R4 10:25 collect, committed 10:25:40   recovers R3 (C archived 10:25:40); one entry fails to normalize
  R5 11:10 collect, committed 11:10:20   after a missed interval (45 min > 2 x 15 min)
Prices: tests/fixtures/news/prices/qqq_2026-11-09.synthetic.jsonl (see its header and the README).
"""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_news_support import FIXTURES, FailingStore, feeds, fixed, no_sleep
from market_data.news import archive as archive_mod
from market_data.news import config
from market_data.news.adapters.rss import FetchResponse
from market_data.news.archive import (LocalObjectStore, collect_once, encode_json, enrich_pending,
                                      scan_history)
from market_data.news.classify import classify_rules
from market_data.news.join import PriceSeries, join
from market_data.news.normalize import parse_iso, sha256_hex
from market_data.news.prices import PriceRecordError, load_price_file, parse_record
from market_data.news.reader import load_archive

PRICES = FIXTURES / "prices" / "qqq_2026-11-09.synthetic.jsonl"
BAD_GUID = "oilprice-fixture-normalize-fails"
TITLE_A1 = "Tanker attacked near the Strait of Hormuz"
TITLE_C = "OPEC+ delegates signal an extra output cut"


def T(hms):
    return f"2026-11-09T{hms}Z"


def serve(**dirs):
    """Serve <source_id>.xml from a per-source scenario directory."""
    by_url = {config.SOURCES[s].url: FIXTURES / d / f"{s}.xml" for s, d in dirs.items()}

    def fetch(url, timeout):
        path = by_url.get(url)
        return FetchResponse(200, path.read_bytes()) if path else FetchResponse(404, b"")
    return fetch


def eia_classifier_down(obs, run_id, at):
    if obs["source_id"] == "eia_today_in_energy":
        raise RuntimeError("synthetic: classifier unavailable for this item")
    return classify_rules(obs, run_id, at)


def failing_bad_entry():
    real = archive_mod.normalize_entry

    def normalize(entry, *a, **kw):
        if entry.get("guid") == BAD_GUID:
            raise ValueError("synthetic normalizer failure")
        return real(entry, *a, **kw)
    return mock.patch.object(archive_mod, "normalize_entry", normalize)


def build_scenario(root: Path) -> LocalObjectStore:
    store, spool = LocalObjectStore(root / "store"), root / "spool"

    def collect(at, committed, store=store, **kw):
        kw.setdefault("only_sources", ["oilprice_com"])
        return collect_once(store, spool, kw.pop("fetch"), fixed(T(at)), commit_clock=fixed(T(committed)),
                            sleep=no_sleep, **kw)

    collect("10:05:00", "10:06:00", fetch=serve(oilprice_com="scenarios/a_v1", eia_today_in_energy="scenarios/b"),
            only_sources=["oilprice_com", "eia_today_in_energy"], classifier=eia_classifier_down)
    collect("10:10:00", "10:10:30", fetch=serve(oilprice_com="scenarios/a_v2"))
    enrich_pending(store, spool, fixed(T("10:12:00")), commit_clock=fixed(T("10:12:20")))
    try:
        collect("10:15:00", "10:15:10", store=FailingStore(root / "store", fail_on="commit.json"),
                fetch=serve(oilprice_com="scenarios/c"), rebuild_index=False)
        raise AssertionError("the R3 crash did not happen")
    except RuntimeError as exc:
        assert "commit.json" in str(exc), exc
    with failing_bad_entry():
        collect("10:25:00", "10:25:40", fetch=serve(oilprice_com="scenarios/d"))
        collect("11:10:00", "11:10:20", fetch=serve(oilprice_com="scenarios/d"))
    return store


class Scenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = build_scenario(Path(cls._tmp.name))
        cls.archive = load_archive(cls.store)
        cls.prices = load_price_file(PRICES)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def j(self, cutoff, mode="collector", tolerance_s=120, **kw):
        return join(self.archive, self.prices, T(cutoff), mode, tolerance_s=tolerance_s, **kw)

    @staticmethod
    def by_title(result):
        return {(r["title"], r["revision_number"]): r for r in result["rows"]}


class RecoveryWindow(Scenario):
    """The manifest-without-receipt crash, before and after recovery."""

    def test_scenario_history_is_as_described(self):
        runs = [(r["run_kind"], r["started_at"], r["committed_at"]) for r in self.archive.runs]
        self.assertEqual(runs, [("collect", T("10:05:00"), T("10:06:00")), ("collect", T("10:10:00"), T("10:10:30")),
                                ("enrich", T("10:12:00"), T("10:12:20")), ("collect", T("10:15:00"), T("10:25:40")),
                                ("collect", T("10:25:00"), T("10:25:40")), ("collect", T("11:10:00"), T("11:10:20"))])

    def test_before_recovery_collector_has_c_and_archive_does_not(self):
        col, arc = self.j("10:20:00"), self.j("10:20:00", "archive")
        self.assertIn((TITLE_C, 1), self.by_title(col))
        self.assertNotIn((TITLE_C, 1), self.by_title(arc))
        # Read before recovery, R3 has no availability record: the reader at 10:20 sees it the same way.
        [seg] = [s for s in arc["news_coverage"]["segments"] if s["end"] == T("10:15:00")]
        self.assertEqual(seg["state"], "pending_availability")
        self.assertNotIn("sources", seg)  # unknown, not reported as clean
        self.assertEqual(arc["news_coverage"]["pending_availability_runs"], [seg["run_id"]])
        [cseg] = [s for s in col["news_coverage"]["segments"] if s["end"] == T("10:15:00")]
        self.assertEqual((cseg["state"], cseg["sources"]["attempted"]), ("partial", ["oilprice_com"]))

    def test_after_recovery_archive_has_c_at_its_recovery_time(self):
        row = self.by_title(self.j("10:30:00", "archive"))[(TITLE_C, 1)]
        self.assertEqual((row["observed_at"], row["archived_at"], row["event_at"], row["event_basis"]),
                         (T("10:15:00"), T("10:25:40"), T("10:25:40"), "archived_at"))
        self.assertEqual((row["price"]["status"], row["price"]["record"]["record_id"]), ("matched", "uq-102500"))
        col = self.by_title(self.j("10:30:00"))[(TITLE_C, 1)]
        self.assertEqual((col["event_at"], col["price"]["status"], col["price"]["record"]["record_id"]),
                         (T("10:15:00"), "stale", "uq-100900"))  # inside the underlying gap
        self.assertEqual(col["price"]["age_seconds"], 360)
        [seg] = [s for s in self.j("10:30:00", "archive")["news_coverage"]["segments"] if s["end"] == T("10:15:00")]
        self.assertEqual((seg["state"], seg["sources"]["ok"]), ("partial", ["oilprice_com"]))

    def test_cutoff_between_crash_and_recovery_commit(self):
        # R4 started 10:25:00 but committed 10:25:40; at 10:25:30 R3 is still pending in archive mode.
        arc = self.j("10:25:30", "archive")
        self.assertNotIn((TITLE_C, 1), self.by_title(arc))
        states = {s["end"]: s["state"] for s in arc["news_coverage"]["segments"]}
        self.assertEqual(states[T("10:15:00")], "pending_availability")
        self.assertEqual(states[T("10:25:00")], "pending_availability")


class Coverage(Scenario):
    def test_entry_failures_are_carried_into_coverage(self):
        segs = {s["end"]: s for s in self.j("11:15:00", "archive")["news_coverage"]["segments"]}
        r4 = segs[T("10:25:00")]
        self.assertEqual((r4["state"], r4["run_status"], r4["sources"]["failed_entries"]), ("partial", "partial", 1))
        [failure] = r4["sources"]["entry_failures"]
        self.assertEqual((failure["source_id"], failure["guid"]), ("oilprice_com", BAD_GUID))
        self.assertIn("synthetic normalizer failure", failure["error"])

    def test_missed_interval_is_flagged(self):
        segs = {s["end"]: s for s in self.j("11:15:00", "archive")["news_coverage"]["segments"]}
        self.assertTrue(segs[T("11:10:00")].get("missed_interval"))
        self.assertEqual(segs[T("11:10:00")]["gap_seconds"], 45 * 60)
        self.assertFalse(segs[T("10:25:00")].get("missed_interval"))

    def test_first_segment_and_tail_are_not_coverage(self):
        cov = self.j("10:08:00")["news_coverage"]
        self.assertEqual((cov["segments"][0]["start"], cov["segments"][0]["clipped_to_window_start"]),
                         (T("10:05:00"), True))  # the default window starts at the first run
        self.assertIn("no news coverage", cov["segments"][0]["detail_before"])
        self.assertEqual(cov["tail"], {"start": T("10:05:00"), "end": T("10:08:00"),
                                       "state": "since_last_observed_run", "seconds": 180})

    def test_runs_after_the_cutoff_are_absent(self):
        ends = [s["end"] for s in self.j("10:12:00")["news_coverage"]["segments"]]
        self.assertEqual(ends, [T("10:05:00"), T("10:10:00")])


class FixtureTimingCases(Scenario):
    """The two saved news timing cases, now joined to prices."""

    def test_fixture_a_collector_and_archive_rows(self):
        self.assertEqual(self.j("10:04:59")["rows"], [])
        col = self.by_title(self.j("10:05:30"))[(TITLE_A1, 1)]
        # The event is the observation at 10:05:00, not the cutoff; the 10:05 quote arrived at 10:05:02.
        self.assertEqual((col["event_at"], col["price"]["record"]["record_id"]), (T("10:05:00"), "uq-100400"))
        self.assertEqual(self.j("10:05:30", "archive")["rows"], [])
        arc = self.by_title(self.j("10:06:00", "archive"))[(TITLE_A1, 1)]
        self.assertEqual((arc["event_at"], arc["price"]["record"]["record_id"], arc["price"]["age_seconds"]),
                         (T("10:06:00"), "uq-100500", 60))

    def test_fixture_a_revision_is_a_separate_later_event(self):
        rows = self.by_title(self.j("10:11:00"))
        v2 = rows[("Tanker attacked near the Strait of Hormuz; crew evacuated", 2)]
        self.assertEqual((v2["event_at"], v2["price"]["record"]["record_id"]), (T("10:10:00"), "uq-100900"))
        self.assertEqual(rows[(TITLE_A1, 1)]["event_at"], T("10:05:00"))

    def test_fixture_b_stream_membership_waits_for_classification(self):
        b_title = next(o["title"] for o in self.archive.observations if o["source_id"] == "eia_today_in_energy")
        early = self.j("10:11:00", stream="oil")
        self.assertNotIn(b_title, [r["title"] for r in early["rows"]])
        self.assertEqual(early["excluded"]["unclassified_for_stream"], 1)
        col = {r["title"]: r for r in self.j("10:13:00", stream="oil")["rows"]}[b_title]
        self.assertEqual((col["observed_at"], col["event_at"], col["event_basis"]),
                         (T("10:05:00"), T("10:12:00"), "classified_at"))
        self.assertEqual((col["price"]["status"], col["price"]["age_seconds"]), ("stale", 180))
        arc = {r["title"]: r for r in self.j("10:13:00", "archive", stream="oil")["rows"]}[b_title]
        self.assertEqual((arc["event_at"], arc["event_basis"]), (T("10:12:20"), "classification archived_at"))
        # Without a stream filter the item is an event as soon as it is observed.
        nofilter = {r["title"]: r for r in self.j("10:13:00")["rows"]}[b_title]
        self.assertEqual(nofilter["event_at"], T("10:05:00"))

    def test_publication_join_is_labeled_hindsight(self):
        pub = self.j("10:03:00", "publication")
        self.assertTrue(pub["hindsight"])
        self.assertIn("RETROSPECTIVE", pub["label"])
        rows = self.by_title(pub)
        a2 = rows[("Tanker attacked near the Strait of Hormuz; crew evacuated", 2)]  # known only later
        self.assertEqual((a2["event_at"], a2["event_basis"]), (T("10:00:00"), "published_at (hindsight)"))
        self.assertEqual(a2["price"]["status"], "missing")  # nothing was available at 10:00:00 itself
        self.assertNotIn((TITLE_C, 1), rows)  # published 10:13, after this cutoff


class PriceMatching(Scenario):
    def test_late_quote_is_used_only_after_it_arrived(self):
        series = PriceSeries(self.prices.records, "etf.qqq", "underlying_quote")
        self.assertEqual(series.latest(parse_iso(T("10:06:00"))).record_id, "uq-100500")
        self.assertEqual(series.latest(parse_iso(T("10:06:01"))).record_id, "uq-100500")
        self.assertEqual(series.latest(parse_iso(T("10:06:02"))).record_id, "uq-100600")
        # Arriving at 10:06:10, it is still older than uq-100600, so it never becomes the newest price.
        self.assertEqual(series.latest(parse_iso(T("10:06:10"))).record_id, "uq-100600")

    def test_option_quotes_never_stand_in_for_the_underlying(self):
        series = PriceSeries(self.prices.records, "etf.qqq", "underlying_quote")
        self.assertTrue(all(r.record_kind == "underlying_quote" for r in series.records))
        # Option quotes keep arriving during the 10:09-10:20 underlying gap.
        self.assertTrue(any(r.record_kind == "option_quote" and T("10:12:00") < r.raw["available_at"] < T("10:20:00")
                            for r in self.prices.records))
        self.assertEqual(series.latest(parse_iso(T("10:15:00"))).record_id, "uq-100900")
        with self.assertRaises(ValueError):
            PriceSeries(self.prices.records, "etf.qqq", "option_quote")

    def test_no_row_uses_a_price_from_after_its_event(self):
        for mode in ("collector", "archive", "publication"):
            for series, tol in (("underlying_quote", 120), ("bar_1m", 120), ("bar_1d", 4 * 86400)):
                for cutoff in ("10:05:30", "10:06:00", "10:13:00", "10:20:00", "10:30:00", "11:15:00"):
                    for row in self.j(cutoff, mode, tolerance_s=tol, series=series)["rows"]:
                        rec = row["price"]["record"]
                        if rec:
                            self.assertLessEqual(rec["available_at"], row["event_at"], (mode, series, cutoff))
                            self.assertLessEqual(row["event_at"], T(cutoff))

    def test_one_minute_bar_only_after_it_closes(self):
        # Archive event 10:06:00: the 10:05-10:06 bar ends exactly then but is finalized at 10:06:03.
        row = self.by_title(self.j("10:07:00", "archive", series="bar_1m"))[(TITLE_A1, 1)]
        rec = row["price"]["record"]
        self.assertEqual((row["event_at"], rec["record_id"], rec["bar_end"], rec["available_at"]),
                         (T("10:06:00"), "b1m-1004", T("10:05:00"), T("10:05:03")))

    def test_daily_bar_only_after_the_close(self):
        series = PriceSeries(self.prices.records, "etf.qqq", "bar_1d")
        self.assertEqual(series.latest(parse_iso(T("21:04:59"))).record_id, "b1d-20261106")
        self.assertEqual(series.latest(parse_iso(T("21:05:00"))).record_id, "b1d-20261109")
        for row in self.j("11:15:00", "archive", tolerance_s=4 * 86400, series="bar_1d")["rows"]:
            self.assertEqual(row["price"]["record"]["record_id"], "b1d-20261106")

    def test_tolerance_is_explicit_and_decides_matched_vs_stale(self):
        with self.assertRaises(TypeError):
            join(self.archive, self.prices, T("10:30:00"), "collector")
        row = lambda tol: self.by_title(self.j("10:30:00", tolerance_s=tol))[(TITLE_C, 1)]["price"]
        self.assertEqual((row(359)["status"], row(360)["status"]), ("stale", "matched"))

    def test_price_coverage_intervals(self):
        cov = self.j("10:30:00", start=T("10:00:00"))["price_coverage"]
        self.assertEqual(cov["missing"], [{"start": T("10:00:00"), "end": T("10:00:02"), "seconds": 2}])
        self.assertEqual(cov["stale"], [{"start": T("10:11:00"), "end": T("10:20:02"), "seconds": 542,
                                         "last_price_record_id": "uq-100900", "last_price_time": T("10:09:00")}])
        tail = self.j("11:20:00", start=T("11:00:00"))["price_coverage"]
        self.assertEqual(tail["stale"][-1]["start"], T("11:17:00"))
        self.assertEqual(tail["stale"][-1]["end"], T("11:20:00"))

    def test_join_reports_its_inputs(self):
        out = self.j("10:30:00")
        self.assertEqual(out["schema_version"], "news-price-join/v2")
        self.assertEqual(out["price_series"]["sha256"], self.prices.sha256)
        self.assertTrue(out["price_series"]["synthetic"])
        self.assertEqual(out["price_series"]["tolerance_s"], 120)


def with_classification(archive, revision_id, classified_at, archived_at, memberships, cid):
    """A copy of the archive with one more (synthetic) classification event for a revision."""
    extra = {"classification_id": cid, "input_revision_id": revision_id, "classified_at": T(classified_at),
             "archived_at": T(archived_at), "method": "rules", "version": "rules-test", "rules_digest": "test",
             "stream_memberships": memberships, "labels": {}, "instrument_mapping": "unknown"}
    return dataclasses.replace(archive, classifications=[*archive.classifications, extra])


class EventKnownHistory(Scenario):
    """M3.1 repair 1: a row reports only what was known at its event time."""

    def rows(self, cutoff, mode="collector", archive=None, **kw):
        out = join(archive or self.archive, self.prices, T(cutoff), mode, tolerance_s=120, **kw)
        return self.by_title(out)

    def b(self):
        return next(o for o in self.archive.observations if o["source_id"] == "eia_today_in_energy")

    def test_revision_count_is_what_was_known_at_the_event(self):
        early = self.rows("10:06:00")[(TITLE_A1, 1)]
        self.assertEqual((early["revisions_known_at_event"], early["later_context"]["revisions_known_by_cutoff"],
                          early["later_context"]["revisions_after_event"]), (1, 1, []))
        late = self.rows("10:30:00")
        a1, a2 = late[(TITLE_A1, 1)], late[("Tanker attacked near the Strait of Hormuz; crew evacuated", 2)]
        self.assertEqual(a1["revisions_known_at_event"], 1)
        self.assertEqual([(r["known_at"], r["title"]) for r in a1["later_context"]["revisions_after_event"]],
                         [(T("10:10:00"), "Tanker attacked near the Strait of Hormuz; crew evacuated")])
        self.assertEqual(a2["revisions_known_at_event"], 2)
        archive_a1 = self.rows("10:30:00", "archive")[(TITLE_A1, 1)]
        self.assertEqual(archive_a1["later_context"]["revisions_after_event"][0]["known_at"], T("10:10:30"))

    def test_unfiltered_row_does_not_carry_a_later_classification(self):
        b = self.rows("10:13:00")[(self.b()["title"], 1)]
        self.assertEqual(b["event_at"], T("10:05:00"))
        self.assertIsNone(b["classification_at_event"])
        [later] = b["later_context"]["classifications_after_event"]
        self.assertEqual((later["classified_at"], later["known_at"]), (T("10:12:00"), T("10:12:00")))
        arc = self.rows("10:13:00", "archive")[(self.b()["title"], 1)]
        self.assertIsNone(arc["classification_at_event"])
        self.assertEqual(arc["later_context"]["classifications_after_event"][0]["known_at"], T("10:12:20"))
        a1 = self.rows("10:13:00")[(TITLE_A1, 1)]  # classified in its own run, at 10:05
        self.assertEqual(a1["classification_at_event"]["classified_at"], T("10:05:00"))

    def test_same_membership_reclassification_does_not_move_the_event(self):
        key = (self.b()["title"], 1)
        before = {m: self.rows("10:30:00", m, stream="oil")[key] for m in ("collector", "archive")}
        again = with_classification(self.archive, self.b()["revision_id"], "10:20:00", "10:20:30",
                                    ["ai_buildout", "oil"], "test-reclass-same")
        for mode in ("collector", "archive"):
            after = self.rows("10:30:00", mode, archive=again, stream="oil")[key]
            self.assertEqual((after["event_at"], after["price"]), (before[mode]["event_at"], before[mode]["price"]))
            self.assertEqual(after["classification_at_event"], before[mode]["classification_at_event"])
            [later] = after["later_context"]["classifications_after_event"]
            self.assertEqual(later["classification_id"], "test-reclass-same")
            self.assertIsNone(after["later_context"]["stream_membership_removed_at"])
        self.assertEqual(before["collector"]["event_at"], T("10:12:00"))

    def test_removed_membership_is_reported_without_rewriting_the_event(self):
        key = (self.b()["title"], 1)
        before = self.rows("10:30:00", stream="oil")[key]
        removed = with_classification(self.archive, self.b()["revision_id"], "10:20:00", "10:20:30",
                                      ["ai_buildout"], "test-reclass-removed")
        after = self.rows("10:30:00", archive=removed, stream="oil")[key]
        self.assertEqual((after["event_at"], after["price"]), (before["event_at"], before["price"]))
        self.assertEqual(after["later_context"]["stream_membership_removed_at"], T("10:20:00"))
        arc = self.rows("10:30:00", "archive", archive=removed, stream="oil")[key]
        self.assertEqual((arc["event_at"], arc["later_context"]["stream_membership_removed_at"]),
                         (T("10:12:20"), T("10:20:30")))
        # Before the removal was known, nothing about it shows.
        self.assertIsNone(self.rows("10:19:00", archive=removed, stream="oil")[key]
                          ["later_context"]["stream_membership_removed_at"])

    def test_first_stream_classification_can_delay_the_first_stream_event(self):
        a2 = next(o for o in self.archive.observations if o["title"].endswith("crew evacuated"))
        added = with_classification(self.archive, a2["revision_id"], "10:20:00", "10:20:30",
                                    ["ai_buildout", "oil"], "test-adds-ai")
        key = ("Tanker attacked near the Strait of Hormuz; crew evacuated", 2)
        self.assertNotIn(key, self.rows("10:30:00", stream="ai_buildout"))
        row = self.rows("10:30:00", archive=added, stream="ai_buildout")[key]
        self.assertEqual((row["event_at"], row["event_basis"], row["classification_at_event"]["classification_id"]),
                         (T("10:20:00"), "classified_at", "test-adds-ai"))
        self.assertEqual(self.rows("10:30:00", archive=added, stream="oil")[key]["event_at"], T("10:10:00"))


class SourceScope(Scenario):
    """M3.1 repair 2: coverage only for sources that were actually fetched."""

    def segs(self, cutoff, stream=None, mode="archive"):
        return {s["end"]: s for s in self.j(cutoff, mode, stream=stream)["news_coverage"]["segments"]}

    def test_oil_only_runs_are_not_ai_buildout_coverage(self):
        segs = self.segs("11:15:00", "ai_buildout")
        r1 = segs[T("10:05:00")]
        self.assertEqual((r1["state"], r1["sources"]["attempted"], sorted(r1["sources"]["not_selected"])),
                         ("partial", ["eia_today_in_energy"], ["ars_technica", "techcrunch"]))
        for end in ("10:10:00", "10:15:00", "10:25:00", "11:10:00"):
            seg = segs[T(end)]
            self.assertEqual((seg["state"], seg["sources"]["attempted"]), ("unknown", []), end)
            self.assertIn("eia_today_in_energy", seg["sources"]["not_selected"])

    def test_a_run_that_fetched_every_enabled_oil_source_is_covered(self):
        segs = self.segs("11:15:00", "oil")
        r1 = segs[T("10:05:00")]
        self.assertEqual((r1["state"], sorted(r1["sources"]["attempted"]), r1["sources"]["not_selected"]),
                         ("covered", ["eia_today_in_energy", "oilprice_com"], []))
        self.assertGreater(r1["sources"]["disabled_count"], 0)  # counted, never claimed as covered
        r2 = segs[T("10:10:00")]
        self.assertEqual((r2["state"], r2["sources"]["not_selected"]), ("partial", ["eia_today_in_energy"]))

    def test_unfiltered_scope_includes_every_enabled_source(self):
        r2 = self.segs("10:12:00", mode="collector")[T("10:10:00")]
        self.assertEqual((r2["state"], sorted(r2["sources"]["not_selected"])),
                         ("partial", ["ars_technica", "eia_today_in_energy", "techcrunch"]))


class Window(Scenario):
    """M3.1 repair 3: one [start, cutoff] window for rows and both coverage summaries."""

    def test_window_applies_to_rows_and_both_coverages(self):
        col = self.j("10:30:00", start=T("10:20:00"))
        self.assertEqual((col["rows"], col["excluded"]["before_window"]), ([], 4))
        arc = self.j("10:30:00", "archive", start=T("10:20:00"))
        self.assertEqual(([r["title"] for r in arc["rows"]], arc["excluded"]["before_window"]), ([TITLE_C], 3))
        self.assertEqual(arc["window"]["start"], T("10:20:00"))
        self.assertEqual(arc["price_coverage"], {"missing": [], "stale": [
            {"start": T("10:20:00"), "end": T("10:20:02"), "seconds": 2,
             "last_price_record_id": "uq-100900", "last_price_time": T("10:09:00")}]})
        [seg] = arc["news_coverage"]["segments"]
        self.assertEqual((seg["start"], seg["end"], seg["clipped_to_window_start"]),
                         (T("10:20:00"), T("10:25:00"), True))
        self.assertEqual(arc["news_coverage"]["tail"]["start"], T("10:25:00"))

    def test_reversed_window_is_rejected(self):
        with self.assertRaises(ValueError):
            self.j("10:30:00", start=T("10:30:01"))
        empty = self.j("10:30:00", start=T("10:30:00"))
        self.assertEqual((empty["rows"], empty["price_coverage"]), ([], {"missing": [], "stale": []}))

    def test_pending_runs_outside_the_window_are_not_listed(self):
        arc = self.j("10:20:00", "archive", start=T("10:16:00"))
        self.assertEqual(arc["news_coverage"]["segments"], [])
        self.assertEqual(arc["news_coverage"]["pending_availability_runs"], [])
        self.assertEqual(self.j("10:20:00", "archive", start=T("10:15:00"))["news_coverage"]
                         ["segments"][0]["state"], "pending_availability")


class StreamFilteredRoster(unittest.TestCase):
    """M3.2 repair 1: a streams=['oil'] run omits other streams' rows; the roster keeps them."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.store = LocalObjectStore(root / "store")
        for at, committed in (("10:05:00", "10:05:20"), ("10:10:00", "10:10:20")):
            collect_once(cls.store, root / "spool", feeds(), fixed(T(at)), commit_clock=fixed(T(committed)),
                         streams=["oil"], sleep=no_sleep)
        cls.archive = load_archive(cls.store)
        cls.prices = load_price_file(PRICES)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def segs(self, stream, archive=None, mode="collector"):
        out = join(archive or self.archive, self.prices, T("10:11:00"), mode, tolerance_s=120, stream=stream)
        return out["news_coverage"]["segments"]

    def test_the_manifest_rows_really_omit_other_streams(self):
        run = self.archive.runs[0]
        self.assertNotIn("techcrunch", {s["source_id"] for s in run["sources"]})
        self.assertEqual(run["source_scope"]["streams_requested"], ["oil"])
        self.assertEqual(sorted(run["source_scope"]["selected"]), ["eia_today_in_energy", "oilprice_com"])
        self.assertEqual({r["source_id"] for r in run["source_scope"]["roster"]}, set(config.SOURCES))

    def test_ai_buildout_is_not_covered_by_eia_alone(self):
        for seg in self.segs("ai_buildout"):
            self.assertEqual(seg["state"], "partial")  # not covered: TechCrunch and Ars were not fetched
            src = seg["sources"]
            self.assertEqual((seg["state"], src["attempted"], src["roster_complete"]),
                             ("partial", ["eia_today_in_energy"], True))
            self.assertEqual(sorted(src["not_selected"]), ["ars_technica", "techcrunch"])
            self.assertGreater(src["disabled_count"], 0)

    def test_the_selected_stream_is_covered(self):
        states = [(s["state"], sorted(s["sources"]["attempted"])) for s in self.segs("oil")]
        self.assertEqual(states, [("covered", ["eia_today_in_energy", "oilprice_com"])] * 2)

    def test_a_run_without_a_roster_is_never_covered(self):
        legacy = dataclasses.replace(self.archive, runs=[dict(r, source_scope=None) for r in self.archive.runs])
        for stream in ("oil", "ai_buildout"):
            for seg in self.segs(stream, legacy):
                self.assertEqual((seg["state"], seg["sources"]["roster_complete"]), ("partial", False), stream)

    def test_a_v2_manifest_without_a_roster_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(Path(tmp) / "store")
            report = collect_once(store, Path(tmp) / "spool", feeds(), fixed(T("10:05:00")), streams=["oil"],
                                  sleep=no_sleep, rebuild_index=False)
            prefix = report.manifest["objects"]["observations"]["key"].rsplit("/", 1)[0] + "/"
            manifest = json.loads(store.get(prefix + "manifest.json"))
            manifest.pop("source_scope")
            manifest["schema_version"] = "news-run-manifest/v2"
            body = encode_json(manifest)
            store.put(prefix + "manifest.json", body)
            record = json.loads(store.get(prefix + "commit.json"))
            store.put(prefix + "commit.json", encode_json(dict(record, manifest_sha256=sha256_hex(body))))
            scan = scan_history(store)
            self.assertEqual((len(scan.runs), scan.problems), (1, []))
            [seg] = join(load_archive(store), self.prices, T("10:11:00"), "collector", tolerance_s=120,
                         stream="oil")["news_coverage"]["segments"]
            self.assertEqual((seg["state"], seg["sources"]["roster_complete"]), ("partial", False))


class PublicationWindow(Scenario):
    """M3.2 repair 2: publication-mode coverage stays inside [start, cutoff]."""

    def test_publication_coverage_ends_at_the_cutoff(self):
        cov = self.j("10:03:00", "publication")["news_coverage"]
        self.assertEqual(cov["segments"], [])
        self.assertIsNone(cov["tail"])
        self.assertEqual([h["end"] for h in cov["after_cutoff_hindsight"]],
                         [T("10:05:00"), T("10:10:00"), T("10:15:00"), T("10:25:00"), T("11:10:00")])

    def test_publication_matches_the_availability_modes_inside_the_window(self):
        for mode in ("collector", "archive", "publication"):
            cov = self.j("10:12:00", mode)["news_coverage"]
            with self.subTest(mode=mode):
                self.assertEqual([s["end"] for s in cov["segments"]], [T("10:05:00"), T("10:10:00")])
                self.assertTrue(all(s["end"] <= T("10:12:00") for s in cov["segments"]))
                self.assertEqual((cov["tail"]["start"], cov["tail"]["end"]), (T("10:10:00"), T("10:12:00")))
        later = self.j("10:12:00", "publication")["news_coverage"]["after_cutoff_hindsight"]
        self.assertEqual([h["end"] for h in later], [T("10:15:00"), T("10:25:00"), T("11:10:00")])
        self.assertEqual(self.j("10:12:00", "collector")["news_coverage"]["after_cutoff_hindsight"], [])

    def test_publication_window_start_applies_too(self):
        cov = self.j("10:30:00", "publication", start=T("10:20:00"))["news_coverage"]
        self.assertEqual([(s["start"], s["end"]) for s in cov["segments"]], [(T("10:20:00"), T("10:25:00"))])
        self.assertEqual([h["end"] for h in cov["after_cutoff_hindsight"]], [T("11:10:00")])


class PriceRecords(unittest.TestCase):
    QUOTE = {"record_id": "q", "record_kind": "underlying_quote", "instrument_id": "etf.qqq", "granularity": "tick",
             "price": 600.0, "price_basis": "last_trade", "session": "pre", "quote_ts": T("10:00:00"),
             "collector_received_at": T("10:00:02"), "available_at": T("10:00:02")}
    BAR = {"record_id": "b", "record_kind": "bar", "instrument_id": "etf.qqq", "granularity": "1m", "price": 600.0,
           "price_basis": "bar_close", "session": "pre", "bar_start": T("10:00:00"), "bar_end": T("10:01:00"),
           "available_at": T("10:01:03")}

    def test_fixture_loads_and_is_labeled_synthetic(self):
        f = load_price_file(PRICES)
        self.assertTrue(f.header["synthetic"])
        self.assertIn("SYNTHETIC", f.header["label"])
        self.assertTrue(all(r.synthetic for r in f.records))

    def test_invalid_records_are_rejected(self):
        bad = [dict(self.BAR, available_at=T("10:00:59")),                      # bar before it closes
               dict(self.QUOTE, available_at=T("10:00:03")),                    # available != received
               dict(self.QUOTE, collector_received_at=T("09:59:59"), available_at=T("09:59:59")),
               dict(self.QUOTE, quote_ts="2026-11-09T10:00:00"),                # no zone
               dict(self.QUOTE, record_kind="option_quote"),                    # no underlying named
               dict(self.QUOTE, underlying_instrument_id="etf.qqq"),            # underlying with an underlying
               dict(self.QUOTE, granularity="1m"),
               dict(self.QUOTE, session="overnight"),
               dict(self.QUOTE, price=float("nan")),
               dict(self.QUOTE, price=float("inf")),
               dict(self.QUOTE, price=float("-inf"))]
        for rec in bad:
            with self.subTest(rec=rec), self.assertRaises(PriceRecordError):
                parse_record(rec)
        parse_record(self.QUOTE)
        parse_record(self.BAR)
        parse_record(dict(self.QUOTE, price=-1.5))  # finiteness only; no positive-price rule

    def test_nan_in_a_price_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nan.jsonl"
            path.write_bytes(json.dumps(dict(self.QUOTE, price=float("nan"))).encode() + b"\n")
            self.assertIn(b"NaN", path.read_bytes())
            with self.assertRaises(PriceRecordError):
                load_price_file(path)


if __name__ == "__main__":
    unittest.main()
