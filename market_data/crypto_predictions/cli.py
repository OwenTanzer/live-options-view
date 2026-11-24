"""CLI for hourly crypto predictions.

Commands (all local unless ``--publish`` is given):

- ``universe`` — print the 50-coin universe (from the candle cache).
- ``predict`` — build prediction files from a candle-cache output
  directory (``--cache-dir``: the directory holding
  ``crypto/candles/<COIN>/1h.json``), writing
  ``crypto/predictions/<COIN>.json`` + sidecars under ``--out``.
- ``seed`` — regenerate the checked-in BTC seed prediction file
  from the checked-in BTC candle seed. Deterministic: the seed
  generation time is fixed, so the output is byte-identical to
  the checked-in file.
- ``status`` — report per-coin prediction coverage under ``--out``.

``--publish`` is opt-in and needs the R2 environment variables
(see :mod:`publish`); without it files are written locally and
the sidecar says ``not_published``. No producer is restarted or
redeployed by any command here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from market_data.crypto_candle_cache import coins as coins_mod
from market_data.crypto_candle_cache import one_clock as oc

from . import predictions as pred_mod
from . import producer as producer_mod
from . import provenance as prov_mod

UTC = timezone.utc

# The checked-in BTC candle seed was fetched live from Coinbase
# 2026-11-24T00:32:44Z; the prediction seed is computed from those
# bars at the same instant, so `seed` reproduces it exactly.
SEED_GENERATED_AT = "2026-11-24T00:32:44Z"


def _now_iso():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="crypto_predictions")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("universe", help="print the 50-coin universe")

    predict = sub.add_parser("predict", help="build prediction files from cached 1h bars")
    predict.add_argument("--cache-dir", required=True,
                         help="candle cache output directory")
    predict.add_argument("--out", required=True)
    predict.add_argument("--symbols", nargs="*")
    predict.add_argument("--publish", action="store_true")
    predict.add_argument("--generated-at", default=None,
                         help="override the run time (ISO-8601 UTC)")

    seed = sub.add_parser("seed", help="regenerate the checked-in BTC seed prediction file")
    seed.add_argument("--out", required=True)
    seed.add_argument("--publish", action="store_true")

    status = sub.add_parser("status", help="report prediction coverage per coin")
    status.add_argument("--out", required=True)
    status.add_argument("--symbols", nargs="*")
    return parser.parse_args(argv)


def _publisher(args):
    if not args.publish:
        return None

    from . import publish as publish_mod

    config = publish_mod.read_config()
    client = publish_mod.make_client(config)
    universe = "crypto_predictions: 50 coins (32 coinbase / 18 coingecko)"

    def publish(symbol, content, sidecar):
        result = publish_mod.publish_symbol(
            symbol, content, sidecar, config=config, client=client)
        return result["keys"]

    return publish, universe


def cmd_universe(_args):
    for coin in coins_mod.COINS:
        print(f"{coin.rank:>3} {coin.symbol:<6} {coin.name} ({coin.source})")
    print(f"{len(coins_mod.COINS)} coins: "
          f"{len(coins_mod.COINBASE_COINS)} coinbase / "
          f"{len(coins_mod.COINGECKO_COINS)} coingecko")


def _publish_all(publisher, built, generated_at):
    """Publish built files through the resolved publisher.

    ``publisher`` is the ``(publish, universe)`` pair from
    :func:`_publisher`. Returns ``{symbol: [keys]}``.
    """
    publish, universe = publisher
    published = {}
    for symbol, (content, status) in built.items():
        sidecar = prov_mod.build_provenance(
            symbol, content, generated_at=generated_at, universe=universe,
            publication_status="published",
            publication_reason="published by this run")
        published[symbol] = publish(
            symbol, content if status != "absent" else None, sidecar)
    return published


def cmd_predict(args):
    generated_at = args.generated_at or _now_iso()
    now_ms = oc.instant_ms(generated_at, "generated_at")
    publisher = _publisher(args)
    built = producer_mod.build_all(args.cache_dir, generated_at, now_ms,
                                   symbols=args.symbols or None)
    universe = "crypto_predictions: 50 coins (32 coinbase / 18 coingecko)"
    written = producer_mod.write_all(args.out, built, generated_at, universe=universe)
    if publisher is not None:
        published = _publish_all(publisher, built, generated_at)
        for symbol, info in written.items():
            print(f"{symbol}: {info['status']} (published {len(published[symbol])} keys)")
        return
    for symbol, info in written.items():
        print(f"{symbol}: {info['status']} ({len(info['keys'])} keys)")


def cmd_seed(args):
    package_dir = Path(__file__).resolve().parent
    # The candle seed files sit beside this package's seed dir in
    # the repo layout (market_data/crypto_candle_cache/seed/BTC/);
    # resolve them from the package location, not the CWD. The
    # seed layout is flat (seed/BTC/1h.json), not the R2 layout,
    # so the bars are read directly rather than through the cache
    # reader's interval path.
    candle_seed = (package_dir.parent / "crypto_candle_cache" / "seed" / "BTC")
    seed_1h = candle_seed / "1h.json"
    if not seed_1h.exists():
        print(f"candle seed not found: {seed_1h}", file=sys.stderr)
        return 1
    from market_data.crypto_candle_cache import cache as cache_mod
    from market_data.crypto_candle_cache import one_clock as seed_oc

    now_ms = oc.instant_ms(SEED_GENERATED_AT, "generated_at")
    content_1h = cache_mod.read_interval_file(seed_1h)
    completed, _forming = seed_oc.split_forming(
        content_1h["bars"], "1h", now_ms)
    content, status = producer_mod.build_seed_history(
        "BTC", completed, SEED_GENERATED_AT, now_ms, count=7)
    out_path = pred_mod.write_prediction_file(args.out, "BTC", content)
    sidecar = prov_mod.build_provenance(
        "BTC", content, generated_at=SEED_GENERATED_AT,
        universe="crypto_predictions: 50 coins (32 coinbase / 18 coingecko)",
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    sidecar_path = Path(args.out) / "crypto" / "predictions" / "BTC" / "provenance.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    print(f"BTC: {status} -> {out_path} ({content['observations_count']} observations)")
    return 0


def cmd_status(args):
    out_dir = Path(args.out)
    symbols = args.symbols or coins_mod.names()
    for symbol in symbols:
        path = pred_mod.prediction_path(out_dir, symbol)
        if not path.exists():
            print(f"{symbol}: absent (no prediction file)")
            continue
        try:
            content = pred_mod.read_prediction_file(path)
        except ValueError as exc:
            print(f"{symbol}: invalid ({exc})")
            continue
        # Live: value rows whose target hour has not passed at the
        # file's generation time; the rest are retained history.
        generated_ms = None
        if content.get("generated_at"):
            generated_ms = oc.instant_ms(
                content["generated_at"], "generated_at")
        live = []
        historical = 0
        for r in content["observations"]:
            if r.get("value") is None:
                continue
            target_end = r["value"].get("target_end")
            if (generated_ms is not None and target_end is not None
                    and oc.instant_ms(target_end, "target_end") <= generated_ms):
                historical += 1
                continue
            live.append(r)
        print(f"{symbol}: {content.get('coverage')} "
              f"({len(live)} live / {historical} historical / "
              f"{len(content['observations'])} observations, "
              f"generated {content.get('generated_at')})")


def main(argv=None):
    args = _parse_args(argv)
    if args.command == "universe":
        cmd_universe(args)
    elif args.command == "predict":
        cmd_predict(args)
    elif args.command == "seed":
        return cmd_seed(args)
    elif args.command == "status":
        cmd_status(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
