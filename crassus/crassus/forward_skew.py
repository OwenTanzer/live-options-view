"""Versioned paper-only overlays for four specific fresh OI accounts."""

from copy import deepcopy
from datetime import datetime
from fractions import Fraction
import json

from .clock import ET
from .durability import atomic_json
from .exchange_calendar import session_close
from .profit_protection import Observation, ProtectConfig, ProtectionChain
from .skew_bars import direction
from .skew_direction import Position
from .strategies.oi_skew import _option_type_from_symbol, oi_skew_qqq
from .strategy import Decision

PAPER_ORIGIN = "https://options.moopertonic.net"
VERSION = "oi-forward-v1"
ARMS = dict(
    crassus_oif_baseline="baseline",
    crassus_oif_stop35="stop",
    crassus_oif_protect="protect_only",
    crassus_oif_recover="protect_reentry",
)


def arm_for(account):
    arm = (getattr(account, "params", None) or {}).get("forward_skew_arm")
    if arm != ARMS.get(getattr(account, "username", None)) or (
        arm and account.strategy_id != "oi_skew_qqq"
    ):
        raise ValueError("forward comparison identity/config mismatch")
    return arm


def snap_time(value):
    if not value:
        return None
    try:
        value = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return value if value.tzinfo else None


def freeze(chain):
    if chain is None:
        return None
    state = vars(chain).copy()
    state.pop("config")
    state["used_recovery_events"] = sorted(state["used_recovery_events"])
    return state


def thaw(state, arm):
    if state is None:
        return None
    chain = ProtectionChain(
        state["side"],
        state["contracts"],
        state["basis"],
        state["entry_bar"],
        state["original_skew_sign"],
        ProtectConfig(
            variant="protect_reentry" if arm == "protect_reentry" else "protect_only"
        ),
    )
    vars(chain).update(state)
    chain.used_recovery_events = set(chain.used_recovery_events)
    return chain


def reconcile(state, ctx, arm):
    pending = state.pop("pending", None)
    if pending:
        fill = next(
            (
                t
                for t in ctx.book.trades
                if t.get("execution_request_id") == pending["id"]
            ),
            None,
        )
        if fill:
            previous = state.get("chain")
            state.update(pending["after"])
            if pending["action"] == "buy":
                symbol = pending["symbol"]
                qty, price = int(fill["qty"]), float(fill["price"])
                if (
                    fill["sym"] != symbol
                    or fill["side"] != "buy"
                    or qty != pending["quantity"]
                    or price <= 0
                ):
                    raise ValueError("forward buy disagrees with reconciled book")
                chain = thaw(state.get("chain"), arm)
                if chain is None:
                    side = _option_type_from_symbol(symbol)
                    chain = ProtectionChain(
                        side,
                        qty,
                        price,
                        pending["bar"],
                        1 if side == "call" else -1,
                        contract_symbol=symbol,
                    )
                    if previous:
                        # Exposure and arming reset on a new OI entry. Recovery
                        # budgets and consumed events belong to the account-day.
                        chain.protect_exits = previous["protect_exits"]
                        chain.reentries = previous["reentries"]
                        chain.used_recovery_events = set(
                            previous["used_recovery_events"]
                        )
                chain.basis = price
                state["chain"] = freeze(chain)
    chain = thaw(state.get("chain"), arm)
    actual = ctx.book.positions
    if chain and chain.held:
        if not actual:
            # Mandatory flatten/settlement can close a position outside the overlay.
            chain.external_exit("eod_flatten")
            state["chain"] = freeze(chain)
        elif (
            set(actual) != {chain.contract_symbol}
            or actual[chain.contract_symbol].quantity != chain.contracts
        ):
            raise ValueError("forward holding differs from persisted chain")
    elif actual:
        raise ValueError("unattributed holding in forward account")
    return chain


