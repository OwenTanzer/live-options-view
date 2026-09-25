#!/usr/bin/env python3
"""Prove MOO-170 findings 6-7: evaluation_report() covers every scored
headline (not just score>=5 posts), keeps Ollama- and VADER-fallback-scored
observations in separate strata (never pooled), and surfaces alert burden
and missed/reconciled unexplained-move coverage.

    python scripts/verify_evaluation.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.storage import Storage  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def scenario_scorers_are_never_pooled() -> None:
    print("\n1. evaluation_report: Ollama-scored and VADER-fallback-scored pins stay in separate strata")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = Storage(Path(tmp.name) / "pins.sqlite3")
        h1 = storage.insert_headline(source="fixture", external_id="1", symbols=["QQQ"],
                                      headline="a", published_at=1, ingested_at=1)
        storage.set_impact_score(h1, 8.0, "reasoning", "mistral-nemo:latest@v1")
        p1 = storage.create_pin(headline_id=h1, symbol="QQQ", window_start=1, price_before=100)
        storage.resolve_pin(p1, price_after=105, pct_move=5.0, volume_ratio=3.0, confirmed=True,
                             classification="subsequent_move")

        h2 = storage.insert_headline(source="fixture", external_id="2", symbols=["QQQ"],
                                      headline="b", published_at=2, ingested_at=2)
        storage.set_impact_score(h2, 6.0, "reasoning", "vader-fallback")
        p2 = storage.create_pin(headline_id=h2, symbol="QQQ", window_start=2, price_before=100)
        storage.resolve_pin(p2, price_after=100, pct_move=0.0, volume_ratio=1.0, confirmed=False,
                             classification="no_qualifying_move")

        report = storage.evaluation_report()
        by_scorer = {row["scorer"]: row for row in report["by_scorer"]}
        check("primary-model and fallback scorer identities appear as separate rows",
              "mistral-nemo:latest@v1" in by_scorer and "vader-fallback" in by_scorer, list(by_scorer))
        check("the primary-model row's totals don't include the fallback pin",
              by_scorer["mistral-nemo:latest@v1"]["total"] == 1, by_scorer.get("mistral-nemo:latest@v1"))
    finally:
        tmp.cleanup()


def scenario_shadow_observations_expand_the_denominator() -> None:
    print("\n2. evaluation_report: below-threshold shadow pins are counted, not just score>=5 posts")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = Storage(Path(tmp.name) / "pins.sqlite3")
        h1 = storage.insert_headline(source="fixture", external_id="1", symbols=["QQQ"],
                                      headline="low score headline", published_at=1, ingested_at=1)
        storage.set_impact_score(h1, 2.0, "reasoning", "mistral-nemo:latest@v1")
        p1 = storage.create_pin(headline_id=h1, symbol="QQQ", window_start=1, price_before=100, shadow=True)
        storage.resolve_pin(p1, price_after=100.4, pct_move=0.4, volume_ratio=1.0, confirmed=False,
                             classification="no_qualifying_move")

        report = storage.evaluation_report()
        check("the shadow pin is counted toward the evaluation denominator", report["shadow_total"] == 1, report["shadow_total"])
        check("but it never reaches the Discord-posting query", storage.unposted_confirmed_pins() == [])
        check("and it's excluded from the headline (posting-worthy) accuracy stat",
              storage.accuracy_stats()["total_pins"] == 0, storage.accuracy_stats())
    finally:
        tmp.cleanup()


def scenario_missed_and_reconciled_moves_are_reported() -> None:
    print("\n3. evaluation_report: unexplained-move coverage is broken into missed vs. later-reconciled")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = Storage(Path(tmp.name) / "pins.sqlite3")
        storage.insert_unexplained_move(symbol="QQQ", pct_move=2.0, zscore=4.0, volume_ratio=3.0)
        move2 = storage.insert_unexplained_move(symbol="AAPL", pct_move=-1.5, zscore=3.5, volume_ratio=2.0)
        h = storage.insert_headline(source="fixture", external_id="1", symbols=["AAPL"],
                                     headline="later news", published_at=100, ingested_at=100)
        storage.attach_matched_headline(move2, h, relation="following", timing_secs=30.0)

        report = storage.evaluation_report()
        check("one move remains unmatched (missed coverage)", report["unexplained_moves_no_match"] == 1, report)
        check("one move was reconciled with a later-arriving headline",
              report["unexplained_moves_reconciled_later"] == 1, report)
    finally:
        tmp.cleanup()


def scenario_scoring_latency_is_reported() -> None:
    print("\n4. scoring_latency_stats: backlog/latency visibility for finding 1")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = Storage(Path(tmp.name) / "pins.sqlite3")
        h1 = storage.insert_headline(source="fixture", external_id="1", symbols=["QQQ"],
                                      headline="a", published_at=1000, ingested_at=1000)
        storage.set_impact_score(h1, 8.0, "reasoning", "fixture")  # scored_at ~= now, not 1000
        stats = storage.scoring_latency_stats()
        check("one scored headline is counted", stats["count"] == 1, stats)
        check("latency is a large positive number (real now - fixture ingested_at=1000)",
              stats["mean_secs"] is not None and stats["mean_secs"] > 0, stats)
    finally:
        tmp.cleanup()


def scenario_control_comparison_reports_explicit_denominator() -> None:
    print("\n5. evaluation_report: no-news control comparison, with pins lacking a clean window counted separately")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = Storage(Path(tmp.name) / "pins.sqlite3")
        h1 = storage.insert_headline(source="fixture", external_id="1", symbols=["QQQ"],
                                      headline="a", published_at=1, ingested_at=1)
        p1 = storage.create_pin(headline_id=h1, symbol="QQQ", window_start=1, price_before=100)
        storage.resolve_pin(p1, price_after=105, pct_move=5.0, volume_ratio=3.0, confirmed=True,
                             classification="subsequent_move")
        storage.set_pin_control(p1, 1.0)

        h2 = storage.insert_headline(source="fixture", external_id="2", symbols=["QQQ"],
                                      headline="b", published_at=2, ingested_at=2)
        p2 = storage.create_pin(headline_id=h2, symbol="QQQ", window_start=2, price_before=100)
        storage.resolve_pin(p2, price_after=100, pct_move=0.0, volume_ratio=1.0, confirmed=False,
                             classification="no_qualifying_move")
        # No set_pin_control call -- simulates a headline sitting too close
        # to the shifted control window to serve as a comparison.

        report = storage.evaluation_report()
        check("only the pin with a clean control window counts toward the denominator",
              report["control_sample_size"] == 1, report["control_sample_size"])
        check("the other pin is reported as having no clean control window, not silently dropped",
              report["control_no_clean_window"] == 1, report["control_no_clean_window"])
        check("the control aggregate reflects the recorded control move",
              report["control_baseline_avg_abs_pct_move"] == 1.0, report["control_baseline_avg_abs_pct_move"])
    finally:
        tmp.cleanup()


def main() -> int:
    scenario_scorers_are_never_pooled()
    scenario_shadow_observations_expand_the_denominator()
    scenario_missed_and_reconciled_moves_are_reported()
    scenario_scoring_latency_is_reported()
    scenario_control_comparison_reports_explicit_denominator()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
