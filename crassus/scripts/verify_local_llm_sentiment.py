#!/usr/bin/env python3
"""Prove local_llm_sentiment.LocalLLMAnalyzer's scoring/fallback behavior,
and that sentiment.py / trump_sentiment.py's _default_analyzer_factory
picks it correctly based on SENTIMENT_ANALYZER_BACKEND without changing
default (VADER) behavior.

Hermetic like the other verify scripts: no real Ollama instance, no
network access. LocalLLMAnalyzer's session is a fake `requests`-shaped
object serving hand-built responses.

    python scripts/verify_local_llm_sentiment.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crassus.sentiment as sentiment_mod  # noqa: E402
import crassus.trump_sentiment as trump_mod  # noqa: E402
from crassus.local_llm_sentiment import LocalLLMAnalyzer  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json_body


class FakeSession:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeSession: no more queued responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ollama_body(compound: float) -> dict:
    return {"response": f'{{"compound": {compound}}}'}


# ---------------------------------------------------------------------------
# LocalLLMAnalyzer.polarity_scores
# ---------------------------------------------------------------------------


def scenario_success_path() -> None:
    print("\n1. polarity_scores: a well-formed Ollama response is parsed")
    session = FakeSession([FakeResponse(200, _ollama_body(0.8))])
    analyzer = LocalLLMAnalyzer(session=session)
    result = analyzer.polarity_scores("Fed unexpectedly cuts rates 50bps")
    check("compound matches the model's score", result["compound"] == 0.8, result)
    check("made exactly one request", len(session.calls) == 1, len(session.calls))


def scenario_clamps_out_of_range_scores() -> None:
    print("\n2. polarity_scores: an out-of-range model score is clamped to [-1, 1]")
    session = FakeSession([FakeResponse(200, _ollama_body(4.2))])
    analyzer = LocalLLMAnalyzer(session=session)
    result = analyzer.polarity_scores("wild overclaim")
    check("clamped to 1.0", result["compound"] == 1.0, result["compound"])


def scenario_falls_back_to_vader_on_http_error() -> None:
    print("\n3. polarity_scores: an HTTP error falls back to VADER, not a raised exception")
    session = FakeSession([FakeResponse(503)])
    analyzer = LocalLLMAnalyzer(session=session)
    try:
        result = analyzer.polarity_scores("Apple beats earnings estimates")
        check("returned a result instead of raising", "compound" in result, result)
        check("compound is a float", isinstance(result["compound"], float), type(result["compound"]))
    except Exception as exc:
        check("returned a result instead of raising", False, f"{type(exc).__name__}: {exc}")


def scenario_falls_back_to_vader_on_malformed_json() -> None:
    print("\n4. polarity_scores: a response with no JSON object falls back to VADER")
    session = FakeSession([FakeResponse(200, {"response": "not json at all"})])
    analyzer = LocalLLMAnalyzer(session=session)
    result = analyzer.polarity_scores("neutral filler text")
    check("still returns a compound score", "compound" in result, result)


def scenario_falls_back_to_vader_on_missing_key() -> None:
    print("\n5. polarity_scores: valid JSON missing the 'compound' key falls back to VADER")
    session = FakeSession([FakeResponse(200, {"response": '{"score": 0.5}'})])
    analyzer = LocalLLMAnalyzer(session=session)
    result = analyzer.polarity_scores("some text")
    check("still returns a compound score", "compound" in result, result)


def scenario_rejects_non_finite_and_boolean_scores() -> None:
    print("\n5b. polarity_scores: NaN/Infinity/bool compound values fall back to VADER instead of clamping to +-1.0")

    # Real VADER score for this exact text, computed once, to assert the
    # fallback path actually ran rather than merely returning "some float".
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    text = "Fed unexpectedly cuts rates 50bps"
    vader_expected = SentimentIntensityAnalyzer().polarity_scores(text)["compound"]

    for label, raw_compound in (
        ("bare JSON NaN", "NaN"),
        ("bare JSON Infinity", "Infinity"),
        ("bare JSON -Infinity", "-Infinity"),
        ("JSON true", "true"),
        ("JSON false", "false"),
        ('string "NaN"', '"NaN"'),
        ('string "Infinity"', '"Infinity"'),
    ):
        session = FakeSession([FakeResponse(200, {"response": f'{{"compound": {raw_compound}}}'})])
        analyzer = LocalLLMAnalyzer(session=session)
        result = analyzer.polarity_scores(text)
        check(
            f"{label}: falls back to VADER instead of clamping to +-1.0/0.0",
            result["compound"] == vader_expected,
            result["compound"],
        )


# ---------------------------------------------------------------------------
# Per-batch time budget and failure circuit (bounding a whole read, not just
# each request)
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable `monotonic`-shaped clock: advances only when told to, so
    a budget/timeout test never actually sleeps in real time."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TimeAdvancingSession(FakeSession):
    """A FakeSession whose `post()` also advances a shared FakeClock by a
    fixed amount, simulating a request that takes real wall-clock time."""

    def __init__(self, responses: list, clock: FakeClock, seconds_per_call: float):
        super().__init__(responses)
        self._clock = clock
        self._seconds_per_call = seconds_per_call

    def post(self, url, json=None, timeout=None):
        self._clock.advance(self._seconds_per_call)
        return super().post(url, json=json, timeout=timeout)


def scenario_batch_budget_exhausted_skips_remaining_requests() -> None:
    print("\n10. Per-batch budget: once exhausted, remaining items in the batch skip Ollama entirely")
    clock = FakeClock()
    # One request "takes" 11s of simulated time against a 10s total budget --
    # the first request still gets a real attempt (budget is only checked at
    # the start of each call), but the second must skip Ollama outright.
    session = TimeAdvancingSession([FakeResponse(200, _ollama_body(0.5))], clock, seconds_per_call=11.0)
    analyzer = LocalLLMAnalyzer(session=session, read_budget_s=10.0, monotonic=clock)
    analyzer.begin_batch()

    first = analyzer.polarity_scores("first post")
    check("the first item still gets a real Ollama attempt", first["compound"] == 0.5, first)
    check("exactly one Ollama request was made so far", len(session.calls) == 1, len(session.calls))

    second = analyzer.polarity_scores("second post")
    check(
        "the second item skips Ollama once the batch budget is spent",
        len(session.calls) == 1,
        len(session.calls),
    )
    check("it still returns a usable (VADER) fallback score, not a crash", "compound" in second, second)


def scenario_request_timeout_capped_to_remaining_budget() -> None:
    print("\n11. Per-batch budget: a request's timeout is capped to what's left of the batch, not the full configured timeout")
    clock = FakeClock()
    session = FakeSession([FakeResponse(200, _ollama_body(0.2))])
    analyzer = LocalLLMAnalyzer(session=session, timeout_s=12.0, read_budget_s=5.0, monotonic=clock)
    analyzer.begin_batch()
    analyzer.polarity_scores("some text")
    used_timeout = session.calls[0]["timeout"]
    check(
        "the request was capped to the remaining ~5s budget, not the full 12s configured timeout",
        used_timeout is not None and 0 < used_timeout <= 5.0,
        used_timeout,
    )


def scenario_failure_circuit_opens_after_consecutive_failures() -> None:
    print("\n12. Failure circuit: N consecutive failures stop further Ollama attempts for the rest of the batch")
    session = FakeSession([FakeResponse(503), FakeResponse(503), FakeResponse(503), FakeResponse(200, _ollama_body(0.9))])
    analyzer = LocalLLMAnalyzer(session=session, failure_circuit_threshold=3)
    analyzer.begin_batch()

    for _ in range(3):
        analyzer.polarity_scores("failing post")
    check("three consecutive failures made three real Ollama attempts", len(session.calls) == 3, len(session.calls))

    result = analyzer.polarity_scores("a fourth post, after the circuit trips")
    check(
        "a fourth call after the circuit trips does not attempt Ollama again",
        len(session.calls) == 3,
        len(session.calls),
    )
    check("it still returns a usable fallback score", "compound" in result, result)


def scenario_begin_batch_resets_circuit_and_budget() -> None:
    print("\n13. begin_batch(): a fresh batch clears a previously-tripped circuit and starts a new budget")
    session = FakeSession([
        FakeResponse(503), FakeResponse(503), FakeResponse(503),  # trips the circuit in batch 1
        FakeResponse(200, _ollama_body(0.9)),  # batch 2's first (and only) attempt
    ])
    analyzer = LocalLLMAnalyzer(session=session, failure_circuit_threshold=3)

    analyzer.begin_batch()
    for _ in range(3):
        analyzer.polarity_scores("x")
    check("circuit is open after three failures in batch 1", analyzer._circuit_open is True)

    analyzer.begin_batch()
    check("begin_batch() clears the circuit for the new batch", analyzer._circuit_open is False)
    result = analyzer.polarity_scores("first post of batch 2")
    check("batch 2 attempts Ollama again rather than staying tripped", len(session.calls) == 4, len(session.calls))
    check("that attempt succeeds", result["compound"] == 0.9, result)


def scenario_reader_begins_batch_on_local_llm_analyzer() -> None:
    print("\n14. Reader wiring: RedditSentimentReader/TrumpSentimentReader call begin_batch() before scoring a read")
    calls: list[str] = []

    class TrackingAnalyzer:
        def begin_batch(self) -> None:
            calls.append("begin_batch")

        def polarity_scores(self, text: str) -> dict[str, float]:
            return {"neg": 0.0, "neu": 0.0, "pos": 0.0, "compound": 0.0}

    reddit_reader = sentiment_mod.RedditSentimentReader(
        symbol="QQQ", subreddits=("wallstreetbets",), post_limit=0,
        analyzer_factory=TrackingAnalyzer,
    )
    reddit_reader.read()
    check("RedditSentimentReader calls begin_batch() exactly once for this read", calls == ["begin_batch"], calls)

    calls.clear()

    class FakeFeedResponse:
        status_code = 200
        content = b"<rss><channel></channel></rss>"

    class FakeFeedSession:
        def get(self, url, timeout=None):
            return FakeFeedResponse()

    trump_reader = trump_mod.TrumpSentimentReader(
        session_factory=FakeFeedSession, analyzer_factory=TrackingAnalyzer,
    )
    trump_reader.read()
    check("TrumpSentimentReader calls begin_batch() exactly once for this read", calls == ["begin_batch"], calls)


def scenario_reader_tolerates_analyzer_without_begin_batch() -> None:
    print("\n15. Reader wiring: a VADER-shaped analyzer with no begin_batch() does not break the read")
    reader = sentiment_mod.RedditSentimentReader(
        symbol="QQQ", subreddits=("wallstreetbets",), post_limit=0,
    )
    snapshot = reader.read()
    check(
        "read() succeeds with the real default (VADER) analyzer, which has no begin_batch",
        snapshot.sample_size == 0,
        snapshot,
    )


class SlowSession(FakeSession):
    """A FakeSession whose `post()` blocks in real wall-clock time before
    returning -- simulating a response trickling in (or a server that
    stalls) past the configured deadline. This is the exact mechanism from
    Owen's PR #72 review: `requests`' `timeout=` bounds inactivity between
    socket reads, not the call's total duration, so a slow/trickling
    response is never caught by it."""

    def __init__(self, responses: list, seconds: float):
        super().__init__(responses)
        self._seconds = seconds

    def post(self, url, json=None, timeout=None):
        time.sleep(self._seconds)
        return super().post(url, json=json, timeout=timeout)


def scenario_real_deadline_bounds_a_trickling_response() -> None:
    print(
        "\n16. _score_via_ollama: a real wall-clock deadline bounds a slow/"
        "trickling response, not just requests' own socket-inactivity timeout"
    )
    # A 0.3s "response" against a 0.05s configured timeout/budget: requests'
    # own timeout kwarg (passed through unchanged below) would not catch
    # this, since FakeSession.post's delay isn't socket inactivity -- only
    # the real-time wait in _call_with_deadline should.
    session = SlowSession([FakeResponse(200, _ollama_body(0.75))], seconds=0.3)
    analyzer = LocalLLMAnalyzer(session=session, timeout_s=0.05, read_budget_s=0.05)
    analyzer.begin_batch()

    start = time.monotonic()
    result = analyzer.polarity_scores("headline that arrives too slowly")
    elapsed = time.monotonic() - start

    check(
        "returns near the ~0.05s deadline rather than waiting out the full 0.3s call",
        elapsed < 0.3,
        f"{elapsed:.3f}s",
    )
    check(
        "falls back to VADER instead of returning the stale 0.75 score that arrives after the deadline",
        result["compound"] != 0.75,
        result,
    )


# ---------------------------------------------------------------------------
# _default_analyzer_factory backend selection (sentiment.py / trump_sentiment.py)
# ---------------------------------------------------------------------------


def scenario_sentiment_defaults_to_vader() -> None:
    print("\n6. sentiment._default_analyzer_factory: SENTIMENT_ANALYZER_BACKEND='vader' (default) picks VADER")
    original = sentiment_mod.SENTIMENT_ANALYZER_BACKEND
    sentiment_mod.SENTIMENT_ANALYZER_BACKEND = "vader"
    try:
        analyzer = sentiment_mod._default_analyzer_factory()
        check(
            "returned a VADER analyzer, not LocalLLMAnalyzer",
            type(analyzer).__name__ == "SentimentIntensityAnalyzer",
            type(analyzer).__name__,
        )
    finally:
        sentiment_mod.SENTIMENT_ANALYZER_BACKEND = original


def scenario_sentiment_opts_into_local_llm() -> None:
    print("\n7. sentiment._default_analyzer_factory: SENTIMENT_ANALYZER_BACKEND='local_llm' picks LocalLLMAnalyzer")
    original = sentiment_mod.SENTIMENT_ANALYZER_BACKEND
    sentiment_mod.SENTIMENT_ANALYZER_BACKEND = "local_llm"
    try:
        analyzer = sentiment_mod._default_analyzer_factory()
        check(
            "returned a LocalLLMAnalyzer",
            type(analyzer).__name__ == "LocalLLMAnalyzer",
            type(analyzer).__name__,
        )
    finally:
        sentiment_mod.SENTIMENT_ANALYZER_BACKEND = original


def scenario_trump_defaults_to_vader() -> None:
    print("\n8. trump_sentiment._default_analyzer_factory: defaults to VADER, same as sentiment.py")
    original = trump_mod.SENTIMENT_ANALYZER_BACKEND
    trump_mod.SENTIMENT_ANALYZER_BACKEND = "vader"
    try:
        analyzer = trump_mod._default_analyzer_factory()
        check(
            "returned a VADER analyzer, not LocalLLMAnalyzer",
            type(analyzer).__name__ == "SentimentIntensityAnalyzer",
            type(analyzer).__name__,
        )
    finally:
        trump_mod.SENTIMENT_ANALYZER_BACKEND = original


def scenario_trump_opts_into_local_llm() -> None:
    print("\n9. trump_sentiment._default_analyzer_factory: 'local_llm' picks LocalLLMAnalyzer")
    original = trump_mod.SENTIMENT_ANALYZER_BACKEND
    trump_mod.SENTIMENT_ANALYZER_BACKEND = "local_llm"
    try:
        analyzer = trump_mod._default_analyzer_factory()
        check(
            "returned a LocalLLMAnalyzer",
            type(analyzer).__name__ == "LocalLLMAnalyzer",
            type(analyzer).__name__,
        )
    finally:
        trump_mod.SENTIMENT_ANALYZER_BACKEND = original


def main() -> int:
    for scenario in (
        scenario_success_path,
        scenario_clamps_out_of_range_scores,
        scenario_falls_back_to_vader_on_http_error,
        scenario_falls_back_to_vader_on_malformed_json,
        scenario_falls_back_to_vader_on_missing_key,
        scenario_rejects_non_finite_and_boolean_scores,
        scenario_batch_budget_exhausted_skips_remaining_requests,
        scenario_request_timeout_capped_to_remaining_budget,
        scenario_failure_circuit_opens_after_consecutive_failures,
        scenario_begin_batch_resets_circuit_and_budget,
        scenario_real_deadline_bounds_a_trickling_response,
        scenario_reader_begins_batch_on_local_llm_analyzer,
        scenario_reader_tolerates_analyzer_without_begin_batch,
        scenario_sentiment_defaults_to_vader,
        scenario_sentiment_opts_into_local_llm,
        scenario_trump_defaults_to_vader,
        scenario_trump_opts_into_local_llm,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
