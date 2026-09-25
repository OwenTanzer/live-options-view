#!/usr/bin/env python3
"""Prove MOO-170 finding 4 end-to-end through main.py's ingest path and
Storage, covering the acceptance-listed relevance scenarios: an irrelevant
intervening headline must not silently explain an anomaly, a genuinely
relevant headline (including one that arrives later) must be attachable as
evidence, a duplicate must not double-count, and untagged-wire matching
must use word boundaries rather than raw substring containment.

    python scripts/verify_relevance_matching.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app  # noqa: E402
from correlate.anomaly import find_relevant_headline  # noqa: E402
from db.storage import Storage  # noqa: E402
from ingest.dedup import RecentTextWindow  # noqa: E402
from ingest.types import Headline  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def make_storage(tmp: tempfile.TemporaryDirectory) -> Storage:
    return Storage(Path(tmp.name) / "pins.sqlite3")


def scenario_word_boundary_match_rejects_substring_collision() -> None:
    print("\n1. main._matches_watchlist_ticker: word-boundary match, not raw substring")
    check("'ON' does not match inside 'MONDAY'", not app._matches_watchlist_ticker("Markets closed Monday", "ON"))
    check("'ON' matches 'ON Semiconductor beats estimates'", app._matches_watchlist_ticker("ON Semiconductor beats estimates", "ON"))
    check("'ALL' does not match inside 'BALLPARK ESTIMATE'", not app._matches_watchlist_ticker("A ballpark estimate for Q3", "ALL"))


async def scenario_irrelevant_intervening_headline_is_not_a_match() -> None:
    print("\n2. find_relevant_headline: a headline for a different symbol is never returned as a match")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = make_storage(tmp)
        storage.insert_headline(source="fixture", external_id="1", symbols=["MSFT"],
                                 headline="MSFT antitrust ruling", published_at=1000.0, ingested_at=1000.0)
        match = find_relevant_headline(storage, "QQQ", 1000.0)
        check("no QQQ-tagged candidate exists, so nothing is matched", match is None)
    finally:
        tmp.cleanup()


async def scenario_genuinely_later_headline_is_reconciled() -> None:
    print("\n3. find_relevant_headline: a QQQ headline published AFTER the anomaly is still found")
    tmp = tempfile.TemporaryDirectory()
    try:
        storage = make_storage(tmp)
        anomaly_ts = 1000.0
        storage.insert_headline(source="fixture", external_id="1", symbols=["QQQ"],
                                 headline="QQQ regulatory filing, reported after the fact",
                                 published_at=anomaly_ts + 90.0, ingested_at=anomaly_ts + 90.0)
        move_id = storage.insert_unexplained_move(symbol="QQQ", pct_move=2.0, zscore=4.0, volume_ratio=3.0)
        match = find_relevant_headline(storage, "QQQ", anomaly_ts)
        check("a later-published headline is found as a candidate", match is not None, match)
        headline_id, relation, timing_secs = match
        check("it's dated as 'following', never presented as known at detection time", relation == "following", relation)
        storage.attach_matched_headline(move_id, headline_id, relation=relation, timing_secs=timing_secs)
        row = storage.unposted_unexplained_moves()[0]
        check("the move row now carries the retrospective link", row["matched_headline_id"] == headline_id, dict(row))
    finally:
        tmp.cleanup()


async def scenario_duplicate_headline_does_not_double_match() -> None:
    print("\n4. ingest_loop: a cross-source duplicate is recorded but not independently re-scored")
    tmp = tempfile.TemporaryDirectory()
    scored = []

    async def fake_score_headline(session, headline, symbols):
        scored.append(headline)
        return 9.0, "fixture", "fixture"

    try:
        storage = make_storage(tmp)
        dedup_window = RecentTextWindow()
        score_queue: asyncio.Queue = asyncio.Queue()

        async def source():
            yield Headline("alpaca", "1", ["QQQ"], "Fed cuts rates unexpectedly", "", "", 1000.0, 1000.0)
            yield Headline("finnhub", "2", ["QQQ"], "Fed cuts rates unexpectedly", "", "", 1005.0, 1005.0)

        await asyncio.wait_for(app.ingest_loop(source(), storage, dedup_window, score_queue), timeout=1.0)
        check("only the first (novel) headline was queued for scoring", score_queue.qsize() == 1, score_queue.qsize())
        with storage._connect() as conn:
            dup_row = conn.execute("SELECT is_duplicate_of FROM headlines WHERE external_id = '2'").fetchone()
        check("the duplicate is still recorded, linked via is_duplicate_of (not silently dropped)",
              dup_row is not None and dup_row[0] == 1, None if dup_row is None else dup_row[0])
    finally:
        tmp.cleanup()


def main() -> int:
    scenario_word_boundary_match_rejects_substring_collision()
    asyncio.run(scenario_irrelevant_intervening_headline_is_not_a_match())
    asyncio.run(scenario_genuinely_later_headline_is_reconciled())
    asyncio.run(scenario_duplicate_headline_does_not_double_match())

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
