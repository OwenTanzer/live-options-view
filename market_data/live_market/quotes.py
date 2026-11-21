"""Live quotes: the ephemeral quote HTTP service and SPY spot observation.

WHY this module: the quote service is the collector's read side -- the
Worker's read-only quote proxy reads it -- and the SPY spot helpers are
how every consumer decides whether a SPY quote is fresh enough to trust.
They lived in the market-compute section of the flat file, but they are
quote observation, not market math: LiveQuoteRegistry already called
_observed_spy_price, and push_prices/push_health/take_snapshot all read
through the same helpers. Moving them here breaks that backward edge
(market imports quotes, never the reverse).
"""

import json
import math
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .config import (
    LIVE_QUOTE_PORT,
    MAX_LIVE_QUOTE_SYMBOLS,
    PRICE_TICKERS,
    SPY_MAX_STRIKES,
    STALE_FEED_SECS,
    TICKER_CLASSES,
    log,
    COLLECT_IBIT,
)
from .chain import _dxlink_symbol
from .feed import DXLinkFeed

# -- ephemeral live quote HTTP service ----------------------------------------

# Extra exact contracts the service will subscribe on its own initiative in a
# session, beyond the chains the collector already collects. Bounds provider
# subscription load; when it is reached the payload says so per symbol
# ("subscription_limit") instead of silently omitting the contract.
MAX_REQUESTED_CONTRACTS = 100