def decide(ctx, account, arm, bars, directory, decision_id):
    now = ctx.now_et
    path = directory / (account.username + "-" + now.date().isoformat() + ".json")
    state = json.loads(path.read_text()) if path.exists() else {}
    chain = reconcile(state, ctx, arm)
    bar = int(now.timestamp() // 300)
    metadata = {"comparison": VERSION, "arm": arm, "decision_id": decision_id}

    def result(action, reason, symbol=None, quantity=None):
        return Decision(
            action, reason, "oi_skew_qqq", VERSION, symbol, quantity, metadata=metadata
        )

    signal = oi_skew_qqq(ctx) if ctx.snapshot else result("no_trade", "No OI snapshot")
    meta = signal.metadata or {}
    metadata["oi_signal"] = signal.to_dict() if ctx.snapshot else None
    if ctx.session_phase != "open":
        if state or not path.exists():
            atomic_json(path, state)
        return result("no_trade", signal.reason)
    metadata["bar_input_error"] = bars.error
    reading = snap_time(ctx.snapshot.timestamp) if ctx.snapshot else None
    imbalance = meta.get("imbalance_ratio")
    threshold = meta.get("params", {}).get("imbalance_threshold", 0.0)

    def stage(proposal, after):
        if proposal.is_trade:
            state["pending"] = {
                "id": decision_id,
                "action": proposal.action,
                "symbol": proposal.symbol,
                "quantity": proposal.quantity,
                "bar": bar,
                "after": after,
            }
        atomic_json(path, state)
        return proposal

    def propose_base():
        if not signal.is_trade:
            return stage(result("no_trade", signal.reason), {})
        if signal.action == "buy":
            if not reading or not 0 <= (now - reading).total_seconds() <= 360:
                return stage(
                    result("no_trade", "OI reading outside causal freshness window"), {}
                )
            if chain:
                allowed, why = chain.strategy_entry_allowed(
                    _option_type_from_symbol(signal.symbol),
                    imbalance,
                    threshold,
                    reading,
                    now,
                )
                if not allowed:
                    return stage(result("no_trade", why), {})
            if state.get(
                "stop_symbol"
            ) == signal.symbol and now.timestamp() < state.get("cooldown_until", 0):
                return stage(result("no_trade", "15-minute stop cooldown"), {})
            after = {
                "chain": None,
                "entry_at": now.isoformat(),
                "entry_spot": ctx.snapshot.underlying_price,
            }
        else:
            ended = deepcopy(chain)
            ended.external_exit("signal_exit")
            after = {"chain": freeze(ended)}
        return stage(
            result(signal.action, signal.reason, signal.symbol, signal.quantity), after
        )

    if not chain:
        return propose_base()
    symbol = chain.contract_symbol
    q = (
        ctx.quotes([symbol]).get(symbol)
        if chain.held or arm == "protect_reentry"
        else None
    )
    eligible = q and q.is_executable and 0 <= q.age_seconds and 0 < q.bid <= q.ask
    bid, ask = (q.bid, q.ask) if eligible else (None, None)
    metadata["mark"] = vars(q) if q else None
    if (
        chain.held
        and bid
        and Fraction(str(bid)) <= Fraction(str(chain.basis)) * Fraction(65, 100)
    ):
        ended = deepcopy(chain)
        ended.external_exit("stop_loss")
        state.update(stop_symbol=symbol, cooldown_until=now.timestamp() + 900)
        metadata.update(trigger="stop_loss", basis=chain.basis)
        return stage(
            result("sell", "35% premium stop", symbol, chain.contracts),
            {"chain": freeze(ended)},
        )
    if chain._last_bar == bar:
        return stage(
            result("no_trade", "Overlay already evaluated this five-minute interval"),
            {},
        )
    row = None
    if arm != "stop":
        position = Position(
            chain.side,
            snap_time(state["entry_at"]),
            state["entry_spot"],
            now.date().isoformat(),
        )
        row = direction(bars.rows, position, now)
    metadata["direction"] = row
    phase = row["phase"] if row else None
    end = snap_time(row["event_end"]) if row else None
    status = (
        "unknown"
        if phase in (None, "unknown")
        else "stale" if phase == "stale" else "ok"
    )
    if end and state.get("protected_at") and phase == "recovery":
        since = snap_time(state["protected_at"])
        if sum(since < snap_time(r["event_end"]) <= end for r in bars.rows) < 2:
            status = "unknown"
    obs = Observation(
        bar,
        phase,
        row["row_id"] if row else None,
        bid,
        ask,
        imbalance,
        threshold,
        (
            datetime.combine(now.date(), session_close(now.date()), ET) - now
        ).total_seconds()
        / 60,
        now,
        status,
        end,
        reading,
        symbol,
    )
    candidate = deepcopy(chain)
    if arm == "stop" and chain.held:
        return propose_base()
    d = candidate.evaluate(obs)
    metadata["protection"] = d.metadata
    if d.action in ("exit", "reenter"):
        # Keep arming observations durable even when a sale is rejected.
        if d.action == "exit":
            chain.armed = True
            state["chain"] = freeze(chain)
            after = {"chain": freeze(candidate), "protected_at": now.isoformat()}
        else:
            if (
                not ctx.snapshot
                or not reading
                or not 0 <= (now - reading).total_seconds() <= 360
            ):
                return stage(
                    result("no_trade", "Recovery lacks fresh underlying reference"), {}
                )
            after = {
                "chain": freeze(candidate),
                "entry_at": now.isoformat(),
                "entry_spot": ctx.snapshot.underlying_price,
            }
        return stage(
            result(
                "sell" if d.action == "exit" else "buy", d.reason, symbol, d.contracts
            ),
            after,
        )
    chain = candidate
    state["chain"] = freeze(chain)
    return propose_base()
