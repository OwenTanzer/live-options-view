#!/usr/bin/env python3
"""Run a real crassus strategy against generated synthetic days.

This is the payoff of the whole pipeline: `day_generator.py` writes
`out/synthetic_*.json` files shaped exactly like `intraday/latest.json`
sequences; this module turns each snapshot into a real `MarketSnapshot` and
a real `StrategyContext`, calls the strategy function completely
unmodified (same object `crassus.strategy.REGISTRY` would hand the live
runner), and fills its `buy`/`sell` decisions against the synthetic
bid/ask -- so a strategy can be run across dozens of statistically-realistic
fake days before it ever sees a real market, the same hermetic-but-now-also-
*sequential* idea `scripts/verify_canopus_down_day.py` uses for single
hand-built scenarios.

**Known gap, flagged in review:** this does not check `crassus/flatten.py`'s
mandatory EOD flatten before calling the strategy -- unlike the real
`Runner._run_account`, which always checks `flatten.maybe_flatten` first.
`flatten.py` doesn't exist on this branch's tree yet (branched off `master`
before that PR merged), so there is nothing to import and wire in today. A
backtested strategy can therefore hold a position later into the session
than the real deployed runner would ever allow. Once that PR merges, this
loop should check `flatten.maybe_flatten(ctx, {})` before `strategy(ctx)`
each cycle, same ordering the real runner uses, rather than calling the
strategy unconditionally as it does now.

Usage:
    python backtest_bridge.py --strategy canopus_down_day_14 --days ./out
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "crassus"))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402
import crassus.strategies  # noqa: E402  (registers every strategy_id into crassus.strategy.REGISTRY)
from crassus.strategy import REGISTRY  # noqa: E402

ET = ZoneInfo("America/New_York")


def load_day(path: Path) -> dict:
    return json.loads(path.read_text())


def _quote_from_row(row: dict, server_ts: str) -> Quote:
    return Quote(
        symbol=row["OptionSymbol"], bid=row.get("Bid"), ask=row.get("Ask"),
        quote_ts=server_ts, server_ts=server_ts,
    )


def run_strategy_on_day(strategy_id: str, day: dict, params: dict | None = None) -> dict:
    """Steps one synthetic day's snapshots through the strategy, filling
    buy/sell decisions at the synthetic ask/bid. Returns a small trade log
    plus end-of-day realized P&L (closed round-trips only; an unresolved
    position at 4pm ET is marked-to-bid, not counted as realized).
    """
    strategy = REGISTRY[strategy_id]
    trades: list[dict] = []

    for snap in day["snapshots"]:
        snapshot = MarketSnapshot.from_payload("synthetic://", snap, raw=b"{}")
        now_et = datetime.fromisoformat(snap["timestamp"]).astimezone(ET)
        row_by_symbol = {r["OptionSymbol"]: r for r in snap["rows"]}

        book = Book(trades)
        ctx = StrategyContext(
            snapshot=snapshot,
            account_state={},
            book=book,
            now_et=now_et,
            session_phase="open",
            quotes=lambda syms, _rows=row_by_symbol, _ts=now_et.isoformat(): {
                s: _quote_from_row(_rows[s], _ts) for s in syms if s in _rows
            },
            params=params or {},
        )

        decision = strategy(ctx)
        if decision.is_trade:
            row = row_by_symbol.get(decision.symbol)
            if row is None:
                continue
            fill_price = row["Ask"] if decision.action == "buy" else row["Bid"]
            if fill_price is None:
                continue
            trades.append({
                "sym": decision.symbol, "side": decision.action, "qty": decision.quantity,
                "price": float(fill_price), "ts": now_et.isoformat(),
            })

    realized_pnl = _realized_pnl(trades)
    final_book = Book(trades)
    open_positions = {s: p.quantity for s, p in final_book.positions.items()}
    return {
        "session_date": day["session_date"], "donor_day": day.get("donor_day"),
        "trades": trades, "realized_pnl": realized_pnl, "open_at_close": open_positions,
    }


def _realized_pnl(trades: list[dict]) -> float:
    """FIFO realized P&L per symbol across the day's trade list -- 100x
    multiplier since these are option contracts."""
    pnl = 0.0
    lots: dict[str, list[list[float]]] = {}  # symbol -> [[qty_remaining, price], ...]
    for t in trades:
        sym, side, qty, price = t["sym"], t["side"], t["qty"], t["price"]
        book = lots.setdefault(sym, [])
        if side == "buy":
            book.append([float(qty), price])
        else:
            remaining = float(qty)
            while remaining > 1e-9 and book:
                lot_qty, lot_price = book[0]
                take = min(lot_qty, remaining)
                pnl += (price - lot_price) * take * 100
                book[0][0] -= take
                remaining -= take
                if book[0][0] <= 1e-9:
                    book.pop(0)
    return round(pnl, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", required=True, help=f"one of: {sorted(REGISTRY)}")
    ap.add_argument("--days", default="./out", help="directory of synthetic_*.json files from day_generator.py")
    ap.add_argument("--params", default=None, help="JSON dict of strategy params override")
    args = ap.parse_args()

    if args.strategy not in REGISTRY:
        print(f"Unknown strategy {args.strategy!r}. Registered: {sorted(REGISTRY)}", file=sys.stderr)
        sys.exit(1)

    params = json.loads(args.params) if args.params else None
    day_files = sorted(Path(args.days).glob("synthetic_*.json"))
    if not day_files:
        print(f"No synthetic_*.json files in {args.days} -- run day_generator.py first.", file=sys.stderr)
        sys.exit(1)

    results = [run_strategy_on_day(args.strategy, load_day(p), params) for p in day_files]

    n_trades_days = sum(1 for r in results if r["trades"])
    total_pnl = sum(r["realized_pnl"] for r in results)
    wins = sum(1 for r in results if r["realized_pnl"] > 0)
    losses = sum(1 for r in results if r["realized_pnl"] < 0)

    print(f"\n{args.strategy} over {len(results)} synthetic days:")
    print(f"  entered a position on {n_trades_days}/{len(results)} days")
    print(f"  realized P&L: total={total_pnl:.2f}  wins={wins}  losses={losses}  flat={n_trades_days - wins - losses}")
    for r in results:
        if r["trades"]:
            print(f"    {r['session_date']} (donor {r['donor_day']}): {len(r['trades'])} fills, pnl={r['realized_pnl']:.2f}, open_at_close={r['open_at_close']}")


if __name__ == "__main__":
    main()
