"""Inactive provisional QQQ 0DTE acceleration strategies for Crassus paper accounts."""

import hashlib
import json
import re
from datetime import datetime, time
from math import isfinite

from ..clock import ET
from .gated_strangle import fresh_spot
from ..durability import atomic_json
from ..strategy import Decision, register
from .me_bots_rules import KINDS, admitted, entry, exit_reason

VERSION = "0.3.0-research"
IDS = {"me_" + kind + "_qqq": kind for kind in KINDS}


def aware(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("missing timezone")
    return dt.astimezone(ET)


def completed_losses(trades, day):
    """The isolated account holds one unit at a time. Only settled fills count."""
    fills = []
    for trade in trades:
        when = aware(trade["ts"])
        if when.date() == day:
            fills.append((when, trade))
    basis = None
    losses = 0
    for when, trade in sorted(fills, key=lambda x: x[0]):
        symbol, side, qty, price = (
            trade.get(k) for k in ("sym", "side", "qty", "price")
        )
        if (
            not symbol
            or qty != 1
            or type(price) not in (int, float)
            or not isfinite(price)
            or price <= 0
        ):
            raise ValueError("unrecognized fill")
        if side == "buy" and basis is None:
            basis = (symbol, price, when)
        elif side == "sell" and basis and basis[0] == symbol:
            losses += price < basis[1]
            basis = None
        else:
            raise ValueError("unmatched fill")
    return losses, basis


def price_for(ctx, symbol, side):
    quote = ctx.quotes([symbol]).get(symbol)
    if not quote or not quote.is_executable or not 0 <= quote.age_seconds <= 15:
        return None
    if not (isfinite(quote.ask) and isfinite(quote.bid) and 0 < quote.bid <= quote.ask):
        return None
    return quote.ask if side == "buy" else quote.bid


def evaluate(ctx, strategy_id):
    kind = IDS[strategy_id]

    def skip(reason):
        return Decision.no_trade(reason, strategy_id, VERSION)

    if ctx.session_phase != "open":
        return skip("Outside regular session")
    inbox = ctx.params.get("_me_inputs")
    directory = ctx.params.get("_me_directory")
    if inbox is None or directory is None:
        return skip("Me Bot paper inputs unavailable")
    plan, rows, error = inbox
    now = ctx.now_et
    account = hashlib.sha256(ctx.account_alias.encode()).hexdigest()
    path = directory / (now.date().isoformat() + "-" + account + ".json")
    try:
        state = json.loads(path.read_text()) if path.exists() else {"attempts": []}
        losses, holding = completed_losses(ctx.book.trades, now.date())
        positions = ctx.book.positions
        if bool(holding) != bool(positions):
            return skip("Held position cannot be reconciled with today's fills")
        if holding:
            symbol, cost, fill_at = holding
            if (
                len(positions) != 1
                or symbol not in positions
                or positions[symbol].quantity != 1
            ):
                return skip("Position outside single-contract paper design")
            seed = state.get("seed")
            if (
                not seed
                or seed.get("symbol") != symbol
                or aware(seed["intent_at"]) > fill_at
            ):
                return skip("No matching durable entry evidence")
            reason = state.get("exit_due")
            if not reason and kind == "ratchet" and now.time() >= time(15, 30):
                reason = "15:30 ET cutoff"
            if not reason:
                if (
                    error
                    or not rows
                    or not 0 <= (now - rows[-1].end).total_seconds() < 60
                ):
                    return skip(error or "Incomplete or stale minute path for exit")
                fill_bar = next((b for b in rows if b.start <= fill_at < b.end), None)
                if fill_bar is None:
                    return skip(
                        "Waiting for completed fill-minute OHLC; underlying fill price unknown"
                    )
                # Bracket the unknown fill-time underlying. A signal that depends
                # on where inside the minute the fill occurred is a protective
                # paper exit, marked separately from a demonstrated stop.
                reasons = [
                    exit_reason(kind, rows, seed, fill_at, anchor, now)
                    for anchor in (fill_bar.low, fill_bar.high)
                ]
                if reasons[0] == reasons[1]:
                    reason = reasons[0]
                elif any(reasons):
                    reason = "Fill-minute ordering or anchor unknown; protective exit"
            if not reason:
                return skip("Holding; no completed stop or adverse close")
            state["exit_due"] = reason
            atomic_json(path, state)
            bid = price_for(ctx, symbol, "sell")
            if bid is None:
                return skip(reason + "; awaiting executable option bid")
            return Decision(
                "sell",
                reason,
                strategy_id,
                VERSION,
                symbol=symbol,
                quantity=1,
                instrument_type="option",
                limit_price=bid,
                metadata={
                    "trigger_end": seed["trigger_end"],
                    "fill_at": fill_at.isoformat(),
                },
            )

        if error or not rows or not 0 <= (now - rows[-1].end).total_seconds() < 60:
            return skip(error or "Incomplete or stale one-minute sequence")
        current = rows[-1].end
        prior = (
            aware(state["last_entry_check"]) if state.get("last_entry_check") else None
        )
        if prior != current:
            if prior is None or (current - prior).total_seconds() > 60:
                state["skip_through"] = current.isoformat()
            state["last_entry_check"] = current.isoformat()
            atomic_json(path, state)
        if state.get("skip_through") == current.isoformat():
            return skip(
                "Entry observation started or resumed after a gap; historical signals skipped"
            )
        if (
            not plan
            or plan["levels"].get("root") != "QQQ"
            or plan["levels"].get("session") != now.date().isoformat()
            or aware(plan["first_seen"]).date() != now.date()
            or aware(plan["first_seen"]).time() >= time(9, 30)
        ):
            return skip("Missing pre-open structural levels or vertical guard")
        if losses >= 3 or (kind == "ratchet" and now.time() >= time(15, 30)):
            return skip("Daily stop or entry cutoff reached")
        reference = plan.get("reference")
        if kind == "runner" and rows[-1].end.time() == time(10):
            if (
                not reference
                or len(reference["thirty"]) != 13
                or reference["first_seen"] > rows[-1].end
            ):
                return skip(
                    "Missing thirteen complete 30-minute bars from the immediately preceding session by the trigger close"
                )
        signal = entry(kind, rows, plan["levels"], reference)
        if signal is None:
            return skip("No qualified completed trigger")
        attempts = state["attempts"]
        if any(a["trigger_end"] == signal["trigger_end"] for a in attempts):
            return skip("Trigger already attempted")
        if kind == "opening" and any(a["side"] == signal["side"] for a in attempts):
            return skip("Opening attempt for direction already used")
        snap = ctx.snapshot
        if snap is None or str(snap.expiration) != now.date().isoformat():
            return skip("Missing same-day option snapshot")
        observed = aware(snap.timestamp)
        if (
            not fresh_spot(snap, now)
            or not rows[-1].end <= observed <= now
            or not rows[-1].end <= aware(snap.underlying_market.spot_ts) <= now
        ):
            return skip("No underlying observation since trigger close")
        spot = snap.underlying_market.spot
        if type(spot) not in (int, float) or not isfinite(spot) or spot <= 0:
            return skip("Underlying entry price unavailable")
        side = signal["side"]
        if side * spot <= side * signal["stop"] or not admitted(
            spot, signal["nova"], signal["first_target"], side
        ):
            return skip("Setup invalidated or first target reached before entry")
        row = snap.atm("call" if side == 1 else "put")
        symbol = (row or {}).get("OptionSymbol", "")
        pattern = (
            r"QQQ" + now.strftime("%y%m%d") + ("C" if side == 1 else "P") + r"\d{8}"
        )
        if not re.fullmatch(pattern, symbol):
            return skip("ATM contract identity does not match QQQ, date and side")
        ask = price_for(ctx, symbol, "buy")
        if ask is None:
            return skip("Missing executable entry ask")
        signal.update(
            symbol=symbol,
            entry_spot=spot,
            intent_at=now.isoformat(),
            trigger_first_seen=plan.get("latest_minute_first_seen"),
        )
        attempts.append({"side": side, "trigger_end": signal["trigger_end"]})
        state.update(seed=signal, exit_due=None)
        atomic_json(path, state)
        return Decision(
            "buy",
            "Qualified completed acceleration setup",
            strategy_id,
            VERSION,
            symbol=symbol,
            quantity=1,
            limit_price=ask,
            instrument_type="option",
            metadata={**signal, "levels_first_seen": plan["first_seen"]},
        )
    except (OSError, TypeError, ValueError, IndexError, KeyError):
        return skip("Unusable input, account history or durable strategy state")


def make_strategy(strategy_id):
    def decide(ctx):
        return evaluate(ctx, strategy_id)

    decide.strategy_id = strategy_id
    decide.strategy_version = VERSION
    decide.requires_snapshot = False
    return register(decide)


for registered_id in IDS:
    make_strategy(registered_id)
