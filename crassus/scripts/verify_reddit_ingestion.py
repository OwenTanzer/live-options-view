#!/usr/bin/env python3
"""Prove the reddit_sentiment_qqq ingestion layer -- not just the aggregation
math and strategy decisions `verify_reddit_sentiment.py` already covers.

Hermetic like the other verify scripts: no network access, no real Reddit
request, no real Playwright/Chromium process. `_fetch_listing_json` is
exercised against a fake `requests`-shaped session; `_fetch_listing_browser`
against fake Playwright-shaped context/page/element objects; and
`RedditSentimentReader._fetch_listing`'s layer-selection, 429 cooldown, and
browser-crash-recovery logic against both together via constructor-injected
factories.

    python scripts/verify_reddit_ingestion.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.sentiment import (  # noqa: E402
    RedditFetchError,
    RedditRateLimited,
    RedditSentimentReader,
    _DEFAULT_RATE_LIMIT_COOLDOWN_S,
    _fetch_listing_browser,
    _fetch_listing_json,
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


# ---------------------------------------------------------------------------
# Fakes for the JSON layer (_fetch_listing_json)
# ---------------------------------------------------------------------------


class FakeJsonResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


def _listing_payload(titles: list[str], after: str | None) -> dict:
    return {
        "data": {
            "children": [{"data": {"title": t, "selftext": ""}} for t in titles],
            "after": after,
        }
    }


class FakeSession:
    """Queues responses/exceptions to return in order; records every call's
    url/params/timeout so pagination and cooldown behavior can be asserted
    on, not just the final return value."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeSession: no more queued responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# Fakes for the browser layer (_fetch_listing_browser)
# ---------------------------------------------------------------------------


class FakeBodyElement:
    def __init__(self, text: str):
        self._text = text

    def inner_text(self) -> str:
        return self._text


class FakeElement:
    def __init__(self, post_id: str, title: str, body: str | None = None):
        self._id = post_id
        self._title = title
        self._body = body

    def get_attribute(self, name: str):
        if name == "id":
            return self._id
        if name == "post-title":
            return self._title
        if name == "permalink":
            return f"/permalink/{self._id}"
        return None

    def query_selector(self, selector: str):
        if selector == '[slot="text-body"]' and self._body is not None:
            return FakeBodyElement(self._body)
        return None


class FakeMouse:
    def __init__(self, page: "FakePage"):
        self._page = page

    def wheel(self, x, y):
        self._page.scroll_count += 1


class FakePage:
    """`batches[i]` is what `query_selector_all` returns for the i-th read;
    the last batch repeats once exhausted (simulating a feed that's stopped
    growing, which is what should trigger the stall limit)."""

    def __init__(
        self,
        batches: list[list[FakeElement]] | None = None,
        goto_error: Exception | None = None,
        selector_error: Exception | None = None,
        crash_browser: "FakeBrowser | None" = None,
    ):
        self._batches = batches or [[]]
        self._read_index = 0
        self._goto_error = goto_error
        self._selector_error = selector_error
        self._crash_browser = crash_browser
        self.scroll_count = 0
        self.closed = False
        self.mouse = FakeMouse(self)

    def goto(self, url, timeout=None):
        if self._goto_error is not None:
            if self._crash_browser is not None:
                self._crash_browser.mark_disconnected()
            raise self._goto_error

    def wait_for_selector(self, selector, timeout=None):
        if self._selector_error is not None:
            if self._crash_browser is not None:
                self._crash_browser.mark_disconnected()
            raise self._selector_error

    def query_selector_all(self, selector):
        idx = min(self._read_index, len(self._batches) - 1)
        return self._batches[idx]

    def wait_for_timeout(self, ms):
        self._read_index += 1

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[FakePage]):
        self._pages = list(pages)
        self.new_page_calls = 0
        self.closed = False

    def new_page(self):
        self.new_page_calls += 1
        return self._pages.pop(0)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, connected: bool = True):
        self._connected = connected
        self.closed = False

    def is_connected(self) -> bool:
        return self._connected

    def mark_disconnected(self) -> None:
        self._connected = False

    def close(self) -> None:
        self.closed = True
        self._connected = False


