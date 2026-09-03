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
import re
from typing import Any

import requests

from .config import OLLAMA_MODEL, OLLAMA_TIMEOUT_S, OLLAMA_URL

log = logging.getLogger(__name__)

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


class LocalLLMAnalyzer:
    """Same call shape as `vaderSentiment.SentimentIntensityAnalyzer`:
    `.polarity_scores(text) -> {"compound": float, ...}`. Only `compound`
    is populated with a real value -- `sentiment.py`/`trump_sentiment.py`'s
    `aggregate()` never reads the other VADER keys (`pos`/`neg`/`neu`), so
    they're included as 0.0 for shape-compatibility only, not computed.
    """

    def __init__(self, *, url: str = OLLAMA_URL, model: str = OLLAMA_MODEL,
                 timeout_s: float = OLLAMA_TIMEOUT_S, session: Any = None):
        self._url = url
        self._model = model
        self._timeout_s = timeout_s
        self._session = session or requests.Session()
        self._vader_fallback = None

    def _get_vader_fallback(self):
        if self._vader_fallback is None:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._vader_fallback = SentimentIntensityAnalyzer()
        return self._vader_fallback

    def polarity_scores(self, text: str) -> dict[str, float]:
        try:
            compound = self._score_via_ollama(text)
        except Exception as exc:
            log.warning("local LLM scoring failed (%s), falling back to VADER", exc)
            compound = float(self._get_vader_fallback().polarity_scores(text)["compound"])
        return {"neg": 0.0, "neu": 0.0, "pos": 0.0, "compound": compound}

    def _score_via_ollama(self, text: str) -> float:
        response = self._session.post(
            f"{self._url}/api/generate",
            json={
                "model": self._model,
                "system": _SYSTEM_PROMPT,
                "prompt": text,
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        raw = body.get("response", "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"no JSON in ollama response: {raw[:200]!r}")
        parsed = json.loads(match.group(0))
        return max(-1.0, min(1.0, float(parsed["compound"])))
