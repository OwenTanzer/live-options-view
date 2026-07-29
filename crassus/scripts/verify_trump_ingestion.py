#!/usr/bin/env python3
"""Prove trump_whisperer_qqq's feed ingestion layer -- not just the
aggregation math and strategy decisions verify_trump_whisperer.py covers.

Hermetic like the other verify scripts: no network access, no real request
to trumpstruth.org. `_parse_feed`/`_fetch_posts` are exercised against a
fake `requests`-shaped session serving hand-built RSS XML bodies.

    python scripts/verify_trump_ingestion.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.trump_sentiment import (  # noqa: E402
    TrumpFetchError,
    TrumpSentimentReader,
    _fetch_posts,
    _parse_feed,
)

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def _rss(items_xml: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Trump's Truth - Latest Posts</title>
{items_xml}
</channel></rss>""".encode("utf-8")


def _item(title: str, link: str = "https://trumpstruth.org/statuses/1", pub_date: str | None = "Wed, 29 Jul 2026 11:46:42 +0000") -> str:
    pub_date_xml = f"<pubDate>{pub_date}</pubDate>" if pub_date is not None else ""
    return f"<item><title><![CDATA[{title}]]></title><link>{link}</link>{pub_date_xml}</item>"


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b""):
        self.status_code = status_code
        self.content = content


class FakeSession:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeSession: no more queued responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# _parse_feed
# ---------------------------------------------------------------------------


def scenario_parse_basic_items() -> None:
    print("\n1. _parse_feed: title/link/pubDate parsed for a normal item")
    xml = _rss(_item("Big news today"))
    posts = _parse_feed(xml)
    check("parsed exactly one post", len(posts) == 1, len(posts))
    check("text is the item title", posts[0]["text"] == "Big news today", posts[0]["text"])
    check("link is captured", posts[0]["link"] == "https://trumpstruth.org/statuses/1")
    check("published_at is parsed from RFC822 pubDate", posts[0]["published_at"] is not None)
    check(
        "published_at is timezone-aware UTC",
        posts[0]["published_at"].utcoffset() is not None,
        posts[0]["published_at"],
    )


def scenario_parse_multiple_items_preserves_order() -> None:
    print("\n2. _parse_feed: multiple items are parsed in feed order")
    xml = _rss(_item("first") + _item("second") + _item("third"))
    posts = _parse_feed(xml)
    check("parsed all three posts", len(posts) == 3, len(posts))
    check("order is preserved", [p["text"] for p in posts] == ["first", "second", "third"])


def scenario_parse_missing_pubdate_is_none_not_an_error() -> None:
    print("\n3. _parse_feed: an item with no pubDate gets published_at=None, not a parse error")
    xml = _rss(_item("no date here", pub_date=None))
    posts = _parse_feed(xml)
    check("still parses the post", len(posts) == 1, len(posts))
    check("published_at is None", posts[0]["published_at"] is None)


def scenario_parse_garbage_pubdate_is_none_not_an_error() -> None:
    print("\n4. _parse_feed: an unparseable pubDate gets published_at=None, not a crash")
    xml = _rss(_item("weird date", pub_date="not a real date"))
    posts = _parse_feed(xml)
    check("still parses the post", len(posts) == 1, len(posts))
    check("published_at is None for garbage input", posts[0]["published_at"] is None)


def scenario_parse_malformed_xml_raises() -> None:
    print("\n5. _parse_feed: malformed XML raises TrumpFetchError, not a raw ParseError")
    try:
        _parse_feed(b"<rss><channel><item><title>unclosed")
        check("raised TrumpFetchError on malformed XML", False)
    except TrumpFetchError:
        check("raised TrumpFetchError on malformed XML", True)
    except Exception as exc:
        check("raised TrumpFetchError on malformed XML (got wrong type)", False, f"{type(exc).__name__}: {exc}")


def scenario_parse_missing_channel_raises() -> None:
    print("\n6. _parse_feed: valid XML but no <channel> element raises TrumpFetchError")
    try:
        _parse_feed(b'<?xml version="1.0"?><rss version="2.0"></rss>')
        check("raised TrumpFetchError on missing channel", False)
    except TrumpFetchError:
        check("raised TrumpFetchError on missing channel", True)


