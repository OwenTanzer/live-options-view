"""CLI for the crypto candle cache.

Commands (all offline unless a fetch is requested)::

    python -m market_data.crypto_candle_cache universe
    python -m market_data.crypto_candle_cache backfill --out cache-out [--symbols BTC ETH] [--days-1m 7] [--days-1h 90] [--max-requests N] [--publish]
    python -m market_data.crypto_candle_cache update --out cache-out [--symbols BTC] [--publish]
    python -m market_data.crypto_candle_cache status --out cache-out [--symbols BTC]
    python -m market_data.crypto_candle_cache seed --out cache-out

``--publish`` is opt-in and needs the R2 environment (see
:mod:`publish`); without it, files are written locally and the
sidecar says ``not_published``. ``seed`` copies the checked-in BTC
seed (``seed/BTC/``) into R2 layout under ``--out`` — the only
command that writes sample data, and the sample is labelled as
such in its own provenance. No command trades, alerts, restarts a
producer, or deploys anything.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import cache as cache_mod
from . import collector as collector_mod
from . import coins as coins_mod
from .one_clock import detect_gaps
from .publish import publish_symbol, read_config

UTC = timezone.utc


def _coins(symbols):
    if not symbols:
        return list(coins_mod.COINS)
    unknown = [s for s in symbols if s not in coins_mod.BY_SYMBOL]
    if unknown:
        raise SystemExit(f"unknown coin(s): {', '.join(unknown)}")
    return [coins_mod.BY_SYMBOL[s] for s in symbols]


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="crypto-candle-cache")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("universe", help="print the 50-coin universe")

    backfill = sub.add_parser("backfill", help="fetch bases and derive intervals")
    backfill.add_argument("--out", required=True)
    backfill.add_argument("--symbols", nargs="*")
    backfill.add_argument("--days-1m", type=int, default=7)
    backfill.add_argument("--days-1h", type=int, default=90)
    backfill.add_argument("--max-requests", type=int, default=None)
    backfill.add_argument("--publish", action="store_true")
    backfill.add_argument("--force", action="store_true",
                          help="allow a re-fetch to replace a stored bar with different OHLC")

    update = sub.add_parser("update", help="fetch only bars newer than stored")
    update.add_argument("--out", required=True)
    update.add_argument("--symbols", nargs="*")
    update.add_argument("--max-requests", type=int, default=None)
    update.add_argument("--publish", action="store_true")

    status = sub.add_parser("status", help="report cached coverage per coin")
    status.add_argument("--out", required=True)
    status.add_argument("--symbols", nargs="*")

    seed = sub.add_parser("seed", help="copy the checked-in BTC seed into R2 layout")
    seed.add_argument("--out", required=True)
    return parser.parse_args(argv)


def _publisher(args):
    if not args.publish:
        return None
    config = read_config()

    def publish(symbol, files, provenance):
        return publish_symbol(symbol, files, provenance, config=config)

    return publish


def _windows(args):
    from datetime import timedelta
    return {"1m": timedelta(days=args.days_1m),
            "1h": timedelta(days=args.days_1h), "1d": None}


def cmd_universe(_args):
    coins_mod.validate()
    for coin in coins_mod.COINS:
        print(f"{coin.rank:>3} {coin.symbol:<6} {coin.name} "
              f"({coin.source}; {coin.coingecko_id})")
    return 0


def cmd_backfill(args):
    selected = _coins(args.symbols)
    now = datetime.now(UTC)
    summaries = collector_mod.run(
        selected, args.out, now=now, windows=_windows(args),
        max_requests=args.max_requests, publish=_publisher(args))
    print(json.dumps({"command": "backfill", "coins": summaries}, indent=2))
    failed = [s for s in summaries if s["status"] != "ok"]
    return 1 if failed else 0


def cmd_update(args):
    selected = _coins(args.symbols)
    now = datetime.now(UTC)
    summaries = [collector_mod.update_coin(
        coin, collector_mod.default_clients(), args.out, now,
        max_requests=args.max_requests, publish=_publisher(args))
        for coin in selected]
    print(json.dumps({"command": "update", "coins": summaries}, indent=2))
    failed = [s for s in summaries if s["status"] != "ok"]
    return 1 if failed else 0


def cmd_status(args):
    selected = _coins(args.symbols)
    report = {}
    for coin in selected:
        stored = cache_mod.read_symbol_files(args.out, coin.symbol)
        per_interval = {}
        for interval, content in sorted(stored.items()):
            bars = content["bars"]
            gaps = detect_gaps(bars, interval)
            per_interval[interval] = {
                "bars": len(bars),
                "first_bar": bars[0]["t"] if bars else None,
                "last_bar": bars[-1]["t"] if bars else None,
                "source": content.get("source"),
                "gaps": gaps,
            }
        report[coin.symbol] = {
            "intervals": sorted(stored),
            "per_interval": per_interval,
        }
    print(json.dumps(report, indent=2))
    return 0


def cmd_seed(args):
    seed_dir = Path(__file__).resolve().parent / "seed" / "BTC"
    if not seed_dir.exists():
        raise SystemExit("seed directory missing from the package")
    out = Path(args.out)
    copied = []
    for name in ("1m.json", "1h.json", "1d.json", "provenance.json"):
        src = seed_dir / name
        dest = out / "crypto" / "candles" / "BTC" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        copied.append(str(dest))
    print(json.dumps({"command": "seed", "coin": "BTC", "files": copied}, indent=2))
    return 0


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "universe":
        return cmd_universe(args)
    if args.command == "backfill":
        return cmd_backfill(args)
    if args.command == "update":
        return cmd_update(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "seed":
        return cmd_seed(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