class FakePlaywrightHandle:
    def __init__(self):
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeBrowserFactory:
    """Constructor-injectable `browser_factory`: returns queued
    (playwright, browser, context) triples in order and records how many
    times a launch was attempted, so tests can assert exactly one relaunch
    happens on a crash, not an unbounded retry loop."""

    def __init__(self, triples: list):
        self._triples = list(triples)
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        if not self._triples:
            raise AssertionError("FakeBrowserFactory: no more queued browsers")
        item = self._triples.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _reader(session, browser_factory=None, rate_limited_until: float | None = None) -> RedditSentimentReader:
    reader = RedditSentimentReader(
        symbol="QQQ",
        session_factory=lambda: session,
        analyzer_factory=lambda: None,  # never invoked by _fetch_listing directly
        browser_factory=browser_factory or (lambda: (_ for _ in ()).throw(AssertionError("browser_factory should not be called"))),
    )
    reader._session = session  # pre-seed so _fetch_listing doesn't need read()
    if rate_limited_until is not None:
        reader._rate_limited_until = rate_limited_until
    return reader


# ---------------------------------------------------------------------------
# Scenarios: _fetch_listing_json
# ---------------------------------------------------------------------------


def scenario_json_single_page() -> None:
    print("\n1. _fetch_listing_json: single page under the 100-item cap")
    session = FakeSession([FakeJsonResponse(200, _listing_payload(["a", "b", "c"], after=None))])
    posts = _fetch_listing_json(session, "wallstreetbets", limit=50)
    check("returns every post from the one page", len(posts) == 3, len(posts))
    check("made exactly one request", len(session.calls) == 1, len(session.calls))
    check("no `after` param on the first request", "after" not in session.calls[0]["params"])


def scenario_json_pagination() -> None:
    print("\n2. _fetch_listing_json: paginates via `after` past the 100-item cap")
    page1 = FakeJsonResponse(200, _listing_payload([f"t{i}" for i in range(100)], after="cursor123"))
    page2 = FakeJsonResponse(200, _listing_payload(["t100", "t101"], after=None))
    session = FakeSession([page1, page2])
    posts = _fetch_listing_json(session, "stocks", limit=102)
    check("collects across both pages", len(posts) == 102, len(posts))
    check("made two requests", len(session.calls) == 2, len(session.calls))
    check("second request carries the `after` cursor", session.calls[1]["params"].get("after") == "cursor123")


def scenario_json_stops_at_limit_not_cursor() -> None:
    print("\n3. _fetch_listing_json: stops once `limit` is reached even if more pages exist")
    page1 = FakeJsonResponse(200, _listing_payload([f"t{i}" for i in range(100)], after="cursor123"))
    session = FakeSession([page1])
    posts = _fetch_listing_json(session, "options", limit=10)
    check("truncates to the requested limit", len(posts) == 10, len(posts))
    check("did not fetch a second page it didn't need", len(session.calls) == 1, len(session.calls))


def scenario_json_http_error() -> None:
    print("\n4. _fetch_listing_json: non-200/429 status raises RedditFetchError")
    session = FakeSession([FakeJsonResponse(403, None)])
    try:
        _fetch_listing_json(session, "wallstreetbets", limit=10)
        check("raised RedditFetchError on HTTP 403", False)
    except RedditFetchError as exc:
        check("raised RedditFetchError on HTTP 403", True)
        check("message names the status code", "403" in str(exc), str(exc))
    except Exception as exc:
        check("raised RedditFetchError on HTTP 403 (got wrong type)", False, f"{type(exc).__name__}: {exc}")


def scenario_json_malformed_payload() -> None:
    print("\n5. _fetch_listing_json: malformed payload raises RedditFetchError, not KeyError")
    session = FakeSession([FakeJsonResponse(200, {"unexpected": "shape"})])
    try:
        _fetch_listing_json(session, "investing", limit=10)
        check("raised RedditFetchError on malformed payload", False)
    except RedditFetchError:
        check("raised RedditFetchError on malformed payload", True)
    except Exception as exc:
        check("raised RedditFetchError on malformed payload (got wrong type)", False, f"{type(exc).__name__}: {exc}")


