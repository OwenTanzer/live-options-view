"""A drop-in replacement analyzer for `sentiment.py` / `trump_sentiment.py`'s
VADER usage, scoring magnitude-of-market-impact via a local Ollama model
instead of generic lexicon sentiment.

Why this exists: VADER's `compound` score is a general-purpose positive/
negative lexicon score -- it has no notion of financial magnitude or
surprise. "Fed unexpectedly cuts rates 50bps" and "stocks had a fine day"
can score similarly under VADER despite one being a much bigger deal for
QQQ than the other. This asks a local model to judge magnitude/surprise
directly, in the same signed [-1, 1] shape VADER's `compound` already
produces, so it's swappable without touching `aggregate()` in either
caller -- both only ever call `analyzer.polarity_scores(text)["compound"]`.

Deliberately not the default analyzer yet (see `config.SENTIMENT_ANALYZER_BACKEND`):
this scores the strategies that place real trades
(`trump_whisperer_qqq`, `reddit_sentiment_qqq`), so switching the default
away from the VADER behavior already running in production is a decision
for whoever reviews this PR, not something this change makes unilaterally.
Every account keeps reading VADER unless SENTIMENT_ANALYZER_BACKEND is
explicitly set to "local_llm" for it.

Requires a local Ollama instance already running (`ollama serve`) with
`OLLAMA_MODEL` pulled. Falls back to VADER on any failure (timeout,
unreachable, malformed response) so a cold/unavailable local model can
never take a strategy offline -- it just silently reads as VADER for that
cycle, same fail-open behavior `impact_scorer.py` in `news_pin_bot/` uses
for the same reason.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Callable

import requests

from .config import OLLAMA_MODEL, OLLAMA_TIMEOUT_S, OLLAMA_URL

log = logging.getLogger(__name__)

# Total wall-clock budget for one batch of `polarity_scores` calls (one
# reader `read()` cycle -- see `begin_batch()`). Flagged in review: without
# this, a hung/slow Ollama instance lets every post in a large batch pay a
# full `OLLAMA_TIMEOUT_S` before falling back -- a 100-post fixture could
# spend 100 * 12s = 1,200s, well past the runner's 300s cadence and 600s
# watchdog. Once the budget is spent, remaining items in the same batch skip
# the network call entirely and score via VADER immediately.
DEFAULT_READ_BUDGET_S = 45.0

# Consecutive-failure circuit breaker within one batch: once this many
# requests in a row fail (timeout, HTTP error, malformed response, invalid
# score), stop attempting Ollama for the rest of the batch -- a instance
# that is up but consistently erroring should not get one full-timeout
# attempt per remaining item, only a short run of them.
DEFAULT_FAILURE_CIRCUIT_THRESHOLD = 3

_SYSTEM_PROMPT = (
    "You are a financial-markets sentiment scorer for QQQ / the Nasdaq-100. "
    "Given one short text (a social post or news headline), output ONLY a "
    'JSON object like {"compound": <float from -1.0 to 1.0>}. '
    "The sign is direction (negative = bearish for QQQ, positive = bullish), "
    "the magnitude is expected impact: near 0 for routine/irrelevant text, "
    "near +/-1 for a surprising, high-magnitude market-moving statement "
    "(a surprise rate move, a major macro data surprise, an extreme policy "
    "statement). Judge magnitude and surprise, not just tone."
)


def _validate_compound(value: Any) -> float:
    """Reject anything that isn't a genuinely finite number before it ever
    reaches `float()`/clamping.

    Flagged in review: `max(-1.0, min(1.0, float(parsed["compound"])))`
    alone accepts far more than a well-formed score. `float()` silently
    turns the strings `"NaN"`, `"Infinity"`, `"-Infinity"` into non-finite
    floats that then clamp to +-1.0 as if they were confident extreme
    scores; a bare JSON `NaN`/`Infinity` (which `json.loads` parses by
    default, even though strict JSON forbids them) does the same; and
    `bool` is a subtype of `int` in Python, so a stray JSON `true`/`false`
    silently becomes `1.0`/`0.0` instead of being treated as garbage output.
    None of those are a real model score and all of them must fall back to
    VADER, not clamp to a confident-looking number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"compound is not a number: {value!r}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"compound is not finite: {value!r}")
    return max(-1.0, min(1.0, numeric))


