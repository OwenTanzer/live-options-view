"""Replay session: hidden-date clock, recorded-quote fills, isolated paper ledger.

Rules (OA-237 S1):
- The session is one qualified historical day. Until reveal, nothing sent to
  the player carries the date: no TradeDate/Expiration/OptionSymbol, no
  weekday, no source keys. Time of day and time to the close are shown.
- Only snapshots at or before the replay clock are ever read for the view, so
  future bars and future quotes cannot leak. The clock only moves forward.
- Buy fills at the recorded ask, sell fills at the recorded bid, of the latest
  snapshot at or before the clock. A fee per contract per side is charged and
  reported. Quotes are disabled -- never modeled -- when the snapshot is
  missing, older than MAX_QUOTE_AGE_S, a frozen repeat older than that, or
  fails the parity synchronization check, or when the side's price is absent.
- A position still open at the close expires: it is settled at intrinsic value
  against the last synchronized recorded underlying and labelled as an
  expiry settlement, not a fill.
- Paper orders live only in this process (plus an optional local journal). No
  account, Worker, KV or R2 write is involved.
"""

from __future__ import annotations

import bisect
import json
import random
import secrets
import time as _time
from pathlib import Path

from archive import MAX_QUOTE_AGE_S, contract_strike, quote_usable

MULTIPLIER = 100
DEFAULT_FEE = 0.65          # USD per contract per side; shown everywhere it applies
DEFAULT_CASH = 10_000.0
BOARD_HALF_WIDTH = 10       # strikes shown each side of the money
MAX_QTY = 20


def hhmmss(t: int) -> str:
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def label(contract: str) -> str:
    kind = "call" if contract[0] == "C" else "put"
    return f"QQQ {contract[1:]} {kind}, 0DTE"


class ReplayError(ValueError):
    pass