def scenario_json_request_exception() -> None:
    print("\n6. _fetch_listing_json: a raised request exception is wrapped, not left to escape")
    session = FakeSession([ConnectionError("boom")])
    try:
        _fetch_listing_json(session, "stocks", limit=10)
        check("wrapped the request exception", False)
    except RedditFetchError as exc:
        check("wrapped the request exception", True)
        check("original exception is chained", isinstance(exc.__cause__, ConnectionError))


def scenario_json_429_with_retry_after() -> None:
    print("\n7. _fetch_listing_json: 429 with a numeric Retry-After raises RedditRateLimited")
    session = FakeSession([FakeJsonResponse(429, None, headers={"Retry-After": "37"})])
    try:
        _fetch_listing_json(session, "wallstreetbets", limit=10)
        check("raised RedditRateLimited", False)
    except RedditRateLimited as exc:
        check("raised RedditRateLimited", True)
        check("retry_after_s matches the header", exc.retry_after_s == 37.0, exc.retry_after_s)
    except RedditFetchError:
        check("raised RedditRateLimited (got base RedditFetchError instead)", False)


def scenario_json_429_without_retry_after() -> None:
    print("\n8. _fetch_listing_json: 429 with no/garbage Retry-After uses the default cooldown")
    for headers in ({}, {"Retry-After": "not-a-number"}):
        session = FakeSession([FakeJsonResponse(429, None, headers=headers)])
        try:
            _fetch_listing_json(session, "wallstreetbets", limit=10)
            check(f"raised RedditRateLimited for headers={headers}", False)
        except RedditRateLimited as exc:
            check(
                f"defaulted retry_after_s for headers={headers}",
                exc.retry_after_s == _DEFAULT_RATE_LIMIT_COOLDOWN_S,
                exc.retry_after_s,
            )


# ---------------------------------------------------------------------------
# Scenarios: _fetch_listing_browser
# ---------------------------------------------------------------------------


def scenario_browser_happy_path() -> None:
    print("\n9. _fetch_listing_browser: reads title + self-text body off shreddit-post elements")
    batch = [FakeElement("t1", "First post", body="hello world"), FakeElement("t2", "Link post", body=None)]
    page = FakePage(batches=[batch])
    context = FakeContext([page])
    posts = _fetch_listing_browser(context, "wallstreetbets", limit=5)
    check("returns both posts", len(posts) == 2, len(posts))
    check("self-post body is captured", posts[0] == {"title": "First post", "selftext": "hello world"}, posts[0])
    check("link post (no body slot) gets empty selftext, not an error", posts[1]["selftext"] == "", posts[1])
    check("page is always closed", page.closed)


def scenario_browser_scrolls_for_more_posts() -> None:
    print("\n10. _fetch_listing_browser: scrolls when the feed lazy-loads more posts")
    batch0 = [FakeElement("t1", "Post 1")]
    batch1 = [FakeElement("t1", "Post 1"), FakeElement("t2", "Post 2"), FakeElement("t3", "Post 3")]
    page = FakePage(batches=[batch0, batch1])
    context = FakeContext([page])
    posts = _fetch_listing_browser(context, "stocks", limit=3)
    check("collected all 3 posts across the scroll", len(posts) == 3, len(posts))
    check("scrolled exactly once to get there", page.scroll_count == 1, page.scroll_count)


def scenario_browser_stalls_on_thin_subreddit() -> None:
    print("\n11. _fetch_listing_browser: gives up after repeated stalls instead of looping forever")
    batch = [FakeElement("t1", "Only post")]
    page = FakePage(batches=[batch])  # same single post on every read -> never grows
    context = FakeContext([page])
    posts = _fetch_listing_browser(context, "investing", limit=50)
    check("returns the one post actually available", len(posts) == 1, len(posts))
    check("terminates in bounded scrolls rather than hanging", page.scroll_count <= 5, page.scroll_count)


def scenario_browser_navigation_failure() -> None:
    print("\n12. _fetch_listing_browser: a goto() failure raises RedditFetchError, page still closed")
    page = FakePage(goto_error=RuntimeError("net::ERR_CONNECTION_RESET"))
    context = FakeContext([page])
    try:
        _fetch_listing_browser(context, "wallstreetbets", limit=5)
        check("raised RedditFetchError on navigation failure", False)
    except RedditFetchError:
        check("raised RedditFetchError on navigation failure", True)
    check("page was still closed via the finally block", page.closed)


