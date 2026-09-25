"""Orchestrator: wires ingest -> dedup -> score -> pin/anomaly -> discord.

Run: python main.py
Requires a .env (see .env.example) with at minimum ALPACA_API_KEY/SECRET
and DISCORD_BOT_TOKEN/CHANNEL_ID. Finnhub key is optional (adds a second
news source for cross-confirmation). Everything else runs local/free.

MOO-170 finding 1: ingestion and scoring are two separate coroutines joined
by an asyncio.Queue (`ingest_loop` -> `score_loop`). `ingest_loop` never
awaits the scorer -- it persists `ingested_at` and enqueues immediately, so
a scoring backlog only delays scoring, never delays parsing the next item
off `source_iter`. Per-source order is preserved through the queue; scoring
completion order is not, but that's safe because every pin/anomaly window
is keyed off a fixed timestamp captured at ingest (see pin_engine.py /
anomaly.py docstrings), never off "whatever the latest tick is when scoring
finishes."
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import aiohttp

from config import settings
from correlate.anomaly import run_anomaly_scanner, run_reconciliation_pass
from correlate.pin_engine import PinEngine
from correlate.price_tracker import PriceTracker
from db.storage import Storage
from ingest import alpaca_stream, free_wires
from ingest.dedup import RecentTextWindow
from score.impact_scorer import score_headline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("main")

IMPACT_POST_THRESHOLD = 5.0  # score >= this opens a pin + posts to Discord
SCORE_CONCURRENCY = 3  # bounded concurrent scorer calls -- a backlog queues, it doesn't unbound-fan-out


def _matches_watchlist_ticker(text: str, symbol: str) -> bool:
    """Word-boundary match, not substring -- MOO-170 finding 4. A raw `s in
    text` check matches "ON" inside "MONDAY" or "ALL" inside "BALLPARK".
    Coverage limit: this still can't disambiguate a ticker that is also a
    common English word used as itself (e.g. a headline that legitimately
    reads "ON Semiconductor" for ON) -- it only removes matches where the
    ticker is a substring of an unrelated word."""
    return re.search(rf"\b{re.escape(symbol)}\b", text.upper()) is not None


@dataclass
class _QueuedHeadline:
    headline_id: int
    headline: str
    symbols: list[str]
    ingested_at: float


async def ingest_loop(
    source_iter,
    storage: Storage,
    dedup_window: RecentTextWindow,
    score_queue: "asyncio.Queue[_QueuedHeadline]",
) -> None:
    """Parses, timestamps and durably records every headline off
    `source_iter` before enqueueing it for scoring. Never awaits the
    scorer -- see module docstring."""
    async for headline in source_iter:
        ingested_at = headline.ingested_at or time.time()
        symbols = [s for s in headline.symbols if s in settings.WATCHLIST]
        if not symbols and settings.WATCHLIST:
            # general-category headlines (e.g. Finnhub) aren't pre-tagged;
            # a word-boundary ticker match against the watchlist stands in
            # until a proper ticker-extraction pass is worth adding.
            symbols = [s for s in settings.WATCHLIST if _matches_watchlist_ticker(headline.headline, s)]
        if not symbols:
            continue

        # A duplicate/rehash still gets inserted, linked via
        # is_duplicate_of, so a second source corroborating the same
        # story is recorded for future cross-confirmation instead of
        # being silently dropped before that link can ever be made. It
        # just isn't re-scored or re-pinned as a second independent
        # signal.
        duplicate_of = dedup_window.find_duplicate(headline.headline)

        headline_id = storage.insert_headline(
            source=headline.source, external_id=headline.external_id,
            symbols=symbols, headline=headline.headline, summary=headline.summary,
            url=headline.url, published_at=headline.published_at,
            ingested_at=ingested_at,
            is_duplicate_of=duplicate_of,
        )
        if headline_id is None:
            continue  # already ingested (source, external_id) pair

        dedup_window.add(headline.headline, headline_id)

        if duplicate_of is not None:
            log.debug("recorded duplicate of #%s: %s", duplicate_of, headline.headline[:80])
            continue

        await score_queue.put(_QueuedHeadline(headline_id, headline.headline, symbols, ingested_at))


async def score_loop(
    storage: Storage,
    pin_engine: PinEngine,
    score_queue: "asyncio.Queue[_QueuedHeadline]",
) -> None:
    """Consumes queued headlines with bounded concurrency. A slow/backlogged
    scorer delays only this loop -- ingest_loop keeps parsing and
    timestamping the source regardless."""
    semaphore = asyncio.Semaphore(SCORE_CONCURRENCY)

    async def handle(session: aiohttp.ClientSession, item: _QueuedHeadline) -> None:
        async with semaphore:
            score, reasoning, scorer = await score_headline(session, item.headline, item.symbols)
        storage.set_impact_score(item.headline_id, score, reasoning, scorer)
        latency = time.time() - item.ingested_at
        log.info("scored %.1f/10 [%s] backlog=%d latency=%.1fs: %s",
                  score, scorer, score_queue.qsize(), latency, item.headline[:100])

        if score >= IMPACT_POST_THRESHOLD:
            for symbol in item.symbols:
                asyncio.create_task(pin_engine.open_pin(
                    item.headline_id, symbol, window_start=item.ingested_at,
                ))
        elif score >= settings.SHADOW_SCORE_FLOOR:
            # Below the posting threshold but still worth a non-posting
            # observation window, so the evaluation denominator (finding 6)
            # covers more than just score>=5 headlines.
            for symbol in item.symbols:
                asyncio.create_task(pin_engine.open_pin(
                    item.headline_id, symbol, window_start=item.ingested_at, shadow=True,
                ))

    async with aiohttp.ClientSession() as session:
        while True:
            item = await score_queue.get()
            asyncio.create_task(handle(session, item))


async def poll_and_post_results(storage: Storage, outbox: asyncio.Queue) -> None:
    """Watches SQLite for pins that just resolved and unexplained moves that
    just got logged, and enqueues Discord embeds for the new ones.

    A row is only marked posted once discord_bot.py reports the send
    actually succeeded (see OutboxItem.on_result). `pending` tracks rows
    already enqueued but not yet resolved, so a slow send doesn't get
    re-enqueued by the next 5s poll before its callback fires.
    """
    from bot.discord_bot import OutboxItem, build_pin_embed, build_unexplained_embed

    pending: set[tuple[str, int]] = set()

    def make_on_result(kind: str, row_id: int, mark_posted: Callable[[int], None]) -> Callable[[bool], None]:
        def on_result(success: bool) -> None:
            pending.discard((kind, row_id))
            if success:
                mark_posted(row_id)
            else:
                log.warning("discord delivery failed for %s #%s, will retry", kind, row_id)
        return on_result

    while True:
        await asyncio.sleep(5.0)

        for row in storage.unposted_confirmed_pins():
            key = ("pin", row["id"])
            if key in pending:
                continue
            embed = build_pin_embed(
                symbol=row["symbol"], headline=row["headline"], url=row["url"],
                impact_score=row["impact_score"] or 0.0, reasoning=row["impact_reasoning"] or "",
                pct_move=row["pct_move"] or 0.0, volume_ratio=row["volume_ratio"] or 0.0,
                confirmed=True, classification=row["classification"],
            )
            pending.add(key)
            await outbox.put(OutboxItem(embed, make_on_result("pin", row["id"], storage.mark_pin_posted)))

        for row in storage.unposted_unexplained_moves():
            key = ("unexplained", row["id"])
            if key in pending:
                continue
            embed = build_unexplained_embed(
                symbol=row["symbol"], pct_move=row["pct_move"] or 0.0,
                zscore=row["zscore"] or 0.0, volume_ratio=row["volume_ratio"] or 0.0,
                matched_headline=row["matched_headline"], matched_url=row["matched_url"],
                matched_relation=row["matched_relation"], matched_timing_secs=row["matched_timing_secs"],
            )
            pending.add(key)
            await outbox.put(OutboxItem(embed, make_on_result("unexplained", row["id"], storage.mark_unexplained_posted)))


async def main() -> None:
    if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
        raise SystemExit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (free paper account).")
    if not settings.DISCORD_BOT_TOKEN or not settings.DISCORD_CHANNEL_ID:
        raise SystemExit("Set DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID in .env.")

    storage = Storage(settings.DB_PATH)
    tracker = PriceTracker()
    pin_engine = PinEngine(storage, tracker)
    dedup_window = RecentTextWindow()
    outbox: asyncio.Queue = asyncio.Queue()
    score_queue: "asyncio.Queue[_QueuedHeadline]" = asyncio.Queue()

    from bot.discord_bot import run_bot

    pin_engine.recover_open_pins()

    tasks = [
        asyncio.create_task(alpaca_stream.stream_trades(settings.WATCHLIST, tracker.on_trade)),
        asyncio.create_task(ingest_loop(
            alpaca_stream.stream_news(settings.WATCHLIST), storage, dedup_window, score_queue,
        )),
        asyncio.create_task(score_loop(storage, pin_engine, score_queue)),
        asyncio.create_task(run_anomaly_scanner(storage, tracker, settings.WATCHLIST)),
        asyncio.create_task(run_reconciliation_pass(storage)),
        asyncio.create_task(poll_and_post_results(storage, outbox)),
        asyncio.create_task(run_bot(outbox)),
    ]
    if settings.FINNHUB_API_KEY:
        tasks.append(asyncio.create_task(ingest_loop(
            free_wires.stream_finnhub_news(settings.WATCHLIST), storage, dedup_window, score_queue,
        )))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