class ReplaySession:
    def __init__(self, pack: dict, *, rng: random.Random | None = None,
                 fee_per_contract_side: float = DEFAULT_FEE, cash: float = DEFAULT_CASH,
                 start_s: int | None = None, journal_dir: Path | None = None):
        self.id = secrets.token_urlsafe(9)
        self.pack = pack
        self.snaps = pack["snapshots"]
        self.times = [s["t"] for s in self.snaps]
        self.open_s, self.close_s = pack["open_s"], pack["close_s"]
        rng = rng or random.Random()
        if start_s is None:
            # land somewhere between 15 min after the open and 60 min before the close
            start_s = rng.randrange(self.open_s + 900, self.close_s - 3600, 60)
        self.clock = max(self.open_s, min(int(start_s), self.close_s))
        self.start_s = self.clock
        self.fee = float(fee_per_contract_side)
        self.cash = float(cash)
        self.cash_start = float(cash)
        self.lots: dict[str, dict] = {}          # contract -> {"qty", "cost"} (cost = premium paid, USD)
        self.fills: list[dict] = []
        self.realized = 0.0                      # net of fees, USD
        self.fees = 0.0
        self.revealed = False
        self.expired = False
        self.journal = None
        if journal_dir:
            journal_dir.mkdir(parents=True, exist_ok=True)
            self.journal = journal_dir / f"{self.id}.jsonl"
        self._log("start", {"start": hhmmss(self.start_s)})

    # -- quotes at the clock ---------------------------------------------

    def _snap_index(self, t: int) -> int | None:
        i = bisect.bisect_right(self.times, t) - 1
        return i if i >= 0 else None

    def board_state(self, t: int | None = None) -> tuple[dict | None, str | None, int | None]:
        """(snapshot, disabled_reason, age_s) governing quotes at time t."""
        t = self.clock if t is None else t
        i = self._snap_index(t)
        if i is None:
            return None, "no recorded snapshot yet", None
        snap = self.snaps[i]
        age = t - snap["t"]
        if age > MAX_QUOTE_AGE_S:
            return snap, f"latest snapshot is {age}s old (limit {MAX_QUOTE_AGE_S}s)", age
        if t - snap.get("content_t", snap["t"]) > MAX_QUOTE_AGE_S:
            return snap, "feed frozen: quotes unchanged for over " f"{MAX_QUOTE_AGE_S}s", age
        if not snap["ok"]:
            return snap, snap["reason"], age
        return snap, None, age

    def _last_synced_spot(self, t: int) -> float | None:
        i = self._snap_index(t)
        while i is not None and i >= 0:
            if self.snaps[i]["ok"]:
                return self.snaps[i]["spot"]
            i -= 1
        return None

    # -- player view (no date-bearing fields) ------------------------------

    def view(self) -> dict:
        snap, reason, age = self.board_state()
        spot = None if (snap is None or reason) else snap["spot"]
        rows = []
        if snap is not None:
            center = spot if spot is not None else self._last_synced_spot(self.clock)
            strikes = sorted({contract_strike(k) for k in snap["q"]})
            if center is not None and strikes:
                strikes.sort(key=lambda k: abs(k - center))
                strikes = sorted(strikes[: 2 * BOARD_HALF_WIDTH + 1])
            for k in strikes:
                row = {"strike": k}
                for side, prefix in (("call", "C"), ("put", "P")):
                    c = prefix + (str(int(k)) if float(k).is_integer() else f"{k:g}")
                    bid, ask = snap["q"].get(c, [None, None])
                    can_buy, can_sell, why = quote_usable(bid, ask)
                    if reason:
                        can_buy = can_sell = False
                        why = reason
                    row[side] = {"contract": c, "bid": bid, "ask": ask, "buy": can_buy,
                                 "sell": can_sell, "reason": why}
                rows.append(row)
        series = [[s["t"], s["spot"]] for s in self.snaps
                  if s["ok"] and self.open_s - 300 <= s["t"] <= self.clock]
        return {
            "session": {
                "id": self.id,
                "time_of_day": hhmmss(self.clock) + " ET",
                "clock_s": self.clock,
                "open_s": self.open_s,
                "close_s": self.close_s,
                "seconds_to_close": max(0, self.close_s - self.clock),
                "closed": self.clock >= self.close_s,
                "revealed": self.revealed,
            },
            "quotes": {"status": "disabled" if reason else "live", "reason": reason,
                       "age_s": age, "snapshot_time": hhmmss(snap["t"]) if snap else None},
            "spot": {"now": spot, "series": series},
            "board": rows,
            "positions": self._positions(snap, reason),
            "fills": [{k: v for k, v in f.items() if k != "source_key"} for f in self.fills],
            "account": self._account(snap, reason),
            "rules": {"fee_per_contract_side": self.fee, "multiplier": MULTIPLIER,
                      "quote_max_age_s": MAX_QUOTE_AGE_S,
                      "buy": "recorded ask", "sell": "recorded bid"},
        }

    def _positions(self, snap, reason) -> list[dict]:
        out = []
        for c, lot in self.lots.items():
            if lot["qty"] <= 0:
                continue
            bid = None
            if snap is not None and not reason:
                b, a = snap["q"].get(c, [None, None])
                if quote_usable(b, a)[1]:
                    bid = b
            exit_fee = self.fee * lot["qty"]
            out.append({
                "contract": c, "label": label(c), "qty": lot["qty"],
                "avg_price": round(lot["cost"] / lot["qty"] / MULTIPLIER, 4),
                "bid": bid,
                "unrealized_net": None if bid is None else
                round(bid * MULTIPLIER * lot["qty"] - lot["cost"] - exit_fee, 2),
            })
        return out

    def _account(self, snap, reason) -> dict:
        return {"cash": round(self.cash, 2), "cash_start": self.cash_start,
                "realized_net": round(self.realized, 2), "fees_paid": round(self.fees, 2)}

    # -- actions ------------------------------------------------------------

    def _require_open(self):
        if self.revealed:
            raise ReplayError("session already revealed")
        if self.clock >= self.close_s:
            raise ReplayError("market closed")

    def advance(self, seconds: int) -> dict:
        if self.revealed:
            raise ReplayError("session already revealed")
        seconds = int(seconds)
        if seconds <= 0 or seconds > 8 * 3600:
            raise ReplayError("advance must be 1..28800 seconds")
        self.clock = min(self.close_s, self.clock + seconds)
        self._log("advance", {"seconds": seconds})
        if self.clock >= self.close_s:
            self._expire()
        return self.view()

    def order(self, side: str, contract: str, qty: int) -> dict:
        self._require_open()
        if side not in ("buy", "sell"):
            raise ReplayError("side must be buy or sell")
        if not isinstance(contract, str) or contract[:1] not in ("C", "P"):
            raise ReplayError("unknown contract")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise ReplayError("qty must be an integer")
        if not 1 <= qty <= MAX_QTY:
            raise ReplayError(f"qty must be 1..{MAX_QTY}")
        snap, reason, age = self.board_state()
        if reason:
            raise ReplayError(f"quotes disabled: {reason}")
        if contract not in snap["q"]:
            raise ReplayError("contract not quoted in the current snapshot")
        bid, ask = snap["q"][contract]
        can_buy, can_sell, why = quote_usable(bid, ask)
        fee = self.fee * qty
        lot = self.lots.setdefault(contract, {"qty": 0, "cost": 0.0})
        if side == "buy":
            if not can_buy:
                raise ReplayError(f"no usable ask: {why or 'ask unavailable'}")
            premium = ask * MULTIPLIER * qty
            if premium + fee > self.cash + 1e-9:
                raise ReplayError("insufficient paper cash")
            self.cash -= premium + fee
            lot["qty"] += qty
            lot["cost"] += premium + fee          # entry fee carried in basis
            price, pnl = ask, None
        else:
            if lot["qty"] < qty:
                raise ReplayError("cannot sell more than held (no short positions in S1)")
            if not can_sell:
                raise ReplayError(f"no usable bid: {why or 'bid unavailable'}")
            proceeds = bid * MULTIPLIER * qty
            basis = lot["cost"] * qty / lot["qty"]
            lot["cost"] -= basis
            lot["qty"] -= qty
            self.cash += proceeds - fee
            pnl = proceeds - fee - basis
            self.realized += pnl
            price = bid
        self.fees += fee
        fill = {"n": len(self.fills) + 1, "kind": "fill", "side": side, "contract": contract,
                "label": label(contract), "qty": qty, "price": price, "fee": round(fee, 2),
                "time_of_day": hhmmss(self.clock), "quote_time": hhmmss(snap["t"]),
                "quote_age_s": age, "bid": bid, "ask": ask,
                "net_pnl": None if pnl is None else round(pnl, 2),
                "source_key": snap["key"]}
        self.fills.append(fill)
        self._log("fill", fill)
        return self.view()

    def _expire(self):
        if self.expired:
            return
        self.expired = True
        spot = self._last_synced_spot(self.close_s)
        for c, lot in self.lots.items():
            if lot["qty"] <= 0:
                continue
            k = contract_strike(c)
            intrinsic = None if spot is None else max(0.0, (spot - k) if c[0] == "C" else (k - spot))
            value = 0.0 if intrinsic is None else intrinsic * MULTIPLIER * lot["qty"]
            pnl = value - lot["cost"]
            self.cash += value
            self.realized += pnl
            self.fills.append({"n": len(self.fills) + 1, "kind": "expiry", "side": "expire",
                               "contract": c, "label": label(c), "qty": lot["qty"],
                               "price": None if intrinsic is None else round(intrinsic, 4),
                               "fee": 0.0, "time_of_day": hhmmss(self.close_s),
                               "note": "expired at the close; settled at intrinsic value vs the last "
                                       "synchronized recorded underlying (not a market fill)",
                               "net_pnl": round(pnl, 2), "source_key": None})
            lot["qty"], lot["cost"] = 0, 0.0
        self._log("expire", {})

    def reveal(self) -> dict:
        """End the session and disclose the day, its full path and the sources."""
        self.revealed = True
        self._log("reveal", {})
        v = self.view()
        full = [[s["t"], s["spot"]] for s in self.snaps
                if s["ok"] and self.open_s - 300 <= s["t"] <= self.close_s]
        v["reveal"] = {
            "date": self.pack["day"],
            "weekday": __import__("datetime").date.fromisoformat(self.pack["day"]).strftime("%A"),
            "start_time": hhmmss(self.start_s) + " ET",
            "full_day_spot": full,
            "coverage_usable": self.pack["coverage_usable"],
            "fills_with_sources": self.fills,
            "net_profit": round(self.realized, 2),
            "open_positions_unsettled": [p for p in v["positions"]],
        }
        return v

    def _log(self, event: str, data: dict):
        if self.journal:
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": _time.time(), "event": event, "clock": self.clock,
                                     "data": data}) + "\n")