# ---------------------------------------------------------------------------
# Scenarios: RedditSentimentReader._fetch_listing (layer selection, 429
# cooldown, browser crash recovery)
# ---------------------------------------------------------------------------


def scenario_reader_json_success_never_touches_browser() -> None:
    print("\n13. Reader: JSON success never invokes the browser fallback")
    session = FakeSession([FakeJsonResponse(200, _listing_payload(["a"], after=None))])
    reader = _reader(session)
    posts = reader._fetch_listing("wallstreetbets")
    check("returns the JSON posts", len(posts) == 1, len(posts))


def scenario_reader_falls_back_to_browser_on_json_failure() -> None:
    print("\n14. Reader: JSON failure (non-429) falls back to the browser and launches it once")
    session = FakeSession([FakeJsonResponse(403, None)])
    page = FakePage(batches=[[FakeElement("t1", "Fallback post")]])
    context = FakeContext([page])
    factory = FakeBrowserFactory([(FakePlaywrightHandle(), FakeBrowser(), context)])
    reader = _reader(session, browser_factory=factory)
    posts = reader._fetch_listing("wallstreetbets")
    check("returns the browser-fetched posts", len(posts) == 1, len(posts))
    check("launched the browser exactly once", factory.call_count == 1, factory.call_count)


def scenario_reader_reuses_browser_context_across_calls() -> None:
    print("\n15. Reader: a healthy browser context is reused, not relaunched, on the next call")
    session_1 = FakeSession([FakeJsonResponse(403, None)])
    session_2 = FakeSession([FakeJsonResponse(403, None)])
    page_1 = FakePage(batches=[[FakeElement("t1", "First")]])
    page_2 = FakePage(batches=[[FakeElement("t2", "Second")]])
    context = FakeContext([page_1, page_2])
    factory = FakeBrowserFactory([(FakePlaywrightHandle(), FakeBrowser(), context)])
    reader = _reader(session_1, browser_factory=factory)
    reader._fetch_listing("wallstreetbets")
    reader._session = session_2
    reader._fetch_listing("stocks")
    check("only launched once across two fallback calls", factory.call_count == 1, factory.call_count)
    check("opened a fresh page each time from the same context", context.new_page_calls == 2, context.new_page_calls)


def scenario_reader_429_declines_without_touching_browser() -> None:
    print("\n16. Reader: a 429 declines outright, never invoking the browser fallback")
    session = FakeSession([FakeJsonResponse(429, None, headers={"Retry-After": "42"})])
    reader = _reader(session)  # browser_factory would assert-fail if called
    try:
        reader._fetch_listing("wallstreetbets")
        check("raised on 429", False)
    except RedditFetchError:
        check("raised on 429", True)
    check("started a cooldown for the reported duration", reader._rate_limited_until > 0)


def scenario_reader_429_cooldown_blocks_subsequent_subreddits() -> None:
    print("\n17. Reader: the 429 cooldown is shared across subreddits in the same and later cycles")
    session = FakeSession([FakeJsonResponse(429, None, headers={"Retry-After": "42"})])
    reader = _reader(session)
    try:
        reader._fetch_listing("wallstreetbets")
    except RedditFetchError:
        pass
    calls_after_first = len(session.calls)
    try:
        reader._fetch_listing("stocks")
        check("second subreddit also declines during cooldown", False)
    except RedditFetchError as exc:
        check("second subreddit also declines during cooldown", True)
        check("reason mentions cooling down", "cooling down" in str(exc), str(exc))
    check("no new HTTP request was made for the second subreddit", len(session.calls) == calls_after_first, session.calls)


def scenario_reader_cooldown_expires() -> None:
    print("\n18. Reader: once the cooldown window has passed, fetching resumes normally")
    session = FakeSession([FakeJsonResponse(200, _listing_payload(["a"], after=None))])
    reader = _reader(session, rate_limited_until=0.0)  # already expired (monotonic time is always > 0)
    posts = reader._fetch_listing("wallstreetbets")
    check("fetches normally once cooldown has expired", len(posts) == 1, len(posts))


