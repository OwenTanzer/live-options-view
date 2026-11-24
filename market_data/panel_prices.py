"""One-shot price overlay for a local hourly baseline. JSON goes to stdout."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from threading import Lock
import time
from urllib.parse import quote

import requests

from .crypto_collector.coins import COINS
from .crypto_collector.collector import COINBASE_BASE, COINGECKO_BASE
from .nasdaq_collector.collector import ET, NASDAQ_QUOTE_URL, USER_AGENT, _parse_trade_ts

WATCHLIST = "https://api.nasdaq.com/api/quote/watchlist"


def utcnow():
    return datetime.now(timezone.utc)


class Capture:
    """Bound each run and stop issuing requests to a throttled provider."""

    def __init__(self):
        self.lock = Lock()
        self.deadline = time.monotonic() + 32
        self.next_start = {}
        self.calls = Counter()
        self.failures = Counter()
        self.backoff = {}

    def get(self, provider, url, params=None):
        with self.lock:
            slot = max(time.monotonic(), self.next_start.get(provider, 0))
            if provider in self.backoff or slot >= self.deadline:
                return None, None
            self.next_start[provider] = slot + 0.2
        time.sleep(max(0, slot - time.monotonic()))
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if provider in self.backoff or remaining < 0.5:
                return None, None
            self.calls[provider] += 1
        try:
            response = requests.get(url, params=params, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json",
            }, timeout=(min(2, remaining), min(4, remaining)), allow_redirects=False)
            receipt = utcnow()
            if response.status_code in (429, 503):
                with self.lock:
                    self.backoff[provider] = response.headers.get("Retry-After") or "60"
            if response.status_code != 200:
                raise ValueError(f"http_{response.status_code}")
            return response.json(), receipt
        except (requests.RequestException, ValueError) as error:
            label = str(error) if str(error).startswith("http_") else type(error).__name__
            with self.lock:
                self.failures[f"{provider}.{label}"] += 1
            return None, None


def mark(provider, value=None, observed=None, receipt=None, label=None):
    try:
        price = float(str(value).replace("$", "").replace(",", ""))
        if not math.isfinite(price) or price <= 0:
            price = None
    except (ValueError, TypeError):
        price = None
    status = "missing"
    if price is not None:
        status = "unknown_time"
        if observed is not None:
            age = (receipt - observed).total_seconds()
            status = "fresh" if 0 <= age <= 60 else "stale"
        elif label:
            try:
                provider_day = datetime.strptime(label, "%b %d, %Y").date()
                if provider_day < receipt.astimezone(ET).date():
                    status = "stale"
            except ValueError:
                pass
    return dict(source=provider, price_usd=price, freshness=status,
                provider_observed_at=observed.isoformat() if observed else None,
                provider_time_label=label, received_at=receipt.isoformat() if receipt else None)


def crypto_time(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def crypto_marks(baseline, network, pool):
    coins = baseline["coins"]
    ids = {coin.symbol: coin.coingecko_id for coin in COINS}
    ids.update({row["symbol"]: row["coingecko_id"] for row in baseline["universe"].get("added", [])
                if row.get("coingecko_id")})
    marks = {row["symbol"]: mark(row["source"]) for row in coins}
    gecko = {symbol: ids[symbol] for symbol, item in marks.items()
             if item["source"] == "coingecko" and symbol in ids}
    exchange = [symbol for symbol, item in marks.items() if item["source"] == "coinbase"]

    def ticker(symbol):
        url = f"{COINBASE_BASE}/products/{quote(symbol, safe='')}-USD/ticker"
        data, receipt = network.get("coinbase", url)
        if isinstance(data, dict):
            return symbol, mark("coinbase", data.get("price"), crypto_time(data.get("time")), receipt)
        return symbol, mark("coinbase")

    futures = [pool.submit(ticker, symbol) for symbol in exchange]
    if gecko:
        data, receipt = network.get("coingecko", f"{COINGECKO_BASE}/simple/price", {
            "ids": ",".join(gecko.values()), "vs_currencies": "usd", "include_last_updated_at": "true"})
        for symbol, coin_id in gecko.items():
            row = data.get(coin_id) if isinstance(data, dict) else None
            if isinstance(row, dict):
                marks[symbol] = mark("coingecko", row.get("usd"), crypto_time(row.get("last_updated_at")), receipt)
    marks.update(future.result() for future in futures)
    return marks


def nasdaq_marks(baseline, network, pool):
    symbols = [row["symbol"] for row in baseline["stocks"]]
    marks = {symbol: mark("nasdaq_watchlist") for symbol in symbols}

    def batch(group):
        rows, receipt = network.get("nasdaq", WATCHLIST, [("symbol", s + "|stocks") for s in group])
        updates = {}
        for row in (rows.get("data") or []) if isinstance(rows, dict) else []:
            if not isinstance(row, dict) or row.get("symbol") not in group:
                continue
            symbol = row["symbol"]
            label = row.get("lastTradeTimestamp")
            updates[symbol] = mark("nasdaq_watchlist", row.get("lastSalePrice"),
                                   _parse_trade_ts(label), receipt, label)
            updates[symbol]["market_status"] = row.get("marketStatus")
        return updates

    for result in pool.map(batch, [symbols[i:i + 20] for i in range(0, len(symbols), 20)]):
        marks.update(result)
    # Date-only watchlist closes can be from an earlier session than the
    # hourly primary quote. Verify changed or absent undated marks at source.
    def primary(symbol):
        url = NASDAQ_QUOTE_URL.format(symbol=quote(symbol, safe=""))
        payload, receipt = network.get("nasdaq", url, {"assetclass": "stocks"})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) and data.get("symbol") == symbol else {}
        row = data.get("primaryData") or {}
        label = row.get("lastTradeTimestamp")
        result = mark("nasdaq_primary", row.get("lastSalePrice"), _parse_trade_ts(label), receipt, label)
        result["market_status"] = data.get("marketStatus")
        return symbol, result

    needs_primary = [row["symbol"] for row in baseline["stocks"]
                     if marks[row["symbol"]]["freshness"] != "fresh"
                     and (marks[row["symbol"]]["price_usd"] is None
                          or marks[row["symbol"]]["price_usd"] != row.get("price"))]
    if getattr(network, "verify_session_quotes", False):
        # In a regular-session run, a date-only unchanged close still needs a
        # current primary quote. Rotate the supplemental group if the provider
        # is slow enough to exhaust the per-run admission window.
        requested = set(needs_primary)
        remaining = [s for s in symbols if s not in requested and marks[s]["freshness"] != "fresh"]
        if remaining:
            start = int(time.time() // 60) % len(remaining)
            needs_primary.extend(remaining[start:] + remaining[:start])
    marks.update(pool.map(primary, needs_primary))
    return marks


def collect(feed, baseline, network=None):
    """Keep hourly fields and their clocks intact. Marks have independent clocks."""
    network = network or Capture()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        marks = (crypto_marks if feed == "crypto" else nasdaq_marks)(baseline, network, pool)
    return dict(schema_version="panel_prices.v1", feed=feed, enrichment=baseline, prices=marks,
                completed_at=utcnow().isoformat(), elapsed_seconds=round(time.monotonic() - start, 3),
                calls=dict(network.calls), failures=dict(network.failures), retry_after=dict(network.backoff),
                coverage=dict(Counter(item["freshness"] for item in marks.values())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", choices=["crypto", "nasdaq"])
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.feed, json.loads(args.baseline.read_bytes())), allow_nan=False))
