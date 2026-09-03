"""Novelty/dedup filtering, same pattern as crassus's trump_sentiment.py.

A wire re-reporting the same story, or a near-identical restatement, should
not count as a second independent signal for scoring/pinning purposes -- it's
the same news priced in once, not twice. But a duplicate from a *different*
source than the original is still useful cross-confirmation evidence, so
`find_duplicate` returns the id of the headline it matches (instead of just
True/False) so the caller can still record the duplicate row, linked via
`is_duplicate_of`, rather than silently discarding it before that link can
ever be made.

Uses a fuzzy similarity ratio (stdlib difflib) rather than exact match,
since restatements are rarely character-for-character identical.
"""
from __future__ import annotations

import difflib

DUPLICATE_SIMILARITY_THRESHOLD = 0.85


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


class RecentTextWindow:
    """Rolling window of recently-seen headline text -> headline id, used to
    flag a new headline as a duplicate/rehash of something already ingested
    in the last `max_items`. Kept in-memory and small -- this is a cheap
    per-cycle check, not a persistent index."""

    def __init__(self, max_items: int = 200):
        self._items: list[tuple[str, int]] = []
        self._max_items = max_items

    def find_duplicate(self, text: str) -> int | None:
        """Returns the headline id of the closest prior match, or None if
        `text` is novel relative to everything currently in the window."""
        normalized = normalize(text)
        if not normalized:
            return None
        for other_text, other_id in self._items:
            if normalized == other_text:
                return other_id
            if difflib.SequenceMatcher(None, normalized, other_text).ratio() >= DUPLICATE_SIMILARITY_THRESHOLD:
                return other_id
        return None

    def is_duplicate(self, text: str) -> bool:
        return self.find_duplicate(text) is not None

    def add(self, text: str, headline_id: int) -> None:
        normalized = normalize(text)
        if not normalized:
            return
        self._items.append((normalized, headline_id))
        if len(self._items) > self._max_items:
            self._items.pop(0)
