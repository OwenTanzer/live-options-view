"""M4a: sanitized, bounded news views built from the accepted M3 join.

The committed fixture views (tests/fixtures/news/views/) are the M3 scenario
of tests/test_news_join.py at as_of 2026-11-09T11:15:00Z, tolerance 120 s,
built with run ids pinned so the bytes are reproducible. Regenerate with
    python tests/test_news_views.py --write
--truncated-index prints the fixture index cut to index_bytes=1000 (the
panel test's empty, incomplete index); --write-harness DIR writes the
browser test's view sets. All data is SYNTHETIC.
"""
import contextlib
import dataclasses
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_news_support import FIXTURES, feeds, fixed, no_sleep
from test_news_join import PRICES, TITLE_C, T, build_scenario
from market_data.news import archive as archive_mod
from market_data.news import config
from market_data.news.__main__ import main
from market_data.news.archive import LocalObjectStore, collect_once
from market_data.news.normalize import parse_iso
from market_data.news.prices import load_price_file
from market_data.news.reader import load_archive
from market_data.news.views import (LIMITS, ROOT, BuildError, build_index, build_view, build_views, check_url,
                                    clean_text, encode, limits_from, project_row)

VIEWS = FIXTURES / "views"
AS_OF = T("11:15:00")
LABEL = "SYNTHETIC fixture data (not real news or market data)"
DATE = "2026-11-09"
FORBIDDEN_KEYS = {"excerpt", "error", "entry_failures", "published_raw", "original_url", "detail", "labels"}


def pinned_run_ids():
    return mock.patch.object(archive_mod, "new_run_id", lambda started: started.strftime("%Y%m%dT%H%M%SZ") + "-fx")


def fixture_views() -> dict:
    with tempfile.TemporaryDirectory() as tmp, pinned_run_ids():
        store = build_scenario(Path(tmp))
        return build_views(load_archive(store), load_price_file(PRICES), as_of=AS_OF, tolerance_s=120,
                           data_label=LABEL)


def quiet_store(root: Path, starts=("10:05:00", "10:10:00", "10:15:00")) -> LocalObjectStore:
    """Oil-only runs over the same feeds: the later runs observe nothing new."""
    store = LocalObjectStore(root / "store")
    with pinned_run_ids():
        for at in starts:
            collect_once(store, root / "spool", feeds(), fixed(T(at)), streams=["oil"], sleep=no_sleep)
    return store


def delayed_archive(archive):
    """One first-run observation whose archive write lands at 10:12, inside the 10:10-10:15 run span."""
    first = archive.runs[0]["run_id"]
    late = next(o for o in archive.observations if o["run_id"] == first)
    obs = [dict(o, archived_at=T("10:12:00")) if o is late else o for o in archive.observations]
    return dataclasses.replace(archive, observations=obs)


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def pages_of(files, mode, stream, date=DATE):
    prefix = f"{ROOT}/{mode}/{stream}/{date}/"
    return [json.loads(files[k]) for k in sorted(files) if k.startswith(prefix)]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        with pinned_run_ids():
            cls.store = build_scenario(Path(cls._tmp.name))
        cls.archive = load_archive(cls.store)
        cls.prices = load_price_file(PRICES)
        cls.files = build_views(cls.archive, cls.prices, as_of=AS_OF, tolerance_s=120, data_label=LABEL)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def view(self, mode="collector", stream="all", as_of=AS_OF, **limits):
        files, entry = build_view(self.archive, self.prices, mode=mode, stream=stream, date=DATE,
                                  as_of=parse_iso(as_of),
                                  tolerance_s=120, instrument_id="etf.qqq", series="underlying_quote",
                                  data_label=LABEL, limits=limits_from(**limits))
        return [json.loads(files[k]) for k in sorted(files)], entry, files


