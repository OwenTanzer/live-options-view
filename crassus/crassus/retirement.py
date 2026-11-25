"""Permanent paper entry stop after a verified account return below -99%.

Valuation follows site/js/bots-panel.js: cash plus signed midpoint market
value, divided by the account's actual starting balance. No partial marks.
The receipt lives on the runner volume; it neither closes nor resets accounts.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import clock
from .durability import atomic_json


def number(value):
    if type(value) not in (int, float):
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def fresh(stamp, now):
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return 0 <= (now - parsed).total_seconds() <= 60
    except (AttributeError, TypeError, ValueError):
        return False


def verified_value(state, book, quotes):
    """Return unrounded evidence or None; unsupported/incomplete inputs abstain."""
    now = clock.now_utc()
    cash, start = number(state.balance_cash), number(state.starting_balance)
    if (not state.is_bot or not state.valuation_complete or not state.trade_history_present
            or cash is None or start is None or start <= 0 or not fresh(state.fetched_at, now)):
        return None
    multipliers = {}
    for trade in state.trades:
        if not isinstance(trade, dict):
            return None
        if "adjustment_units" in trade:
            if number(trade["adjustment_units"]) is None:
                return None
            continue  # cash already includes financing/fee adjustments
        sym, kind = trade.get("sym"), trade.get("instrument_type", "option")
        qty, price = number(trade.get("qty")), number(trade.get("price"))
        multiplier = number(trade.get("multiplier", 1 if kind == "share" else 100))
        if (not isinstance(sym, str) or kind not in ("option", "share")
                or not re.fullmatch(r"[A-Z]+\d{6}[CP]\d{8}" if kind == "option" else r"[A-Z][A-Z0-9.\-]*", sym)
                or trade.get("side") not in ("buy", "sell")
                or qty is None or qty <= 0 or qty != int(qty)
                or price is None or price < 0
                or multiplier != (1 if kind == "share" else 100)):
            return None
        if sym in multipliers and multipliers[sym] != multiplier:
            return None
        multipliers[sym] = multiplier
    marks = quotes(list(book.positions)) if book.positions else {}
    equity, evidence = cash, {}
    # Check all timestamps against the completion of the quote batch too.
    now = clock.now_utc()
    if not fresh(state.fetched_at, now):
        return None
    for sym, position in book.positions.items():
        q = marks.get(sym)
        if q is None or q.symbol != sym or sym not in multipliers:
            return None
        bid, ask = number(q.bid), number(q.ask)
        if (bid is None or ask is None or bid < 0 or ask < bid
                or not all(fresh(stamp, now) for stamp in
                           (q.server_ts, q.quote_ts, q.bid_ts, q.ask_ts))):
            return None
        mark = (bid + ask) / 2
        equity += mark * position.quantity * multipliers[sym]
        evidence[sym] = {"quantity": position.quantity, "multiplier": str(multipliers[sym]),
                         "midpoint": str(mark), "bid_ts": q.bid_ts, "ask_ts": q.ask_ts}
    return {"net_equity": str(equity), "starting_balance": str(start),
            "return_fraction": str((equity - start) / start),
            "below_threshold": equity * 100 < start,
            "account_fetched_at": state.fetched_at, "marks": evidence}


class LossRetirement:
    def __init__(self, state_dir: Path, origin: str):
        self.directory = Path(state_dir)
        self.origin = origin.rstrip("/")

    def path(self, username):
        key = hashlib.sha256(f"{self.origin}\n{username}".encode()).hexdigest()
        return self.directory / f"loss-retirement-{key}.json"

    def read(self, username):
        path = self.path(username)
        if not path.exists():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (receipt.get("username") != username or receipt.get("origin") != self.origin
                or receipt.get("reason") != "account_return_below_minus_99_percent"):
            raise ValueError("Invalid loss retirement receipt; refusing to resume entries")
        return receipt

    def record(self, username, value):
        existing = self.read(username)
        if existing is not None:
            return existing
        receipt = {"username": username, "origin": self.origin,
                   "reason": "account_return_below_minus_99_percent",
                   "retired_at": clock.iso_utc(), "valuation": value}
        atomic_json(self.path(username), receipt)
        return receipt


def reduces_only(decision, book):
    position = book.positions.get(decision.symbol)
    qty = number(decision.quantity)
    return (position is not None and qty is not None and 0 < qty <= abs(position.quantity)
            and qty == int(qty)
            and decision.action == ("sell" if position.quantity > 0 else "buy"))
