"""Hourly crypto snapshot collector (read-only, no auth, no trading).

Fetches public prices and publishes ``crypto/latest.json`` to the R2
bucket ``qqq-options-chain-data`` (see :mod:`publish`):

- Coinbase Exchange (public, no key) for the 30 ``coinbase`` coins:
  ``/products/<SYMBOL>-USD/ticker`` for price and last-trade time,
  ``/products/<SYMBOL>-USD/stats`` for 24h open/high/low/volume.
  24h change is ``(last - open) / open`` from the stats response.
- CoinGecko ``/coins/markets`` (public, no key) for the 9
  ``coingecko`` coins -- price, 24h high/low/change, volume, market
  cap, rank, 1h/7d change -- and for rank/market-cap/1h/7d metadata
  on the Coinbase coins. CoinGecko is a *metadata* source for
  Coinbase coins; it never supplies their price.
- CoinGecko ``/global`` for total market cap and BTC dominance
  (best-effort; null with an explicit reason when unavailable).
- alternative.me Fear & Greed (``/fng/?limit=1``), best-effort with
  an explicit ``unavailable`` state when the feed cannot be read.
  No value is fabricated.

Session model: crypto trades 24/7, so this collector has no equity
session close. It runs on a continuous hourly cycle; ``session_model``
is ``continuous_24_7`` in every artifact. Freshness is judged
against the provider's own observation time, never against an
equity session clock: a row is ``fresh`` while its provider
observation is under ``STALE_AFTER_SECONDS`` old, ``stale`` after
that, and ``unavailable`` when its source could not be read at all.

Membership is dynamic, mirroring the Coinbase Price Tracker: the
universe is the live top 50 by market cap (see
:func:`resolve_universe`), not the embedded 39-coin list — the
embedded list is the fallback and the diff baseline. A failed,
empty or partial CoinGecko top-markets fetch falls back to the
embedded 39 and records that fallback in the snapshot's
``universe`` block; a bad fetch never shrinks or empties the
snapshot. Membership changes (added/removed, with symbol, rank,
source and date) are recorded in the same block, so the panel can
show them.

A missing source is never papered over: a Coinbase coin whose
product is absent (404) or whose ticker fails publishes a row with
``price: null``, ``status: "source_unavailable"`` and a reason -- it
is never priced from CoinGecko. The reverse holds for CoinGecko
coins. A failed run publishes nothing; the previous ``latest.json``
stays in place and goes stale on its own as-of time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import requests

from .coins import (COINGECKO_COINS, COINBASE_COINS, COINS,
                    coin_from_market_row, diff_universe, parse_top_markets)
from .patterns import dedupe_patterns, detect_patterns

SCHEMA_VERSION = "crypto_snapshot.v1"
ARTIFACT_KEY = "crypto/latest.json"
BUCKET = "qqq-options-chain-data"

COINBASE_BASE = "https://api.exchange.coinbase.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

# Hourly publication; a provider observation older than this is stale.
STALE_AFTER_SECONDS = 2 * 3600
REQUEST_TIMEOUT_SECONDS = 15


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    """Parse an ISO-8601 timestamp (or epoch seconds) to aware datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def freshness_status(observed_at, *, now, stale_after_seconds=STALE_AFTER_SECONDS):
    """Freshness of one provider observation: fresh / stale / unavailable."""
    observed = parse_time(observed_at)
    if observed is None:
        return "unavailable"
    age = (now - observed).total_seconds()
    if age < 0 or age > stale_after_seconds:
        return "stale"
    return "fresh"


def _get(session, url, *, params=None):
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def fetch_coinbase(session, coins=COINBASE_COINS):
    """Fetch ticker + 24h stats per Coinbase coin.

    Returns ``{symbol: {"ticker": ..., "stats": ...} | {"error": str}}``.
    A 404 (no such USD product) is recorded per coin, never raised:
    one missing product must not fail the other 38 coins.
    """
    out = {}
    for coin in coins:
        product = coin.coinbase_product
        entry = {}
        for kind, path in (("ticker", f"/products/{product}/ticker"),
                           ("stats", f"/products/{product}/stats")):
            try:
                entry[kind] = _get(session, COINBASE_BASE + path)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                entry[kind] = {"error": f"HTTP {status}" if status else str(exc)}
            except requests.RequestException as exc:
                entry[kind] = {"error": str(exc)}
        out[coin.symbol] = entry
    return out