class FixtureViews(Base):
    def test_committed_fixture_matches_the_generator(self):
        committed = {p.relative_to(VIEWS).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
                     for p in VIEWS.rglob("*.json")}
        self.assertEqual(sorted(committed), sorted(self.files))
        for key, body in self.files.items():
            self.assertEqual(committed[key], body, key)

    def test_layout_and_index(self):
        index = json.loads(self.files[f"{ROOT}/index.json"])
        self.assertEqual((index["complete"], index["overflow_reasons"], index["views_total"], index["views_omitted"]),
                         (True, [], 9, 0))
        self.assertEqual({(v["mode"], v["stream"]) for v in index["views"]},
                         {(m, s) for m in ("collector", "archive", "publication") for s in ("all", "ai_buildout", "oil")})
        for v in index["views"]:
            self.assertIn(v["first_page"], self.files)
            self.assertEqual(v["first_page"], f"{ROOT}/{v['mode']}/{v['stream']}/{DATE}/page-0001.json")

    def test_every_page_is_labelled_and_bounded(self):
        for key, body in self.files.items():
            page = json.loads(body)
            with self.subTest(key=key):
                self.assertLessEqual(len(body), LIMITS.page_bytes)
                self.assertEqual(page["data_label"], LABEL)
                self.assertFalse(FORBIDDEN_KEYS & set(walk_keys(page)), "raw or private field leaked")
                if key.endswith("index.json"):
                    continue
                self.assertTrue(page["synthetic_prices"])
                self.assertEqual(page["hindsight"], page["mode"] == "publication")
                self.assertIn("RETROSPECTIVE" if page["hindsight"] else "as-of", page["mode_label"])
                for row in page["rows"]:
                    self.assertEqual((row["instruments"]["mapping"], row["instruments"]["linked"]), ("unknown", []))

    def test_rows_keep_timing_revision_and_price_context(self):
        [page] = pages_of(self.files, "collector", "oil")
        rows = page["rows"]
        self.assertEqual([r["event_at"] for r in rows],
                         sorted((r["event_at"] for r in rows), reverse=True))
        first = next(r for r in rows if r["event_at"] == T("10:05:00") and r["source_id"] == "oilprice_com")
        self.assertEqual(first["revisions_known_at_event"], 1)
        self.assertEqual([x["known_at"] for x in first["later_context"]["revisions_after_event"]], [T("10:10:00")])
        statuses = {r["price"]["status"] for r in rows}
        self.assertIn("stale", statuses)  # the synthetic underlying gap stays visible
        self.assertTrue(page["coverage"]["price"]["stale"])

    def test_retrospective_classification_is_not_labelled_as_known_at_event(self):
        [page] = pages_of(self.files, "publication", "all")
        [eia] = [r for r in page["rows"] if r["source_id"] == "eia_today_in_energy"]
        cls = eia["classification_at_event"]
        self.assertEqual((eia["event_at"], cls["classified_at"], cls["known_at"], cls["timing"]),
                         (T("09:55:00"), T("10:12:00"), None, "retrospective"))
        self.assertTrue(all(r["classification_at_event"]["timing"] == "retrospective" for r in page["rows"]))
        for mode in ("collector", "archive"):
            for row in pages_of(self.files, mode, "all")[0]["rows"]:
                cls = row["classification_at_event"]
                if cls is not None:
                    self.assertEqual(cls["timing"], "at_event", (mode, row["revision_id"]))
                    self.assertLessEqual(parse_iso(cls["known_at"]), parse_iso(row["event_at"]))

    def test_archive_mode_keeps_pending_and_missed_interval_visible(self):
        pages, _, _ = self.view("archive", "oil", as_of=T("10:20:00"))
        cov = pages[0]["coverage"]
        self.assertEqual(cov["pending_availability_runs"], 1)
        pending = [s for s in cov["segments"] if s["state"] == "pending_availability"]
        self.assertEqual([(s["sources"], s["quiet"]) for s in pending], [(None, False)])
        [late] = [s for s in pages_of(self.files, "archive", "oil")[0]["coverage"]["segments"]
                  if s["end"] == T("11:10:00")]
        self.assertEqual((late["missed_interval"], late["gap_seconds"], late["quiet"]), (True, 2700, False))


