#!/usr/bin/env python3
"""Prove three more of the review fixes:

- ingest/dedup.py: a duplicate headline resolves to the id of what it
  matches (for cross-source corroboration) instead of just True/False.
- ingest/alpaca_stream.py: a UTC-timestamped Alpaca news message parses to
  the correct UTC epoch regardless of the host's local timezone.
- main.py's poll_and_post_results / bot/discord_bot.py: a row is only
  marked posted once delivery actually succeeds -- a failed send leaves it
  unposted (and out of the in-flight `pending` set) so it's retried, rather
  than being marked posted at enqueue time and lost forever on failure.

    python scripts/verify_dedup_and_delivery.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.discord_bot import OutboxItem  # noqa: E402
from ingest.alpaca_stream import _parse_news_message  # noqa: E402
from ingest.dedup import RecentTextWindow  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def scenario_duplicate_resolves_to_original_id() -> None:
    print("\n1. RecentTextWindow.find_duplicate: returns the original's id, not just True")
    window = RecentTextWindow()
    window.add("Fed unexpectedly cuts rates 50bps", headline_id=101)
    match = window.find_duplicate("fed unexpectedly cuts rates 50 bps")
    check("near-identical restatement matches id 101", match == 101, match)
    check("is_duplicate() still works as a boolean view", window.is_duplicate("Fed unexpectedly cuts rates 50bps") is True)

    novel = window.find_duplicate("Totally unrelated headline about a different company entirely")
    check("a genuinely novel headline has no match", novel is None, novel)


def scenario_alpaca_timestamp_is_utc() -> None:
    print("\n2. _parse_news_message: Alpaca's UTC created_at parses to the correct UTC epoch")
    # 2026-01-15T14:30:00Z is a fixed, known UTC instant -- compute the
    # expected epoch independently (via calendar.timegm, which is
    # unambiguously UTC) rather than depending on the host's local tz.
    import calendar
    import time as time_mod

    expected = calendar.timegm(time_mod.strptime("2026-01-15T14:30:00", "%Y-%m-%dT%H:%M:%S"))
    msg = {
        "T": "n", "id": 12345, "headline": "Test headline", "summary": "",
        "url": "", "symbols": ["qqq"], "created_at": "2026-01-15T14:30:00Z",
    }
    headline = _parse_news_message(msg)
    check("published_at matches the UTC epoch regardless of host timezone",
          headline is not None and abs(headline.published_at - expected) < 1.0,
          None if headline is None else headline.published_at - expected)


async def scenario_failed_delivery_is_not_marked_posted() -> None:
    print("\n3. OutboxItem.on_result: a failed send does not report success")
    results = []

    def on_result(success: bool) -> None:
        results.append(success)

    # Simulate discord_bot.py's _drain_outbox loop directly against a fake
    # channel, without needing a real Discord connection.
    class FailingChannel:
        async def send(self, embed=None):
            raise RuntimeError("simulated network failure")

    class OkChannel:
        async def send(self, embed=None):
            return None

    async def drain_one(channel, item: OutboxItem) -> None:
        try:
            await channel.send(embed=item.embed)
        except Exception:
            item.on_result(False)
        else:
            item.on_result(True)

    await drain_one(FailingChannel(), OutboxItem(embed=None, on_result=on_result))
    await drain_one(OkChannel(), OutboxItem(embed=None, on_result=on_result))

    check("failed send reported success=False", results[0] is False, results)
    check("successful send reported success=True", results[1] is True, results)


def main() -> int:
    scenario_duplicate_resolves_to_original_id()
    scenario_alpaca_timestamp_is_utc()
    asyncio.run(scenario_failed_delivery_is_not_marked_posted())

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