def fetch_coingecko_top_markets(session):
    """Fetch the CoinGecko top-100 markets list (no id filter).

    Returns the raw row list, ordered by market cap. Raises on a
    transport/HTTP error or a non-list payload; the caller falls
    back to the embedded universe rather than publishing a bad one.
    This is the same ``/coins/markets`` endpoint as
    :func:`fetch_coingecko_markets`, without the ``ids`` filter, so
    the universe — not just the already-known coins — is re-read.
    """
    rows = _get(session, COINGECKO_BASE + "/coins/markets", params={
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "price_change_percentage": "1h,24h,7d",
    })
    if not isinstance(rows, list):
        raise ValueError("unexpected CoinGecko top-markets payload")
    return rows


def probe_coinbase(session, symbol):
    """Does Coinbase Exchange list a USD pair for ``symbol``?

    One ticker read: HTTP 200 means the pair answers. A 404/400
    means no such product. Other failures propagate so an outage is
    not mistaken for a verified provider assignment. Only
    entrants are probed (see :func:`resolve_universe`): embedded
    coins keep their verified source label even if a probe would
    now answer differently (USDT keeps its CoinGecko label, as the
    snapshot contract requires).
    """
    try:
        _get(session, COINBASE_BASE + f"/products/{symbol}-USD/ticker")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (400, 404):
            return False
        raise
    return True


def resolve_universe(session, now=None):
    """Resolve the universe for this run: live top 50 vs embedded fallback.

    Returns a dict with ``coins`` (the :class:`coins.Coin` tuple to
    publish, in live rank order), ``added`` / ``removed`` (the diff
    against the embedded list, each a list of change records with
    symbol, name, CoinGecko id, rank and source), ``source``
    (``"live"`` or ``"embedded_fallback"``) and ``error`` (the fetch
    failure, or None). A failed, empty or partial top-markets fetch
    returns the embedded 39 with no recorded changes — a bad fetch
    never shrinks or empties the snapshot, and the fallback is
    visible in the snapshot's ``universe`` block.
    """
    now = now or utcnow()
    as_of = now.date().isoformat()
    try:
        entries = parse_top_markets(fetch_coingecko_top_markets(session))
    except Exception as exc:  # noqa: BLE001 - any list failure falls back
        return {"coins": COINS, "added": [], "removed": [],
                "source": "embedded_fallback", "error": str(exc),
                "as_of": as_of}
    by_id = {coin.coingecko_id: coin for coin in COINS}
    live = []
    for entry in entries:
        embedded = by_id.get(entry["coingecko_id"])
        if embedded is not None:
            # Keep the embedded coin (symbol, name, verified source)
            # with its live rank.
            live.append(type(embedded)(
                embedded.symbol, embedded.name, embedded.coingecko_id,
                embedded.source, embedded.coinbase_product, entry["rank"]))
        else:
            try:
                listed = probe_coinbase(session, entry["symbol"])
            except requests.RequestException as exc:
                return {"coins": COINS, "added": [], "removed": [],
                        "source": "embedded_fallback",
                        "error": f"Coinbase probe for {entry['symbol']} unavailable: {exc}",
                        "as_of": as_of}
            source = "coinbase" if listed else "coingecko"
            live.append(coin_from_market_row(entry, source))
    live = tuple(live)
    changes = diff_universe(COINS, live)
    return {"coins": live, "added": changes["added"],
            "removed": changes["removed"], "source": "live",
            "error": None, "as_of": as_of}


def fetch_coingecko_markets(session, coins=COINS):
    """Fetch CoinGecko markets for the whole universe in one call.

    Returns ``{coingecko_id: row}``; on a failed call returns
    ``{"_error": str}`` so the caller can mark every CoinGecko-
    sourced row unavailable instead of raising.
    """
    ids = ",".join(c.coingecko_id for c in coins)
    try:
        rows = _get(session, COINGECKO_BASE + "/coins/markets", params={
            "vs_currency": "usd",
            "ids": ids,
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "price_change_percentage": "1h,24h,7d",
        })
    except requests.RequestException as exc:
        return {"_error": str(exc)}
    if not isinstance(rows, list):
        return {"_error": "unexpected CoinGecko markets payload"}
    return {row["id"]: row for row in rows if isinstance(row, dict) and "id" in row}


def fetch_coingecko_global(session):
    """Total market cap / BTC dominance (best-effort)."""
    try:
        payload = _get(session, COINGECKO_BASE + "/global")
    except requests.RequestException as exc:
        return {"error": str(exc)}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"error": "unexpected CoinGecko global payload"}
    return data


def fetch_fear_greed(session):
    """Fear & Greed value with its own as-of time (best-effort).

    Returns ``{"value", "classification", "as_of", "status"}`` where
    ``status`` is ``"ok"`` or ``"unavailable"``. Never fabricates a
    value: on any failure the value is null.
    """
    try:
        payload = _get(session, FEAR_GREED_URL)
        entry = payload["data"][0]
        value = int(entry["value"])
        observed = parse_time(int(entry["timestamp"]))
        return {
            "value": value,
            "classification": entry.get("value_classification"),
            "as_of": iso(observed),
            "status": "ok",
            "source": "alternative.me",
        }
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        return {
            "value": None,
            "classification": None,
            "as_of": None,
            "status": "unavailable",
            "reason": str(exc),
            "source": "alternative.me",
        }


