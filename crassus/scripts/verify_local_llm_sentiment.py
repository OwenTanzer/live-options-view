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