class Sanitization(unittest.TestCase):
    def test_urls(self):
        good = ["https://example.com/a?b=1#c", "http://example.com:8080/x"]
        bad = {"javascript:alert(1)": "scheme_not_http", "data:text/html,x": "scheme_not_http",
               "ftp://example.com/": "scheme_not_http", "https://user:pw@example.com/": "userinfo",
               "https://user@example.com/": "userinfo", "https://exa mple.com/": "whitespace_or_control",
               "https://example.com/\x00": "whitespace_or_control", "https://example.com/‮": "whitespace_or_control",
               "http:///nohost": "no_host", "https://example.com:0/": "bad_port", "https://example.com:99999/": "unparseable",
               "https://[::1/": "unparseable", "": "missing", None: "missing",
               "https://example.com/" + "a" * 2048: "too_long", "//example.com/x": "scheme_not_http",
               r"https://example.com\@evil.test/": "backslash", r"http:\\example.com/": "backslash"}
        for u in good:
            self.assertEqual(check_url(u), (u, None))
        for u, reason in bad.items():
            with self.subTest(url=u if u is None else u[:40]):
                self.assertEqual(check_url(u), (None, reason))

    def test_text(self):
        self.assertEqual(clean_text("a\x00b‮c\n\t d", 50), ("abc d", False))
        text, cut = clean_text("x" * 400, 300)
        self.assertEqual((len(text), cut, text[-1]), (300, True, "…"))

    def test_row_projection_rejects_a_bad_link_and_drops_raw_fields(self):
        row = {"item_id": "i", "revision_id": "r", "revision_number": 1, "revisions_known_at_event": 1,
               "title": "Hello\x07 <b>world</b>", "source_id": "oilprice_com", "outlet": "OilPrice​.com",
               "canonical_url": "javascript:alert(1)", "url_status": "ok", "published_at": None,
               "time_quality": "missing", "observed_at": T("10:05:00"), "archived_at": None, "run_id": "x",
               "classification_at_event": None, "event_at": T("10:05:00"), "event_basis": "observed_at",
               "later_context": {"revisions_known_by_cutoff": 1, "revisions_after_event": [],
                                 "classifications_after_event": []},
               "price": {"status": "missing", "record": None, "age_seconds": None}, "excerpt": "secret"}
        out = project_row(row, {})
        self.assertEqual((out["canonical_url"], out["url_rejected"]), (None, "scheme_not_http"))
        self.assertEqual((out["title"], out["outlet"]), ("Hello <b>world</b>", "OilPrice.com"))
        self.assertNotIn("excerpt", out)
        self.assertEqual(out["instruments"]["mapping"], "unknown")

    def test_a_bad_archived_url_never_reaches_a_page(self):
        with tempfile.TemporaryDirectory() as tmp, pinned_run_ids():
            archive = load_archive(build_scenario(Path(tmp)))
        obs = [dict(o, canonical_url="javascript:alert(document.cookie)") if i == 0 else o
               for i, o in enumerate(archive.observations)]
        files = build_views(dataclasses.replace(archive, observations=obs), load_price_file(PRICES), as_of=AS_OF,
                            tolerance_s=120, data_label=LABEL, modes=("collector",), streams=("all",))
        self.assertNotIn(b"javascript:", b"".join(files.values()))
        rejected = [r for r in pages_of(files, "collector", "all")[0]["rows"] if r["url_rejected"]]
        self.assertEqual([r["url_rejected"] for r in rejected], ["scheme_not_http"])


