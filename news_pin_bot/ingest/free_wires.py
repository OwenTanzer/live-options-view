"""Supplementary free, unauthenticated (or free-key) news sources.

These fill gaps Alpaca's Benzinga-sourced feed can miss and add
cross-confirmation: a headline hitting two independent sources within a
short window is higher confidence than one source alone.

- Finnhub market news: free-key REST, polled well under the 60/min limit.

SEC EDGAR full-text search RSS is a natural next source (official filings,
no key, no ToS friction) but isn't wired up yet -- add a poller here
following the same shape as poll_finnhub_news when needed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

import aiohttp

from config import settings
from ingest.types import Headline

log = logging.getLogger("ingest.free_wires")


async def poll_finnhub_news(session: aiohttp.ClientSession, last_id: int) -> tuple[list[Headline], int]:
    """Returns (new headlines, new last_id). Uses minId pagination so we
    never re-fetch or duplicate-process the same article twice."""
    if not settings.FINNHUB_API_KEY:
        return [], last_id
    params = {"category": "general", "token": settings.FINNHUB_API_KEY}
    if last_id:
        params["minId"] = str(last_id)
    async with session.get("https://finnhub.io/api/v1/news", params=params, timeout=10) as resp:
        if resp.status != 200:
            log.warning("finnhub news poll failed: HTTP %s", resp.status)
            return [], last_id
        items = await resp.json()

    headlines: list[Headline] = []
    max_id = last_id
    for item in items:
        max_id = max(max_id, item.get("id", 0))
        headlines.append(Headline(
            source="finnhub",
            external_id=str(item.get("id")),
            symbols=[],  # general category isn't ticker-tagged; scorer infers relevance
            headline=item.get("headline", ""),
            summary=item.get("summary", ""),
            url=item.get("url", ""),
            published_at=float(item.get("datetime", time.time())),
            ingested_at=time.time(),
        ))
    return headlines, max_id


async def stream_finnhub_news(watchlist: tuple[str, ...]) -> AsyncIterator[Headline]:
    """Polling loop, well under Finnhub's free 60 calls/min limit."""
    last_id = 0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                headlines, last_id = await poll_finnhub_news(session, last_id)
                for h in headlines:
                    yield h
            except Exception as exc:
                log.warning("finnhub poll error: %s", exc)
            await asyncio.sleep(settings.FINNHUB_NEWS_POLL_SECS)
