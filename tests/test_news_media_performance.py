"""Media performance: mapping + forward join (fixture-only, no network).

Covers, in the order the pipeline runs them:
  1. companies.map_company — exact ticker, company name, ambiguous ->
     unknown, no match -> unknown, conflict -> unknown.
  2. performance.forward_after / forward_return / performance_for_event —
     5m/30m/1h returns correct on the synthetic fixture, missing forward
     price, stale / market-closed gap, availability cutoff respected,
     publication mode labelled hindsight with no return.
  3. prices.parse_record validation — NaN rejected, bar-before-close
     rejected (re-asserted here because performance depends on them).

Run (offline, after the execution-risk blocker is cleared):
  python -m unittest discover -s tests -p "test_news_media_performance.py" -v

All data is SYNTHETIC (see tests/fixtures/news/prices/
media_performance.synthetic.jsonl and media_performance_events.json).
No live feeds, no bucket writes, no credentials.
"""

import json
import math
import unittest
from pathlib import Path

from market_data.news.companies import map_company
from market_data.news.join import PriceSeries
from market_data.news.normalize import parse_iso
from market_data.news.performance import (
    HORIZONS,
    forward_after,
    forward_return,
    performance_for_event,
    performance_for_join,
)
from market_data.news.performance_views import (
    attach_performance,
    honesty_label,
    summarise_for_event,
)
from market_data.news.prices import PriceRecordError, load_price_file, parse_record

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "news"
PRICES = FIXTURES / "prices" / "media_performance.synthetic.jsonl"
EVENTS = FIXTURES / "media_performance_events.json"


def load_events():
    return json.loads(EVENTS.read_text())["events"]


class MappingTests(unittest.TestCase):
    def test_exact_ticker(self):
        got = map_company("NVDA guides higher", "NVIDIA data-center revenue")
        self.assertEqual(got["instrument_id"], "eq.nvda")
        self.assertEqual(got["mapping_basis"], "ticker_and_name")
        self.assertEqual(got["reason"], "ok")

    def test_ticker_with_dollar_sign_and_case(self):
        got = map_company("$nvda beats", "")
        self.assertEqual(got["ticker"], "NVDA")
        self.assertEqual(got["mapping_basis"], "ticker")

    def test_company_name_only(self):
        got = map_company("Exxon Mobil shares in focus", "")
        self.assertEqual(got["instrument_id"], "eq.xom")
        self.assertEqual(got["mapping_basis"], "company_name")

    def test_apple_alone_is_unknown_without_ticker_or_distinctive(self):
        got = map_company("Apple harvest falls", "An apple a day")
        self.assertEqual(got["instrument_mapping"], "unknown")
        self.assertEqual(got["reason"], "no_match")

    def test_apple_with_ticker_maps(self):
        got = map_company("AAPL: iPhone sales rise", "")
        self.assertEqual(got["instrument_id"], "eq.aapl")

    def test_meta_analysis_is_not_meta(self):
        got = map_company("A meta-analysis of chip yields", "")
        self.assertEqual(got["instrument_mapping"], "unknown")

    def test_ambiguous_two_companies_is_unknown(self):
        got = map_company("NVIDIA and Microsoft both guide higher", "")
        self.assertEqual(got["instrument_mapping"], "unknown")
        self.assertEqual(got["reason"], "ambiguous")
        self.assertEqual(got["candidates"], ["MSFT", "NVDA"])

    def test_no_match_is_unknown(self):
        got = map_company("A startup raises a round to build a new chip", "")
        self.assertEqual(got["instrument_mapping"], "unknown")
        self.assertEqual(got["reason"], "no_match")

    def test_ticker_inside_longer_word_does_not_match(self):
        got = map_company("ANVDA guides higher", "")
        self.assertEqual(got["instrument_mapping"], "unknown")

    def test_private_company_is_unknown(self):
        got = map_company("OpenAI raises a new round", "")
        self.assertEqual(got["instrument_mapping"], "unknown")


class ForwardJoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prices = load_price_file(PRICES)
        cls.events = {e["item_id"]: e for e in load_events()}
        cls.series = {
            "eq.nvda": PriceSeries(cls.prices.records, "eq.nvda", "underlying_quote"),
            "eq.xom": PriceSeries(cls.prices.records, "eq.xom", "underlying_quote"),
            "eq.xom-bar-1d": PriceSeries(cls.prices.records, "eq.xom", "bar_1d"),
        }

    def series_by_instrument(self):
        return {"eq.nvda": self.series["eq.nvda"],
                "eq.xom": self.series["eq.xom"]}

    def test_forward_5m_nvda_uses_earliest_at_or_after_target(self):
        # E1: event 10:05, base 180.50. Target 10:10. NVDA gap 10:10-10:19,
        # so the earliest qualifying forward is 10:20 at 182.00.
        event = self.events["tc-nvda-001"]
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T11:05:00Z", "collector")
        five = [r for r in rows if r["horizon"] == "5m"][0]
        self.assertEqual(five["status"], "matched")
        self.assertEqual(five["forward"]["record_id"], "nvda-102000")
        self.assertEqual(five["coverage"]["forward_lag_seconds"], 600.0)
        expected = (182.0 - 180.5) / 180.5
        self.assertAlmostEqual(five["return_pct"], expected * 100.0, places=6)
        self.assertAlmostEqual(five["return_bps"], expected * 10000.0, places=4)

    def test_forward_30m_and_1h_nvda(self):
        event = self.events["tc-nvda-001"]
        # Cutoff 11:05:02: the 11:05 forward quote is received 2 s later
        # (available_at 11:05:02), so a cutoff of 11:05:00 would
        # correctly exclude it under the no-lookahead rule.
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T11:05:02Z", "collector")
        by_h = {r["horizon"]: r for r in rows}
        # 30m target 10:35 -> 183.50; 1h target 11:05 -> 186.50.
        self.assertAlmostEqual(by_h["30m"]["return_pct"], (183.5 - 180.5) / 180.5 * 100.0)
        self.assertAlmostEqual(by_h["1h"]["return_pct"], (186.5 - 180.5) / 180.5 * 100.0)

    def test_missing_forward_when_cutoff_before_target(self):
        event = self.events["tc-nvda-001"]
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T10:06:00Z", "collector")
        for row in rows:
            self.assertEqual(row["status"], "missing")
            self.assertEqual(row["missing_reason"], "not_yet_available")
            self.assertIsNone(row["return_pct"])

    def test_availability_cutoff_respected(self):
        # Forward price at 11:05 exists in the file, but a cutoff of
        # 11:00 must not use it for the 1h horizon (target 11:05).
        event = self.events["tc-nvda-001"]
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T11:00:00Z", "collector")
        one_h = [r for r in rows if r["horizon"] == "1h"][0]
        self.assertEqual(one_h["status"], "missing")
        self.assertIsNone(one_h["return_pct"])

    def test_no_base_price_has_no_return(self):
        event = self.events["tc-unmapped-001"]
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T11:05:00Z", "collector")
        for row in rows:
            self.assertEqual(row["status"], "missing")
            self.assertIsNone(row["return_pct"])

    def test_stale_base_is_flagged_not_hidden(self):
        event = self.events["tc-nvda-002"]
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T11:05:00Z", "collector")
        five = [r for r in rows if r["horizon"] == "5m"][0]
        # Event 10:55, target 11:00 -> forward 186.00. Base was stale.
        self.assertEqual(five["status"], "matched")
        self.assertTrue(five["base_stale"])
        self.assertEqual(honesty_label(five), "stale base")

    def test_publication_mode_is_hindsight_with_no_return(self):
        event = self.events["tc-nvda-001"]
        rows = performance_for_event(
            event, event["price"], self.series_by_instrument(),
            "2026-11-09T11:05:00Z", "publication")
        for row in rows:
            self.assertEqual(row["status"], "hindsight")
            self.assertTrue(row["hindsight"])
            self.assertIsNone(row["return_pct"])
            self.assertIsNone(row["return_bps"])
            self.assertEqual(honesty_label(row), "hindsight")

    def test_forward_return_math(self):
        ret, bps = forward_return(100.0, 101.0)
        self.assertAlmostEqual(ret, 0.01)
        self.assertAlmostEqual(bps, 100.0)
        with self.assertRaises(ValueError):
            forward_return(0.0, 101.0)

    def test_forward_after_ignores_records_not_yet_available(self):
        series = self.series["eq.nvda"]
        # A quote with price_time 10:20 but available_at after the cutoff
        # must not be used. Cutoff 10:20:01 is before its 10:20:02 receipt.
        got = forward_after(series, parse_iso("2026-11-09T10:20:00Z"),
                            parse_iso("2026-11-09T10:20:01Z"))
        self.assertIsNone(got)

    def test_performance_for_join_summary_counts(self):
        join_result = {"mode": "collector", "cutoff": "2026-11-09T11:05:00Z",
                       "rows": [self.events["tc-nvda-001"],
                                self.events["tc-unmapped-001"]]}
        out = performance_for_join(join_result, self.series_by_instrument())
        self.assertEqual(out["schema_version"], "news-performance/v1")
        self.assertEqual(out["summary"]["events"], 2)
        self.assertEqual(out["summary"]["rows"], 8)


class PriceValidationTests(unittest.TestCase):
    def test_nan_price_rejected(self):
        rec = {"record_id": "bad-nan", "record_kind": "underlying_quote",
               "instrument_id": "eq.nvda", "granularity": "tick",
               "price": float("nan"), "price_basis": "last_trade",
               "session": "pre", "quote_ts": "2026-11-09T10:00:00Z",
               "collector_received_at": "2026-11-09T10:00:02Z",
               "available_at": "2026-11-09T10:00:02Z"}
        with self.assertRaises(PriceRecordError):
            parse_record(rec)

    def test_bar_available_before_close_rejected(self):
        rec = {"record_id": "bad-bar", "record_kind": "bar",
               "instrument_id": "eq.xom", "granularity": "1d",
               "price": 111.5, "price_basis": "daily_bar_close",
               "session": "regular", "bar_start": "2026-11-09T14:30:00Z",
               "bar_end": "2026-11-09T21:00:00Z",
               "available_at": "2026-11-09T20:00:00Z"}
        with self.assertRaises(PriceRecordError):
            parse_record(rec)


class ViewHookTests(unittest.TestCase):
    def test_missing_performance_is_none_not_zero(self):
        page = {"rows": [{"item_id": "tc-unmapped-001", "revision_id": "rev-unmapped-001"}]}
        out = attach_performance(page, {"schema_version": "news-performance/v1", "rows": []})
        self.assertIsNone(out["rows"][0]["performance"])

    def test_honesty_label_never_invents_a_number(self):
        summary = summarise_for_event([{
            "horizon": "5m", "status": "missing", "hindsight": False,
            "return_pct": None, "return_bps": None, "forward": None,
            "base": {"record": None}, "instrument_id": "eq.nvda",
            "missing_reason": "not_yet_available"}])
        self.assertEqual(summary["horizons"]["5m"]["honesty_label"], "pending")
        self.assertIsNone(summary["horizons"]["5m"]["return_pct"])


if __name__ == "__main__":
    unittest.main()