class Limits(Base):
    def test_rows_page_in_order_with_navigation(self):
        pages, entry, files = self.view(rows_per_page=1)
        total = pages[0]["view"]["rows_total"]
        self.assertEqual((len(pages), entry["page_count"], entry["complete"]), (total, total, True))
        for n, page in enumerate(pages, 1):
            self.assertEqual((page["page_number"], page["page_count"], page["rows_offset"]), (n, total, n - 1))
            self.assertEqual(page["prev_page"], None if n == 1 else sorted(files)[n - 2])
            self.assertEqual(page["next_page"], None if n == total else sorted(files)[n])
        times = [p["rows"][0]["event_at"] for p in pages]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_rows_beyond_the_page_limit_are_counted_not_dropped(self):
        pages, entry, _ = self.view(rows_per_page=1, pages_per_view=2)
        total = pages[0]["view"]["rows_total"]
        self.assertEqual(len(pages), 2)
        for page in pages:
            self.assertEqual((page["complete"], page["view"]["complete"]), (False, False))
            self.assertIn("rows_beyond_max_pages", page["overflow_reasons"])
            self.assertEqual(page["omitted"]["rows_omitted"], total - 2)
            self.assertEqual(page["view"]["pages_omitted"], total - 2)
        self.assertEqual((entry["rows_omitted"], entry["complete"]), (total - 2, False))

    def test_byte_cap_moves_rows_to_the_next_page(self):
        obs = [dict(o, item_id=f"{o['item_id']}-{k}", revision_id=f"{o['revision_id']}-{k}")
               for k in range(15) for o in self.archive.observations]
        big = dataclasses.replace(self.archive, observations=obs)
        full = build_view(big, self.prices, mode="collector", stream="all", date=DATE, as_of=parse_iso(AS_OF),
                          tolerance_s=120, instrument_id="etf.qqq", series="underlying_quote", data_label=LABEL)
        first = json.loads(full[0][sorted(full[0])[0]])
        total = first["view"]["rows_total"]
        self.assertEqual((first["page_count"], total), (2, 60))  # 50 rows, then 10
        cap = 2 * len(encode(dict(first, rows=[]))) + 12000
        files, entry = build_view(big, self.prices, mode="collector", stream="all", date=DATE,
                                  as_of=parse_iso(AS_OF), tolerance_s=120, instrument_id="etf.qqq",
                                  series="underlying_quote", data_label=LABEL, limits=limits_from(page_bytes=cap))
        pages = [json.loads(files[k]) for k in sorted(files)]
        self.assertGreater(len(pages), 2)
        self.assertTrue(all(len(b) <= cap for b in files.values()))
        self.assertTrue(all(p["rows_on_page"] < 50 for p in pages))
        self.assertEqual(sum(p["rows_on_page"] for p in pages), total)
        self.assertEqual([r["revision_id"] for p in pages for r in p["rows"]],
                         [r["revision_id"] for p in [json.loads(b) for _, b in sorted(full[0].items())]
                          for r in p["rows"]])
        self.assertTrue(all(p["complete"] for p in pages) and entry["complete"])

    def test_pages_omitted_uses_byte_aware_packing(self):
        title = "\u539f\u6cb9\u30bf\u30f3\u30ab\u30fc \u2014 \u00d6lpreis \u00fcber Erwartung \u2713 " * 4
        obs = [dict(o, item_id=f"{o['item_id']}-{k}", revision_id=f"{o['revision_id']}-{k}", title=title)
               for k in range(15) for o in self.archive.observations]
        big = dataclasses.replace(self.archive, observations=obs)

        def build(**limits):
            files, entry = build_view(big, self.prices, mode="collector", stream="all", date=DATE,
                                      as_of=parse_iso(AS_OF), tolerance_s=120, instrument_id="etf.qqq",
                                      series="underlying_quote", data_label=LABEL, limits=limits_from(**limits))
            return [json.loads(files[k]) for k in sorted(files)], entry
        full, full_entry = build(page_bytes=24000)
        self.assertEqual((full_entry["rows_total"], full_entry["complete"]), (60, True))
        self.assertGreater(len(full), 6)  # bytes, not the 50-row limit, decide the pages
        [first], entry = build(page_bytes=24000, pages_per_view=1)
        self.assertEqual(entry["pages_omitted"], len(full) - 1)
        self.assertEqual(first["view"]["pages_omitted"], len(full) - 1)
        self.assertEqual(entry["rows_omitted"], 60 - first["rows_on_page"])
        self.assertEqual([r["revision_id"] for r in first["rows"]], [r["revision_id"] for r in full[0]["rows"]])
        self.assertEqual((first["complete"], first["overflow_reasons"]), (False, ["rows_beyond_max_pages"]))

    def test_coverage_over_its_count_limit_is_marked_incomplete(self):
        pages, entry, _ = self.view(segments_per_page=2)
        cov = pages[0]["coverage"]
        self.assertEqual((len(cov["segments"]), cov["coverage_complete"]), (2, False))
        self.assertEqual(pages[0]["omitted"]["segments_omitted"], 3)
        self.assertEqual([s["end"] for s in cov["segments"]], [T("10:25:00"), T("11:10:00")])  # newest kept
        self.assertIn("coverage_over_count_limits", pages[0]["overflow_reasons"])
        self.assertEqual((pages[0]["complete"], entry["coverage_complete"]), (False, False))

    def test_coverage_over_its_byte_budget_is_trimmed_with_counts(self):
        full, _, _ = self.view()
        cap = len(json.dumps(dict(full[0], rows=[]), indent=1)) + 1500
        pages, _, files = self.view(page_bytes=cap)
        self.assertTrue(all(len(b) <= cap for b in files.values()))
        page = pages[0]
        self.assertIn("coverage_over_byte_budget", page["overflow_reasons"])
        self.assertFalse(page["coverage"]["coverage_complete"])
        dropped = page["omitted"]["segments_omitted"] + page["omitted"]["intervals_omitted"]
        self.assertGreater(dropped, 0)
        self.assertEqual(len(page["coverage"]["segments"]) + page["omitted"]["segments_omitted"], 5)

    def test_the_mandatory_envelope_must_fit_or_the_build_fails(self):
        with self.assertRaisesRegex(BuildError, "mandatory page envelope"):
            self.view(page_bytes=2000)

    def test_index_has_its_own_completeness(self):
        entries = [json.loads(self.files[f"{ROOT}/index.json"])["views"][0]] * 7
        index = json.loads(build_index(entries, as_of=self._as_of(), data_label=LABEL,
                                       limits=limits_from(index_views=5)))
        self.assertEqual((index["complete"], index["overflow_reasons"], index["views_total"],
                          index["views_omitted"], len(index["views"])), (False, ["index_views"], 7, 2, 5))
        small = len(build_index([], as_of=self._as_of(), data_label=LABEL)) + 600
        body = build_index(entries, as_of=self._as_of(), data_label=LABEL, limits=limits_from(index_bytes=small))
        index = json.loads(body)
        self.assertLessEqual(len(body), small)
        self.assertEqual((index["complete"], index["overflow_reasons"]), (False, ["index_bytes"]))
        self.assertEqual(index["views_omitted"] + len(index["views"]), 7)
        empty = json.loads(truncated_index())
        self.assertEqual((empty["complete"], empty["overflow_reasons"], empty["views_total"], empty["views_omitted"],
                          empty["views"]), (False, ["index_bytes"], 9, 9, []))
        with self.assertRaisesRegex(BuildError, "mandatory index envelope"):
            build_index(entries, as_of=self._as_of(), data_label=LABEL, limits=limits_from(index_bytes=300))

    @staticmethod
    def _as_of():
        return parse_iso(AS_OF)


