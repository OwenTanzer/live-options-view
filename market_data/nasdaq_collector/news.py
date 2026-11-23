"""Per-ticker daily news from Google News RSS.

One RSS query per ticker (``<SYMBOL> stock``), re-fetched only when the
stored headlines are more than 12 hours old, as in the tracker. No API key.
Headlines are evidence, not a cause of price moves: the snapshot carries
them with source, link and publication time and never asserts a causal link.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

NEWS_REFRESH_SECONDS = 12 * 3600
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
    return items[:MAX_ITEMS_PER_TICKER]


def _text(item, tag):
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text else None


def should_refresh(last_fetched_at, now=None):
    """True when stored headlines are older than 12h, or never fetched.

    ``last_fetched_at`` is an ISO-8601 timestamp or None. ``now`` defaults
    to UTC now; both are compared in UTC.
    """
    if not last_fetched_at:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(last_fetched_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) > timedelta(seconds=NEWS_REFRESH_SECONDS)


def fetch_news(symbol, session):
    """Fetch and parse one ticker's Google News RSS feed."""
    resp = session.get(news_url(symbol), timeout=20)
    resp.raise_for_status()
    return parse_rss(resp.text)