def _coinbase_row(coin, entry, cg_row, *, now):
    ticker = entry.get("ticker") or {}
    stats = entry.get("stats") or {}
    ticker_error = ticker.get("error")
    if ticker_error:
        return _unavailable_row(coin, now, reason=f"coinbase ticker: {ticker_error}")
    try:
        price = float(ticker["price"])
        observed = parse_time(ticker.get("time"))
    except (KeyError, TypeError, ValueError):
        return _unavailable_row(coin, now, reason="coinbase ticker: missing price/time")
    stats_error = stats.get("error")
    gaps = []
    high = low = volume = change_24h = None
    if stats_error:
        gaps.append(f"coinbase stats unavailable: {stats_error}")
    else:
        try:
            high = float(stats["high"])
            low = float(stats["low"])
            # Coinbase stats volume is in the base asset (e.g. BTC),
            # not USD. Convert at the ticker price so volume_24h is
            # USD for every row, matching CoinGecko's total_volume.
            volume = float(stats["volume"]) * price
            open_24h = float(stats["open"])
            change_24h = (price - open_24h) / open_24h * 100 if open_24h else None
        except (KeyError, TypeError, ValueError):
            gaps.append("coinbase stats: missing fields")
    row = _base_row(coin, cg_row, now, price=price, observed=observed,
                    high_24h=high, low_24h=low, volume_24h=volume,
                    change_24h_pct=change_24h, price_basis="coinbase")
    if gaps:
        row["status"] = "partial"
        row["status_reason"] = "; ".join(gaps)
        row["gaps"] = gaps
    return row


def _coingecko_row(coin, cg_row, *, now):
    if cg_row is None:
        return _unavailable_row(coin, now, reason="coingecko markets: coin missing")
    try:
        price = float(cg_row["current_price"])
        observed = parse_time(cg_row.get("last_updated"))
    except (KeyError, TypeError, ValueError):
        return _unavailable_row(coin, now, reason="coingecko markets: missing price/time")
    return _base_row(
        coin, cg_row, now, price=price, observed=observed,
        high_24h=_float(cg_row.get("high_24h")),
        low_24h=_float(cg_row.get("low_24h")),
        volume_24h=_float(cg_row.get("total_volume")),
        change_24h_pct=_float(cg_row.get("price_change_percentage_24h")),
        price_basis="coingecko")


def _float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _base_row(coin, cg_row, now, *, price, observed, high_24h, low_24h,
              volume_24h, change_24h_pct, price_basis):
    """Shared row shape; rank/market-cap/1h/7d always come from CoinGecko.

    Those four fields are *metadata* (``metadata_source``), distinct
    from the price source: the price itself comes only from
    ``price_basis``. When CoinGecko metadata is missing the row keeps
    its price and reports the gap instead of inventing a rank.
    """
    cg_row = cg_row or {}
    observed_iso = iso(observed)
    return {
        "rank": cg_row.get("market_cap_rank") or coin.rank,
        "symbol": coin.symbol,
        "name": coin.name,
        "source": coin.source,
        "price_basis": price_basis,
        "metadata_source": "coingecko" if cg_row else None,
        "currency": "USD",
        "price": price,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "change_24h_pct": change_24h_pct,
        "change_1h_pct": _float(cg_row.get("price_change_percentage_1h_in_currency")),
        "change_7d_pct": _float(cg_row.get("price_change_percentage_7d_in_currency")),
        "volume_24h": volume_24h,
        "volume_24h_unit": "USD",
        "market_cap": _float(cg_row.get("market_cap")),
        "provider_observed_at": observed_iso,
        "as_of": observed_iso,
        "freshness": freshness_status(observed_iso, now=now),
        "status": "ok",
        "status_reason": None,
        "gaps": [],
    }


def _unavailable_row(coin, now, *, reason):
    return {
        "rank": coin.rank,
        "symbol": coin.symbol,
        "name": coin.name,
        "source": coin.source,
        # price_basis stays the coin's own source even when the price
        # is missing: a null with the right basis is honest, a price
        # from the other source would be a substitution.
        "price_basis": coin.source,
        "metadata_source": None,
        "currency": "USD",
        "price": None,
        "high_24h": None,
        "low_24h": None,
        "change_24h_pct": None,
        "change_1h_pct": None,
        "change_7d_pct": None,
        "volume_24h": None,
        "volume_24h_unit": "USD",
        "market_cap": None,
        "provider_observed_at": None,
        "as_of": None,
        "freshness": "unavailable",
        "status": "source_unavailable",
        "status_reason": reason,
        "gaps": [reason],
    }


