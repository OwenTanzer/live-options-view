"""Per-ticker news from Google News RSS.

One RSS query per ticker (``<SYMBOL> stock``), re-fetched fresh on every
collector run (hourly, and on demand) — there is no fetch-age reuse rule.
Only headlines published within the last 12 hours are kept or shown:
the 12-hour publication window is enforced at fetch time, in storage
(prune stored headlines with :func:`filter_recent`), and at read time
(``build_snapshot`` filters again). A headline older than 12 hours, or
with no parseable publication time, is excluded, never kept. No API key.
Headlines are evidence, not a cause of price moves: the snapshot carries
them with source, link and publication time and never asserts a causal link.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

NEWS_WINDOW_SECONDS = 12 * 3600
MAX_ITEMS_PER_TICKER = 5

RSS_URL = ("https://news.google.com/rss/search?q={query}"
           "&hl=en-US&gl=US&ceid=US:en")


def news_url(symbol):
    """Google News RSS search URL for one ticker."""
    from urllib.parse import quote_plus
    return RSS_URL.format(query=quote_plus(f"{symbol} stock"))


def parse_rss(text):
    """Parse a Google News RSS document into headline dicts.

    Each item: ``title``, ``source`` (the RSS ``<source>`` element, or
    None), ``link`` and ``published_at`` (ISO-8601 UTC, or None when the
    feed gives no parseable date). Malformed XML yields an empty list.
    No age filtering here — that is :func:`filter_recent`'s job, applied
    after parsing so undated or stale items never take one of the
    ``MAX_ITEMS_PER_TICKER`` slots.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        title = _text(item, "title")
        link = _text(item, "link")
        source = _text(item, "source")
        pub = _text(item, "pubDate")
        published_at = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    published_at = dt.astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published_at = None
        if not title:
            continue
        items.append({"title": html.unescape(title),
                      "source": html.unescape(source) if source else None,
                      "link": link or None,
                      "published_at": published_at})
    return items


def _text(item, tag):
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _published_dt(published_at):
    """Parse an ISO-8601 ``published_at`` to an aware UTC datetime, or None."""
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_recent(item, now=None):
    """True when a headline was published within the last 12 hours.

    ``now`` defaults to UTC now; both are compared in UTC. A headline
    with no parseable ``published_at`` is not recent — undated headlines
    are excluded, never kept. A headline exactly 12 hours old is kept;
    anything older than 12 hours is not.
    """
    now = now or datetime.now(timezone.utc)
    published = _published_dt(item.get("published_at"))
    if published is None:
        return False
    return (now - published) <= timedelta(seconds=NEWS_WINDOW_SECONDS)


def filter_recent(items, now=None):
    """Keep only headlines published within the last 12 hours.

    Also the storage-pruning step: pass stored headlines through this
    before re-publishing them and anything past 12 hours is cycled out.
    Undated headlines are dropped. Order is preserved.
    """
    now = now or datetime.now(timezone.utc)
    return [item for item in (items or []) if is_recent(item, now)]


# Back-compat alias: pruning stored headlines is the same operation.
prune_news = filter_recent


def fetch_news(symbol, session, now=None):
    """Fetch one ticker's Google News RSS feed, fresh, last-12-hours only.

    Fetches on every call — the collector runs hourly and on demand, and
    there is no reuse window. Returns at most ``MAX_ITEMS_PER_TICKER``
    headlines, all published within the last 12 hours.
    """
    resp = session.get(news_url(symbol), timeout=20)
    resp.raise_for_status()
    return filter_recent(parse_rss(resp.text), now)[:MAX_ITEMS_PER_TICKER]
