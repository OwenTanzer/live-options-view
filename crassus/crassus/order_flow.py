"""Reader for the collector's live QQQ order-flow projection (``recent.json``).

The Tradier collector (``market_data/tradier/recent.py``) republishes this file
every ~3 s during the session. Its ``session.contracts`` map holds, per option
contract, the session's accumulated contracts by inferred aggressor side, and
(from the tiered-label change) a ``label`` block that splits that volume by how
the side was established:

    label = {"q1": [buy, sell], "q5": [buy, sell], "low": [buy, sell], "U": n}

``q1``/``q5`` are quote-rule signs against a prevailing quote at most 1 s / 5 s
old; ``low`` pools older-quote and tick-test signs, which agreed with the
provider's own trade-time bid/ask little better than chance in the audit; ``U``
is volume nothing could sign. This module only fetches and validates; it makes
no trading judgement.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

ORDER_FLOW_URL = os.environ.get(
    "CRASSUS_ORDER_FLOW_URL",
    "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/order-flow/qqq/v1/recent.json",
)
_USER_AGENT = "crassus-order-flow-reader/1.0"
OCC = re.compile(r"QQQ(\d{6})([CP])(\d{8})")


class OrderFlowError(Exception):
    """The projection could not be fetched or does not have the expected shape."""


@dataclass(frozen=True)
class ContractFlow:
    symbol: str
    type: str          # "call" | "put"
    strike: float
    high_buy: int      # q1 + q5 buy contracts
    high_sell: int     # q1 + q5 sell contracts
    low: int           # low-confidence signed contracts (either side)
    unsigned: int      # contracts nothing could sign


@dataclass(frozen=True)
class FlowSnapshot:
    day: str
    last_received_ms: int
    gaps: tuple[tuple[int, int | None], ...]   # (start_ms, end_ms or None while open)
    contracts: dict[str, ContractFlow]


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise OrderFlowError(f"bad count {value!r}")
    return value


def parse(payload: Any) -> FlowSnapshot:
    """Validate one ``recent.json`` body. Raises OrderFlowError on any shape problem,
    including a collector that predates the tiered labels."""
    if not isinstance(payload, dict):
        raise OrderFlowError("payload is not an object")
    session = payload.get("session")
    last = payload.get("lastReceived")
    if not isinstance(session, dict) or type(last) is not int:
        raise OrderFlowError("missing session or lastReceived")
    contracts = {}
    for symbol, c in (session.get("contracts") or {}).items():
        match = OCC.fullmatch(symbol)
        label = c.get("label") if isinstance(c, dict) else None
        if not match:
            continue
        if not isinstance(label, dict):
            raise OrderFlowError("collector projection has no tiered labels (pre-label collector)")
        try:
            q1, q5, low = (label[k] for k in ("q1", "q5", "low"))
            buy, sell = _count(q1[0]) + _count(q5[0]), _count(q1[1]) + _count(q5[1])
            low_total = _count(low[0]) + _count(low[1])
            unsigned = _count(label["U"])
        except (KeyError, IndexError, TypeError) as exc:
            raise OrderFlowError(f"malformed label for {symbol}") from exc
        strike = int(match[3]) / 1000
        if not math.isfinite(strike):
            continue
        contracts[symbol] = ContractFlow(symbol, "call" if match[2] == "C" else "put", strike,
                                         buy, sell, low_total, unsigned)
    gaps = []
    for gap in session.get("gaps") or []:
        try:
            start, end = gap["start"], gap.get("end")
        except (KeyError, TypeError) as exc:
            raise OrderFlowError("malformed gap") from exc
        if type(start) is not int or (end is not None and type(end) is not int):
            raise OrderFlowError("malformed gap")
        gaps.append((start, end))
    return FlowSnapshot(str(session.get("day")), last, tuple(gaps), contracts)


class OrderFlowReader:
    """Fetch ``recent.json`` at most once per ``cache_s`` (every account in one
    runner cycle reads the same body instead of refetching it)."""

    def __init__(self, url: str = ORDER_FLOW_URL, timeout_s: float = 10.0, cache_s: float = 20.0):
        self.url, self.timeout_s, self.cache_s = url, timeout_s, cache_s
        self._lock = threading.Lock()
        self._cached: tuple[float, FlowSnapshot] | None = None

    def read(self) -> FlowSnapshot:
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self.cache_s:
                return self._cached[1]
            try:
                resp = requests.get(self.url, timeout=self.timeout_s, headers={"User-Agent": _USER_AGENT})
                resp.raise_for_status()
                snapshot = parse(resp.json())
            except OrderFlowError:
                raise
            except Exception as exc:  # network, HTTP status, JSON decoding
                raise OrderFlowError(f"{type(exc).__name__}: {exc}") from exc
            self._cached = (time.monotonic(), snapshot)
            return snapshot