def build_snapshot(*, coinbase, coingecko_markets, coingecko_global,
                   fear_greed, previous=None, now=None, universe=None):
    """Assemble ``crypto_snapshot.v1`` from fetched provider payloads.

    ``coinbase``: ``fetch_coinbase`` output. ``coingecko_markets``:
    ``fetch_coingecko_markets`` output. ``coingecko_global``:
    ``fetch_coingecko_global`` output. ``fear_greed``:
    ``fetch_fear_greed`` output. ``previous``: the previous snapshot
    dict, or None. ``universe``: a :func:`resolve_universe` result,
    or None for the embedded fallback (the shape existing callers
    and tests use). Pure apart from the ``now`` default.
    """
    now = now or utcnow()
    if universe is None:
        universe = {"coins": COINS, "added": [], "removed": [],
                    "source": "embedded_fallback", "error": None,
                    "as_of": now.date().isoformat()}
    coins = universe["coins"]
    cg_error = coingecko_markets.get("_error") if isinstance(coingecko_markets, dict) else None
    rows = []
    for coin in coins:
        if coin.source == "coinbase":
            rows.append(_coinbase_row(
                coin, coinbase.get(coin.symbol, {}),
                None if cg_error else coingecko_markets.get(coin.coingecko_id),
                now=now))
        else:
            if cg_error:
                rows.append(_unavailable_row(
                    coin, now, reason=f"coingecko markets: {cg_error}"))
            else:
                rows.append(_coingecko_row(
                    coin, coingecko_markets.get(coin.coingecko_id), now=now))
    # Rank order is live market-cap rank; ties / missing ranks fall
    # back to the fixed verification order (stable sort keeps it).
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"]))

    global_data = coingecko_global if isinstance(coingecko_global, dict) else {}
    total_cap = _float((global_data.get("total_market_cap") or {}).get("usd"))
    dominance = _float((global_data.get("market_cap_percentage") or {}).get("btc"))
    market = {
        "total_market_cap_usd": total_cap,
        "btc_dominance_pct": dominance,
        "fear_greed": fear_greed,
        "status": "ok" if total_cap is not None else "partial",
        "status_reason": None if total_cap is not None else (
            f"coingecko global unavailable: {global_data.get('error', 'missing')}"),
    }

    constructed = iso(now)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "artifact": ARTIFACT_KEY,
        "constructed_at": constructed,
        "generated_at": constructed,
        "session_model": "continuous_24_7",
        "cadence": "hourly",
        "coins": rows,
        "universe": {
            "source": universe["source"],
            "as_of": universe["as_of"],
            "coins": len(rows),
            "membership_source": universe["source"],
            "added": universe["added"],
            "removed": universe["removed"],
            "error": universe["error"],
        },
        "market": market,
        "patterns": [],
        "sources": {
            "coinbase": "Coinbase Exchange public API (no auth)",
            "coingecko": "CoinGecko public markets API (no key)",
            "fear_greed": "alternative.me Fear & Greed (best-effort)",
        },
        "counts": {
            "coins": len(rows),
            "available": sum(1 for r in rows if r["status"] == "ok"),
            "partial": sum(1 for r in rows if r["status"] == "partial"),
            "source_unavailable": sum(1 for r in rows if r["status"] == "source_unavailable"),
        },
    }
    detected = detect_patterns(rows, market, previous, now=now)
    previous_patterns = (previous or {}).get("patterns") or []
    snapshot["patterns"] = dedupe_patterns(detected, previous_patterns, now=now)
    return snapshot


def collect(*, previous=None, now=None, session=None):
    """One hourly collection: fetch all providers, build the snapshot.

    The universe is resolved first (live top 50, or the embedded
    fallback on a bad list fetch); the price/metadata fetches then
    run over exactly the resolved coins, so an entrant is priced in
    the same run it enters.
    """
    session = session or requests.Session()
    resolution = resolve_universe(session, now=now or utcnow())
    coins = resolution["coins"]
    coinbase = fetch_coinbase(session, coins=tuple(
        c for c in coins if c.source == "coinbase"))
    coingecko_markets = fetch_coingecko_markets(session, coins=coins)
    coingecko_global = fetch_coingecko_global(session)
    fear_greed = fetch_fear_greed(session)
    return build_snapshot(
        coinbase=coinbase, coingecko_markets=coingecko_markets,
        coingecko_global=coingecko_global, fear_greed=fear_greed,
        previous=previous, now=now or utcnow(), universe=resolution)


def snapshot_to_json(snapshot) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