class LiveQuoteRegistry:
    """Thread-safe session pointer behind the process-lifetime HTTP server."""

    def __init__(self):
        self.btc = None
        self._lock = threading.Lock()
        self._feed: Optional[DXLinkFeed] = None
        self._contracts: dict[str, dict] = {}
        self._requested_contracts = set()
        self._ticker_fallbacks: dict[str, dict] = {}
        # WHY a session is absent. `offline` alone cannot tell "the session
        # finished on purpose" from "the process has not started one yet" or
        # "the session died", and callers that treat them alike either hide a
        # trading-hours outage or cry failure every evening. The value is
        # explicit, never inferred from a clock.
        self._session = "uninitialized"

    def set_session(self, feed: DXLinkFeed, contracts: dict[str, dict]):
        with self._lock:
            self._feed = feed
            self._contracts = dict(contracts)
            self._requested_contracts = set()
            self._session = "open"

    def clear_session(self, reason: str = "session_closed"):
        """Drop the session pointer, recording WHY it went.

        `session_closed` is the deliberate end of a trading session and the
        only value a client may show as a closed lane. `session_failed` is an
        auth circuit, a token-refresh failure or an unhandled session
        exception: the lane is down while it should be up, so callers must
        keep treating it as a failure and keep backing off.
        """
        if reason not in ("session_closed", "session_failed"):
            raise ValueError(f"unknown session end reason: {reason}")
        with self._lock:
            self._feed = None
            self._contracts = {}
            self._session = reason

    def await_next_session(self):
        """Record the scheduler's DELIBERATE wait for the next session.

        main() builds the registry uninitialized and then blocks in
        wait_for_premarket, so without this a cold start after hours would
        report "uninitialized" all evening and no client could show the closed
        state it is genuinely in. This is not a failure: it is the same
        outside-session condition as a finished session, reached without one.
        """
        with self._lock:
            self._feed = None
            self._contracts = {}
            self._session = "waiting_for_session"

    def session_state(self) -> str:
        with self._lock:
            return self._session

    def update_ticker_fallbacks(self, prices: dict[str, dict], observed_at: str):
        with self._lock:
            self._ticker_fallbacks = {
                symbol: {**quote, "quote_ts": quote.get("quote_ts")}
                for symbol, quote in prices.items()
            }

    def health(self) -> dict:
        with self._lock:
            feed = self._feed
            session = self._session
        now = datetime.now(timezone.utc)
        if feed is None:
            return {"state": "offline", "session": session,
                    "last_quote_at": None, "server_ts": now.isoformat()}
        feed_health = feed.get_health()
        last_event = feed_health["last_feed_event_time"]
        if not (feed_health["connected"] and feed_health["authorized"] and feed_health["channel_open"]):
            state = "connecting"
        elif last_event is None:
            state = "connecting"
        elif (now - last_event).total_seconds() > STALE_FEED_SECS:
            state = "stale"
        else:
            state = "live"
        return {
            "state": state,
            "session": session,
            "last_quote_at": last_event.isoformat() if last_event else None,
            "server_ts": now.isoformat(),
        }

    def quote_payload(self, symbols: list[str]) -> dict:
        # Held contracts outside today's fixed window remain quoteable. The
        # authenticated service adds at most 100 exact contracts per session,
        # on the existing connection, without expanding the collected chain.
        #
        # `unavailable` explains every requested symbol that produced no
        # quote. Without it a caller sees only returned < requested and cannot
        # tell an expired contract from a later expiry that the provider has
        # not answered yet, an exhausted subscription budget, or a closed
        # session -- which is exactly the ambiguity that made a silent empty
        # response look like a broken quote lane.
        skip_reason: dict[str, str] = {}
        with self._lock:
            for symbol in symbols:
                if symbol in self._contracts:
                    continue
                # Shape first: a malformed symbol is malformed in any session
                # state, and saying "session_closed" about it would be a lie.
                match = re.fullmatch(r"(QQQ|SPY|IBIT)(\d{6})([CP])(\d{8})", symbol)
                if not match:
                    skip_reason[symbol] = "unknown_symbol"
                    continue
                if not self._feed:
                    skip_reason[symbol] = self._session
                    continue
                if match[1] == "IBIT" and not COLLECT_IBIT:
                    skip_reason[symbol] = "not_collected"
                    continue
                if len(self._requested_contracts) >= MAX_REQUESTED_CONTRACTS:
                    skip_reason[symbol] = "subscription_limit"
                    continue
                try:
                    expiry = datetime.strptime(match[2], "%y%m%d").date()
                except ValueError:
                    skip_reason[symbol] = "unknown_symbol"
                    continue
                if expiry < datetime.now().date():
                    skip_reason[symbol] = "expired"
                    continue
                streamer = _dxlink_symbol(symbol)
                self._feed.add_option_subscriptions([streamer])
                self._contracts[symbol] = {"streamer_symbol": streamer, "strike": int(match[4]) / 1000,
                                           "type": "call" if match[3] == "C" else "put", "exp": expiry.isoformat()}
                self._requested_contracts.add(symbol)
        with self._lock:
            feed = self._feed
            contracts = dict(self._contracts)
            ticker_fallbacks = dict(self._ticker_fallbacks)
        health = self.health()
        state = feed.get_state() if feed is not None else {}
        quotes = []
        unavailable: dict[str, str] = {}
        for symbol in symbols:
            if symbol in PRICE_TICKERS or symbol == "SPY":
                # A priced ticker without a class must not crash the handler
                # (NVDA returned HTTP 502): report it unavailable instead of
                # raising KeyError. TICKER_CLASSES is derived from the
                # universe, so this is defensive -- it only fires if the
                # registry and the class map drift apart.
                instrument_class = TICKER_CLASSES.get(symbol)
                if instrument_class is None:
                    unavailable[symbol] = "ticker_unavailable"
                    continue
                raw = state.get(PRICE_TICKERS.get(symbol, symbol), {})
                bid, ask, last = raw.get("bid"), raw.get("ask"), raw.get("last")
                mid = (bid + ask) / 2 if bid is not None and ask is not None else None
                if symbol == "SPY":
                    price, spy_ts = _observed_spy_price(raw)
                    observed = [spy_ts] if spy_ts else []
                else:
                    price = last if last is not None else mid
                    observed = [ts for ts in (raw.get("last_ts"), raw.get("bid_ts"), raw.get("ask_ts")) if ts]
                if price is not None and observed:
                    prev = raw.get("prev_close")
                    quotes.append({
                        "kind": "ticker", "symbol": symbol, "price": price,
                        "bid": bid, "ask": ask, "prev_close": prev,
                        "chg_pct": round((price - prev) / prev * 100, 2) if prev else None,
                        "quote_ts": max(observed),
                        "bid_ts": raw.get("bid_ts"), "ask_ts": raw.get("ask_ts"),
                        "source": "dxlink",
                        "instrument_class": instrument_class,
                    })
                elif ticker_fallbacks.get(symbol, {}).get("price") is not None:
                    quotes.append({
                        **ticker_fallbacks[symbol], "kind": "ticker", "symbol": symbol,
                        "instrument_class": instrument_class,
                    })
                else:
                    unavailable[symbol] = "ticker_unavailable"
                continue
            contract = contracts.get(symbol)
            if not contract:
                unavailable[symbol] = skip_reason.get(symbol, "unknown_symbol")
                continue
            quote = state.get(contract["streamer_symbol"], {})
            bid = quote.get("bid")
            ask = quote.get("ask")
            bid_ts = quote.get("bid_ts")
            ask_ts = quote.get("ask_ts")
            observed = [ts for ts in (bid_ts, ask_ts) if ts]
            if bid is None or ask is None or not observed:
                unavailable[symbol] = "no_quote_yet"
                continue
            quotes.append({
                "kind": "option",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "bid_ts": bid_ts,
                "ask_ts": ask_ts,
                "quote_ts": max(observed),
                "strike": contract["strike"],
                "type": contract["type"],
                "exp": contract["exp"],
            })
        return {
            "quotes": quotes,
            "requested": len(symbols),
            "returned": len(quotes),
            "unavailable": unavailable,
            "server_ts": health["server_ts"],
            "health": health,
        }


