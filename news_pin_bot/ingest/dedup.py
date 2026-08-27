"""Novelty/dedup filtering, same pattern as crassus's trump_sentiment.py.

A wire re-reporting the same story, or a near-identical restatement, should
not count as a second independent signal -- it's the same news priced in
once, not twice. Uses a fuzzy similarity ratio (stdlib difflib) rather than
exact match, since restatements are rarely character-for-character
identical.
"""
from __future__ import annotations

import difflib

DUPLICATE_SIMILARITY_THRESHOLD = 0.85


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


class RecentTextWindow:
    """Rolling window of recently-seen headline text, used to flag a new
    headline as a duplicate/rehash of something already ingested in the
    last `max_items`. Kept in-memory and small -- this is a cheap
    per-cycle check, not a persistent index."""

    def __init__(self, max_items: int = 200):
        self._items: list[str] = []
        self._max_items = max_items

    def is_duplicate(self, text: str) -> bool:
        normalized = normalize(text)
        if not normalized:
            return False
        for other in self._items:
            if normalized == other:
                return True
            if difflib.SequenceMatcher(None, normalized, other).ratio() >= DUPLICATE_SIMILARITY_THRESHOLD:
                return True
        return False

    def add(self, text: str) -> None:
        normalized = normalize(text)
        if not normalized:
            return
        self._items.append(normalized)
        if len(self._items) > self._max_items:
            self._items.pop(0)