class LocalLLMAnalyzer:
    """Same call shape as `vaderSentiment.SentimentIntensityAnalyzer`:
    `.polarity_scores(text) -> {"compound": float, ...}`. Only `compound`
    is populated with a real value -- `sentiment.py`/`trump_sentiment.py`'s
    `aggregate()` never reads the other VADER keys (`pos`/`neg`/`neu`), so
    they're included as 0.0 for shape-compatibility only, not computed.

    Bounded per batch, not just per request: a caller that scores many
    texts in one pass (one reader `read()` cycle) should call `begin_batch()`
    once before the first `polarity_scores()` call in that pass. This resets
    a wall-clock budget (`read_budget_s`) and a consecutive-failure counter
    (`failure_circuit_threshold`) shared across every call until the next
    `begin_batch()`. Once the budget is spent or the circuit has tripped,
    `polarity_scores()` skips the network call entirely for the rest of the
    batch and scores via VADER immediately -- without `begin_batch()` ever
    being called (a caller using this analyzer directly, one text at a
    time) every call gets its own full budget/circuit, same as before this
    existed. `monotonic` is injectable so tests can drive the clock instead
    of sleeping in real time.
    """

    def __init__(self, *, url: str = OLLAMA_URL, model: str = OLLAMA_MODEL,
                 timeout_s: float = OLLAMA_TIMEOUT_S, session: Any = None,
                 read_budget_s: float = DEFAULT_READ_BUDGET_S,
                 failure_circuit_threshold: int = DEFAULT_FAILURE_CIRCUIT_THRESHOLD,
                 monotonic: Callable[[], float] = time.monotonic):
        self._url = url
        self._model = model
        self._timeout_s = timeout_s
        self._session = session or requests.Session()
        self._vader_fallback = None
        self._read_budget_s = read_budget_s
        self._failure_circuit_threshold = failure_circuit_threshold
        self._monotonic = monotonic
        self._read_deadline: float | None = None
        self._consecutive_failures = 0
        self._circuit_open = False

    def begin_batch(self) -> None:
        """Reset the per-batch time budget and failure circuit. Call once
        before scoring a batch of texts drawn from the same reader cycle."""
        self._read_deadline = self._monotonic() + self._read_budget_s
        self._consecutive_failures = 0
        self._circuit_open = False

    def _get_vader_fallback(self):
        if self._vader_fallback is None:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._vader_fallback = SentimentIntensityAnalyzer()
        return self._vader_fallback

    def _vader_score(self, text: str) -> float:
        return float(self._get_vader_fallback().polarity_scores(text)["compound"])

    def _remaining_budget_s(self) -> float | None:
        if self._read_deadline is None:
            return None
        return self._read_deadline - self._monotonic()

    def polarity_scores(self, text: str) -> dict[str, float]:
        remaining = self._remaining_budget_s()
        if self._circuit_open:
            log.warning("local LLM failure circuit open for this batch, scoring via VADER")
            compound = self._vader_score(text)
        elif remaining is not None and remaining <= 0:
            log.warning("local LLM per-batch time budget exhausted, scoring via VADER")
            compound = self._vader_score(text)
        else:
            # Cap this request to whatever's left of the batch budget, not
            # the full configured timeout -- a single slow request must not
            # be able to overrun the budget the rest of the batch depends on.
            request_timeout = self._timeout_s if remaining is None else max(0.0, min(self._timeout_s, remaining))
            try:
                if request_timeout <= 0:
                    raise TimeoutError("per-batch scoring budget exhausted")
                compound = self._score_via_ollama(text, timeout_s=request_timeout)
                self._consecutive_failures = 0
            except Exception as exc:
                log.warning("local LLM scoring failed (%s), falling back to VADER", exc)
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_circuit_threshold:
                    self._circuit_open = True
                compound = self._vader_score(text)
        return {"neg": 0.0, "neu": 0.0, "pos": 0.0, "compound": compound}

    def _score_via_ollama(self, text: str, *, timeout_s: float) -> float:
        response = self._session.post(
            f"{self._url}/api/generate",
            json={
                "model": self._model,
                "system": _SYSTEM_PROMPT,
                "prompt": text,
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        raw = body.get("response", "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"no JSON in ollama response: {raw[:200]!r}")
        parsed = json.loads(match.group(0))
        if "compound" not in parsed:
            raise ValueError(f"ollama response has no 'compound' key: {parsed!r}")
        return _validate_compound(parsed["compound"])
