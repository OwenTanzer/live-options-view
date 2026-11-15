"""Replay session: hidden-date clock, recorded-quote fills, isolated paper ledger.

Rules (OA-237 S1, S2a):
- The session is one qualified historical day. Until reveal, nothing sent to
  the player carries the date: no TradeDate/Expiration/OptionSymbol, no
  weekday, no source keys. Time of day and time to the close are shown.
- Only snapshots at or before the replay clock are ever read for the view, so
  future bars and future quotes cannot leak. The clock only moves forward;
  reset returns to the landing time with a fresh ledger (earlier attempts are
  kept and reported).
- A *bar* is one recorded snapshot. Every clock move is capped to at most one
  new bar and must come at least MIN_DWELL_S after the previous move, so each
  bar stays on screen for at least 500 ms at any speed.
- Buy fills at the recorded ask, sell fills at the recorded bid, of the latest
  snapshot at or before the clock. A fee per contract per side is charged and
  reported. Quotes are disabled -- never modeled -- when the snapshot is
  missing, older than MAX_QUOTE_AGE_S, a frozen repeat older than that, or
  fails the parity consistency check, or when the side's price is absent.
  Parity is a consistency check on the recorded values (the archive has no
  per-quote timestamps), not proof that spot and quotes share an instant.
- A position still open at the close expires: it is settled at intrinsic value
  against the last parity-consistent recorded underlying and labelled as an
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
from datetime import date as _date
from pathlib import Path
from typing import Callable

from archive import MAX_QUOTE_AGE_S, contract_strike, quote_usable

MULTIPLIER = 100
DEFAULT_FEE = 0.65          # USD per contract per side; shown everywhere it applies
DEFAULT_CASH = 10_000.0
MAX_QTY = 20
MIN_DWELL_S = 0.5           # minimum wall-clock time between clock moves


def hhmmss(t: int) -> str:
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def label(contract: str) -> str:
    kind = "call" if contract[0] == "C" else "put"
    return f"QQQ {contract[1:]} {kind}, 0DTE"


class ReplayError(ValueError):
    status = 409


class DwellError(ReplayError):
    status = 429


def explain(code: str, snap: dict | None, age: int | None) -> tuple[str, str]:
    """(short label, plain-language sentence) for a disabled-board reason code."""
    if code == "no_snapshot":
        return "no data yet", "Nothing has been recorded yet at this time of day."
    if code == "gap":
        return "data gap", (f"The latest recorded snapshot is {age}s old (limit {MAX_QUOTE_AGE_S}s); "
                            "the collector recorded nothing newer, so its quotes are no longer current.")
    if code == "frozen":
        return "feed frozen", (f"The recorded chain and underlying have not changed for over {MAX_QUOTE_AGE_S}s, "
                               "so the feed looks frozen and its quotes are treated as stale.")
    if code == "no_underlying":
        return "no underlying", "This snapshot has no recorded underlying price."
    if code == "no_pair":
        return "no two-sided pair", ("No call/put pair near the money has both a bid and an ask, "
                                     "so the snapshot cannot be checked for consistency.")
    if code == "parity":
        spot, implied, tol = snap.get("spot"), snap.get("parity_spot"), snap.get("parity_tol")
        diff = abs(implied - spot) if (implied is not None and spot is not None) else None
        return "parity check failed", (
            f"Consistency check failed: the recorded underlying {spot:.2f} differs from the "
            f"{implied:.2f} implied by the option mids nearest the money by ${diff:.2f}, more than this "
            f"snapshot's ${tol:.2f} allowance ($0.15 plus half the two spreads). The quotes may not match "
            "the recorded spot, so they are not tradable. This checks the recorded values against each "
            "other; it does not prove when they were observed."
            if diff is not None and tol is not None else
            "Consistency check failed between the recorded underlying and the option mids.")
    return code, code


class ReplaySession:
    def __init__(self, pack: dict, *, rng: random.Random | None = None,
                 fee_per_contract_side: float = DEFAULT_FEE, cash: float = DEFAULT_CASH,
                 start_s: int | None = None, journal_dir: Path | None = None,
                 now: Callable[[], float] = _time.monotonic,
                 raw_lookup: Callable[[str, str], dict | None] | None = None):
        self.id = secrets.token_urlsafe(9)
        self.pack = pack
        self.snaps = pack["snapshots"]
        self.times = [s["t"] for s in self.snaps]
        self.open_s, self.close_s = pack["open_s"], pack["close_s"]
        rng = rng or random.Random()
        if start_s is None:
            # land somewhere between 15 min after the open and 60 min before the close
            start_s = rng.randrange(self.open_s + 900, self.close_s - 3600, 60)
        self.start_s = max(self.open_s, min(int(start_s), self.close_s))
        self.fee = float(fee_per_contract_side)
        self.cash_start = float(cash)
        self.now = now
        self.raw_lookup = raw_lookup
        self.revealed = False
        self.attempts: list[dict] = []
        self.journal = None
        if journal_dir:
            journal_dir.mkdir(parents=True, exist_ok=True)
            self.journal = journal_dir / f"{self.id}.jsonl"
        self._fresh_ledger()
        self._log("start", {"start": hhmmss(self.start_s)})

    def _fresh_ledger(self):
        self.clock = self.start_s
        self.furthest = self.start_s
        self.cash = self.cash_start
        self.lots: dict[str, dict] = {}          # contract -> {"qty", "cost"} (cost = premium + entry fees, USD)
        self.fills: list[dict] = []
        self.realized = 0.0                      # net of fees, USD
        self.fees = 0.0
        self.expired = False
        self.last_move = None

    # -- quotes at the clock ---------------------------------------------

    def _snap_index(self, t: int) -> int | None:
        i = bisect.bisect_right(self.times, t) - 1
        return i if i >= 0 else None

    def board_state(self, t: int | None = None) -> tuple[dict | None, str | None, int | None]:
        """(snapshot, disabled_reason_code, age_s) governing quotes at time t."""
        t = self.clock if t is None else t
        i = self._snap_index(t)
        if i is None:
            return None, "no_snapshot", None
        snap = self.snaps[i]
        age = t - snap["t"]
        if age > MAX_QUOTE_AGE_S:
            return snap, "gap", age
        if t - snap.get("content_t", snap["t"]) > MAX_QUOTE_AGE_S:
            return snap, "frozen", age
        if not snap["ok"]:
            return snap, snap["reason"], age
        return snap, None, age

    def dark_stretches(self, until: int) -> list[dict]:
        """Past intervals in [open, until] where the board was disabled, with reasons.
        Evaluated only at snapshot boundaries and age cut-offs at or before `until`."""
        points = {self.open_s, until}
        for s in self.snaps:
            for p in (s["t"], s["t"] + MAX_QUOTE_AGE_S + 1, s.get("content_t", s["t"]) + MAX_QUOTE_AGE_S + 1):
                if self.open_s <= p <= until:
                    points.add(p)
        pts = sorted(points)
        out: list[dict] = []
        for a, b in zip(pts, pts[1:]):
            _, code, _ = self.board_state(a)
            if code is None:
                continue
            if out and out[-1]["code"] == code and out[-1]["end"] == a:
                out[-1]["end"] = b
            else:
                out.append({"start": a, "end": b, "code": code, "label": explain(code, None, 0)[0]
                            if code != "parity" else "parity check failed"})
        return out

    def _last_consistent_spot(self, t: int) -> float | None:
        i = self._snap_index(t)
        while i is not None and i >= 0:
            if self.snaps[i]["ok"]:
                return self.snaps[i]["spot"]
            i -= 1
        return None

    def _next_bar_after(self, t: int) -> int | None:
        i = bisect.bisect_right(self.times, t)
        while i < len(self.times) and self.times[i] < self.open_s:
            i += 1
        return self.times[i] if i < len(self.times) and self.times[i] < self.close_s else None

    # -- player view (no date-bearing fields) ------------------------------

    def view(self) -> dict:
        snap, code, age = self.board_state()
        spot = None if (snap is None or code) else snap["spot"]
        short, detail = explain(code, snap, age) if code else (None, None)
        rows = []
        if snap is not None:
            for k in sorted({contract_strike(c) for c in snap["q"]}):
                row = {"strike": k}
                for side, prefix in (("call", "C"), ("put", "P")):
                    c = prefix + (str(int(k)) if float(k).is_integer() else f"{k:g}")
                    bid, ask = snap["q"].get(c, [None, None])
                    can_buy, can_sell, why = quote_usable(bid, ask)
                    if code:
                        can_buy = can_sell = False
                        why = short
                    row[side] = {"contract": c, "bid": bid, "ask": ask, "buy": can_buy,
                                 "sell": can_sell, "reason": why}
                rows.append(row)
        dark = self.dark_stretches(self.clock)
        current_dark = dark[-1] if (code and dark and dark[-1]["end"] >= self.clock) else None
        series = [[s["t"], s["spot"]] for s in self.snaps
                  if s["ok"] and self.open_s - 300 <= s["t"] <= self.clock]
        nxt = self._next_bar_after(self.clock)
        return {
            "session": {
                "id": self.id,
                "time_of_day": hhmmss(self.clock) + " ET",
                "clock_s": self.clock,
                "start_s": self.start_s,
                "open_s": self.open_s,
                "close_s": self.close_s,
                "seconds_to_close": max(0, self.close_s - self.clock),
                "closed": self.clock >= self.close_s,
                "revealed": self.revealed,
                "attempt": len(self.attempts) + 1,
                "has_next_bar": nxt is not None or self.clock < self.close_s,
                "min_dwell_ms": int(MIN_DWELL_S * 1000),
            },
            "quotes": {"status": "disabled" if code else "live", "code": code, "reason": short,
                       "detail": detail, "age_s": age,
                       "since": hhmmss(current_dark["start"]) if current_dark else None,
                       "snapshot_time": hhmmss(snap["t"]) if snap else None},
            "spot": {"now": spot, "last": self._last_consistent_spot(self.clock), "series": series},
            "dark": dark,
            "board": rows,
            "positions": self._positions(snap, code),
            "fills": [{k: v for k, v in f.items() if k != "source_key"} for f in self.fills],
            "account": {"cash": round(self.cash, 2), "cash_start": self.cash_start,
                        "realized_net": round(self.realized, 2), "fees_paid": round(self.fees, 2)},
            "attempts": [{k: v for k, v in a.items() if k != "fills"} for a in self.attempts],
            "rules": {"fee_per_contract_side": self.fee, "multiplier": MULTIPLIER,
                      "quote_max_age_s": MAX_QUOTE_AGE_S,
                      "buy": "recorded ask", "sell": "recorded bid"},
        }

    def _positions(self, snap, code) -> list[dict]:
        out = []
        for c, lot in self.lots.items():
            if lot["qty"] <= 0:
                continue
            bid = None
            if snap is not None and not code:
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

    # -- actions ------------------------------------------------------------

    def _require_open(self):
        if self.revealed:
            raise ReplayError("session already revealed")
        if self.clock >= self.close_s:
            raise ReplayError("market closed")

    def _dwell(self):
        now = self.now()
        if self.last_move is not None and now - self.last_move < MIN_DWELL_S:
            raise DwellError(f"each bar is shown for at least {int(MIN_DWELL_S * 1000)} ms")
        self.last_move = now

    def advance(self, seconds: int, max_bars: int | None = 1) -> dict:
        """Move the clock forward by `seconds`, crossing at most `max_bars` new bars."""
        if self.revealed:
            raise ReplayError("session already revealed")
        if self.clock >= self.close_s:
            raise ReplayError("market closed")
        seconds = int(seconds)
        if seconds <= 0 or seconds > 8 * 3600:
            raise ReplayError("advance must be 1..28800 seconds")
        self._dwell()
        target = min(self.close_s, self.clock + seconds)
        if max_bars is not None:
            cursor = self.clock
            for _ in range(int(max_bars)):
                cursor = self._next_bar_after(cursor) or self.close_s
            following = self._next_bar_after(cursor)
            if following is not None:
                target = min(target, following - 1)   # never reveal a bar beyond the allowance
        self.clock = max(self.clock + 1, target)
        self.furthest = max(self.furthest, self.clock)
        self._log("advance", {"seconds": seconds, "max_bars": max_bars})
        if self.clock >= self.close_s:
            self._expire()
        return self.view()

    def step_bar(self) -> dict:
        """Move exactly to the next recorded bar (or the close)."""
        if self.revealed:
            raise ReplayError("session already revealed")
        if self.clock >= self.close_s:
            raise ReplayError("market closed")
        nxt = self._next_bar_after(self.clock) or self.close_s
        return self.advance(nxt - self.clock, max_bars=None)

    def reset(self) -> dict:
        """Back to the landing time with a fresh ledger; the attempt is recorded."""
        if self.revealed:
            raise ReplayError("session already revealed")
        self.attempts.append({"attempt": len(self.attempts) + 1, "realized_net": round(self.realized, 2),
                              "fees_paid": round(self.fees, 2),
                              "fill_count": len([f for f in self.fills if f["kind"] == "fill"]),
                              "open_contracts": sum(l["qty"] for l in self.lots.values()),
                              "seen_until": hhmmss(self.furthest),
                              "fills": list(self.fills)})
        self._log("reset", {"attempt": len(self.attempts)})
        self._fresh_ledger()
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
        snap, code, age = self.board_state()
        if code:
            raise ReplayError(f"quotes disabled: {explain(code, snap, age)[0]}")
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
            price, pnl, cash_flow = ask, None, -(premium + fee)
        else:
            if lot["qty"] < qty:
                raise ReplayError("cannot sell more than held (no short positions)")
            if not can_sell:
                raise ReplayError(f"no usable bid: {why or 'bid unavailable'}")
            proceeds = bid * MULTIPLIER * qty
            basis = lot["cost"] * qty / lot["qty"]
            lot["cost"] -= basis
            lot["qty"] -= qty
            self.cash += proceeds - fee
            pnl = proceeds - fee - basis
            self.realized += pnl
            price, cash_flow = bid, proceeds - fee
        self.fees += fee
        fill = {"n": len(self.fills) + 1, "kind": "fill", "side": side, "contract": contract,
                "label": label(contract), "qty": qty, "price": price, "fee": round(fee, 2),
                "time_of_day": hhmmss(self.clock), "quote_time": hhmmss(snap["t"]),
                "quote_age_s": age, "bid": bid, "ask": ask,
                "cash_flow": round(cash_flow, 2),
                "net_pnl": None if pnl is None else round(pnl, 2),
                "source_key": snap["key"]}
        self.fills.append(fill)
        self._log("fill", fill)
        return self.view()

    def _expire(self):
        if self.expired:
            return
        self.expired = True
        spot = self._last_consistent_spot(self.close_s)
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
                                       "parity-consistent recorded underlying (not a market fill)",
                               "cash_flow": round(value, 2),
                               "net_pnl": round(pnl, 2), "source_key": None})
            lot["qty"], lot["cost"] = 0, 0.0
        self._log("expire", {})

    # -- reveal + reconciliation --------------------------------------------

    def reconciliation(self, fills: list[dict] | None = None) -> dict:
        """Trade-by-trade money trail, each fill checked against its raw archived row."""
        fills = self.fills if fills is None else fills
        lines, gross, fees = [], 0.0, 0.0
        for f in fills:
            raw = self.raw_lookup(f["source_key"], f["contract"]) if (self.raw_lookup and f.get("source_key")) else None
            expect = None
            if raw is not None and f["kind"] == "fill":
                expect = raw["Ask"] if f["side"] == "buy" else raw["Bid"]
            premium = (f["price"] or 0) * MULTIPLIER * f["qty"] * (-1 if f["side"] == "buy" else 1)
            gross += premium
            fees += f["fee"]
            lines.append({
                "n": f["n"], "kind": f["kind"], "side": f["side"], "contract": f["contract"], "qty": f["qty"],
                "decision_time": f["time_of_day"] + " ET", "quote_time": f.get("quote_time"),
                "quote_age_s": f.get("quote_age_s"), "source_key": f.get("source_key"),
                "recorded_bid": f.get("bid"), "recorded_ask": f.get("ask"), "fill_price": f["price"],
                "raw_row": raw, "raw_matches": None if expect is None else abs(expect - f["price"]) < 1e-9,
                "premium_cash": round(premium, 2), "fee": f["fee"], "cash_flow": f.get("cash_flow"),
                "note": f.get("note"),
            })
        net = gross - fees
        return {"lines": lines, "gross_premium": round(gross, 2), "fees": round(fees, 2),
                "net": round(net, 2), "ledger_realized_net": round(self.realized, 2) if fills is self.fills else None,
                "reconciles": abs(net - self.realized) < 0.005 if fills is self.fills else None,
                "formula": "net = sum(sell bid x 100 x qty) - sum(buy ask x 100 x qty) - fee x contracts x sides"}

    def reveal(self) -> dict:
        """End the session and disclose the day, its full path, sources and reconciliation."""
        self.revealed = True
        self._log("reveal", {})
        v = self.view()
        full = [[s["t"], s["spot"]] for s in self.snaps
                if s["ok"] and self.open_s - 300 <= s["t"] <= self.close_s]
        v["reveal"] = {
            "date": self.pack["day"],
            "weekday": _date.fromisoformat(self.pack["day"]).strftime("%A"),
            "start_time": hhmmss(self.start_s) + " ET",
            "full_day_spot": full,
            "full_day_dark": self.dark_stretches(self.close_s),
            "coverage_usable": self.pack["coverage_usable"],
            "net_profit": round(self.realized, 2),
            "open_positions_unsettled": v["positions"],
            "reconciliation": self.reconciliation(),
            "earlier_attempts": [{**{k: x for k, x in a.items() if k != "fills"},
                                  "reconciliation": self.reconciliation(a["fills"])} for a in self.attempts],
        }
        return v

    def _log(self, event: str, data: dict):
        if self.journal:
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": _time.time(), "event": event, "clock": self.clock,
                                     "data": data}) + "\n")