def scenario_parse_empty_channel() -> None:
    print("\n7. _parse_feed: a channel with no items returns an empty list, not an error")
    posts = _parse_feed(_rss(""))
    check("returns an empty list", posts == [], posts)


# ---------------------------------------------------------------------------
# _fetch_posts
# ---------------------------------------------------------------------------


def scenario_fetch_success() -> None:
    print("\n8. _fetch_posts: a 200 response is parsed and truncated to `limit`")
    xml = _rss(_item("a") + _item("b") + _item("c"))
    session = FakeSession([FakeResponse(200, xml)])
    posts = _fetch_posts(session, feed_url="https://trumpstruth.org/feed", limit=2)
    check("truncates to the requested limit", len(posts) == 2, len(posts))
    check("made exactly one request (no pagination)", len(session.calls) == 1, len(session.calls))


def scenario_fetch_http_error() -> None:
    print("\n9. _fetch_posts: non-200 status raises TrumpFetchError")
    session = FakeSession([FakeResponse(503, b"")])
    try:
        _fetch_posts(session, feed_url="https://trumpstruth.org/feed", limit=10)
        check("raised TrumpFetchError on HTTP 503", False)
    except TrumpFetchError as exc:
        check("raised TrumpFetchError on HTTP 503", True)
        check("message names the status code", "503" in str(exc), str(exc))


def scenario_fetch_request_exception() -> None:
    print("\n10. _fetch_posts: a raised request exception is wrapped, not left to escape")
    session = FakeSession([ConnectionError("boom")])
    try:
        _fetch_posts(session, feed_url="https://trumpstruth.org/feed", limit=10)
        check("wrapped the request exception", False)
    except TrumpFetchError as exc:
        check("wrapped the request exception", True)
        check("original exception is chained", isinstance(exc.__cause__, ConnectionError))


def scenario_fetch_malformed_body_raises() -> None:
    print("\n11. _fetch_posts: a 200 response with malformed XML still raises TrumpFetchError")
    session = FakeSession([FakeResponse(200, b"not xml at all <<<")])
    try:
        _fetch_posts(session, feed_url="https://trumpstruth.org/feed", limit=10)
        check("raised TrumpFetchError on malformed body", False)
    except TrumpFetchError:
        check("raised TrumpFetchError on malformed body", True)


# ---------------------------------------------------------------------------
# TrumpSentimentReader -- caching behavior
# ---------------------------------------------------------------------------


class FakeAnalyzer:
    def polarity_scores(self, text: str) -> dict[str, float]:
        return {"compound": 0.5}


def scenario_reader_caches_within_min_interval() -> None:
    print("\n12. TrumpSentimentReader: does not re-fetch inside min_interval_s")
    xml = _rss(_item("a"))
    session = FakeSession([FakeResponse(200, xml)])
    reader = TrumpSentimentReader(
        session_factory=lambda: session,
        analyzer_factory=lambda: FakeAnalyzer(),
        min_interval_s=300.0,
    )
    reader.read()
    reader.read()
    check("only fetched once across two read() calls", len(session.calls) == 1, len(session.calls))


def scenario_reader_force_refetches() -> None:
    print("\n13. TrumpSentimentReader: force=True bypasses the cache")
    xml = _rss(_item("a"))
    session = FakeSession([FakeResponse(200, xml), FakeResponse(200, xml)])
    reader = TrumpSentimentReader(
        session_factory=lambda: session,
        analyzer_factory=lambda: FakeAnalyzer(),
        min_interval_s=300.0,
    )
    reader.read()
    reader.read(force=True)
    check("fetched twice when forced", len(session.calls) == 2, len(session.calls))


def main() -> int:
    for scenario in (
        scenario_parse_basic_items,
        scenario_parse_multiple_items_preserves_order,
        scenario_parse_missing_pubdate_is_none_not_an_error,
        scenario_parse_garbage_pubdate_is_none_not_an_error,
        scenario_parse_malformed_xml_raises,
        scenario_parse_missing_channel_raises,
        scenario_parse_empty_channel,
        scenario_fetch_success,
        scenario_fetch_http_error,
        scenario_fetch_request_exception,
        scenario_fetch_malformed_body_raises,
        scenario_reader_caches_within_min_interval,
        scenario_reader_force_refetches,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