class QuietRule(unittest.TestCase):
    """Rule 1: 'observed, no items' needs whole-span coverage and a pre-pagination zero count."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.archive = load_archive(quiet_store(Path(cls._tmp.name)))
        cls.prices = load_price_file(PRICES)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def segs(self, mode="collector", stream="oil", **limits):
        files = build_views(self.archive, self.prices, as_of=T("10:20:00"), tolerance_s=120, data_label=LABEL,
                            modes=(mode,), streams=(stream,), limits=limits_from(**limits))
        pages = pages_of(files, mode, stream)
        return pages, [(s["end"][11:19], s["state"], s["observations_in_span"], s["quiet"])
                       for s in pages[0]["coverage"]["segments"]]

    def test_covered_spans_without_rows_are_quiet(self):
        _, segs = self.segs()
        self.assertEqual(segs[0][1:], ("covered", segs[0][2], False))  # before the first run: never quiet
        self.assertGreater(segs[0][2], 0)
        self.assertEqual(segs[1:], [("10:10:00", "covered", 0, True), ("10:15:00", "covered", 0, True)])

    def test_the_count_is_taken_before_pagination(self):
        pages, segs = self.segs(rows_per_page=1)
        self.assertGreater(len(pages), 1)
        for page in pages:  # same verdicts on every page, whatever rows the page holds
            self.assertEqual([(s["end"][11:19], s["state"], s["observations_in_span"], s["quiet"])
                              for s in page["coverage"]["segments"]], segs)

    def test_a_partial_scope_is_never_quiet(self):
        _, segs = self.segs(stream="ai_buildout")
        self.assertTrue(segs and all(state == "partial" and not quiet for _, state, _, quiet in segs))

    def test_publication_mode_never_claims_quiet(self):
        pages, segs = self.segs(mode="publication")
        self.assertIn("publication_mode_hindsight", pages[0]["coverage"]["quiet_blocked_by"])
        self.assertFalse(any(q for *_, q in segs))

    def test_unclassified_revisions_block_quiet(self):
        with tempfile.TemporaryDirectory() as tmp, pinned_run_ids():
            archive = load_archive(build_scenario(Path(tmp)))
        files = build_views(archive, self.prices, as_of=T("10:11:00"), tolerance_s=120, data_label=LABEL,
                            modes=("collector",), streams=("oil",))
        cov = pages_of(files, "collector", "oil")[0]["coverage"]
        self.assertIn("revisions_not_yet_classified_for_stream", cov["quiet_blocked_by"])
        self.assertFalse(any(s["quiet"] for s in cov["segments"]))

    def test_the_span_before_the_first_run_is_never_quiet(self):
        first = self.archive.runs[0]["run_id"]
        no_rows = dataclasses.replace(self.archive,
                                      observations=[o for o in self.archive.observations if o["run_id"] != first])
        files = build_views(no_rows, self.prices, as_of=T("10:20:00"), tolerance_s=120, data_label=LABEL,
                            modes=("collector",), streams=("oil",), dates=[DATE])
        seg = pages_of(files, "collector", "oil")[0]["coverage"]["segments"][0]
        self.assertEqual((seg["state"], seg["observations_in_span"], seg["events_in_span"], seg["before_first_run"],
                          seg["quiet"]), ("covered", 0, 0, True, False))

    def test_a_failed_source_is_partial_and_carries_no_error_text(self):
        fetch = feeds()
        eia = config.SOURCES["eia_today_in_energy"].url

        def failing(url, timeout):
            if url == eia:
                raise OSError("synthetic: connection reset by peer at 10.0.0.1")
            return fetch(url, timeout)
        with tempfile.TemporaryDirectory() as tmp, pinned_run_ids():
            store = LocalObjectStore(Path(tmp) / "store")
            collect_once(store, Path(tmp) / "spool", fetch, fixed(T("10:05:00")), streams=["oil"], sleep=no_sleep)
            collect_once(store, Path(tmp) / "spool", failing, fixed(T("10:10:00")), streams=["oil"], sleep=no_sleep)
            archive = load_archive(store)
        files = build_views(archive, self.prices, as_of=T("10:20:00"), tolerance_s=120, data_label=LABEL,
                            modes=("collector",), streams=("oil",), dates=[DATE])
        body = files[f"{ROOT}/collector/oil/{DATE}/page-0001.json"]
        seg = json.loads(body)["coverage"]["segments"][1]
        self.assertEqual((seg["state"], seg["observations_in_span"], seg["quiet"]), ("partial", 0, False))
        self.assertEqual([f["source_id"] for f in seg["sources"]["failed"]], ["eia_today_in_energy"])
        self.assertEqual(set(seg["sources"]["failed"][0]), {"source_id", "status"})
        self.assertNotIn(b"connection reset", body)
        self.assertNotIn(b"10.0.0.1", body)

    def test_a_delayed_archive_event_is_counted_in_its_span(self):
        delayed = delayed_archive(self.archive)
        out = {}
        for mode in ("collector", "archive"):
            files = build_views(delayed, self.prices, as_of=T("10:20:00"), tolerance_s=120, data_label=LABEL,
                                modes=(mode,), streams=("oil",), dates=[DATE])
            page = pages_of(files, mode, "oil")[0]
            out[mode] = [(s["end"][11:19], s["observations_in_span"], s["events_in_span"], s["quiet"])
                         for s in page["coverage"]["segments"]][1:]
            if mode == "archive":
                self.assertIn(T("10:12:00"), [r["event_at"] for r in page["rows"]])
        # The runs saw nothing new, but in archive mode one row's event lands at 10:12: no quiet claim there.
        self.assertEqual(out["archive"], [("10:10:00", 0, 0, True), ("10:15:00", 0, 1, False)])
        self.assertEqual(out["collector"], [("10:10:00", 0, 0, True), ("10:15:00", 0, 0, True)])

    def test_a_missed_interval_is_never_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = load_archive(quiet_store(Path(tmp), ("10:05:00", "10:10:00", "11:00:00")))
        files = build_views(archive, self.prices, as_of=T("11:05:00"), tolerance_s=120, data_label=LABEL,
                            modes=("collector",), streams=("oil",))
        segs = pages_of(files, "collector", "oil")[0]["coverage"]["segments"]
        self.assertEqual([(s["end"][11:19], s["missed_interval"], s["quiet"]) for s in segs],
                         [("10:05:00", False, False), ("10:10:00", False, True), ("11:00:00", True, False)])


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_build_views_writes_a_new_directory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = quiet_store(Path(tmp))
            out = Path(tmp) / "views"
            args = ("build-views", "--store", str(store.root), "--as-of", T("10:20:00"), "--tolerance-s", "120",
                    "--label", LABEL, "--out", str(out))
            code, stdout, _ = self.run_cli(*args)
            self.assertEqual(code, 0)
            report = json.loads(stdout)
            index = json.loads((out / ROOT / "index.json").read_bytes())
            self.assertEqual((report["views_total"], report["views_omitted"], report["complete"]),
                             (len(index["views"]), 0, True))
            self.assertEqual(len(index["views"]) % 9, 0)  # 3 modes x 3 streams per UTC date
            self.assertTrue((out / ROOT / "index.json").is_file())
            code, _, err = self.run_cli(*args)
            self.assertEqual(code, 2)
            self.assertIn("not empty", err)
            code, _, err = self.run_cli(*args[:-1], str(Path(tmp) / "v2"), "--page-bytes", "999999999")
            self.assertEqual(code, 2)
            self.assertIn("page_bytes must be between", err)
            code, _, err = self.run_cli(*args[:-1], str(Path(tmp) / "v3"), "--page-bytes", "2000")
            self.assertEqual(code, 2)
            self.assertIn("mandatory page envelope", err)


def truncated_index() -> bytes:
    """The committed fixture's index entries under index_bytes=1000: no entry fits, all 9 are omitted."""
    entries = json.loads((VIEWS / ROOT / "index.json").read_bytes())["views"]
    return build_index(entries, as_of=parse_iso(AS_OF), data_label=LABEL, limits=limits_from(index_bytes=1000))


