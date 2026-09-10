"""Alpaca real-time news + IEX trade websockets.

Free paper-trading account, no card required:
https://app.alpaca.markets/signup -- then https://app.alpaca.markets/paper/dashboard/overview
for the API key/secret pair (works for market data even without funding).

News: wss://stream.data.alpaca.markets/v1beta1/news -- pushed the moment an
article is published, pre-tagged with `symbols`, so no ticker-extraction
step is needed on our side.

Price: wss://stream.data.alpaca.markets/v2/iex -- real-time trade prints on
IEX only (a few percent of consolidated volume -- enough to detect a move,
not a precise fill-quality feed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

import websockets

from config import settings
from ingest.types import Headline

log = logging.getLogger("ingest.alpaca")


async def _authed_connect(url: str):
    ws = await websockets.connect(url)
    hello = json.loads(await ws.recv())
    log.debug("alpaca connect: %s", hello)
    await ws.send(json.dumps({
        "action": "auth",
        "key": settings.ALPACA_API_KEY,
        "secret": settings.ALPACA_SECRET_KEY,
    }))
    auth_resp = json.loads(await ws.recv())
    log.debug("alpaca auth: %s", auth_resp)
    return ws


def _parse_utc_rfc3339(raw: str | None) -> float | None:
    """Parses an Alpaca RFC3339 UTC timestamp ("...Z" or "+00:00", optional
    fractional seconds) to a Unix epoch. Returns None if `raw` is missing or
    unparseable -- callers decide the fallback, never silently substitute
    the wrong clock."""
    if not raw:
        return None
    try:
        # Parse the naive "YYYY-MM-DDTHH:MM:SS" prefix as explicitly UTC --
        # time.mktime would instead interpret it in the local timezone,
        # skewing the result by the host's UTC offset.
        return (
            datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except Exception:
        return None


def _parse_news_message(msg: dict[str, Any]) -> Headline | None:
    if msg.get("T") != "n":
        return None
    published_raw = msg.get("created_at") or msg.get("updated_at")
    published_at = _parse_utc_rfc3339(published_raw)
    if published_at is None:
        published_at = time.time()
    return Headline(
        source="alpaca",
        external_id=str(msg.get("id")),
        symbols=[s.upper() for s in msg.get("symbols", [])],
        headline=msg.get("headline", ""),
        summary=msg.get("summary", ""),
        url=msg.get("url", ""),
        published_at=published_at,
        ingested_at=time.time(),
    )


async def stream_news(watchlist: tuple[str, ...]) -> AsyncIterator[Headline]:
    """Reconnects with backoff on any disconnect -- a dropped websocket
    should never silently end the news feed for the rest of the process."""
    backoff = 1.0
    while True:
        try:
            ws = await _authed_connect(settings.ALPACA_NEWS_WS_URL)
            await ws.send(json.dumps({"action": "subscribe", "news": list(watchlist)}))
            backoff = 1.0
            async for raw in ws:
                for msg in json.loads(raw):
                    headline = _parse_news_message(msg)
                    if headline is not None:
                        yield headline
        except Exception as exc:
            log.warning("alpaca news stream error, reconnecting in %.0fs: %s", backoff, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def stream_trades(
    watchlist: tuple[str, ...],
    on_trade: Callable[[str, float, float, float, float], None],
) -> None:
    """on_trade(symbol, price, size, ts, received_at) called for every trade
    print. `ts` is the exchange's own trade time (Alpaca's `t` field on each
    IEX trade message), not local receipt time -- every pin/anomaly window
    is keyed off this clock so ingestion or scoring delays elsewhere in the
    process can never shift it. `received_at` is the local wall-clock time
    the print was processed, kept only for latency/backlog visibility
    (MOO-170 finding 1). Runs forever with reconnect-with-backoff, same as
    stream_news."""
    backoff = 1.0
    while True:
        try:
            ws = await _authed_connect(settings.ALPACA_IEX_WS_URL)
            await ws.send(json.dumps({"action": "subscribe", "trades": list(watchlist)}))
            backoff = 1.0
            async for raw in ws:
                for msg in json.loads(raw):
                    if msg.get("T") != "t":
                        continue
                    received_at = time.time()
                    exchange_ts = _parse_utc_rfc3339(msg.get("t")) or received_at
                    on_trade(msg["S"], float(msg["p"]), float(msg.get("s", 0)), exchange_ts, received_at)
        except Exception as exc:
            log.warning("alpaca trade stream error, reconnecting in %.0fs: %s", backoff, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
