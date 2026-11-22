"""Offline semantic and compatibility checks for the headline pilot."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_news_support import at
from market_data.news.classify import classify_rules
from market_data.news.normalize import canonical_json, normalize_entry
from market_data.news import config, theme_mapping

FIXTURES = Path(__file__).parent / "fixtures" / "news_theme_mapping"
CASES = json.loads((FIXTURES / "cases.json").read_text())
WHEN = at("2026-11-22T05:20:00Z")


class ThemeMapping(unittest.TestCase):
    def classify(self, obs, enabled=True):
        return classify_rules(obs, "fixture-replay", WHEN, fed_treasury_pilot=enabled)

    def headline(self, title):
        obs, _ = normalize_entry(
            {"title": title, "link": "https://example.invalid/review-regression"},
            config.SOURCES["oilprice_com"], "fixture-input", "2026-11-22T05:10:00Z",
            config.config_digest(), {})
        return obs

    def test_notes_and_bills_as_verbs_stay_unknown(self):
        for title in (
            "U.S. Treasury notes sanctions target cybercrime",
            "U.S. Treasury bills contractor for the cost of repairs",
            "U.S. Treasury notes a rise in cybercrime",
        ):
            with self.subTest(title=title):
                result = self.classify(self.headline(title))
                self.assertEqual("unknown", result["instrument_mapping"])
                self.assertEqual([], result["instruments"])
                self.assertEqual([], result["theme_mapping"]["matches"])

    def test_notes_and_bills_with_financing_context(self):
        for title in (
            "U.S. Treasury sells 13-week bills",
            "U.S. Treasury issues 2-year notes",
            "U.S. Treasury announces auction of bills and notes",
            "Auction of bills and notes announced by U.S. Treasury",
            "Demand for 2-year notes sold by U.S. Treasury grows",
            "U.S. Treasury issues notes",
            "United States Treasury sells bills",
            "US Treasury auctions bonds",
            "Notes issued by U.S. Treasury draw demand",
            "U.S. Treasury 13-week bills attract buyers",
            "U.S. Treasury's 2-year notes draw demand",
        ):
            with self.subTest(title=title):
                result = self.classify(self.headline(title))
                self.assertEqual(["treasury_financing"], result["theme_mapping"]["themes"])
                self.assertEqual(["TLT"], result["instruments"])

    def test_nearby_unrelated_financing_stays_unknown(self):
        for title in (
            "U.S. Treasury sanctions cybercriminals; city debt climbs",
            "U.S. Treasury sues an auction house over sanctions breach",
            "Court auction proceeds awarded after lawsuit against U.S. Treasury",
            "U.S. Treasury announces sanctions; city auctions bonds",
            "U.S. Treasury notes city debt issuance",
            "U.S. Treasury bills contractor; 2-year notes sold by city",
            "U.S. Treasury discusses city bond auction",
            "Auction of notes announced by city; U.S. Treasury comments",
            "U.S. Treasury announces auction house sanctions",
        ):
            with self.subTest(title=title):
                obs = self.headline(title)
                before = copy.deepcopy(obs)
                result = self.classify(obs)
                self.assertEqual("unknown", result["instrument_mapping"])
                self.assertEqual([], result["instruments"])
                self.assertEqual([], result["theme_mapping"]["matches"])
                self.assertEqual(before, obs)
                for key, value in result["theme_mapping"]["attribution"].items():
                    self.assertEqual(obs[key], value)

    def test_fed_label_asserts_topic_only_without_new_subjects(self):
        result = self.classify(self.headline("Federal Reserve reports a cybersecurity incident"))
        self.assertEqual(["fed_topic"], result["theme_mapping"]["themes"])
        self.assertEqual("topic_relevance_only", result["theme_mapping"]["meaning"])
        self.assertEqual(["QQQ", "SPY", "TLT"], result["instruments"])
        for title in ("Fed reports a cybersecurity incident", "Bank of England announces a rate cut"):
            self.assertEqual([], self.classify(self.headline(title))["instruments"])

    def test_disabled_matches_frozen_base_byte_for_byte(self):
        baseline = json.loads((FIXTURES / "off_baseline.json").read_text())
        for row, expected in zip(CASES, baseline, strict=True):
            obs = row["observation"]
            self.assertEqual(canonical_json(expected), canonical_json(self.classify(obs, False)))
            self.assertEqual(expected, classify_rules(obs, "fixture-replay", WHEN))

    def test_semantics_attribution_determinism_and_no_mutation(self):
        for row in CASES:
            with self.subTest(title=row["observation"]["title"]):
                obs = copy.deepcopy(row["observation"])
                result = self.classify(obs)
                self.assertEqual(result, self.classify(obs))
                self.assertEqual(obs, row["observation"])
                self.assertEqual(row["expected_themes"], result["theme_mapping"]["themes"])
                self.assertEqual(row["expected_instruments"], result["instruments"])
                self.assertEqual(result["instrument_mapping"], "theme_relevance" if row["expected_themes"] else "unknown")
                for key, value in result["theme_mapping"]["attribution"].items():
                    self.assertEqual(obs[key], value)
                for match in result["theme_mapping"]["matches"]:
                    self.assertEqual(match["text"], obs["title"][match["start"]:match["end"]])
                self.assertEqual(obs["revision_id"], result["input_revision_id"])
                self.assertEqual(self.classify(obs, False)["labels"], result["labels"])

    def test_version_and_identity_separate_from_baseline(self):
        obs = CASES[0]["observation"]
        off, on = self.classify(obs, False), self.classify(obs)
        for key in ("classification_id", "version", "rules_digest"):
            self.assertNotEqual(off[key], on[key])
        revised = {**obs, "revision_id": "new-revision"}
        self.assertNotEqual(on["classification_id"], self.classify(revised)["classification_id"])

    def test_only_frozen_approved_sources(self):
        self.assertEqual(set(theme_mapping.SOURCE_IDS), {s.id for s in config.enabled_sources()})
        for source in ("unknown", "marketwatch", None):
            result = self.classify({**CASES[0]["observation"], "source_id": source})
            self.assertEqual([], result["instruments"])
            self.assertEqual("source_out_of_scope", result["theme_mapping"]["basis"])

    def test_no_excerpt_category_or_source_name_matches(self):
        obs = {**CASES[0]["observation"], "title": "Unrelated story",
               "excerpt": "Federal Reserve", "categories": ["FOMC"], "source_name": "Federal Reserve"}
        self.assertEqual([], self.classify(obs)["instruments"])

    def test_missing_time_is_not_imputed(self):
        result = self.classify(CASES[1]["observation"])
        self.assertIsNone(result["theme_mapping"]["attribution"]["published_at"])
        self.assertEqual("missing", result["theme_mapping"]["attribution"]["time_quality"]["published"])

    def test_cli_is_disabled_by_default_and_rejects_naive_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            path.write_text(canonical_json(CASES[0]["observation"]) + "\n", encoding="utf-8")
            command = [sys.executable, "-m", "market_data.news.theme_mapping", str(path),
                       "--run-id", "fixture-replay", "--classified-at", "2026-11-22T05:20:00Z"]
            off = subprocess.run(command, capture_output=True, text=True, check=True)
            on = subprocess.run(command + ["--enable-fed-treasury"], capture_output=True, text=True, check=True)
            self.assertEqual(self.classify(CASES[0]["observation"], False), json.loads(off.stdout))
            self.assertEqual(self.classify(CASES[0]["observation"]), json.loads(on.stdout))
            command[-1] = "2026-11-22T05:20:00"
            self.assertNotEqual(0, subprocess.run(command, capture_output=True).returncode)


if __name__ == "__main__":
    unittest.main()