def start_live_quote_server(registry: LiveQuoteRegistry, port: Optional[int] = None):
    access_key = os.environ.get("LIVE_QUOTE_KEY", "")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, registry.health())
                return
            if parsed.path == "/btc-options":
                if not access_key or self.headers.get("X-Live-Quote-Key") != access_key:
                    self._json(403, {"error": "Forbidden"})
                    return
                if registry.btc is None:
                    self._json(503, {"error": "BTC product disabled"})
                    return
                query = parse_qs(parsed.query)
                try:
                    symbols = list(dict.fromkeys(filter(None, query.get("symbols", [""])[0].split(","))))
                    held = json.loads(query.get("held", ["[]"])[0])
                    if len(symbols) > 64 or any(not re.fullmatch(r"BTCOPT:[0-9a-f]{40}", s) for s in symbols):
                        raise ValueError()
                    if not isinstance(held, list) or len(held) > 64 or any(not isinstance(s, str) or len(s) > 128 for s in held):
                        raise ValueError()
                except (ValueError, TypeError):
                    self._json(400, {"error": "Invalid BTC request"})
                    return
                self._json(200, registry.btc.payload(symbols, held, expiry=query.get("expiry", [None])[0]))
                return
            if parsed.path != "/live-quotes":
                self._json(404, {"error": "Not found"})
                return
            if not access_key or self.headers.get("X-Live-Quote-Key") != access_key:
                self._json(403, {"error": "Forbidden"})
                return
            raw_symbols = parse_qs(parsed.query).get("symbols", [""])[0]
            symbols = list(dict.fromkeys(
                symbol.strip().replace(" ", "")
                for symbol in raw_symbols.split(",")
                if symbol.strip()
            ))
            if not symbols or len(symbols) > MAX_LIVE_QUOTE_SYMBOLS:
                self._json(400, {"error": f"Request 1-{MAX_LIVE_QUOTE_SYMBOLS} symbols"})
                return
            health = registry.health()
            has_tickers = any(symbol in PRICE_TICKERS or symbol == "SPY" for symbol in symbols)
            if health["state"] in {"offline", "connecting"} and not has_tickers:
                self._json(
                    503,
                    {"error": "Live quote feed unavailable", "health": health},
                    {"Retry-After": "5"},
                )
                return
            self._json(200, registry.quote_payload(symbols))

        def _json(self, status: int, value: dict, headers: Optional[dict[str, str]] = None):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            log.debug("live quote HTTP: " + format, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port or LIVE_QUOTE_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, name="live-quote-http", daemon=True)
    thread.start()
    log.info(f"live quote HTTP service started on port {server.server_address[1]}")
    return server



# -- SPY spot observation ---------------------------------------------------------
# Moved here from the market-compute section: these five helpers decide what
# a SPY quote is worth (own receipt time, freshness window, strike window,
# subscription coverage). Every reader -- LiveQuoteRegistry above,
# push_prices, push_health, take_snapshot -- goes through them.

def _observed_spy_price(quote: dict) -> tuple[float | None, str | None]:
    """Use only a SPY price with its own complete collector receipt time."""
    price, received, _ = _observed_spy_price_with_basis(quote)
    return price, received


