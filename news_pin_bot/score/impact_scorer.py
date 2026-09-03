"""Financial impact scoring, run entirely locally.

Primary: a local Ollama model (already running on this machine for other
projects) scores expected-volatility 0-10 for a headline given its ticker
context -- this is the piece that beats both FinancialJuice's fixed keyword
list (misses novel events) and crassus's VADER usage (generic sentiment,
not magnitude-of-impact). No API cost, no external call at all.

Fallback: VADER (stdlib-adjacent, offline, instant) if Ollama is
unreachable or too slow -- so a slow/cold local model never blocks the
pipeline. VADER's compound score is remapped onto the same 0-10 scale by
magnitude, not treated as a sentiment gauge.
"""
from __future__ import annotations

import json
import logging
import re

import aiohttp

from config import settings

log = logging.getLogger("score.impact_scorer")

_SYSTEM_PROMPT = (
    "You are a financial-markets impact scorer. Given a news headline and "
    "the tickers it's tagged to, output ONLY a JSON object like "
    '{"score": <0-10 float>, "reasoning": "<one sentence>"}. '
    "Score is expected short-term (next few minutes to hours) volatility "
    "impact on the named ticker(s): 0 = irrelevant noise, 5 = a real but "
    "routine market-moving item (an inline earnings beat, a minor guidance "
    "update), 10 = an extreme, immediate, high-magnitude shock (surprise "
    "rate decision, M&A announcement, guidance withdrawal, executive exit, "
    "major regulatory action). Judge magnitude and surprise, not whether "
    "the news is 'good' or 'bad'."
)

_vader_analyzer = None


def _get_vader():
    global _vader_analyzer
    if _vader_analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _vader_analyzer = SentimentIntensityAnalyzer()
    return _vader_analyzer


def _vader_fallback_score(headline: str) -> tuple[float, str]:
    compound = abs(_get_vader().polarity_scores(headline)["compound"])
    return compound * 10.0, "VADER magnitude fallback (Ollama unavailable)"


async def score_headline(session: aiohttp.ClientSession, headline: str, symbols: list[str]) -> tuple[float, str, str]:
    """Returns (score 0-10, reasoning, scorer_name)."""
    ticker_ctx = ", ".join(symbols) if symbols else "unspecified"
    prompt = f"Tickers: {ticker_ctx}\nHeadline: {headline}"
    try:
        async with session.post(
            f"{settings.OLLAMA_URL}/api/generate",
            json={
                "model": settings.OLLAMA_MODEL,
                "system": _SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=settings.OLLAMA_TIMEOUT_S,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ollama HTTP {resp.status}")
            body = await resp.json()
            text = body.get("response", "")
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError(f"no JSON in ollama response: {text[:200]}")
            parsed = json.loads(match.group(0))
            score = max(0.0, min(10.0, float(parsed["score"])))
            return score, str(parsed.get("reasoning", "")), settings.OLLAMA_MODEL
    except Exception as exc:
        log.warning("ollama scoring failed (%s), falling back to VADER", exc)
        score, reasoning = _vader_fallback_score(headline)
        return score, reasoning, "vader-fallback"