def scenario_reader_recovers_from_crashed_browser() -> None:
    print("\n19. Reader: a crashed/disconnected browser is torn down and relaunched exactly once")
    session = FakeSession([FakeJsonResponse(403, None), FakeJsonResponse(403, None)])
    dead_browser = FakeBrowser(connected=True)
    dead_page = FakePage(goto_error=RuntimeError("target crashed"), crash_browser=dead_browser)
    dead_context = FakeContext([dead_page])
    dead_playwright = FakePlaywrightHandle()

    healthy_browser = FakeBrowser(connected=True)
    healthy_page = FakePage(batches=[[FakeElement("t1", "Recovered post")]])
    healthy_context = FakeContext([healthy_page])
    healthy_playwright = FakePlaywrightHandle()

    factory = FakeBrowserFactory([
        (dead_playwright, dead_browser, dead_context),
        (healthy_playwright, healthy_browser, healthy_context),
    ])
    reader = _reader(session, browser_factory=factory)
    posts = reader._fetch_listing("wallstreetbets")
    check("recovered and returned posts from the relaunched browser", len(posts) == 1, len(posts))
    check("relaunched exactly once (two total launches)", factory.call_count == 2, factory.call_count)
    check("the dead context was closed", dead_context.closed)
    check("the dead playwright handle was stopped", dead_playwright.stopped)


def scenario_reader_gives_up_when_relaunch_also_fails() -> None:
    print("\n20. Reader: if the relaunch also fails, it raises rather than retrying forever")
    session = FakeSession([FakeJsonResponse(403, None)])
    dead_browser = FakeBrowser(connected=True)
    dead_page = FakePage(goto_error=RuntimeError("target crashed"), crash_browser=dead_browser)
    dead_context = FakeContext([dead_page])
    factory = FakeBrowserFactory([
        (FakePlaywrightHandle(), dead_browser, dead_context),
        RuntimeError("no browsers left on this box"),
    ])
    reader = _reader(session, browser_factory=factory)
    try:
        reader._fetch_listing("wallstreetbets")
        check("raised after relaunch also failed", False)
    except RedditFetchError as exc:
        check("raised after relaunch also failed", True)
        check("message mentions the relaunch failure", "relaunch" in str(exc).lower(), str(exc))
    check("exactly two launch attempts were made, not more", factory.call_count == 2, factory.call_count)
    check("browser marked unavailable after the failed relaunch", reader._browser_unavailable)


def scenario_reader_healthy_browser_failure_does_not_relaunch() -> None:
    print("\n21. Reader: a live browser that simply fails to fetch (e.g. bad selector) is not treated as a crash")
    session = FakeSession([FakeJsonResponse(403, None)])
    live_browser = FakeBrowser(connected=True)
    # No crash_browser wired in -- goto fails but the browser process itself
    # is still reported as connected, distinguishing "this page load failed"
    # from "the whole browser died".
    page = FakePage(goto_error=RuntimeError("some page-level error"))
    context = FakeContext([page])
    factory = FakeBrowserFactory([(FakePlaywrightHandle(), live_browser, context)])
    reader = _reader(session, browser_factory=factory)
    try:
        reader._fetch_listing("wallstreetbets")
        check("raised on page-level failure", False)
    except RedditFetchError:
        check("raised on page-level failure", True)
    check("did not attempt a relaunch since the browser reported itself alive", factory.call_count == 1, factory.call_count)


def main() -> int:
    for scenario in (
        scenario_json_single_page,
        scenario_json_pagination,
        scenario_json_stops_at_limit_not_cursor,
        scenario_json_http_error,
        scenario_json_malformed_payload,
        scenario_json_request_exception,
        scenario_json_429_with_retry_after,
        scenario_json_429_without_retry_after,
        scenario_browser_happy_path,
        scenario_browser_scrolls_for_more_posts,
        scenario_browser_stalls_on_thin_subreddit,
        scenario_browser_navigation_failure,
        scenario_reader_json_success_never_touches_browser,
        scenario_reader_falls_back_to_browser_on_json_failure,
        scenario_reader_reuses_browser_context_across_calls,
        scenario_reader_429_declines_without_touching_browser,
        scenario_reader_429_cooldown_blocks_subsequent_subreddits,
        scenario_reader_cooldown_expires,
        scenario_reader_recovers_from_crashed_browser,
        scenario_reader_gives_up_when_relaunch_also_fails,
        scenario_reader_healthy_browser_failure_does_not_relaunch,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