def _observed_spy_price_with_basis(quote: dict) -> tuple[float | None, str | None, str | None]:
    """Return the selected SPY input, its receipt time and price basis."""
    bid, ask = quote.get("bid"), quote.get("ask")
    bid_ts, ask_ts = quote.get("bid_ts"), quote.get("ask_ts")
    if bid is not None and ask is not None and bid_ts and ask_ts:
        try:
            older = min((bid_ts, ask_ts), key=datetime.fromisoformat)
            return round((bid + ask) / 2, 2), older, "bid_ask_midpoint"
        except (TypeError, ValueError):
            pass
    last, last_ts = quote.get("last"), quote.get("last_ts")
    if last is not None and last_ts:
        try:
            datetime.fromisoformat(last_ts)
            return last, last_ts, "last_trade"
        except (TypeError, ValueError):
            pass
    return None, None, None


def _older_observed(*pairs: tuple[str | None, str | None]) -> str | None:
    """Provider event time for a two-sided midpoint: only when both sides
    carry one, and then the older side -- a fresh half-quote must not make
    an old half look observed. Unknown stays None; nothing here is inferred
    from the collector receipt clock. Mirrors Brian #184's _older_observed
    (UnderlyingSelection.provider_observed_at)."""
    if not all(ts for _, ts in pairs):
        return None
    parsed = []
    for _, ts in pairs:
        try:
            parsed.append((datetime.fromisoformat(ts), ts))
        except (TypeError, ValueError):
            return None
    return min(parsed, key=lambda item: item[0])[1]


def _provider_observed_spy_price(quote: dict) -> tuple[float | None, str | None, str | None]:
    """Return the selected SPY input and its PROVIDER-observed time.

    The price and basis are selected exactly as _observed_spy_price_with_basis
    (same input, same order), so the provider time always describes the same
    price the receipt-time helper returns. The provider time is the
    provider's own event time for that input -- never the collector receipt
    time: a two-sided midpoint needs both sides' provider times (older
    wins); a last trade needs its own last_observed_at; anything else has
    no provider time (None). A remembered/carried-forward spot is never
    stamped with a feed time. Use this for settlement marks, which must be
    irreversible provider observations, not fresh receipts of unknown-age
    prices. Returns (price, provider_observed_at, basis)."""
    price, _receipt, basis = _observed_spy_price_with_basis(quote)
    if price is None:
        return None, None, None
    if basis == "bid_ask_midpoint":
        provider = _older_observed(
            ("bid", quote.get("bid_observed_at")), ("ask", quote.get("ask_observed_at")))
    elif basis == "last_trade":
        provider = quote.get("last_observed_at")
        try:
            datetime.fromisoformat(provider)
        except (TypeError, ValueError):
            provider = None
    else:
        provider = None
    return price, provider, basis


def _fresh_spy_spot(quote: dict, now: datetime) -> tuple[float | None, str | None]:
    """Accept a finite, positive SPY feed price observed within the stale threshold."""
    price, observed_at = _observed_spy_price(quote)
    try:
        age = (now - datetime.fromisoformat(observed_at)).total_seconds()
        if (price is not None and not isinstance(price, bool) and
                math.isfinite(float(price)) and float(price) > 0 and
                0 <= age <= STALE_FEED_SECS):
            return float(price), observed_at
    except (TypeError, ValueError, OverflowError):
        pass
    return None, None


def _select_spy_strikes(strikes: list[dict], spot: float) -> list[dict]:
    """Choose at most 67 distinct nearest strikes; lower strike wins a tie."""
    unique = {row["strike"]: row for row in strikes}
    nearest = sorted(unique.values(), key=lambda row: (abs(row["strike"] - spot), row["strike"]))
    return sorted(nearest[:SPY_MAX_STRIKES], key=lambda row: row["strike"])


def _spy_subscription_coverage(coverage: dict | None, spot: float | None) -> dict | None:
    if coverage is None:
        return None
    result = dict(coverage)
    low, high = result["min_strike"], result["max_strike"]
    result["current_spot"] = spot
    result["out_of_range"] = None if spot is None else spot < low or spot > high
    result["status"] = ("spot_unavailable" if spot is None else
                        "out_of_range" if result["out_of_range"] else "within_window")
    return result



__all__ = [
    "LiveQuoteRegistry",
    "start_live_quote_server",
    "_observed_spy_price",
    "_observed_spy_price_with_basis",
    "_older_observed",
    "_provider_observed_spy_price",
    "_fresh_spy_spot",
    "_select_spy_strikes",
    "_spy_subscription_coverage",
]