def write_fixture():
    files = fixture_views()
    if VIEWS.exists():
        shutil.rmtree(VIEWS)
    for key, body in files.items():
        path = VIEWS / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    print(f"wrote {len(files)} files under {VIEWS}")


def write_harness_views(out: Path):
    """Builder output for the browser test: m3/ (the fixture), quiet/, delayed/ (a late archive write),
    edge/ (overflow, bad link, markup) and truncated/ (an index cut before its first entry)."""
    sets = {"m3": fixture_views()}
    with tempfile.TemporaryDirectory() as tmp:
        quiet = load_archive(quiet_store(Path(tmp)))
        sets["quiet"] = build_views(quiet, load_price_file(PRICES), as_of=T("10:20:00"), tolerance_s=120,
                                    data_label=LABEL, dates=[DATE])
        sets["delayed"] = build_views(delayed_archive(quiet), load_price_file(PRICES), as_of=T("10:20:00"),
                                      tolerance_s=120, data_label=LABEL, dates=[DATE], modes=("archive",),
                                      streams=("oil",))
    with tempfile.TemporaryDirectory() as tmp, pinned_run_ids():
        archive = load_archive(build_scenario(Path(tmp)))
    obs = [dict(o, canonical_url="javascript:alert(document.cookie)",
                title="<img src=x onerror=alert(1)>‮ Tanker update") if o["title"] == TITLE_C
           else o for o in archive.observations]
    sets["edge"] = build_views(dataclasses.replace(archive, observations=obs), load_price_file(PRICES), as_of=AS_OF,
                               tolerance_s=120, data_label=LABEL, modes=("collector",),
                               limits=limits_from(rows_per_page=1, pages_per_view=2, segments_per_page=2))
    sets["truncated"] = {f"{ROOT}/index.json": truncated_index()}
    for name, files in sets.items():
        for key, body in files.items():
            path = out / name / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    print(json.dumps({name: len(files) for name, files in sets.items()}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write_fixture()
    elif sys.argv[1:] == ["--truncated-index"]:
        sys.stdout.buffer.write(truncated_index())
    elif sys.argv[1:2] == ["--write-harness"] and len(sys.argv) == 3:
        write_harness_views(Path(sys.argv[2]))
    else:
        unittest.main()
